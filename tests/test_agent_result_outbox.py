from pathlib import Path

import pytest

from cli import agent as agent_module
from core.agent_result_outbox import AgentResultOutbox
from core.agent_result_outbox import AgentResultOutboxError


def test_result_outbox_survives_store_reopen_and_tracks_attempts(tmp_path: Path):
    db = tmp_path / "result-outbox.db"
    first = AgentResultOutbox(db, now_fn=lambda: 10.0)
    first.put(
        "stage-1",
        attempt=2,
        agent_id="windows-1",
        status="succeeded",
        returncode=0,
        result={"summary": {"file_count": 2}},
    )
    first.mark_failure("stage-1", 2, "control plane unavailable")

    reopened = AgentResultOutbox(db, now_fn=lambda: 11.0)
    pending = reopened.pending()
    assert len(pending) == 1
    assert pending[0].task_id == "stage-1"
    assert pending[0].attempt == 2
    assert pending[0].attempts == 1
    assert pending[0].result == {"summary": {"file_count": 2}}


def test_control_client_queues_result_when_control_plane_is_down_then_flushes(tmp_path, monkeypatch):
    outbox = AgentResultOutbox(tmp_path / "result-outbox.db")
    client = agent_module._ControlClient("http://control.invalid", timeout=1)
    client.attach_result_outbox(outbox)
    client._task_attempts["stage-1"] = 3
    monkeypatch.setattr(agent_module.time, "sleep", lambda _seconds: None)

    def unavailable(_method, _path, _payload):
        raise agent_module._agent_transport_error("POST", "/api/tasks/result", OSError("offline"))

    client._request = unavailable
    queued = client.submit_result(
        "stage-1",
        agent_id="windows-1",
        status="succeeded",
        returncode=0,
        result={"ok": True},
    )
    assert queued["result_delivery"] == "pending"
    assert [(item.task_id, item.attempt) for item in outbox.pending()] == [("stage-1", 3)]

    calls = []

    def available(method, path, payload):
        calls.append((method, path, dict(payload)))
        return {"status": "succeeded"}

    client._request = available
    assert client.flush_result_outbox() == 1
    assert outbox.pending() == []
    assert calls[0][2]["attempt"] == 3


def test_result_outbox_rejects_different_payload_for_same_attempt(tmp_path):
    outbox = AgentResultOutbox(tmp_path / "result-outbox.db")
    outbox.put(
        "stage-1",
        attempt=1,
        agent_id="windows-1",
        status="succeeded",
        returncode=0,
        result={"value": 1},
    )
    with pytest.raises(AgentResultOutboxError, match="different terminal payload"):
        outbox.put(
            "stage-1",
            attempt=1,
            agent_id="windows-1",
            status="failed",
            returncode=1,
            result={"value": 2},
        )
