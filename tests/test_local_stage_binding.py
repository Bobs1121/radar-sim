from core.agent_policy import DEFAULT_FULL_CAPABILITIES
from core.control_service import ControlService, INTERNAL_V1_SCHEDULER_AGENT_ID
from core.stage_binder import advance_after_stage_result


def _completed(job, stage_type):
    return next(item for item in job["stages"] if item["stage_type"] == stage_type)


def test_local_four_stage_chain_stays_on_same_windows_full_agent(tmp_path):
    control = ControlService(tmp_path / "control.db")
    control.register_agent(
        "full",
        agent_id="full-1",
        capabilities=list(DEFAULT_FULL_CAPABILITIES),
        metadata={"node_kind": "windows_full", "windows_mode": "full"},
    )
    spec = {
        "schema_version": "2.0",
        "selena": {"source": "build"},
        "data": {"path": "D:/data", "limit": 0},
        "simulation": {
            "target": "local", "adapter_file": "D:/assets/a.txt",
            "mat_filter": "D:/assets/m.filter", "timeout_minutes": 1,
        },
        "result": {"retain_days": 7},
    }
    tasks = []
    dependencies = {
        "resolve_spec": [], "environment_check": ["resolve_spec"],
        "prepare_source": ["environment_check"], "prepare_data": ["environment_check"],
        "build_selena": ["prepare_source"], "register_artifact": ["build_selena"],
        "preflight": ["environment_check", "register_artifact", "prepare_data"],
        "run_simulation": ["preflight"], "collect_results": ["run_simulation"],
        "finalize_manifest": ["collect_results"],
    }
    for stage_type in dependencies:
        task = {
            "task_type": stage_type, "stage_type": stage_type,
            "dependencies": dependencies[stage_type],
            "assigned_agent_id": INTERNAL_V1_SCHEDULER_AGENT_ID,
        }
        if stage_type in {"resolve_spec", "environment_check", "prepare_source", "build_selena"}:
            task["status"] = "skipped"
        if stage_type == "register_artifact":
            task["assigned_agent_id"] = "full-1"
            task["required_agent_id"] = "full-1"
            task["payload"] = {
                "project": "demo",
                "runtime_bundle_lease_ref": "runtime-bundle-lease:sha256:" + "1" * 64,
            }
        if stage_type == "prepare_data":
            task["assigned_agent_id"] = "full-1"
            task["required_agent_id"] = "full-1"
            task["payload"] = {"project": "demo", "dispatch_scope": "local_data"}
        tasks.append(task)
    job = control.create_job(
        "simulation.run_config.v2",
        owner="alice",
        spec=spec,
        resolved_spec={"source_config_hash": "sha256:" + "9" * 64, "decisions": {}},
        metadata={"owner": "alice", "contract": "user-run-config/2.0"},
        tasks=tasks,
    )

    register = next(item for item in job["stages"] if item["stage_type"] == "register_artifact")
    registered_job = control.submit_task_result(
        register["stage_id"], agent_id="full-1", status="succeeded", returncode=0,
        result={
            "runtime_bundle": {
                "id": "selena-bundle:sha256:" + "2" * 64,
                "storage_ref": "shared://selena-bundles/demo/runtime-bundle.zip",
            },
            "build_evidence_ref": "build:1",
        },
    )
    assert advance_after_stage_result(control, _completed(registered_job, "register_artifact")) is None

    data = _completed(control.get_job(job["job_id"]), "prepare_data")
    data_job = control.submit_task_result(
        data["stage_id"], agent_id="full-1", status="succeeded", returncode=0,
        result={
            "dataset": {"id": "dataset:sha256:" + "3" * 64, "source_kind": "agent_local"},
            "data_lease_ref": "data-lease:sha256:" + "4" * 32,
            "evidence_ref": f"{data['stage_id']}:1",
        },
    )
    preflight = advance_after_stage_result(control, _completed(data_job, "prepare_data"))
    assert preflight["required_agent_id"] == "full-1"
    assert preflight["payload"]["runtime_bundle_lease_ref"].startswith("runtime-bundle-lease:")
    assert preflight["payload"]["data_lease_ref"].startswith("data-lease:")
    assert preflight["payload"]["owner"] == "alice"

    for current, successor in (
        ("preflight", "run_simulation"),
        ("run_simulation", "collect_results"),
    ):
        stage = _completed(control.get_job(job["job_id"]), current)
        completed = control.submit_task_result(
            stage["stage_id"], agent_id="full-1", status="succeeded", returncode=0,
            result={"local_run_lease_ref": "local-run-lease:sha256:" + "5" * 64},
        )
        bound = advance_after_stage_result(control, _completed(completed, current))
        assert bound["stage_type"] == successor
        assert bound["required_agent_id"] == "full-1"

    collect = _completed(control.get_job(job["job_id"]), "collect_results")
    collected = control.submit_task_result(
        collect["stage_id"], agent_id="full-1", status="succeeded", returncode=0,
        result={
            "local_run_lease_ref": "local-run-lease:sha256:" + "5" * 64,
            "result_ref": "result:sha256:" + "6" * 64,
        },
    )
    finalize = advance_after_stage_result(control, _completed(collected, "collect_results"))
    assert finalize["stage_type"] == "finalize_manifest"
    assert finalize["required_agent_id"] == "full-1"
    assert finalize["payload"]["runtime_bundle_id"].startswith("selena-bundle:")
    assert finalize["payload"]["dataset_id"].startswith("dataset:")
    assert finalize["payload"]["result_ref"].startswith("result:")
