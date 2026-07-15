import concurrent.futures
import sqlite3
import threading

import pytest

from core.control_service import ControlService


def make_service(tmp_path):
    return ControlService(tmp_path / "control.db")


def test_explicit_dependencies_allow_source_and_data_branches_and_skipped_success(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job(
        "workflow",
        tasks=[
            {"task_type": "resolve", "stage_type": "resolve_spec"},
            {"task_type": "env", "stage_type": "environment_check", "dependencies": ["resolve_spec"]},
            {"task_type": "source", "stage_type": "prepare_source", "dependencies": ["environment_check"], "status": "skipped", "skip_reason": "existing"},
            {"task_type": "data", "stage_type": "prepare_data", "dependencies": ["environment_check"]},
            {"task_type": "preflight", "stage_type": "preflight", "dependencies": ["prepare_source", "prepare_data"]},
        ],
    )
    agent = service.register_agent("runner", agent_id="runner", capabilities=["*"])

    first = service.claim_next_task("runner")
    assert first["stage_type"] == "resolve_spec"
    service.submit_task_result(first["stage_id"], agent_id="runner", returncode=0)

    second = service.claim_next_task("runner")
    assert second["stage_type"] == "environment_check"
    service.submit_task_result(second["stage_id"], agent_id="runner", returncode=0)

    data = service.claim_next_task("runner")
    assert data["stage_type"] == "prepare_data"
    service.submit_task_result(data["stage_id"], agent_id="runner", returncode=0)

    preflight = service.claim_next_task("runner")
    assert preflight["stage_type"] == "preflight"
    current = service.get_job(job["job_id"])
    assert current["stages"][2]["status"] == "skipped"
    assert current["stages"][4]["dependencies"] == [current["stages"][2]["stage_id"], current["stages"][3]["stage_id"]]


def test_legacy_order_claim_still_treats_skipped_as_success(tmp_path):
    service = make_service(tmp_path)
    service.create_job(
        "legacy",
        tasks=[
            {"task_type": "legacy.skip", "status": "skipped", "skip_reason": "not needed"},
            {"task_type": "legacy.next"},
        ],
    )
    service.register_agent("runner", agent_id="runner", capabilities=["legacy.*"])
    claimed = service.claim_next_task("runner")
    assert claimed["task_type"] == "legacy.next"


def test_event_sequence_is_job_local_and_concurrent_append_is_monotonic(tmp_path):
    db_path = tmp_path / "events.db"
    service = ControlService(db_path)
    job_a = service.create_job("event.job", tasks=[{"task_type": "noop", "status": "skipped"}])
    job_b = service.create_job("event.job", tasks=[{"task_type": "noop", "status": "skipped"}])
    barrier = threading.Barrier(8)

    def append(index: int) -> int:
        svc = ControlService(db_path)
        barrier.wait(timeout=5)
        return svc.append_job_event(job_a["job_id"], message=f"event-{index}")["sequence"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        sequences = sorted(pool.map(append, range(8)))

    # A single skipped task flips the job to a terminal status at creation, so
    # create_job writes job.created + stage.skipped + job.status (3 initial
    # events). The 8 concurrent appends therefore start at sequence 4.
    assert sequences == list(range(4, 12))
    service.append_job_event(job_b["job_id"], message="other")
    assert [event["sequence"] for event in service.list_events(job_b["job_id"])["events"]] == [1, 2, 3, 4]


def test_append_logs_double_writes_task_logs_and_structured_log_events(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("local.check")
    stage_id = job["stages"][0]["stage_id"]
    service.append_logs(stage_id, ["line-1"], stream="stderr")

    assert service.get_logs(job_id=job["job_id"])["entries"][0]["message"] == "line-1"
    events = service.list_events(job["job_id"])["events"]
    log_events = [event for event in events if event["event"] == "log"]
    assert log_events[0]["level"] == "error"
    assert log_events[0]["stage_id"] == stage_id


def test_progress_event_does_not_infer_final_status(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("local.check")
    stage_id = job["stages"][0]["stage_id"]
    event = service.report_stage_progress(stage_id, progress=0.4, message="working", code="P40")

    current = service.get_job(job["job_id"])
    assert current["stages"][0]["progress"] == 0.4
    assert current["stages"][0]["status"] == "queued"
    assert event["event"] == "stage.progress"
    assert event["code"] == "P40"


def test_attempt_fail_retry_attempt_two_success_preserves_attempt_one(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job(
        "pipeline",
        tasks=[
            {"task_type": "first", "stage_type": "first"},
            {"task_type": "second", "stage_type": "second", "dependencies": ["first"]},
            {"task_type": "third", "stage_type": "third", "dependencies": ["second"]},
        ],
    )
    service.register_agent("runner", agent_id="runner", capabilities=["*"])
    first = service.claim_next_task("runner")
    failed_job = service.submit_task_result(
        first["stage_id"],
        agent_id="runner",
        status="failed",
        returncode=2,
        result={"code": "E_FIRST", "message": "failed", "actions": [{"type": "fix"}]},
    )
    assert failed_job["status"] == "failed"
    assert [stage["status"] for stage in failed_job["stages"]] == ["failed", "cancelled", "cancelled"]
    attempt1 = service.list_attempts(first["stage_id"])[0]
    assert attempt1["attempt"] == 1
    assert attempt1["status"] == "failed"
    assert attempt1["result"]["code"] == "E_FIRST"

    retried = service.retry_stage(job["job_id"], first["stage_id"])
    assert [stage["status"] for stage in retried["stages"]] == ["queued", "queued", "queued"]
    second_try = service.claim_next_task("runner")
    assert second_try["stage_id"] == first["stage_id"]
    service.submit_task_result(second_try["stage_id"], agent_id="runner", returncode=0)

    attempts = service.list_attempts(first["stage_id"])
    assert [attempt["attempt"] for attempt in attempts] == [1, 2]
    assert attempts[0] == attempt1
    assert attempts[1]["status"] == "succeeded"


def test_retry_rejects_invalid_stage_state(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("local.check")
    with pytest.raises(ValueError, match="only failed/cancelled"):
        service.retry_stage(job["job_id"], job["stages"][0]["stage_id"])


def test_cancel_preserves_skipped_stage_and_finishes_running_cancelled(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job(
        "pipeline",
        tasks=[
            {"task_type": "skip", "status": "skipped", "skip_reason": "not needed"},
            {"task_type": "run"},
        ],
    )
    service.register_agent("runner", agent_id="runner", capabilities=["*"])
    running = service.claim_next_task("runner")
    cancelling = service.cancel_job(job["job_id"])
    assert cancelling["status"] == "cancel_requested"
    assert cancelling["stages"][0]["status"] == "skipped"
    assert cancelling["stages"][1]["cancel_requested"] is True

    finished = service.submit_task_result(running["stage_id"], agent_id="runner", returncode=-15)
    assert finished["status"] == "cancelled"
    assert [stage["status"] for stage in finished["stages"]] == ["skipped", "cancelled"]


def test_old_db_stage_migration_adds_compatible_columns(tmp_path):
    db_path = tmp_path / "old-stage.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            task_type TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            assigned_agent_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            claimed_at REAL NOT NULL DEFAULT 0,
            started_at REAL NOT NULL DEFAULT 0,
            completed_at REAL NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            returncode INTEGER
        );
        CREATE TABLE task_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            stream TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE agents (
            agent_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            platform TEXT NOT NULL,
            hostname TEXT NOT NULL,
            capabilities_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            status TEXT NOT NULL,
            registered_at REAL NOT NULL,
            last_heartbeat REAL NOT NULL,
            current_task_id TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.close()

    job = ControlService(db_path).create_job("local.check")
    assert job["stages"][0]["stage_type"] == "local.check"
    columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(tasks)").fetchall()}
    assert {"stage_type", "dependencies_json", "progress", "input_ref_json", "output_ref_json", "error_json", "skip_reason", "initial_status"} <= columns


def test_create_job_writes_initial_job_and_stage_events(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job(
        "pipeline",
        tasks=[
            {"task_type": "first", "stage_type": "first"},
            {"task_type": "skip", "stage_type": "skip", "status": "skipped", "skip_reason": "not needed"},
        ],
    )

    events = service.list_events(job["job_id"])["events"]
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert [event["event"] for event in events] == ["job.created", "stage.queued", "stage.skipped"]
    assert events[1]["detail"]["stage_type"] == "first"
    assert events[2]["detail"]["skip_reason"] == "not needed"


def test_create_job_rejects_invalid_duplicate_and_self_dependencies(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(ValueError, match="unknown dependency"):
        service.create_job("bad", tasks=[{"task_type": "a", "stage_type": "a", "dependencies": ["missing"]}])
    with pytest.raises(ValueError, match="self-dependency"):
        service.create_job("bad", tasks=[{"task_type": "a", "stage_type": "a", "dependencies": ["a"]}])
    with pytest.raises(ValueError, match="duplicate dependency"):
        service.create_job(
            "bad",
            tasks=[
                {"task_type": "a", "stage_type": "a"},
                {"task_type": "b", "stage_type": "b", "dependencies": ["a", "a"]},
            ],
        )


def test_reclaim_stale_requeues_with_terminal_attempt_and_event(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("local.check")
    service.register_agent("runner", agent_id="runner", capabilities=["*"])
    stage = service.claim_next_task("runner")
    service.heartbeat("runner", status="busy", current_task_id=stage["stage_id"])

    reclaimed = service.reclaim_stale_tasks(stale_after_seconds=-1, max_attempts=3)

    assert reclaimed[0]["new_status"] == "queued"
    current = service.get_job(job["job_id"])
    assert current["stages"][0]["status"] == "queued"
    assert current["stages"][0]["error"]["code"] == "AGENT_STALE"
    attempts = service.list_attempts(stage["stage_id"])
    assert attempts[0]["attempt"] == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error"]["code"] == "AGENT_STALE"
    events = service.list_events(job["job_id"])["events"]
    assert any(event["event"] == "stage.requeued" and event["code"] == "AGENT_STALE" for event in events)
    assert service.list_agents()[0]["current_task_id"] == ""

    claimed_again = service.claim_next_task("runner")
    assert claimed_again["attempt_count"] == 2


def test_reclaim_stale_max_attempts_fails_stage_attempt_and_downstream(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job(
        "pipeline",
        tasks=[
            {"task_type": "first", "stage_type": "first"},
            {"task_type": "second", "stage_type": "second", "dependencies": ["first"]},
        ],
    )
    service.register_agent("runner", agent_id="runner", capabilities=["*"])
    stage = service.claim_next_task("runner")

    reclaimed = service.reclaim_stale_tasks(stale_after_seconds=-1, max_attempts=1)

    assert reclaimed[0]["new_status"] == "failed"
    current = service.get_job(job["job_id"])
    assert current["status"] == "failed"
    assert [stage["status"] for stage in current["stages"]] == ["failed", "cancelled"]
    assert current["stages"][0]["error"]["code"] == "AGENT_STALE"
    assert current["stages"][1]["error"]["code"] == "UPSTREAM_FAILED"
    assert service.list_attempts(stage["stage_id"])[0]["status"] == "failed"
    assert any(event["event"] == "stage.failed" and event["code"] == "AGENT_STALE" for event in service.list_events(job["job_id"])["events"])


def test_reclaim_stale_cancel_requested_finishes_cancelled_without_requeue(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("local.check")
    service.register_agent("runner", agent_id="runner", capabilities=["*"])
    stage = service.claim_next_task("runner")
    service.cancel_job(job["job_id"])

    reclaimed = service.reclaim_stale_tasks(stale_after_seconds=-1, max_attempts=3)

    assert reclaimed[0]["new_status"] == "cancelled"
    current = service.get_job(job["job_id"])
    assert current["status"] == "cancelled"
    assert current["stages"][0]["status"] == "cancelled"
    assert service.list_attempts(stage["stage_id"])[0]["status"] == "cancelled"
    assert service.claim_next_task("runner") is None
    assert any(event["event"] == "stage.cancelled" and event["code"] == "AGENT_STALE" for event in service.list_events(job["job_id"])["events"])


def test_direct_submit_queued_task_creates_synthetic_attempt(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job("local.check")
    stage_id = job["stages"][0]["stage_id"]

    finished = service.submit_task_result(stage_id, status="succeeded", returncode=0, result={"direct": True})

    assert finished["status"] == "succeeded"
    attempts = service.list_attempts(stage_id)
    assert len(attempts) == 1
    assert attempts[0]["attempt"] == 1
    assert attempts[0]["status"] == "succeeded"
    assert attempts[0]["result"] == {"direct": True}
    with pytest.raises(ValueError, match="already completed"):
        service.submit_task_result(stage_id, status="succeeded", returncode=0)


def test_concurrent_append_logs_keeps_task_logs_and_events_contiguous(tmp_path):
    db_path = tmp_path / "concurrent-logs.db"
    service = ControlService(db_path)
    job = service.create_job("local.check")
    stage_id = job["stages"][0]["stage_id"]
    barrier = threading.Barrier(8)

    def append(index: int) -> int:
        svc = ControlService(db_path)
        barrier.wait(timeout=5)
        return svc.append_logs(stage_id, [f"line-{index}"])["appended"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(append, range(8))) == [1] * 8

    logs = service.get_logs(job_id=job["job_id"], limit=20)["entries"]
    events = service.list_events(job["job_id"], limit=50)["events"]
    log_events = [event for event in events if event["event"] == "log"]
    assert len(logs) == 8
    assert len(log_events) == 8
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))


def test_retry_source_restores_upstream_cancelled_parallel_branch_and_downstream(tmp_path):
    service = make_service(tmp_path)
    job = service.create_job(
        "standard",
        tasks=[
            {"task_type": "resolve", "stage_type": "resolve_spec"},
            {"task_type": "env", "stage_type": "environment_check", "dependencies": ["resolve_spec"]},
            {"task_type": "source", "stage_type": "prepare_source", "dependencies": ["environment_check"]},
            {"task_type": "data", "stage_type": "prepare_data", "dependencies": ["environment_check"]},
            {"task_type": "build", "stage_type": "build_selena", "dependencies": ["prepare_source"]},
            {"task_type": "artifact", "stage_type": "register_artifact", "dependencies": ["build_selena"]},
            {"task_type": "preflight", "stage_type": "preflight", "dependencies": ["register_artifact", "prepare_data"]},
        ],
    )
    service.register_agent("runner", agent_id="runner", capabilities=["*"])
    resolve = service.claim_next_task("runner")
    service.submit_task_result(resolve["stage_id"], agent_id="runner", returncode=0)
    env = service.claim_next_task("runner")
    service.submit_task_result(env["stage_id"], agent_id="runner", returncode=0)
    source = service.claim_next_task("runner")
    data = service.register_agent("data-runner", agent_id="data-runner", capabilities=["*"]) and service.claim_next_task("data-runner")
    assert source["stage_type"] == "prepare_source"
    assert data["stage_type"] == "prepare_data"

    failed = service.submit_task_result(source["stage_id"], agent_id="runner", status="failed", returncode=2)
    assert failed["status"] == "cancel_requested"
    data_cancelled = service.submit_task_result(data["stage_id"], agent_id="data-runner", status="cancelled", returncode=-15)
    by_type = {stage["stage_type"]: stage for stage in data_cancelled["stages"]}
    assert by_type["prepare_data"]["error"]["code"] == "UPSTREAM_FAILED"
    assert by_type["prepare_data"]["error"]["upstream_stage_id"] == source["stage_id"]

    retried = service.retry_stage(job["job_id"], source["stage_id"])
    by_type = {stage["stage_type"]: stage for stage in retried["stages"]}
    assert by_type["prepare_source"]["status"] == "queued"
    assert by_type["prepare_data"]["status"] == "queued"
    assert by_type["build_selena"]["status"] == "queued"
    assert by_type["preflight"]["status"] == "queued"

    source2 = service.claim_next_task("runner")
    data2 = service.claim_next_task("data-runner")
    assert source2["stage_id"] == source["stage_id"]
    assert data2["stage_id"] == data["stage_id"]
    assert source2["attempt_count"] == 2
    assert data2["attempt_count"] == 2
    service.submit_task_result(source2["stage_id"], agent_id="runner", returncode=0)
    service.submit_task_result(data2["stage_id"], agent_id="data-runner", returncode=0)
    build = service.claim_next_task("runner")
    assert build["stage_type"] == "build_selena"


def test_finalize_manifest_stage_publishes_job_result(tmp_path):
    service = ControlService(tmp_path / "control.db")
    job = service.create_job(
        "simulation.run_config.v2",
        owner="alice",
        tasks=[{"task_type": "finalize_manifest", "stage_type": "finalize_manifest"}],
    )
    service.register_agent("finalizer", agent_id="finalizer", capabilities=["*"])
    task = service.claim_next_task("finalizer")
    manifest = {"schema_version": "radar-sim.run-manifest/2.0", "result_ref": "result:sha256:" + "a" * 64}
    completed = service.submit_task_result(
        task["stage_id"], agent_id="finalizer", status="succeeded", returncode=0,
        result={"manifest": manifest},
    )

    assert completed["status"] == "succeeded"
    assert completed["result"] == {"manifest": manifest}
