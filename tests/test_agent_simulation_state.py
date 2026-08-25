from __future__ import annotations

from pathlib import Path

from core.agent_simulation_state import AgentSimulationStateStore


def _config(tmp_path: Path, *, data: str = "D:/data/one.MF4") -> dict:
    return {
        "schema_version": "2.0",
        "selena": {
            "source": "build",
            "code_path": str(tmp_path / "repo"),
            "branch": "feature/demo",
            "selena_build_script": str(tmp_path / "repo" / "build_selena.bat"),
            "runtime_xml": str(tmp_path / "repo" / "Runtime.xml"),
        },
        "data": {"path": data},
        "simulation": {"target": "auto", "source": "", "adapter_file": "", "mat_filter": ""},
        "result": {"path": ""},
    }


def test_active_profile_round_trip_and_context_selection(tmp_path: Path) -> None:
    store = AgentSimulationStateStore(tmp_path / "state.json")
    record = store.save(_config(tmp_path), job_id="job_1", status="queued")

    result = store.get(context_path=str(tmp_path / "repo" / "nested"))

    assert result["found"] is True
    assert result["profile"]["last_job_id"] == "job_1"
    assert result["profile"]["config"]["data"]["path"] == "D:/data/one.MF4"
    assert str(tmp_path / "state.json") not in str(record)


def test_repeat_profile_updates_without_duplicate_records(tmp_path: Path) -> None:
    store = AgentSimulationStateStore(tmp_path / "state.json")
    store.save(_config(tmp_path), job_id="job_1", status="succeeded")
    store.save(_config(tmp_path, data="D:/data/two.MF4"), job_id="job_2", status="queued")

    payload = store._read()

    assert len(payload["profiles"]) == 1
    assert payload["profiles"][0]["last_job_id"] == "job_2"
    assert payload["profiles"][0]["config"]["data"]["path"] == "D:/data/two.MF4"


def test_same_path_with_changed_content_is_not_silently_merged(tmp_path: Path) -> None:
    store = AgentSimulationStateStore(tmp_path / "state.json")
    first = _config(tmp_path)
    second = _config(tmp_path)
    second["selena"]["code_path"] = str(tmp_path / "other-repo")
    store.save(first, job_id="job_1")
    store.save(second, job_id="job_2")

    assert len(store._read()["profiles"]) == 2
