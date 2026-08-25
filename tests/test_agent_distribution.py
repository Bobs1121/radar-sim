"""Tests for the no-source Agent Tools distribution surface."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

from fastapi.testclient import TestClient

from core.api_v1_fastapi import create_app
from core.agent_distribution import AgentToolsDistribution
from scripts.build_agent_tools_bundle import build


def _bundle(tmp_path: Path) -> tuple[Path, Path]:
    bundle = tmp_path / "agent-tools.zip"
    bundle.write_bytes(b"agent-tools-bundle")
    manifest = bundle.with_suffix(".json")
    manifest.write_text(
        json.dumps(
            {
                "release_version": "4.0.0-agent.1",
                "sdk_version": "4.0.0",
                "mcp_version": "0.1.0",
                "skill_version": "0.1.0",
                "api_version": "v1",
                "config_schema_version": "2.0",
                "connector_contract_version": "16",
                "python_requires": ">=3.10",
                "bundle_size": bundle.stat().st_size,
                "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return bundle, manifest


def test_agent_tools_manifest_bundle_and_installer_are_public_path_free(tmp_path: Path):
    bundle, manifest = _bundle(tmp_path)
    client = TestClient(
        create_app(agent_tools_bundle=bundle, agent_tools_manifest=manifest)
    )

    public = client.get("/api/v1/agent-tools/manifest")
    assert public.status_code == 200
    payload = public.json()
    assert payload["release_version"] == "4.0.0-agent.1"
    assert str(tmp_path) not in json.dumps(payload)
    assert payload["bundle"]["download_url"].endswith("/api/v1/agent-tools/package.zip")

    package = client.get("/api/v1/agent-tools/package.zip")
    assert package.status_code == 200
    assert package.content == bundle.read_bytes()
    assert package.headers["X-Content-SHA256"].startswith("sha256:")

    installer = client.get("/api/v1/agent-tools/install.ps1")
    assert installer.status_code == 200
    assert "__RSIM_SERVER_URL__" not in installer.text
    assert "agent-tools/install.py" in installer.text

    bootstrap = client.get("/api/v1/agent-tools/install.py")
    assert bootstrap.status_code == 200
    assert "__RSIM_SERVER_URL__" not in bootstrap.text
    assert "/api/v1/agent-tools/package.zip" in bootstrap.text
    assert "_install_offline_wheels" in bootstrap.text
    assert "parse_wheel_filename" in bootstrap.text


def test_agent_tools_routes_fail_closed_when_release_is_not_published(tmp_path: Path):
    client = TestClient(create_app())

    response = client.get("/api/v1/agent-tools/manifest")

    assert response.status_code == 503
    assert response.json()["code"] == "agent_tools_unavailable"


def test_agent_tools_bundle_builder_is_source_free_and_manifest_verified(tmp_path: Path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "radar_sim-4.0.0-py3-none-any.whl").write_bytes(b"sdk-wheel")
    (wheels / "mcp-1.28.1-py3-none-any.whl").write_bytes(b"mcp-wheel")
    skill = tmp_path / "skill" / "radar-sim-simulation"
    (skill / "agents").mkdir(parents=True)
    (skill / "SKILL.md").write_text("skill", encoding="utf-8")
    (skill / "agents" / "openai.yaml").write_text("interface: {}\n", encoding="utf-8")
    output = tmp_path / "agent-tools.zip"

    bundle, manifest = build(
        wheel_dir=wheels,
        skill_dir=skill,
        output=output,
        release_version="4.0.0-agent.1",
        sdk_version="4.0.0",
        mcp_version="0.1.0",
        skill_version="0.1.0",
        connector_contract_version="16",
    )

    distribution = AgentToolsDistribution.from_files(bundle, manifest)
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
    public = distribution.public_manifest(base_url="https://rsim.example.com")

    assert "wheels/radar_sim-4.0.0-py3-none-any.whl" in names
    assert "skill/radar-sim-simulation/SKILL.md" in names
    assert "runtime/agent_mcp_launcher.py" in names
    assert str(tmp_path) not in json.dumps(public)
    assert public["bundle"]["sha256"].startswith("sha256:")
