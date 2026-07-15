from core.agent_bindings import make_workspace_binding_id, make_workspace_path_id
from core.agent_asset_bindings import make_asset_binding_id
from core.agent_policy import DEFAULT_LIGHT_CAPABILITIES
from core.api_v1 import ApiV1Service
from core.control_service import ControlService
from core.environment_snapshot import EnvironmentCheckResult, EnvironmentSnapshot
from core.stage_binder import advance_after_stage_result
from tests.test_api_v1_service import run_config_dict


def test_project_free_job_binds_only_matching_workspace_agent_and_hides_internal_identity(tmp_path):
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(control_service_factory=lambda owner: control)
    code_path = "D:/workspace/byd"
    binding_id = make_workspace_binding_id("bydod25", code_path)
    control.register_agent(
        "light",
        agent_id="light-1",
        capabilities=list(DEFAULT_LIGHT_CAPABILITIES),
        metadata={
            "node_kind": "windows_agent",
            "workspace_bindings": [
                {
                    "id": binding_id,
                    "path_id": make_workspace_path_id(code_path),
                    "project": "bydod25",
                    "healthy": True,
                }
            ],
            "asset_bindings": [{"id": make_asset_binding_id("D:/data"), "healthy": True}],
        },
    )
    job = api.submit_user_run("alice", config_payload=run_config_dict())

    bound = control.bind_pending_run_config_resolution("light-1")
    assert bound is not None
    assert bound["stage_type"] == "resolve_spec"
    claimed = control.claim_next_task("light-1")
    completed = control.submit_task_result(
        claimed["stage_id"],
        agent_id="light-1",
        status="succeeded",
        returncode=0,
        result={
            "recognition": {
                "status": "resolved",
                "adapter_key": "recipe:g3n_fvg3_od25",
                "internal_project": "bydod25",
                "workspace_binding_id": binding_id,
                "confidence": 0.9,
                "evidence": ["workspace_exact_match"],
                "asset_bindings": {
                    "runtime_xml": make_asset_binding_id("D:/data"),
                },
            }
        },
    )
    completed_stage = next(item for item in completed["stages"] if item["stage_type"] == "resolve_spec")
    handoff = advance_after_stage_result(control, completed_stage)
    assert handoff["stage_type"] == "environment_check"
    assert handoff["required_agent_id"] == "light-1"
    assert handoff["payload"]["project"] == "bydod25"
    assert handoff["payload"]["asset_bindings"]["runtime_xml"].startswith("asset-root:sha256:")

    environment = control.claim_next_task("light-1")
    assert environment["stage_type"] == "environment_check"
    snapshot = EnvironmentSnapshot(
        agent_id="light-1",
        node_kind="windows_agent",
        project="bydod25",
        workspace_binding_id=binding_id,
        scope="selena_build",
        checks=(
            EnvironmentCheckResult("workspace_binding", "source.workspace.read", "passed"),
            EnvironmentCheckResult("selena_build_toolchain", "build.selena", "passed"),
            EnvironmentCheckResult("artifact_local_staging", "artifact.validate", "passed"),
        ),
        created_at=100,
        expires_at=10_000_000_000,
        workspace={"branch": "feature/dirty", "commit": "1" * 40, "dirty": True, "sha256": "2" * 64},
    ).to_dict()
    env_completed = control.submit_task_result(
        environment["stage_id"],
        agent_id="light-1",
        status="succeeded",
        returncode=0,
        result={"environment_snapshot": snapshot},
    )
    env_stage = next(item for item in env_completed["stages"] if item["stage_type"] == "environment_check")
    build = advance_after_stage_result(control, env_stage)
    assert build["stage_type"] == "build_selena"
    assert build["required_agent_id"] == "light-1"

    public = api.get_job("alice", job["id"])
    serialized = str(public)
    assert "recipe:g3n_fvg3_od25" not in serialized
    assert "internal_project" not in serialized
    assert "assigned_agent_id" not in serialized
    assert public["resolved_spec"]["decisions"]["recognition"]["status"] == "resolved"


def test_nonmatching_agent_cannot_receive_run_config_path(tmp_path):
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(control_service_factory=lambda owner: control)
    control.register_agent(
        "other",
        agent_id="other-1",
        capabilities=list(DEFAULT_LIGHT_CAPABILITIES),
        metadata={
            "node_kind": "windows_agent",
            "workspace_bindings": [
                {
                    "id": make_workspace_binding_id("other", "D:/other"),
                    "path_id": make_workspace_path_id("D:/other"),
                    "project": "other",
                    "healthy": True,
                }
            ],
            "asset_bindings": [{"id": make_asset_binding_id("D:/data"), "healthy": True}],
        },
    )
    api.submit_user_run("alice", config_payload=run_config_dict())
    assert control.bind_pending_run_config_resolution("other-1") is None
    assert control.claim_next_task("other-1") is None


def test_one_click_agent_can_receive_first_run_for_local_auto_configuration(tmp_path):
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    control.register_agent(
        "fresh-light",
        agent_id="fresh-light-1",
        capabilities=list(DEFAULT_LIGHT_CAPABILITIES),
        metadata={
            "node_kind": "windows_agent",
            "windows_mode": "light",
            "auto_configure": True,
            "workspace_bindings": [],
            "asset_bindings": [],
            "data_bindings": [],
        },
    )
    api.submit_user_run("alice", config_payload=run_config_dict())

    bound = control.bind_pending_run_config_resolution("fresh-light-1")

    assert bound is not None
    assert bound["payload"]["auto_configure"] is True
    assert bound["payload"]["selena_build_script"].endswith("build_selena.bat")
    assert bound["payload"]["package_build_script"].endswith("build_package.bat")
    assert bound["payload"]["data_path"] == "//shared/data"
