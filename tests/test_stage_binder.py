from __future__ import annotations

import pytest

from core.agent_policy import DEFAULT_LIGHT_CAPABILITIES
from core.control_service import ControlService, INTERNAL_V1_SCHEDULER_AGENT_ID
from core.environment_snapshot import EnvironmentCheckResult, EnvironmentSnapshot
from core.stage_binder import StageBindingError, bind_current_workspace_build, bind_register_artifact


BINDING_ID = "workspace:sha256:" + "b" * 24


def _snapshot(agent_id="agent-a", *, expires_at=400):
    return EnvironmentSnapshot(
        agent_id=agent_id,
        node_kind="windows_agent",
        project="ovrs25",
        workspace_binding_id=BINDING_ID,
        scope="selena_build",
        checks=(
            EnvironmentCheckResult("workspace_binding", "source.workspace.read", "passed"),
            EnvironmentCheckResult("selena_build_toolchain", "build.selena", "passed"),
            EnvironmentCheckResult("artifact_local_staging", "artifact.validate", "passed"),
        ),
        created_at=100,
        expires_at=expires_at,
        workspace={
            "branch": "feature/local-change",
            "commit": "1" * 40,
            "dirty": True,
            "sha256": "2" * 64,
        },
    ).to_dict()


def _job(service: ControlService):
    spec = {
        "schema_version": "1.0",
        "project": "ovrs25",
        "data": {"path": "shared://measurements/a"},
        "selena": {"mode": "current_workspace", "auto_build": True, "build_mode": "Release"},
        "simulation": {"target": "cluster", "profile": "default"},
    }
    return service.create_job(
        "simulation.v1",
        owner="alice",
        spec=spec,
        tasks=[
            {"task_type": "resolve_spec", "stage_type": "resolve_spec", "status": "skipped"},
            {
                "task_type": "environment_check",
                "stage_type": "environment_check",
                "dependencies": ["resolve_spec"],
                "assigned_agent_id": "agent-a",
                "payload": {
                    "project": "ovrs25",
                    "workspace_binding_id": BINDING_ID,
                    "build_mode": "Release",
                    "selena_build_script_ref": "selena/build.bat",
                    "package_build_script_ref": "build/package.bat",
                },
            },
            {
                "task_type": "prepare_source",
                "stage_type": "prepare_source",
                "dependencies": ["environment_check"],
                "status": "skipped",
            },
            {
                "task_type": "build_selena",
                "stage_type": "build_selena",
                "dependencies": ["prepare_source"],
                "assigned_agent_id": INTERNAL_V1_SCHEDULER_AGENT_ID,
            },
            {
                "task_type": "register_artifact",
                "stage_type": "register_artifact",
                "dependencies": ["build_selena"],
                "assigned_agent_id": INTERNAL_V1_SCHEDULER_AGENT_ID,
            },
        ],
    )


def _service(tmp_path):
    service = ControlService(tmp_path / "control.db", now_fn=lambda: 100)
    for agent_id in ("agent-a", "agent-b"):
        service.register_agent(
            agent_id,
            agent_id=agent_id,
            capabilities=list(DEFAULT_LIGHT_CAPABILITIES),
            metadata={"node_kind": "windows_agent", "windows_mode": "light"},
        )
    return service


def test_ready_environment_attempt_binds_build_to_same_required_agent(tmp_path):
    service = _service(tmp_path)
    job = _job(service)
    environment = service.claim_next_task("agent-a")
    service.submit_task_result(
        environment["stage_id"],
        agent_id="agent-a",
        status="succeeded",
        returncode=0,
        result={"environment_snapshot": _snapshot()},
    )

    bound = bind_current_workspace_build(service, job["job_id"], environment["stage_id"], now_fn=lambda: 200)

    assert bound["assigned_agent_id"] == "agent-a"
    assert bound["required_agent_id"] == "agent-a"
    assert bound["payload"]["workspace_binding_id"] == BINDING_ID
    assert bound["payload"]["environment_snapshot_ref"] == f"{environment['stage_id']}:1"
    assert bound["payload"]["selena_build_script_ref"] == "selena/build.bat"
    assert bound["payload"]["package_build_script_ref"] == "build/package.bat"
    assert service.claim_next_task("agent-b") is None
    assert service.claim_next_task("agent-a")["stage_type"] == "build_selena"


def test_expired_snapshot_does_not_release_sentinel_stage(tmp_path):
    service = _service(tmp_path)
    job = _job(service)
    environment = service.claim_next_task("agent-a")
    service.submit_task_result(
        environment["stage_id"],
        agent_id="agent-a",
        status="succeeded",
        returncode=0,
        result={"environment_snapshot": _snapshot(expires_at=150)},
    )

    with pytest.raises(StageBindingError, match="expired"):
        bind_current_workspace_build(service, job["job_id"], environment["stage_id"], now_fn=lambda: 200)
    build = {item["stage_type"]: item for item in service.get_job(job["job_id"])["stages"]}["build_selena"]
    assert build["assigned_agent_id"] == INTERNAL_V1_SCHEDULER_AGENT_ID
    assert build["required_agent_id"] == ""


def test_binding_is_compare_and_swap_and_cannot_be_stolen(tmp_path):
    service = _service(tmp_path)
    job = _job(service)
    environment = service.claim_next_task("agent-a")
    service.submit_task_result(
        environment["stage_id"],
        agent_id="agent-a",
        status="succeeded",
        returncode=0,
        result={"environment_snapshot": _snapshot()},
    )
    bind_current_workspace_build(service, job["job_id"], environment["stage_id"], now_fn=lambda: 200)

    with pytest.raises(StageBindingError, match="assignment changed"):
        bind_current_workspace_build(service, job["job_id"], environment["stage_id"], now_fn=lambda: 200)


def test_successful_build_binds_register_to_same_agent_and_exact_attempt(tmp_path):
    service = _service(tmp_path)
    job = _job(service)
    environment = service.claim_next_task("agent-a")
    service.submit_task_result(
        environment["stage_id"],
        agent_id="agent-a",
        status="succeeded",
        returncode=0,
        result={"environment_snapshot": _snapshot()},
    )
    bind_current_workspace_build(service, job["job_id"], environment["stage_id"], now_fn=lambda: 200)
    build = service.claim_next_task("agent-a")
    service.submit_task_result(
        build["stage_id"],
        agent_id="agent-a",
        status="succeeded",
        returncode=0,
        result={
            "workspace_binding_id": BINDING_ID,
            "artifact_lease_ref": "artifact-lease:sha256:" + "d" * 64,
            "artifact": {"logical_path": "selena.exe", "checksum": "sha256:" + "e" * 64, "size": 1},
        },
    )

    register = bind_register_artifact(service, job["job_id"], build["stage_id"])
    assert register["assigned_agent_id"] == "agent-a"
    assert register["required_agent_id"] == "agent-a"
    assert register["payload"]["build_evidence_ref"] == f"{build['stage_id']}:1"
    assert register["payload"]["artifact_lease_ref"].startswith("artifact-lease:sha256:")
    assert service.claim_next_task("agent-b") is None
    assert service.claim_next_task("agent-a")["stage_type"] == "register_artifact"
