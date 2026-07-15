from pathlib import Path

from core.agent_data_bindings import make_data_binding_id
from core.control_service import ControlService, INTERNAL_V1_SCHEDULER_AGENT_ID
from core.stage_binder import complete_data_resolution


def _job(service: ControlService, data_path: str):
    return service.create_job(
        "simulation.v1",
        owner="alice",
        assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
        tasks=[
            {"task_type": "resolve_spec", "stage_type": "resolve_spec", "status": "skipped"},
            {
                "task_type": "environment_check",
                "stage_type": "environment_check",
                "dependencies": ["resolve_spec"],
                "status": "skipped",
            },
            {
                "task_type": "prepare_data",
                "stage_type": "prepare_data",
                "dependencies": ["environment_check"],
                "payload": {
                    "dispatch_scope": "data_upload",
                    "project": "ovrs25",
                    "data_path": data_path,
                },
            },
        ],
    )


def test_pending_data_stage_binds_to_agent_advertising_ancestor_root(tmp_path: Path):
    root = tmp_path / "measurements"
    case = root / "case"
    case.mkdir(parents=True)
    service = ControlService(tmp_path / "control.db")
    binding_id = make_data_binding_id("ovrs25", str(root))
    service.register_agent(
        "light",
        agent_id="agent-1",
        node_kind="windows_agent",
        capabilities=["data.local.read", "data.upload"],
        metadata={
            "node_kind": "windows_agent",
            "data_bindings": [{"id": binding_id, "project": "ovrs25", "healthy": True}],
        },
    )
    job = _job(service, str(case))

    bound = service.bind_pending_data_stage("agent-1")
    assert bound["required_agent_id"] == "agent-1"
    assert bound["payload"]["data_binding_id"] == binding_id
    claimed = service.claim_next_task("agent-1")
    assert claimed["stage_type"] == "prepare_data"
    assert claimed["job_id"] == job["job_id"]


def test_pending_data_stage_does_not_leak_to_unmatched_agent(tmp_path: Path):
    case = tmp_path / "measurements" / "case"
    case.mkdir(parents=True)
    service = ControlService(tmp_path / "control.db")
    service.register_agent(
        "light",
        agent_id="agent-1",
        node_kind="windows_agent",
        capabilities=["data.local.read", "data.upload"],
        metadata={
            "node_kind": "windows_agent",
            "data_bindings": [
                {
                    "id": make_data_binding_id("ovrs25", str(tmp_path / "other")),
                    "project": "ovrs25",
                    "healthy": True,
                }
            ],
        },
    )
    _job(service, str(case))
    assert service.bind_pending_data_stage("agent-1") is None
    assert service.claim_next_task("agent-1") is None


def test_successful_agent_data_upload_updates_path_free_resolved_spec(tmp_path: Path):
    service = ControlService(tmp_path / "control.db")
    service.register_agent(
        "light",
        agent_id="agent-1",
        node_kind="windows_agent",
        capabilities=["data.local.read", "data.upload"],
        metadata={"node_kind": "windows_agent"},
    )
    job = service.create_job(
        "simulation.v1",
        owner="alice",
        assigned_agent_id="agent-1",
        spec={"project": "ovrs25", "data": {"path": "D:/data"}},
        resolved_spec={"status": "pending_node", "decisions": {}},
        tasks=[
            {"task_type": "prepare_data", "stage_type": "prepare_data", "required_agent_id": "agent-1"},
            {"task_type": "preflight", "stage_type": "preflight", "dependencies": ["prepare_data"]},
        ],
    )
    stage = service.claim_next_task("agent-1")
    attempt = stage["attempt_count"]
    dataset = {
        "id": "dataset:sha256:" + "a" * 64,
        "project": "ovrs25",
        "owner": "alice",
        "source_kind": "agent_upload",
        "accessibility": "cluster",
        "storage_ref": "shared://datasets/ovrs25/opaque",
        "files": [{"relative_path": "a.MF4", "size": 1, "checksum": "sha256:" + "b" * 64}],
    }
    completed = service.submit_task_result(
        stage["stage_id"],
        agent_id="agent-1",
        status="succeeded",
        returncode=0,
        result={"dataset": dataset, "evidence_ref": f"{stage['stage_id']}:{attempt}"},
    )
    complete_data_resolution(service, job["job_id"], stage["stage_id"])
    resolved = service.get_job(job["job_id"])["resolved_spec"]
    assert resolved["decisions"]["data"]["code"] == "agent_dataset_uploaded"
    assert resolved["decisions"]["data"]["dataset"]["id"] == dataset["id"]
    assert "D:/data" not in str(resolved["decisions"]["data"])
    assert next(item for item in completed["stages"] if item["stage_type"] == "prepare_data")["status"] == "succeeded"
