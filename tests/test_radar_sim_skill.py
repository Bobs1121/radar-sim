"""Black-box checks for the portable radar-sim Skill discovery helper."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "skills" / "radar-sim-simulation" / "scripts" / "discover_candidates.py"
BOOTSTRAP_SCRIPT = Path(__file__).parents[1] / "skills" / "radar-sim-simulation" / "scripts" / "bootstrap_agent_tools.py"


def _load_bootstrap_module():
    spec = importlib.util.spec_from_file_location("radar_sim_skill_bootstrap", BOOTSTRAP_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(root: Path, data_root: Path, *extra: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--data-root", str(data_root), *extra],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_skill_discovery_reports_semantic_candidates_without_reading_contents(tmp_path: Path):
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    script = repo / "apl" / "jenkins_selena_build.bat"
    runtime = repo / "runtime" / "Runtime_For_test.xml"
    executable = repo / "build" / "RelWithDebInfo" / "bin" / "Selena.exe"
    dll = executable.with_name("selena_core.dll")
    mf4 = data / "one.MF4"
    for path in (script, runtime, executable, dll, mf4):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"do-not-read")

    result = _run(repo, data)

    assert result["build_scripts"][0]["path"].endswith("jenkins_selena_build.bat")
    assert result["runtime_xml"][0]["path"].endswith("Runtime_For_test.xml")
    assert result["selena_outputs"][0]["dll_count"] == 1
    assert any(item["kind"] == "mf4_file" for item in result["data_candidates"])
    assert result["truncated"] is False


def test_skill_discovery_excludes_generated_result_inputs(tmp_path: Path):
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    source = data / "source.MF4"
    generated = data / "job_previous" / "outputs" / "0001-source-out.MF4"
    for path in (source, generated):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")

    result = _run(repo, data)

    paths = {item["path"] for item in result["data_candidates"]}
    assert str(source) in paths
    assert str(generated) not in paths


def test_skill_discovery_bound_is_reported_as_unknown(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for index in range(5):
        (repo / f"item-{index}.txt").write_text("content", encoding="utf-8")

    result = _run(repo, repo, "--max-entries", "2")

    assert result["truncated"] is True
    assert result["warnings"] == [
        "discovery_bound_reached; unresolved candidates require user confirmation"
    ]


def test_source_skill_bootstrap_requires_provider_metadata_but_has_no_deployment_binding(
    tmp_path: Path, monkeypatch
):
    module = _load_bootstrap_module()
    monkeypatch.setenv("RADAR_SIM_MCP_ROOT", str(tmp_path / "mcp"))
    monkeypatch.delenv("RADAR_SIM_SERVICE_URL", raising=False)
    monkeypatch.delenv("RADAR_SIM_BASE_URL", raising=False)

    with pytest.raises(module.BootstrapFailure, match="未包含"):
        module.resolve_server_url()


def test_skill_bootstrap_prefers_agent_override_and_rejects_credentials_in_url(
    tmp_path: Path, monkeypatch
):
    module = _load_bootstrap_module()
    monkeypatch.setenv("RADAR_SIM_MCP_ROOT", str(tmp_path / "mcp"))
    monkeypatch.setenv("RADAR_SIM_SERVICE_URL", "https://sim.example.test/radar/")

    url, source = module.resolve_server_url()

    assert url == "https://sim.example.test/radar"
    assert source == "RADAR_SIM_SERVICE_URL"

    with pytest.raises(module.BootstrapFailure, match="不能包含"):
        module._normalize_url("https://alice:secret@sim.example.test")


def test_source_skill_profile_does_not_bind_a_deployment_host():
    profile = json.loads(
        (Path(__file__).parents[1] / "skills" / "radar-sim-simulation" / "references" / "service-profile.json")
        .read_text(encoding="utf-8")
    )

    assert profile["service_url"] == ""
    assert profile["service_urls"] == []
