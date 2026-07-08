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
