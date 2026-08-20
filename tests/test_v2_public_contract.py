"""Focused contract checks for the project-free V2 public surface."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.api_v1_fastapi import create_app
from core.control_service import ControlService
from core.user_config import UserRunConfig
from radar_sim_sdk import RadarSimClient


REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_CONTRACT_DOCS = (
    REPO_ROOT / "docs" / "V2_ARCHITECTURE.md",
    REPO_ROOT / "docs" / "USER_GUIDE.md",
)


def _yaml_blocks(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    blocks: list[dict[str, Any]] = []
    for match in re.finditer(
        r"^```(?:yaml|yml)\s*\r?\n(.*?)^```\s*$",
        text,
        flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
    ):
        parsed = yaml.safe_load(match.group(1))
        assert isinstance(parsed, dict), f"{path} contains a non-mapping public YAML example"
        blocks.append(parsed)
    return blocks


def _mapping_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            keys.add(str(key).casefold())
            keys.update(_mapping_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_mapping_keys(child))
    return keys


def _run_config_dict() -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "selena": {
            "source": "existing",
            "existing_path": "D:/selena/RelWithDebInfo",
            "runtime_xml": "D:/selena/Runtime.xml",
        },
        "data": {"path": "//shared/data/one.MF4"},
        "simulation": {
            "target": "cluster",
            "source": "RadarFR",
            "adapter_file": "D:/inputs/adapter.txt",
            "mat_filter": "D:/inputs/MatFilter.cfg",
        },
        "result": {"path": "D:/RadarSim/results"},
    }


def _make_clients(tmp_path: Path) -> tuple[TestClient, RadarSimClient]:
    services: dict[str, ControlService] = {}

    def factory(owner: str) -> ControlService:
        services.setdefault(owner, ControlService(tmp_path / f"{owner}.db"))
        return services[owner]

    http_client = TestClient(create_app(control_service_factory=factory))
    sdk = RadarSimClient("http://testserver", client=http_client, user="alice")
    return http_client, sdk


@pytest.mark.parametrize("path", PUBLIC_CONTRACT_DOCS)
def test_public_yaml_examples_reject_project_profile_recipe_keys(path: Path):
    """Public YAML stays independent of legacy project/profile/recipe selectors."""

    blocks = _yaml_blocks(path)
    assert blocks, f"no public YAML examples found in {path}"
    for payload in blocks:
        assert _mapping_keys(payload).isdisjoint({"project", "profile", "recipe"})


def test_public_yaml_places_mat_filter_and_adapter_under_simulation():
    for path in PUBLIC_CONTRACT_DOCS:
        for payload in _yaml_blocks(path):
            if "simulation" not in payload:
                continue
            simulation = payload["simulation"]
            selena = payload.get("selena", {})
            assert isinstance(simulation, Mapping)
            assert "mat_filter" in simulation
            assert "adapter_file" in simulation
            assert "mat_filter" not in selena
            assert "adapter_file" not in selena


def test_user_run_config_2_rejects_legacy_yaml_without_silent_migration():
    legacy = {
        "schema_version": "1.0",
        "project": "legacy-name",
        "selena": {"mode": "auto"},
        "data": {"path": "D:/measurements"},
        "simulation": {"target": "cluster", "profile": "default"},
        "result": {"name": "legacy-run"},
    }

    with pytest.raises(ValidationError):
        UserRunConfig.from_dict(legacy)

    config = _run_config_dict()
    config["simulation"]["profile"] = "default"
    with pytest.raises(ValidationError, match="profile"):
        UserRunConfig.from_dict(config)

    assert UserRunConfig.model_config["extra"] == "forbid"


def test_explicit_source_and_result_path_are_identical_in_user_api_and_sdk_roundtrip(tmp_path: Path):
    config = _run_config_dict()
    parsed = UserRunConfig.from_dict(config)
    expected = parsed.to_dict()
    assert expected["simulation"]["source"] == "RadarFR"
    assert expected["result"]["path"] == "D:/RadarSim/results"
    assert UserRunConfig.from_yaml(parsed.to_yaml()).to_dict() == expected

    http_client, sdk = _make_clients(tmp_path)
    exported = http_client.post("/api/v1/run-configs/export", json={"config": config})
    assert exported.status_code == 200
    imported = http_client.post(
        "/api/v1/run-configs/import",
        json={"yaml_content": exported.json()["yaml_content"]},
    )
    assert imported.status_code == 200
    assert imported.json()["config"] == expected

    validation = sdk.validate_run(parsed)
    assert validation.config.to_dict() == expected
    job = sdk.submit_run(parsed, dry_run=True, idempotency_key="explicit-source-result")
    assert job.spec == expected


def test_openapi_hides_internal_agent_upload_and_runtime_bundle_routes():
    paths = set(create_app().openapi()["paths"])
    assert "/api/v1/run-jobs" in paths
    for path in paths:
        lowered = path.casefold()
        assert not lowered.startswith(("/api/agents", "/api/tasks"))
        assert "upload" not in lowered
        assert "runtime-bundle" not in lowered
