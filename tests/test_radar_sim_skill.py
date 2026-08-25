"""Black-box checks for the portable radar-sim Skill discovery helper."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).parents[1] / "skills" / "radar-sim-simulation" / "scripts" / "discover_candidates.py"


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
