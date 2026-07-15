from core.agent_asset_bindings import make_asset_binding_id
from core.agent_bindings import make_workspace_binding_id, make_workspace_path_id
from core.agent_policy import DEFAULT_LIGHT_CAPABILITIES
from core.api_v1 import ApiV1Service
from core.control_service import ControlService
from core.environment_snapshot import EnvironmentCheckResult, EnvironmentSnapshot
from core.stage_binder import advance_after_stage_result
from tests.test_api_v1_service import run_config_dict


def test_branch_job_handoffs_environment_to_source_lease_then_build(tmp_path):
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    code_path = "D:/workspace/byd"
    binding_id = make_workspace_binding_id("internal-demo", code_path)
    asset_id = make_asset_binding_id("D:/data")
    control.register_agent(
        "light", agent_id="agent-1", capabilities=list(DEFAULT_LIGHT_CAPABILITIES),
        metadata={
            "node_kind": "windows_agent",
            "workspace_bindings": [{
                "id": binding_id, "path_id": make_workspace_path_id(code_path),
                "project": "internal-demo", "healthy": True,
            }],
            "asset_bindings": [{"id": asset_id, "healthy": True}],
        },
    )
    config = run_config_dict()
    config["selena"]["branch"] = "feature/demo"
    job = api.submit_user_run("alice", config_payload=config)
    control.bind_pending_run_config_resolution("agent-1")
    resolve = control.claim_next_task("agent-1")
    resolved = control.submit_task_result(
        resolve["stage_id"], agent_id="agent-1", status="succeeded", returncode=0,
        result={"recognition": {
            "status": "resolved", "adapter_key": "recipe:demo", "internal_project": "internal-demo",
            "workspace_binding_id": binding_id, "confidence": 1.0, "evidence": ["exact"],
            "asset_bindings": {"runtime_xml": asset_id},
        }},
    )
    resolve_stage = next(stage for stage in resolved["stages"] if stage["stage_type"] == "resolve_spec")
    advance_after_stage_result(control, resolve_stage)
    environment = control.claim_next_task("agent-1")
    snapshot = EnvironmentSnapshot(
        agent_id="agent-1", node_kind="windows_agent", project="internal-demo",
        workspace_binding_id=binding_id, scope="selena_build",
        checks=(
            EnvironmentCheckResult("workspace_binding", "source.workspace.read", "passed"),
            EnvironmentCheckResult("selena_build_toolchain", "build.selena", "passed"),
            EnvironmentCheckResult("artifact_local_staging", "artifact.validate", "passed"),
        ),
        created_at=1, expires_at=10_000_000_000,
        workspace={"branch": "dirty-main", "commit": "a" * 40, "dirty": True, "sha256": "b" * 64},
    ).to_dict()
    env_done = control.submit_task_result(
        environment["stage_id"], agent_id="agent-1", status="succeeded", returncode=0,
        result={"environment_snapshot": snapshot},
    )
    env_stage = next(stage for stage in env_done["stages"] if stage["stage_type"] == "environment_check")
    source_bound = advance_after_stage_result(control, env_stage)
    assert source_bound["stage_type"] == "prepare_source"
    assert source_bound["required_agent_id"] == "agent-1"
    assert source_bound["payload"]["branch"] == "feature/demo"

    source = control.claim_next_task("agent-1")
    source_done = control.submit_task_result(
        source["stage_id"], agent_id="agent-1", status="succeeded", returncode=0,
        result={"source_lease": {
            "lease_id": "source-lease:sha256:" + "c" * 64,
            "source_evidence_ref": f"{source['stage_id']}:1",
            "project": "internal-demo", "workspace_binding_id": binding_id,
            "source_kind": "branch_worktree", "branch": "feature/demo", "commit": "d" * 40,
        }},
    )
    source_stage = next(stage for stage in source_done["stages"] if stage["stage_type"] == "prepare_source")
    build = advance_after_stage_result(control, source_stage)
    assert build["stage_type"] == "build_selena"
    assert build["required_agent_id"] == "agent-1"
    assert build["payload"]["source_lease_ref"].startswith("source-lease:sha256:")
    assert build["payload"]["commit"] == "d" * 40
    public = api.get_job("alice", job["id"])
    assert public["resolved_spec"]["decisions"]["selena"]["action"] == "build_isolated_branch"
    assert "source-lease" not in str(public)
