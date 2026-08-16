"""Tests for dead-agent recovery (ControlService.reclaim_stale_tasks)."""

from core.control_service import ControlService


class _Clock:
    """Controllable monotonic clock for deterministic reclaim tests."""

    def __init__(self, start: float = 1_000_000.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


def _service(tmp_path, clock):
    return ControlService(db_path=tmp_path / "control.db", now_fn=clock)


def test_reclaim_requeues_task_when_agent_silent(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    agent = service.register_agent("dead-agent", capabilities=["local.*"])
    job = service.create_job("local.check", payload={"project": "ovrs25"})
    task = service.claim_next_task(agent["agent_id"])
    assert task["status"] == "running"

    # Agent never heartbeats again — advance past the stale threshold.
    clock.advance(400)  # > default 300s
    reclaimed = service.reclaim_stale_tasks(stale_after_seconds=300.0, max_attempts=3)
    assert len(reclaimed) == 1
    assert reclaimed[0]["task_id"] == task["task_id"]
    assert reclaimed[0]["new_status"] == "queued"

    # The task is now claimable by a fresh agent.
    fresh = service.register_agent("fresh-agent", capabilities=["local.*"])
    reclaimed_task = service.claim_next_task(fresh["agent_id"])
    assert reclaimed_task is not None
    assert reclaimed_task["task_id"] == task["task_id"]
    assert reclaimed_task["status"] == "running"
    # attempt_count incremented on re-claim.
    assert reclaimed_task["attempt_count"] >= 2


def test_reclaim_fails_task_after_max_attempts(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    agent = service.register_agent("crash-agent", capabilities=["local.*"])
    service.create_job("local.check", payload={"project": "ovrs25"})
    task = service.claim_next_task(agent["agent_id"])
    # The original claim already bumped attempt_count to 1.

    # Repeatedly crash + reclaim until max_attempts (3) is hit.
    for _ in range(3):
        clock.advance(400)
        service.reclaim_stale_tasks(stale_after_seconds=300.0, max_attempts=3)
        # Try to re-claim with a fresh agent (simulating the crash loop).
        fresh = service.register_agent(
            f"agent-{clock.t}", capabilities=["local.*"]
        )
        maybe = service.claim_next_task(fresh["agent_id"])
        if maybe is None:
            break  # task is no longer queued — it failed

    final = service.get_job(task["job_id"])["tasks"][0]
    assert final["status"] == "failed"
    assert final["returncode"] == -1
    assert "max_attempts" in (final["result"].get("error") or "")


def test_reclaim_leaves_healthy_running_task_alone(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    agent = service.register_agent("healthy-agent", capabilities=["local.*"])
    service.create_job("local.check", payload={"project": "ovrs25"})
    service.claim_next_task(agent["agent_id"])

    # Agent keeps heartbeating — well within the threshold.
    clock.advance(100)
    service.heartbeat(agent["agent_id"])
    clock.advance(100)  # 200s since heartbeat, < 300s threshold
    reclaimed = service.reclaim_stale_tasks(stale_after_seconds=300.0, max_attempts=3)
    assert reclaimed == []


def test_reclaim_is_idempotent(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    agent = service.register_agent("dead-agent", capabilities=["local.*"])
    service.create_job("local.check", payload={"project": "ovrs25"})
    service.claim_next_task(agent["agent_id"])
    clock.advance(400)

    first = service.reclaim_stale_tasks(stale_after_seconds=300.0, max_attempts=5)
    assert len(first) == 1
    # Second call finds nothing running (task already requeued).
    second = service.reclaim_stale_tasks(stale_after_seconds=300.0, max_attempts=5)
    assert second == []


def test_reclaim_unlimited_attempts(tmp_path):
    """max_attempts=None (0 from CLI) never fails — keeps requeueing forever."""
    clock = _Clock()
    service = _service(tmp_path, clock)
    agent = service.register_agent("loop-agent", capabilities=["local.*"])
    service.create_job("local.check", payload={"project": "ovrs25"})
    task = service.claim_next_task(agent["agent_id"])

    for _ in range(10):
        clock.advance(400)
        service.reclaim_stale_tasks(stale_after_seconds=300.0, max_attempts=None)

    final = service.get_job(task["job_id"])["tasks"][0]
    assert final["status"] == "queued"  # never failed despite many reclaims


def test_reclaim_online_idle_orphan_after_assignment_grace(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    agent = service.register_agent("idle-agent", capabilities=["local.*"])
    service.create_job("local.check")
    task = service.claim_next_task(agent["agent_id"])

    # The machine still heartbeats, but explicitly reports no owned task.
    service.heartbeat(agent["agent_id"], status="idle", current_task_id="")
    clock.advance(31)
    reclaimed = service.reclaim_stale_tasks(stale_after_seconds=300.0, max_attempts=3)

    assert reclaimed[0]["task_id"] == task["task_id"]
    assert reclaimed[0]["new_status"] == "queued"


def test_reclaim_online_agent_running_another_task_orphans_old_task(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    agent = service.register_agent("busy-agent", capabilities=["local.*"])
    first_job = service.create_job("local.check")
    second_job = service.create_job("local.check")
    first = service.claim_next_task(agent["agent_id"])
    # Jobs created against the deterministic test clock have identical
    # timestamps, so claim_next_task is free to pick either one.  Select the
    # other task by id instead of relying on FIFO ordering for a timestamp tie.
    candidate_task_ids = [
        service.get_job(job["job_id"])["tasks"][0]["task_id"]
        for job in (first_job, second_job)
    ]
    second_task_id = next(
        task_id for task_id in candidate_task_ids if task_id != first["task_id"]
    )

    # A heartbeat naming another task is rejected instead of being allowed to
    # refresh the wrong assignment.  Once the real heartbeat becomes stale,
    # the first task can be reclaimed normally.
    try:
        service.heartbeat(
            agent["agent_id"], status="busy", current_task_id=second_task_id
        )
    except ValueError as exc:
        assert "heartbeat task" in str(exc)
    else:
        raise AssertionError("heartbeat must reject another task identity")
    clock.advance(301)
    reclaimed = service.reclaim_stale_tasks(stale_after_seconds=300.0, max_attempts=3)

    assert [item["task_id"] for item in reclaimed] == [first["task_id"]]
    assert service.get_task(first["task_id"])["status"] == "queued"
    assert service.get_task(second_task_id)["status"] == "queued"


def test_reclaim_does_not_take_just_claimed_task_during_assignment_grace(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    agent = service.register_agent("new-agent", capabilities=["local.*"])
    service.create_job("local.check")
    task = service.claim_next_task(agent["agent_id"])
    service.heartbeat(agent["agent_id"], status="idle", current_task_id="")

    # The assignment is recent, so a Connector status hand-off cannot be
    # mistaken for a dead process.
    assert service.reclaim_stale_tasks(stale_after_seconds=300.0, max_attempts=3) == []
    assert service.get_task(task["task_id"])["status"] == "running"


def test_reclaim_keeps_true_busy_task_with_matching_current_task(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    agent = service.register_agent("healthy-agent", capabilities=["local.*"])
    service.create_job("local.check")
    task = service.claim_next_task(agent["agent_id"])
    service.heartbeat(
        agent["agent_id"], status="busy", current_task_id=task["task_id"]
    )
    # Keep the connector's heartbeat fresh even though the assignment has
    # been running for longer than the stale threshold.  Reclaim must use the
    # latest heartbeat and ownership, not the original claim timestamp.
    clock.advance(400)
    service.heartbeat(
        agent["agent_id"], status="busy", current_task_id=task["task_id"]
    )
    clock.advance(100)

    assert service.reclaim_stale_tasks(stale_after_seconds=300.0, max_attempts=3) == []
    assert service.get_task(task["task_id"])["status"] == "running"


def test_reclaim_accepts_legacy_busy_empty_task_contract(tmp_path):
    clock = _Clock()
    service = _service(tmp_path, clock)
    agent = service.register_agent("legacy-busy-agent", capabilities=["local.*"])
    service.create_job("local.check")
    task = service.claim_next_task(agent["agent_id"])
    service.heartbeat(agent["agent_id"], status="busy", current_task_id="")
    # Legacy agents identify an active task only through ``status=busy`` and
    # an empty current_task_id.  Their heartbeat must still remain fresh.
    clock.advance(400)
    service.heartbeat(agent["agent_id"], status="busy", current_task_id="")
    clock.advance(100)

    assert service.reclaim_stale_tasks(stale_after_seconds=300.0, max_attempts=3) == []
    assert service.get_task(task["task_id"])["status"] == "running"
