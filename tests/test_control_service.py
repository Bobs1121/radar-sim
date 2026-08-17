"""Focused tests for the minimal control-plane service."""

import json
import sqlite3

import pytest

from core.control_service import ControlService, INTERNAL_V1_SCHEDULER_AGENT_ID
from core.agent_policy import WINDOWS_CONNECTOR_CONTRACT_VERSION


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


def test_run_config_v2_requires_current_windows_connector_contract(tmp_path):
    service = make_service(tmp_path)
    service.create_job(
        "simulation.run_config.v2",
        owner="alice",
        tasks=[{"task_type": "run_simulation", "stage_type": "run_simulation"}],
    )
    service.register_agent(
        "old-windows",
        agent_id="old-windows",
        capabilities=["simulation.local"],
        metadata={"node_kind": "windows_full", "user": "alice"},
    )
    assert service.claim_next_task("old-windows") is None

    service.register_agent(
        "current-windows",
        agent_id="current-windows",
        capabilities=["simulation.local"],
        metadata={
            "node_kind": "windows_full",
            "user": "alice",
            "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
        },
    )
    claimed = service.claim_next_task("current-windows")
    assert claimed is not None
    assert claimed["stage_type"] == "run_simulation"

def test_windows_agent_never_claims_another_owners_task_in_shared_db(tmp_path):
    service = make_service(tmp_path)
    bob = service.create_job("local.check", owner="bob")
    alice = service.create_job("local.check", owner="alice")
    service.register_agent(
        "alice-windows",
        agent_id="alice-windows",
        node_kind="windows_agent",
        capabilities=["local.check"],
        metadata={"node_kind": "windows_agent", "user": "alice"},
    )

    claimed = service.claim_next_task("alice-windows")

    assert claimed is not None
    assert claimed["job_id"] == alice["job_id"]
    assert service.get_job(bob["job_id"])["status"] == "queued"


def test_auto_configuring_windows_agent_does_not_bind_other_owners_data(tmp_path):
    service = make_service(tmp_path)
    bob = service.create_job(
        "simulation.run_config.v2",
        owner="bob",
        tasks=[
            {
                "task_type": "prepare_data",
                "stage_type": "prepare_data",
                "assigned_agent_id": INTERNAL_V1_SCHEDULER_AGENT_ID,
                "payload": {
                    "dispatch_scope": "data_upload",
                    "project": "anonymous",
                    "data_path": "D:/bob/data/input.mf4",
                },
            }
        ],
    )
    service.register_agent(
        "alice-windows",
        agent_id="alice-windows",
        node_kind="windows_agent",
        capabilities=["data.local.read", "data.upload"],
        metadata={
            "node_kind": "windows_agent",
            "user": "alice",
            "auto_configure": True,
            "data_bindings": [],
        },
    )

    assert service.bind_pending_data_stage("alice-windows") is None
    stage = service.get_job(bob["job_id"])["stages"][0]
    assert stage["assigned_agent_id"] == INTERNAL_V1_SCHEDULER_AGENT_ID


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


