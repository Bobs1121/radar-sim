"""Focused tests for the minimal control-plane service."""

from core.control_service import ControlService


def make_service(tmp_path):
    return ControlService(db_path=tmp_path / "control.db")


def test_create_job_register_agent_and_claim_task(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("local.check", payload={"project": "ovrs25"})
    assert job["status"] == "queued"
    assert job["tasks"][0]["task_type"] == "local.check"

    agent = service.register_agent("win-agent", capabilities=["local.*"])
    task = service.claim_next_task(agent["agent_id"])
    assert task is not None
    assert task["job_id"] == job["job_id"]
    assert task["status"] == "running"

    claimed_job = service.get_job(job["job_id"])
    assert claimed_job["status"] == "running"
    assert claimed_job["tasks"][0]["assigned_agent_id"] == agent["agent_id"]


def test_logs_and_result_flow_updates_job(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("local.run_sim", payload={"project": "ovrs25", "input_mf4": "D:/data/case.MF4"})
    agent = service.register_agent("runner", capabilities=["local.run_sim"])
    task = service.claim_next_task(agent["agent_id"])
    assert task is not None

    service.append_logs(task["task_id"], ["line-1", "line-2"])
    logs = service.get_logs(job_id=job["job_id"])
    assert [entry["message"] for entry in logs["entries"]] == ["line-1", "line-2"]
    assert logs["next_since"] >= 2

    completed = service.submit_task_result(
        task["task_id"],
        agent_id=agent["agent_id"],
        returncode=0,
        result={"summary": "ok"},
    )
    assert completed["status"] == "succeeded"
    assert completed["tasks"][0]["status"] == "succeeded"
    assert completed["result"]["task_results"][0]["result"]["summary"] == "ok"


def test_cancel_running_job_sets_cancel_requested_and_final_cancelled(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("local.build_selena", payload={"project": "ovrs25"})
    agent = service.register_agent("builder", capabilities=["local.build_selena"])
    task = service.claim_next_task(agent["agent_id"])
    assert task is not None

    cancelled = service.cancel_job(job["job_id"])
    assert cancelled["status"] == "cancel_requested"
    assert cancelled["tasks"][0]["cancel_requested"] is True

    heartbeat = service.heartbeat(agent["agent_id"], status="busy", current_task_id=task["task_id"])
    assert heartbeat["cancel_requested"] is True

    finished = service.submit_task_result(
        task["task_id"],
        agent_id=agent["agent_id"],
        returncode=-15,
    )
    assert finished["status"] == "cancelled"
    assert finished["tasks"][0]["status"] == "cancelled"


def test_heartbeat_without_current_task_keeps_assignment(tmp_path):
    service = make_service(tmp_path)
    service.create_job("local.check", payload={"project": "ovrs25"})
    agent = service.register_agent("checker", capabilities=["local.check"])
    task = service.claim_next_task(agent["agent_id"])
    assert task is not None

    heartbeat = service.heartbeat(agent["agent_id"], status="busy")
    assert heartbeat["agent"]["current_task_id"] == task["task_id"]


def test_cancel_queued_job_immediately_cancels_task(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("cluster.run", payload={"project": "ovrs25", "dataset": "smoke"})
    cancelled = service.cancel_job(job["job_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["tasks"][0]["status"] == "cancelled"


def test_multistep_job_claims_tasks_in_order_and_stops_after_failure(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job(
        "pipeline",
        tasks=[
            {"task_type": "local.check", "payload": {"project": "ovrs25"}},
            {"task_type": "local.run_sim", "payload": {"project": "ovrs25", "input_mf4": "D:/data/case.MF4"}},
            {"task_type": "cluster.run", "payload": {"project": "ovrs25", "dataset": "smoke"}},
        ],
    )
    agent = service.register_agent("runner", capabilities=["local.*"])

    first_task = service.claim_next_task(agent["agent_id"])
    assert first_task is not None
    assert first_task["order_index"] == 0
    assert first_task["task_type"] == "local.check"
    assert service.claim_next_task(agent["agent_id"]) == first_task

    queued_job = service.submit_task_result(first_task["task_id"], agent_id=agent["agent_id"], returncode=0)
    assert queued_job["status"] == "queued"
    assert [task["status"] for task in queued_job["tasks"]] == ["succeeded", "queued", "queued"]

    second_task = service.claim_next_task(agent["agent_id"])
    assert second_task is not None
    assert second_task["order_index"] == 1
    assert second_task["task_type"] == "local.run_sim"

    failed_job = service.submit_task_result(second_task["task_id"], agent_id=agent["agent_id"], returncode=2)
    assert failed_job["status"] == "failed"
    assert [task["status"] for task in failed_job["tasks"]] == ["succeeded", "failed", "cancelled"]
    assert service.claim_next_task(agent["agent_id"]) is None


def test_cancel_completed_job_is_noop(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("local.check", payload={"project": "ovrs25"})
    agent = service.register_agent("checker", capabilities=["local.check"])
    task = service.claim_next_task(agent["agent_id"])
    assert task is not None

    finished = service.submit_task_result(task["task_id"], agent_id=agent["agent_id"], returncode=0)
    cancelled = service.cancel_job(job["job_id"])

    assert cancelled["status"] == "succeeded"
    assert cancelled["cancel_requested"] is False
    assert cancelled["completed_at"] == finished["completed_at"]


def test_submit_task_result_rejects_different_agent(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("local.check", payload={"project": "ovrs25"})
    agent = service.register_agent("checker", capabilities=["local.check"])
    task = service.claim_next_task(agent["agent_id"])
    assert task is not None

    other_agent = service.register_agent("other", capabilities=["local.check"])

    try:
        service.submit_task_result(task["task_id"], agent_id=other_agent["agent_id"], returncode=0)
    except ValueError as exc:
        assert "assigned to" in str(exc)
    else:
        raise AssertionError("submit_task_result should reject a different agent")

    current = service.get_job(job["job_id"])
    assert current["status"] == "running"
    assert current["tasks"][0]["status"] == "running"


def test_claim_respects_capabilities(tmp_path):
    service = make_service(tmp_path)
    service.create_job("cluster.run", payload={"project": "ovrs25"})
    agent = service.register_agent("local-only", capabilities=["local.*"])
    assert service.claim_next_task(agent["agent_id"]) is None


def test_multi_task_job_waits_for_all_tasks_before_success(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job(
        "workflow",
        tasks=[
            {"task_type": "local.check", "payload": {"project": "ovrs25"}},
            {"task_type": "local.run_sim", "payload": {"project": "ovrs25", "input_mf4": "D:/data/case.MF4"}},
        ],
    )
    agent = service.register_agent("runner", capabilities=["local.*"])
    first = service.claim_next_task(agent["agent_id"])
    assert first is not None

    partial = service.submit_task_result(first["task_id"], agent_id=agent["agent_id"], returncode=0)
    assert partial["status"] == "queued"
    assert [task["status"] for task in partial["tasks"]] == ["succeeded", "queued"]

    second = service.claim_next_task(agent["agent_id"])
    assert second is not None
    done = service.submit_task_result(second["task_id"], agent_id=agent["agent_id"], returncode=0)
    assert done["status"] == "succeeded"
    assert [task["status"] for task in done["tasks"]] == ["succeeded", "succeeded"]


def test_list_agents_returns_shape_and_status(tmp_path):
    """list_agents() returns every registered agent with the same shape as
    register_agent/heartbeat, so the observability endpoint has consistent
    fields for operators verifying agent registration."""
    service = make_service(tmp_path)
    assert service.list_agents() == []  # empty before any registration

    a = service.register_agent(
        "win-01", agent_id="agent-a", hostname="winhost1",
        platform="Windows", capabilities=["local.check", "local.run_sim"],
    )
    b = service.register_agent(
        "win-02", agent_id="agent-b", hostname="winhost2",
        platform="Windows", capabilities=["local.build_selena"],
    )

    agents = service.list_agents()
    assert {x["agent_id"] for x in agents} == {"agent-a", "agent-b"}
    by_id = {x["agent_id"]: x for x in agents}
    # Shape parity with register_agent output.
    assert by_id["agent-a"]["name"] == "win-01"
    assert by_id["agent-a"]["hostname"] == "winhost1"
    assert by_id["agent-a"]["capabilities"] == ["local.check", "local.run_sim"]
    assert by_id["agent-a"]["status"] == "idle"
    assert by_id["agent-a"]["current_task_id"] == ""
    assert "registered_at" in by_id["agent-a"]
    assert "last_heartbeat" in by_id["agent-a"]

    # Re-registering the same agent_id upserts (no duplicate row).
    service.register_agent(
        "win-01-renamed", agent_id="agent-a", hostname="winhost1",
        platform="Windows", capabilities=["local.check"],
    )
    agents = service.list_agents()
    assert len(agents) == 2  # still two, not three
    by_id = {x["agent_id"]: x for x in agents}
    assert by_id["agent-a"]["name"] == "win-01-renamed"
