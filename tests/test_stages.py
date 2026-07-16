from core.spec import SimulationSpec
from core.user_config import UserRunConfig
from core.stages import plan_user_run_stages
from core.stages import STAGE_DEPENDENCIES, STAGE_TYPES, plan_simulation_stages
from tests.test_api_v1_service import spec_dict


def _spec(mode: str) -> SimulationSpec:
    selena = {"mode": mode, "branch": "", "artifact": "", "auto_build": True, "build_mode": "Release"}
    if mode == "branch":
        selena["branch"] = "feature/x"
    if mode == "existing":
        selena["artifact"] = "artifact-1"
        selena["auto_build"] = False
    return SimulationSpec.from_dict(spec_dict(selena=selena))


def _run_config(*, source="build") -> UserRunConfig:
    selena = {
        "source": source,
        "code_path": "D:/code/selena" if source == "build" else "",
        "selena_build_script": "D:/code/selena/build_selena.bat" if source == "build" else "",
        "package_build_script": "D:/code/selena/build_package.bat" if source == "build" else "",
        "runtime_xml": "D:/cfg/Runtime.xml",
    }
    if source == "existing":
        selena.update({"existing_path": "D:/existing/Selena"})
    return UserRunConfig.from_dict(
        {
            "selena": selena,
            "data": {"path": "//share/data"},
            "simulation": {
                "target": "cluster",
                "adapter_file": "D:/cfg/adapter.txt",
                "mat_filter": "D:/cfg/signals.filter",
            },
        }
    )


def test_project_free_run_config_starts_with_internal_recognition():
    plan = plan_user_run_stages(_run_config())
    resolve = plan.stages[0]
    assert resolve.stage_type == "resolve_spec"
    assert resolve.required_capabilities == ("source.workspace.recognize",)
    assert plan.resolved_spec["status"] == "pending_recognition"
    assert "project" not in plan.resolved_spec
    assert all("D:/" not in str(item) for item in plan.resolved_spec["environment_plan"]["requirements"])


def test_named_expected_branch_still_skips_source_checkout():
    raw = _run_config().to_dict()
    raw["selena"]["branch"] = "feature/expected"
    plan = plan_user_run_stages(UserRunConfig.from_dict(raw))
    source = next(item for item in plan.stages if item.stage_type == "prepare_source")
    assert source.initial_status == "skipped"
    assert source.skip_reason == "current_workspace_selected"


def test_existing_folder_skips_source_build_but_keeps_internal_registration():
    plan = plan_user_run_stages(_run_config(source="existing"))
    statuses = {item.stage_type: item.initial_status for item in plan.stages}
    assert statuses["prepare_source"] == "skipped"
    assert statuses["build_selena"] == "skipped"
    assert statuses["register_artifact"] == "queued"


def test_all_selena_modes_create_same_fixed_ten_stage_dag():
    for mode in ["auto", "current_workspace", "branch", "existing"]:
        plan = plan_simulation_stages(_spec(mode))
        assert [stage.stage_type for stage in plan.stages] == list(STAGE_TYPES)
        assert {stage.stage_type: stage.dependencies for stage in plan.stages} == STAGE_DEPENDENCIES
        assert plan.resolved_spec["status"] == "pending"
        assert plan.resolved_spec["source_spec_hash"] == _spec(mode).fingerprint()
        assert plan.resolved_spec["decisions"] == {}


def test_existing_selena_keeps_source_and_build_as_visible_skipped_stages():
    plan = plan_simulation_stages(_spec("existing"))
    by_type = {stage.stage_type: stage for stage in plan.stages}

    assert by_type["prepare_source"].initial_status == "skipped"
    assert by_type["build_selena"].initial_status == "skipped"
    assert by_type["prepare_source"].skip_reason
    assert by_type["build_selena"].skip_reason
    assert by_type["register_artifact"].dependencies == ("build_selena",)
    assert by_type["preflight"].dependencies == (
        "environment_check",
        "register_artifact",
        "prepare_data",
    )


def test_preflight_keeps_environment_snapshot_as_an_explicit_gate():
    assert "environment_check" in STAGE_DEPENDENCIES["preflight"]

    for mode in ["auto", "current_workspace", "branch"]:
        statuses = {stage.stage_type: stage.initial_status for stage in plan_simulation_stages(_spec(mode)).stages}
        assert statuses["prepare_source"] == "queued"
        assert statuses["build_selena"] == "queued"