def test_same_connector_reregistration_preserves_running_assignment(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job(
        "simulation.run_config.v2",
        owner="alice",
        tasks=[{"task_type": "run_simulation", "stage_type": "run_simulation"}],
    )
    metadata = {
        "node_kind": "windows_full",
        "user": "alice",
        "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
    }
    service.register_agent(
        "windows", agent_id="windows-a", capabilities=["simulation.local"], metadata=metadata
    )
    claimed = service.claim_next_task("windows-a")
    attempt = claimed["attempt_count"]

    registered = service.register_agent(
        "windows-restarted",
        agent_id="windows-a",
        hostname="new-hostname",
        capabilities=["simulation.local"],
        metadata=metadata,
    )

    assert registered["status"] == "busy"
    assert registered["current_task_id"] == claimed["task_id"]
    resumed = service.claim_next_task("windows-a")
    assert resumed["task_id"] == claimed["task_id"]
    assert resumed["attempt_count"] == attempt
    assert service.get_job(job["job_id"])["status"] == "running"


def test_windows_connector_owner_binding_is_atomic_at_the_shared_store(tmp_path):
    service = make_service(tmp_path)
    base = {
        "node_kind": "windows_full",
        "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
    }
    service.register_agent(
        "alice-pc",
        agent_id="stable-device-id",
        node_kind="windows_full",
        metadata={**base, "user": "user-alice"},
    )

    with pytest.raises(ValueError, match="connector_owner_mismatch"):
        service.register_agent(
            "bob-pc",
            agent_id="stable-device-id",
            node_kind="windows_full",
            metadata={**base, "user": "user-bob"},
        )

    stored = next(
        item for item in service.list_agents() if item["agent_id"] == "stable-device-id"
    )
    assert stored["metadata"]["user"] == "user-alice"


def test_windows_connector_store_allows_one_v8_double_namespace_repair(tmp_path):
    service = make_service(tmp_path)
    base = {
        "node_kind": "windows_full",
        "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
    }
    service.register_agent(
        "legacy-pc",
        agent_id="legacy-device-id",
        node_kind="windows_full",
        metadata={**base, "user": "user-web-0123456789abcdef01234567"},
    )
    repaired = service.register_agent(
        "legacy-pc",
        agent_id="legacy-device-id",
        node_kind="windows_full",
        metadata={**base, "user": "web-0123456789abcdef01234567"},
    )

    assert repaired["metadata"]["user"] == "web-0123456789abcdef01234567"


def test_claim_repairs_legacy_orphan_before_claiming_new_work(tmp_path):
    service = make_service(tmp_path)
    metadata = {
        "node_kind": "windows_full",
        "user": "alice",
        "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
    }
    first = service.create_job(
        "simulation.run_config.v2",
        owner="alice",
        tasks=[{"task_type": "run_simulation", "stage_type": "run_simulation"}],
    )
    service.register_agent(
        "windows", agent_id="windows-a", capabilities=["simulation.local"], metadata=metadata
    )
    claimed = service.claim_next_task("windows-a")
    second = service.create_job(
        "simulation.run_config.v2",
        owner="alice",
        tasks=[{"task_type": "run_simulation", "stage_type": "run_simulation"}],
    )
    with sqlite3.connect(tmp_path / "control.db") as conn:
        conn.execute(
            "UPDATE agents SET status='idle', current_task_id='' WHERE agent_id='windows-a'"
        )
        conn.commit()

    resumed = service.claim_next_task("windows-a")

    assert resumed["task_id"] == claimed["task_id"]
    assert resumed["attempt_count"] == claimed["attempt_count"]
    assert service.get_job(first["job_id"])["status"] == "running"
    assert service.get_job(second["job_id"])["status"] == "queued"
    agent = next(item for item in service.list_agents() if item["agent_id"] == "windows-a")
    assert agent["status"] == "busy"
    assert agent["current_task_id"] == claimed["task_id"]


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


def test_failed_task_promotes_structured_diagnostic_to_public_stage_error(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("build_selena", payload={})
    agent = service.register_agent("builder", capabilities=["build_selena"])
    task = service.claim_next_task(agent["agent_id"])

    completed = service.submit_task_result(
        task["task_id"],
        agent_id=agent["agent_id"],
        status="failed",
        returncode=1,
        result={
            "error": "The selected Visual Studio is unavailable",
            "code": "VISUAL_STUDIO_UNAVAILABLE",
            "diagnostic": {
                "code": "VISUAL_STUDIO_UNAVAILABLE",
                "category": "environment",
                "action": "Adapt the Selena script and retry",
            },
        },
    )

    error = completed["tasks"][0]["error"]
    assert error["code"] == "VISUAL_STUDIO_UNAVAILABLE"
    assert error["message"] == "The selected Visual Studio is unavailable"
    assert error["action"] == "Adapt the Selena script and retry"
    assert error["diagnostic"]["category"] == "environment"


def test_failed_local_selena_result_promotes_engine_code_and_diagnostics(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("run_simulation", payload={})
    agent = service.register_agent("windows", capabilities=["run_simulation"])
    task = service.claim_next_task(agent["agent_id"])

    completed = service.submit_task_result(
        task["task_id"],
        agent_id=agent["agent_id"],
        status="failed",
        returncode=1,
        result={
            "status": "failed",
            "summary": {"error_code": "selena_failed", "failed_input_count": 1},
            "diagnostics": {"items": [{"index": 1, "status": "failed"}]},
        },
    )

    error = completed["tasks"][0]["error"]
    assert error["code"] == "selena_failed"
    assert "Selena returned a non-zero result" in error["message"]
    assert error["diagnostics"]["items"][0]["index"] == 1


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


@pytest.mark.parametrize(
    ("target", "expected_scope"),
    [
        ("local", "local_runtime_registration"),
        ("cluster", "direct_transfer"),
    ],
)
def test_retry_repairs_legacy_register_artifact_route(tmp_path, target, expected_scope):
    service = make_service(tmp_path)
    job = service.create_job(
        "simulation.run_config.v2",
        owner="alice",
        spec={"simulation": {"target": target}},
        tasks=[
            {
                "task_type": "register_artifact",
                "stage_type": "register_artifact",
                "payload": {"build_evidence_ref": "build:1"},
            }
        ],
    )
    service.register_agent("windows", agent_id="windows", capabilities=["register_artifact"])
    claimed = service.claim_next_task("windows")
    service.submit_task_result(
        claimed["stage_id"],
        agent_id="windows",
        status="failed",
        returncode=1,
        result={"code": "old_route"},
    )

    retried = service.retry_stage(job["job_id"], claimed["stage_id"])
    stage = next(item for item in retried["stages"] if item["stage_id"] == claimed["stage_id"])

    assert stage["status"] == "queued"
    assert stage["payload"]["dispatch_scope"] == expected_scope


def test_retry_repairs_local_finalizer_bundle_and_result_handoff(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job(
        "simulation.run_config.v2",
        owner="alice",
        spec={"simulation": {"target": "local"}},
        resolved_spec={
            "decisions": {
                "execution": {"selected_target": "local"},
                "data": {"dataset": {"id": "dataset:sha256:" + "3" * 64}},
            }
        },
        tasks=[
            {"task_id": "register", "task_type": "register_artifact", "stage_type": "register_artifact"},
            {"task_id": "collect", "task_type": "collect_results", "stage_type": "collect_results"},
            {
                "task_id": "finalize",
                "task_type": "finalize_manifest",
                "stage_type": "finalize_manifest",
                "payload": {
                    "dispatch_scope": "local_simulation",
                    "runtime_bundle_id": "",
                    "result_ref": "result:sha256:" + "6" * 64,
                },
                "dependencies": ["collect"],
            },
        ],
    )
    with sqlite3.connect(tmp_path / "control.db") as conn:
        conn.execute(
            "UPDATE tasks SET status='succeeded', result_json=? WHERE task_id='register'",
            (json.dumps({"runtime_bundle": {"id": "selena-bundle:sha256:" + "2" * 64}}),),
        )
        conn.execute(
            "UPDATE tasks SET status='succeeded', result_json=? WHERE task_id='collect'",
            (
                json.dumps(
                    {
                        "local_run_lease_ref": "local-run-lease:sha256:" + "4" * 64,
                        "result_ref": "result:sha256:" + "6" * 64,
                        "delivery": {"status": "delivered", "file_count": 1, "checksum": "sha256:" + "5" * 64},
                    }
                ),
            ),
        )
        conn.execute(
            "UPDATE tasks SET status='failed', error_json=? WHERE task_id='finalize'",
            (json.dumps({"code": "local_stage_failed"}),),
        )
        conn.execute("UPDATE jobs SET status='failed' WHERE job_id=?", (job["job_id"],))

    retried = service.retry_stage(job["job_id"], "finalize")
    final = next(item for item in retried["stages"] if item["stage_id"] == "finalize")
    assert final["status"] == "queued"
    assert final["payload"]["runtime_bundle_id"].startswith("selena-bundle:")
    assert final["payload"]["result_ref"].startswith("result:")
    assert final["payload"]["delivery"]["status"] == "delivered"


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


def test_late_result_from_reclaimed_attempt_cannot_complete_new_attempt(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("local.check")
    first_agent = service.register_agent("first", agent_id="first", capabilities=["local.check"])
    claimed = service.claim_next_task(first_agent["agent_id"])
    old_attempt = int(claimed["attempt_count"])

    reclaimed = service.reclaim_stale_tasks(stale_after_seconds=-1, max_attempts=3)
    assert reclaimed and reclaimed[0]["new_status"] == "queued"

    with pytest.raises(ValueError, match="stale"):
        service.submit_task_result(
            claimed["task_id"],
            agent_id=first_agent["agent_id"],
            attempt=old_attempt,
            status="succeeded",
            returncode=0,
        )

    second_agent = service.register_agent("second", agent_id="second", capabilities=["local.check"])
    claimed_again = service.claim_next_task(second_agent["agent_id"])
    assert claimed_again["attempt_count"] == old_attempt + 1
    completed = service.submit_task_result(
        claimed_again["task_id"],
        agent_id=second_agent["agent_id"],
        attempt=int(claimed_again["attempt_count"]),
        status="succeeded",
        returncode=0,
    )
    assert completed["status"] == "succeeded"


def test_heartbeat_cannot_claim_another_agent_task_identity(tmp_path):
    service = make_service(tmp_path)
    first_job = service.create_job("local.check")
    second_job = service.create_job("local.check")
    agent = service.register_agent("runner", agent_id="runner", capabilities=["local.check"])
    first = service.claim_next_task(agent["agent_id"])
    second_id = service.get_job(second_job["job_id"])["stages"][0]["stage_id"]

    with pytest.raises(ValueError, match="heartbeat task"):
        service.heartbeat(agent["agent_id"], status="busy", current_task_id=second_id)

    current = service.get_job(first_job["job_id"])["stages"][0]
    assert current["status"] == "running"
    assert service.list_agents()[0]["current_task_id"] == first["task_id"]


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


def test_light_agent_registration_filters_self_declared_runtime_capabilities(tmp_path):
    service = make_service(tmp_path)
    agent = service.register_agent(
        "light",
        agent_id="light-a",
        capabilities=["LOCAL.BUILD_SELENA", "*", "local.run_sim", "cluster.run"],
        metadata={"node_kind": " Windows_Agent ", "windows_mode": "light"},
    )
    assert agent["capabilities"] == ["local.build_selena"]
    assert agent["metadata"]["node_kind"] == "windows_agent"
    assert agent["metadata"]["capability_policy"] == "filtered"
    assert agent["metadata"]["rejected_capability_count"] == 3
    assert "rejected_capabilities" not in agent["metadata"]


def test_light_agent_claim_gate_blocks_corrupt_wildcard_runtime_record(tmp_path):
    db_path = tmp_path / "control.db"
    service = ControlService(db_path=db_path)
    service.register_agent(
        "light",
        agent_id="light-a",
        capabilities=["local.build_selena"],
        metadata={"node_kind": "windows_agent"},
    )
    forbidden = service.create_job("local.run_sim", payload={"project": "demo"})
    allowed = service.create_job("local.build_selena", payload={"project": "demo"})
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE agents SET capabilities_json=? WHERE agent_id=?",
            (json.dumps(["*", "local.run_sim", "cluster.run"]), "light-a"),
        )

    claimed = service.claim_next_task("light-a")
    assert claimed["job_id"] == allowed["job_id"]
    assert service.get_job(forbidden["job_id"])["status"] == "queued"


def test_windows_full_can_claim_local_sim_but_not_cluster_runtime(tmp_path):
    service = make_service(tmp_path)
    agent = service.register_agent(
        "full",
        agent_id="full-a",
        capabilities=["local.run_sim", "cluster.run"],
        metadata={"node_kind": "windows_full", "windows_mode": "full"},
    )
    assert agent["capabilities"] == ["local.run_sim"]
    cluster = service.create_job("cluster.run", payload={})
    local = service.create_job("local.run_sim", payload={})
    claimed = service.claim_next_task("full-a")
    assert claimed["job_id"] == local["job_id"]
    assert service.get_job(cluster["job_id"])["status"] == "queued"


def test_public_registration_rejects_unknown_declared_node_kind(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(ValueError, match="unsupported agent node kind"):
        service.register_agent(
            "typo",
            capabilities=["*"],
            metadata={"node_kind": "window_agent"},
        )


def test_light_agent_formal_build_capability_claims_v5_build_stage_alias(tmp_path):
    service = make_service(tmp_path)
    service.register_agent(
        "light",
        agent_id="light-a",
        capabilities=["build.selena"],
        metadata={"node_kind": "windows_agent"},
    )
    job = service.create_job("simulation.v1", tasks=[{"task_type": "build_selena"}])
    claimed = service.claim_next_task("light-a")
    assert claimed["job_id"] == job["job_id"]
    assert claimed["task_type"] == "build_selena"
