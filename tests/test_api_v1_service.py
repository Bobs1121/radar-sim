import ast
import concurrent.futures
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import subprocess
import sys
import threading

import pytest

from core.api_v1 import (
    ApiV1Error,
    ApiV1Service,
    SourceResolutionInputs,
    SourceResolutionProviderError,
    V1_SCHEDULER_AGENT_ID,
    iter_sse,
)
from core.artifacts import SelenaArtifact
from core.control_service import ControlService
from core.local_results import ResultCatalog
from core.selena_resolver import SourceResolutionContext
from core.spec import ProjectCatalog, ProjectProfile, UserBindings
from core.spec import SimulationSpec

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


def spec_dict(**patch):
    data = {
        "schema_version": "1.0",
        "project": "bydod25",
        "selena": {"mode": "auto", "branch": "", "artifact": "", "auto_build": True, "build_mode": "Release"},
        "data": {"path": "D:\\measurement\\CBNA_0117", "limit": 0, "required_signals": []},
        "simulation": {"target": "auto", "profile": "default", "timeout_minutes": 0},
        "result": {"name": "", "retain_days": 30},
    }
    data.update(patch)
    return data


def run_config_dict(**patch):
    data = {
        "schema_version": "2.0",
        "selena": {
            "source": "build",
            "code_path": "D:/workspace/byd",
            "branch": "",
            "selena_build_script": "D:/workspace/byd/build_selena.bat",
            "package_build_script": "D:/workspace/byd/build_package.bat",
            "runtime_xml": "D:/data/Runtime.xml",
        },
        "data": {"path": "//shared/data"},
        "simulation": {
            "target": "cluster",
            "adapter_file": "D:/data/adapter.txt",
            "mat_filter": "D:/data/signals.filter",
        },
    }
    data.update(patch)
    return data


def make_api(tmp_path):
    services: dict[str, ControlService] = {}

    def factory(owner: str) -> ControlService:
        services.setdefault(owner, ControlService(tmp_path / f"{owner}.db"))
        return services[owner]

    return ApiV1Service(control_service_factory=factory), services


def test_execution_capabilities_require_both_cluster_roles_and_hide_agent_details(tmp_path):
    control = ControlService(tmp_path / "capabilities.db", now_fn=lambda: 100)
    api = ApiV1Service(control_service_factory=lambda owner: control, now_fn=lambda: 100)
    control.register_agent(
        "light-a",
        agent_id="light-a",
        capabilities=["build.selena"],
        metadata={"node_kind": "windows_agent"},
    )
    control.register_agent(
        "linux-a",
        agent_id="linux-a",
        capabilities=["environment.cluster.check", "cluster.prepare", "result.collect"],
        metadata={"node_kind": "linux_executor"},
    )

    partial = api.execution_capabilities("alice")
    assert partial["capabilities"]["windows_light"] == {
        "available": True,
        "count": 1,
        "configured_count": 1,
        "reconnecting": False,
    }
    assert partial["capabilities"]["cluster"] == {
        "available": False,
        "count": 0,
        "linux_executor_count": 1,
        "platform_gateway_count": 0,
    }
    assert "linux-a" not in str(partial)

    control.register_agent(
        "gateway-a",
        agent_id="gateway-a",
        capabilities=["simulation.cluster", "cluster.gateway"],
        metadata={"node_kind": "platform_gateway"},
    )
    ready = api.execution_capabilities("alice")
    assert ready["capabilities"]["cluster"]["available"] is True
    assert ready["capabilities"]["cluster"]["count"] == 1


def test_execution_capabilities_reports_configured_windows_reconnecting(tmp_path):
    control = ControlService(tmp_path / "capabilities.db", now_fn=lambda: 100)
    api = ApiV1Service(control_service_factory=lambda owner: control, now_fn=lambda: 300)
    control.register_agent(
        "full-a",
        agent_id="full-a",
        capabilities=["simulation.local"],
        metadata={"node_kind": "windows_full"},
    )

    full = api.execution_capabilities("alice")["capabilities"]["windows_full"]
    assert full == {
        "available": False,
        "count": 0,
        "configured_count": 1,
        "reconnecting": True,
    }
    waiting = api.submit_user_run("alice", config_payload=run_config_dict())["waiting"]
    assert waiting["connection_state"] == "reconnecting"
    assert waiting["action"] == {
        "type": "wait_windows_reconnect",
        "label": "Wait for automatic reconnection",
        "mode": "light",
    }


def test_v1_task_center_lists_only_owner_v1_jobs_with_progress_and_filter(tmp_path):
    shared = ControlService(tmp_path / "shared.db")
    api = ApiV1Service(control_service_factory=lambda owner: shared)

    first = api.submit_job("alice", spec_payload=spec_dict(), idempotency_key="alice-1")
    second = api.submit_job("alice", spec_payload=spec_dict(), idempotency_key="alice-2")
    api.submit_job("bob", spec_payload=spec_dict(), idempotency_key="bob-1")
    shared.create_job("legacy.local", owner="alice")
    api.cancel_job("alice", first["id"])

    page = api.list_jobs("alice")
    assert page["count"] == 2
    assert {item["id"] for item in page["jobs"]} == {first["id"], second["id"]}
    assert all(0.0 <= item["progress"] <= 1.0 for item in page["jobs"])
    queued = next(item for item in page["jobs"] if item["id"] == second["id"])
    assert queued["current_stage"] == "resolve_spec"
    assert queued["available_actions"] == [
        {"type": "cancel_job", "label": "Cancel job", "job_id": second["id"]}
    ]

    cancelled = api.list_jobs("alice", status="cancelled")
    assert [item["id"] for item in cancelled["jobs"]] == [first["id"]]


def test_diagnosis_reports_pending_job_without_exposing_user_paths(tmp_path):
    api, _ = make_api(tmp_path)
    job = api.submit_job("alice", spec_payload=spec_dict())

    diagnosis = api.diagnosis("alice", job["id"])

    assert diagnosis["schema_version"] == "radar-sim.job-diagnosis/1.0"
    assert diagnosis["outcome"] == "pending"
    assert diagnosis["code"] == "job_queued"
    assert diagnosis["category"] == "none"
    assert diagnosis["terminal"] is False
    assert diagnosis["artifacts_available"] is False
    assert diagnosis["action"]["type"] == "wait_job"
    assert "D:\\measurement" not in json.dumps(diagnosis)


def test_diagnosis_keeps_failed_simulation_artifacts_downloadable(tmp_path):
    controlled = tmp_path / "runs"
    source = controlled / "failed-run"
    source.mkdir(parents=True)
    (source / "result.ini").write_text("successfull=0\n", encoding="utf-8")
    catalog = ResultCatalog(
        tmp_path / "result-store",
        tmp_path / "results.db",
        allowed_source_root=controlled,
    )
    published = catalog.publish(
        owner="alice",
        run_ref="cluster-run:failed",
        source_root=source,
        files=["result.ini"],
        retain_until=10_000_000_000,
    )
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(
        control_service_factory=lambda _owner: control,
        result_catalog=catalog,
    )
    job = control.create_job(
        "simulation.run_config.v2",
        owner="alice",
        tasks=[{"task_type": "finalize_manifest", "stage_type": "finalize_manifest"}],
    )
    control.register_agent("finalizer", agent_id="finalizer", capabilities=["*"])
    stage = control.claim_next_task("finalizer")
    control.submit_task_result(
        stage["stage_id"],
        agent_id="finalizer",
        status="succeeded",
        returncode=0,
        result={
            "manifest": {
                "schema_version": "radar-sim.run-manifest/2.0",
                "status": "failed",
                "result_ref": published.ref,
                "summary": {"errors": [str(tmp_path / "private" / "failure.log")]},
            }
        },
    )

    diagnosis = api.diagnosis("alice", job["job_id"])

    assert diagnosis["status"] == "failed"
    assert diagnosis["outcome"] == "failed"
    assert diagnosis["code"] == "simulation_failed"
    assert diagnosis["category"] == "simulation"
    assert diagnosis["artifacts_available"] is True
    assert diagnosis["result_ref"] == published.ref
    assert diagnosis["action"] == {
        "type": "download_result",
        "label": "Download result artifacts",
        "result_ref": published.ref,
    }
    assert diagnosis["consistency"] == {"state": "consistent", "warnings": []}
    assert str(tmp_path) not in json.dumps(diagnosis)


def test_diagnosis_normalizes_historical_manifest_mismatch_and_infrastructure_failure(tmp_path):
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    job = control.create_job(
        "simulation.run_config.v2",
        owner="alice",
        tasks=[{"task_type": "run_simulation", "stage_type": "run_simulation"}],
    )
    control.register_agent("gateway", agent_id="gateway", capabilities=["*"])
    stage = control.claim_next_task("gateway")
    control.submit_task_result(
        stage["stage_id"],
        agent_id="gateway",
        status="failed",
        returncode=1,
        result={
            "code": "cluster_gateway_unavailable",
            "message": str(tmp_path / "gateway-secret.log"),
        },
    )

    infrastructure = api.diagnosis("alice", job["job_id"])
    assert infrastructure["outcome"] == "failed"
    assert infrastructure["code"] == "infrastructure_failed"
    assert infrastructure["category"] == "infrastructure"
    assert infrastructure["action"]["type"] == "retry_stage"
    assert infrastructure["evidence"]["failed_stage"]["source_code"] == "cluster_gateway_unavailable"
    assert str(tmp_path) not in json.dumps(infrastructure)

    historical = control.create_job(
        "simulation.run_config.v2",
        owner="alice",
        tasks=[{"task_type": "finalize_manifest", "stage_type": "finalize_manifest"}],
    )
    final_stage = historical["stages"][0]
    failed_manifest = {
        "manifest": {
            "status": "failed",
            "result_ref": "result:sha256:" + "f" * 64,
        }
    }
    with sqlite3.connect(tmp_path / "control.db") as conn:
        conn.execute(
            "UPDATE jobs SET status='succeeded', result_json=? WHERE job_id=?",
            (json.dumps(failed_manifest), historical["job_id"]),
        )
        conn.execute(
            "UPDATE tasks SET status='succeeded' WHERE task_id=?",
            (final_stage["stage_id"],),
        )

    mismatch = api.diagnosis("alice", historical["job_id"])
    assert mismatch["status"] == "succeeded"
    assert mismatch["outcome"] == "failed"
    assert mismatch["code"] == "simulation_failed"
    assert mismatch["artifacts_available"] is False
    assert mismatch["consistency"] == {
        "state": "warning",
        "warnings": [
            "job_manifest_outcome_mismatch",
            "result_reference_unavailable",
        ],
    }


def project_catalog(project: str = "bydod25") -> ProjectCatalog:
    return ProjectCatalog(
        project=project,
        display_name=project,
        platform="gen5_selena",
        default_profile="default",
        selected_profile="default",
        default_build_mode="Release",
        profiles=(
            ProjectProfile(
                name="default",
                description="Default",
                target="cluster",
                selena_source="existing",
                required_signals=(),
                timeout_minutes=0,
            ),
        ),
        revision="revision-a",
    )


def user_bindings(project: str = "bydod25", *, workspace: bool = False) -> UserBindings:
    return UserBindings(
        project=project,
        workspace_path="D:/workspace/bydod25" if workspace else "",
        selena_build_script="D:/workspace/bydod25/build_selena.bat" if workspace else "",
        environment_build_script="",
        existing_selena=(),
    )


def artifact(project: str = "bydod25", owner: str = "alice", **patch) -> SelenaArtifact:
    data = {
        "id": "artifact-cluster",
        "project": project,
        "owner": owner,
        "visibility": "shared",
        "branch": "main",
        "commit": "1" * 40,
        "source_kind": "branch",
        "dirty": False,
        "dirty_fingerprint": "",
        "source_changed_during_build": False,
        "build_mode": "Release",
        "toolchain_fingerprint": "toolchain:v1",
        "binary_checksum": SHA_A,
        "interface_manifest": {},
        "signal_manifest": {},
        "storage_ref": "artifact://bydod25/cluster",
        "accessibility": "cluster",
        "health": "ready",
        "created_by": "builder",
        "created_at": 100.0,
        "retain_until": 1000.0,
    }
    data.update(patch)
    return SelenaArtifact(**data)


def source_inputs(
    *,
    owner: str = "alice",
    project: str = "bydod25",
    artifacts: tuple[SelenaArtifact, ...] = (),
    workspace_binding_id: str = "",
) -> SourceResolutionInputs:
    catalog = project_catalog(project)
    return SourceResolutionInputs(
        project_catalog=catalog,
        user_bindings=user_bindings(project, workspace=bool(workspace_binding_id)),
        context=SourceResolutionContext(
            project_revision=catalog.revision,
            owner=owner,
            evaluated_at=100.0,
            workspace_binding_id=workspace_binding_id,
            workspace_project=project if workspace_binding_id else "",
            workspace_fingerprint=None,
            branch_commits={},
            artifacts=artifacts,
        ),
    )


def test_validate_returns_canonical_spec_and_fingerprint(tmp_path):
    api, _ = make_api(tmp_path)
    result = api.validate(spec_dict())
    spec = SimulationSpec.from_dict(result["spec"])

    assert result["valid"] is True
    assert spec.data.path == "D:/measurement/CBNA_0117"
    assert result["fingerprint"] == spec.fingerprint()


def test_project_free_run_config_validate_and_submit_waits_for_node_recognition(tmp_path):
    api, _ = make_api(tmp_path)
    validation = api.validate_user_run_config(run_config_dict())
    assert validation["valid"] is True
    assert "project" not in validation["config"]
    assert validation["environment_plan"]["status"] == "planned"
    assert len(validation["execution_plan"]) == 10
    assert validation["execution"]["selected_target"] in {"local", "cluster"}

    job = api.submit_user_run(
        "alice",
        config_payload=run_config_dict(),
        idempotency_key="run-v2-1",
    )
    assert job["type"] == "simulation.run_config.v2"
    assert job["spec_hash"] == validation["fingerprint"]
    assert job["resolved_spec"]["status"] == "pending_recognition"
    assert job["stages"][0]["stage_type"] == "resolve_spec"
    assert "project" not in job["spec"]
    assert api.list_jobs("alice")["count"] == 1


def test_run_config_job_reports_path_free_windows_connection_wait(tmp_path):
    api, services = make_api(tmp_path)
    job = api.submit_user_run("alice", config_payload=run_config_dict())

    assert job["waiting"] == {
        "reason": "windows_connection_required",
        "mode": "light",
        "stage": "resolve_spec",
        "missing_capability": "windows_light",
        "connection_state": "not_configured",
        "message": "This task is waiting for a connected Windows computer with build capability.",
        "action": {
            "type": "connect_windows",
            "label": "Connect this Windows computer",
            "mode": "light",
        },
    }
    assert "D:/" not in json.dumps(job["waiting"])
    assert "agent_id" not in json.dumps(job["waiting"])

    services["alice"].register_agent(
        "light",
        agent_id="light-1",
        capabilities=["build.selena"],
        metadata={"node_kind": "windows_agent"},
    )
    assert api.get_job("alice", job["id"])["waiting"] is None


def test_local_target_requires_full_connection_and_shared_cluster_does_not(tmp_path):
    api, services = make_api(tmp_path)
    services.setdefault("alice", ControlService(tmp_path / "alice.db")).register_agent(
        "light",
        agent_id="light-1",
        capabilities=["build.selena"],
        metadata={"node_kind": "windows_agent"},
    )
    local_config = run_config_dict()
    local_config["simulation"] = {**local_config["simulation"], "target": "local"}
    local = api.submit_user_run("alice", config_payload=local_config)
    assert local["waiting"]["mode"] == "full"
    assert local["waiting"]["missing_capability"] == "windows_full"

    shared_config = run_config_dict()
    shared_config["selena"] = {
        "source": "existing",
        "existing_path": "//shared/selena",
        "runtime_xml": "//shared/runtime/Runtime.xml",
    }
    shared_config["data"] = {"path": "//shared/data"}
    shared_config["simulation"] = {
        "target": "cluster",
        "adapter_file": "",
        "mat_filter": "//shared/config/signals.filter",
    }
    shared = api.submit_user_run("alice", config_payload=shared_config)
    assert shared["waiting"] is None


def test_project_free_dry_run_is_plan_only_and_claims_no_stage(tmp_path):
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    job = api.submit_user_run("alice", config_payload=run_config_dict(), dry_run=True)
    assert job["type"] == "simulation.run_config.v2.dry_run"
    assert job["status"] == "succeeded"
    assert all(stage["status"] == "skipped" for stage in job["stages"])
    assert all(stage["skip_reason"] == "dry_run_plan_only" for stage in job["stages"])
    assert control.list_jobs(owner="alice")[0]["status"] == "succeeded"


def test_auto_prefers_full_windows_for_local_simulation(tmp_path):
    control = ControlService(tmp_path / "control.db")
    control.register_agent(
        "full", agent_id="full-1", capabilities=["simulation.local"],
        metadata={"node_kind": "windows_full"},
    )
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    config = run_config_dict()
    config["simulation"]["target"] = "auto"
    config["data"] = {"path": "D:/measurements/local"}

    job = api.submit_user_run(
        "alice",
        config_payload=config,
    )

    execution = job["resolved_spec"]["decisions"]["execution"]
    assert execution == {
        "status": "selected",
        "requested_target": "auto",
        "selected_target": "local",
        "reason": "windows_full_available",
    }
    stages = {item["stage_type"]: item for item in control.get_job(job["id"])["stages"]}
    assert stages["preflight"]["required_agent_id"] == ""
    assert stages["run_simulation"]["required_agent_id"] == ""


def test_auto_keeps_uploaded_data_on_cluster_even_when_full_windows_is_online(tmp_path):
    control = ControlService(tmp_path / "control.db")
    control.register_agent(
        "full", agent_id="full-1", capabilities=["simulation.local"],
        metadata={"node_kind": "windows_full"},
    )
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    config = run_config_dict()
    config["simulation"]["target"] = "auto"
    config["data"] = {"path": "dataset://sha256/" + "c" * 64}

    job = api.submit_user_run(
        "alice",
        config_payload=config,
    )

    assert job["resolved_spec"]["decisions"]["execution"] == {
        "status": "selected",
        "requested_target": "auto",
        "selected_target": "cluster",
        "reason": "cluster_accessible_data",
    }


def test_build_cluster_shared_data_is_owned_by_linux_before_bundle_exists(tmp_path):
    from core.cluster_stage_executor import LINUX_STAGE_AGENT_ID

    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)

    job = api.submit_user_run("alice", config_payload=run_config_dict())

    prepare_data = next(
        stage
        for stage in control.get_job(job["id"])["stages"]
        if stage["stage_type"] == "prepare_data"
    )
    assert prepare_data["required_agent_id"] == LINUX_STAGE_AGENT_ID
    assert prepare_data["assigned_agent_id"] == LINUX_STAGE_AGENT_ID


def test_existing_bundle_cluster_route_is_assigned_without_windows(tmp_path):
    control = ControlService(tmp_path / "control.db")
    bundle_id = "selena-bundle:sha256:" + "a" * 64
    record = SimpleNamespace(
        internal_project="bydod25",
        public_dict={
            "id": bundle_id,
            "storage_ref": "shared://selena-bundles/bydod25/runtime-bundle.zip",
            "archive_checksum": "sha256:" + "b" * 64,
            "archive_size": 10,
            "files": [],
            "source": {"branch": "main", "build_mode": "Release"},
        },
    )
    upload_service = SimpleNamespace(resolve_bundle=lambda owner, selected: record)
    api = ApiV1Service(
        control_service_factory=lambda _owner: control,
        runtime_bundle_upload_service_factory=lambda _owner: upload_service,
    )
    config = run_config_dict()
    config["selena"] = {
        "source": "existing",
        "existing_path": "D:/existing/Selena",
        "runtime_xml": "D:/existing/Selena/Runtime.xml",
    }
    config["data"]["path"] = "dataset://sha256/" + "c" * 64
    config["simulation"]["target"] = "cluster"
    config["simulation"]["adapter_file"] = "config-asset://sha256/" + "d" * 64
    config["simulation"]["mat_filter"] = "config-asset://sha256/" + "e" * 64

    job = api.submit_user_run(
        "alice",
        config_payload=config,
        prepared_runtime_bundle_id=bundle_id,
    )
    stages = {item["stage_type"]: item for item in control.get_job(job["id"])["stages"]}
    from core.cluster_stage_executor import CLUSTER_GATEWAY_AGENT_ID, LINUX_STAGE_AGENT_ID

    assert stages["resolve_spec"]["status"] == "skipped"
    assert stages["environment_check"]["required_agent_id"] == LINUX_STAGE_AGENT_ID
    assert stages["prepare_data"]["required_agent_id"] == LINUX_STAGE_AGENT_ID
    assert stages["preflight"]["required_agent_id"] == LINUX_STAGE_AGENT_ID
    assert stages["run_simulation"]["required_agent_id"] == CLUSTER_GATEWAY_AGENT_ID
    assert stages["collect_results"]["required_agent_id"] == LINUX_STAGE_AGENT_ID
    assert stages["finalize_manifest"]["required_agent_id"] == LINUX_STAGE_AGENT_ID


def test_existing_bundle_local_data_path_creates_agent_upload_stage(tmp_path):
    control = ControlService(tmp_path / "control.db")
    bundle_id = "selena-bundle:sha256:" + "a" * 64
    record = SimpleNamespace(
        internal_project="bydod25",
        public_dict={
            "id": bundle_id,
            "storage_ref": "shared://selena-bundles/bydod25/runtime-bundle.zip",
            "archive_checksum": "sha256:" + "b" * 64,
            "archive_size": 10,
            "files": [],
            "source": {"branch": "main", "build_mode": "Release"},
        },
    )
    api = ApiV1Service(
        control_service_factory=lambda _owner: control,
        runtime_bundle_upload_service_factory=lambda _owner: SimpleNamespace(
            resolve_bundle=lambda _owner, _selected: record
        ),
    )
    config = run_config_dict()
    config["selena"] = {
        "source": "existing",
        "existing_path": "D:/existing/Selena",
        "runtime_xml": "D:/existing/Selena/Runtime.xml",
    }
    config["data"]["path"] = "D:/measurements/local"
    config["simulation"]["target"] = "cluster"

    job = api.submit_user_run(
        "alice",
        config_payload=config,
        prepared_runtime_bundle_id=bundle_id,
    )
    stage = next(item for item in control.get_job(job["id"])["stages"] if item["stage_type"] == "prepare_data")
    assert stage["required_agent_id"] == ""
    assert stage["payload"]["dispatch_scope"] == "data_upload"
    assert stage["payload"]["project"] == "bydod25"
    assert stage["payload"]["data_path"] == "D:/measurements/local"


def test_submit_get_cancel_and_manifest_are_logical_jobs(tmp_path):
    api, services = make_api(tmp_path)
    submitted = api.submit_job("alice", spec_payload=spec_dict(), dry_run=True)

    assert submitted["type"] == "simulation.v1.dry_run"
    assert submitted["status"] == "queued"
    assert submitted["spec_hash"] == SimulationSpec.from_dict(spec_dict()).fingerprint()
    assert submitted["metadata"]["owner"] == "alice"
    assert submitted["spec"] == SimulationSpec.from_dict(spec_dict()).to_dict()
    assert submitted["resolved_spec"]["status"] == "pending"
    assert submitted["resolved_spec"]["source_spec_hash"] == submitted["spec_hash"]
    assert len(submitted["stages"]) == 10
    assert [stage["stage_type"] for stage in submitted["stages"]] == [
        "resolve_spec",
        "environment_check",
        "prepare_source",
        "prepare_data",
        "build_selena",
        "register_artifact",
        "preflight",
        "run_simulation",
        "collect_results",
        "finalize_manifest",
    ]

    fetched = api.get_job("alice", submitted["id"])
    assert fetched["id"] == submitted["id"]
    services["alice"].register_agent("legacy-agent", agent_id="legacy-agent", capabilities=["local.*", "cluster.run"])
    assert services["alice"].claim_next_task("legacy-agent") is None
    assert all(stage["assigned_agent_id"] == V1_SCHEDULER_AGENT_ID for stage in fetched["stages"])

    manifest = api.manifest("alice", submitted["id"])
    assert manifest == {"job_id": submitted["id"], "available": False, "manifest": None}

    cancelled = api.cancel_job("alice", submitted["id"])
    assert cancelled["status"] == "cancelled"


def test_durable_idempotency_survives_new_api_service_instance(tmp_path):
    db_path = tmp_path / "control.db"

    def factory(owner: str) -> ControlService:
        return ControlService(db_path)

    first_api = ApiV1Service(control_service_factory=factory)
    first = first_api.submit_job("alice", spec_payload=spec_dict(), dry_run=False, idempotency_key="same-key")

    second_api = ApiV1Service(control_service_factory=factory)
    second = second_api.submit_job("alice", spec_payload=spec_dict(), dry_run=False, idempotency_key="same-key")

    assert second["id"] == first["id"]

    changed = spec_dict(data={"path": "D:/other", "limit": 0, "required_signals": []})
    with pytest.raises(ApiV1Error) as excinfo:
        second_api.submit_job("alice", spec_payload=changed, dry_run=False, idempotency_key="same-key")
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "idempotency_conflict"


def test_idempotency_same_request_concurrent_returns_one_job(tmp_path):
    db_path = tmp_path / "race.db"
    barrier = threading.Barrier(2)

    def submit_once():
        api = ApiV1Service(control_service_factory=lambda owner: ControlService(db_path))
        barrier.wait(timeout=5)
        return api.submit_job("alice", spec_payload=spec_dict(), idempotency_key="race-key")["id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: submit_once(), range(2)))

    assert ids[0] == ids[1]
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE idempotency_key='race-key'").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_idempotency_different_request_concurrent_returns_409_not_500(tmp_path):
    db_path = tmp_path / "race-conflict.db"
    barrier = threading.Barrier(2)
    specs = [
        spec_dict(data={"path": "D:/a", "limit": 0, "required_signals": []}),
        spec_dict(data={"path": "D:/b", "limit": 0, "required_signals": []}),
    ]

    def submit_once(index: int):
        api = ApiV1Service(control_service_factory=lambda owner: ControlService(db_path))
        barrier.wait(timeout=5)
        try:
            return ("ok", api.submit_job("alice", spec_payload=specs[index], idempotency_key="race-key")["id"])
        except ApiV1Error as exc:
            return ("err", exc.status_code, exc.code)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit_once, range(2)))

    assert sum(1 for item in results if item[0] == "ok") == 1
    assert ("err", 409, "idempotency_conflict") in results


def test_idempotency_is_scoped_by_owner(tmp_path):
    api, _ = make_api(tmp_path)
    alice = api.submit_job("alice", spec_payload=spec_dict(), idempotency_key="k")
    bob = api.submit_job("bob", spec_payload=spec_dict(), idempotency_key="k")
    assert alice["id"] != bob["id"]


def test_events_json_cursor_maps_task_logs(tmp_path):
    api, services = make_api(tmp_path)
    job = api.submit_job("alice", spec_payload=spec_dict())
    task_id = job["tasks"][0]["task_id"]
    services["alice"].append_logs(task_id, ["line-1", "line-2"])

    page = api.events("alice", job["id"], since=0, limit=50)
    assert page["status"] == "queued"
    log_events = [event for event in page["events"] if event["event"] == "log"]
    assert [event["message"] for event in log_events] == ["line-1", "line-2"]
    assert page["next_cursor"] == page["events"][-1]["id"]
    assert page["terminal"] is False

    next_page = api.events("alice", job["id"], since=log_events[0]["id"], limit=10)
    assert [event["message"] for event in next_page["events"] if event["event"] == "log"] == ["line-2"]
    assert "event: log" in "".join(iter_sse([event for event in next_page["events"] if event["event"] == "log"]))


def test_events_tail_returns_latest_page_in_chronological_order(tmp_path):
    api, services = make_api(tmp_path)
    job = api.submit_job("alice", spec_payload=spec_dict())
    task_id = job["tasks"][0]["task_id"]
    services["alice"].append_logs(task_id, [f"line-{index}" for index in range(20)])

    page = api.events("alice", job["id"], since=0, limit=5, tail=True)

    assert [event["message"] for event in page["events"]] == [
        "line-15", "line-16", "line-17", "line-18", "line-19"
    ]
    assert page["next_cursor"] == page["events"][-1]["id"]


def test_v1_submit_existing_selena_keeps_skipped_source_build_visible(tmp_path):
    api, _ = make_api(tmp_path)
    existing = spec_dict(
        selena={"mode": "existing", "branch": "", "artifact": "artifact-1", "auto_build": False, "build_mode": "Release"}
    )
    job = api.submit_job("alice", spec_payload=existing, dry_run=True)
    by_type = {stage["stage_type"]: stage for stage in job["stages"]}

    assert by_type["prepare_source"]["status"] == "skipped"
    assert by_type["build_selena"]["status"] == "skipped"
    assert by_type["prepare_source"]["skip_reason"]
    assert by_type["preflight"]["dependencies"] == [
        by_type["environment_check"]["stage_id"],
        by_type["register_artifact"]["stage_id"],
        by_type["prepare_data"]["stage_id"],
    ]
    assert job["status"] == "queued"


def test_v1_submit_provider_none_preserves_pending_contract(tmp_path):
    api, _ = make_api(tmp_path)
    job = api.submit_job("alice", spec_payload=spec_dict())

    assert job["status"] == "queued"
    assert job["resolved_spec"]["status"] == "pending"
    assert job["resolved_spec"]["decisions"] == {}
    assert job["metadata"]["source_resolution"] == {"status": "pending", "code": ""}


def test_current_workspace_missing_only_machine_snapshot_routes_to_matching_agent(tmp_path):
    services: dict[str, ControlService] = {}

    def factory(owner: str) -> ControlService:
        services.setdefault(owner, ControlService(tmp_path / f"{owner}.db"))
        return services[owner]

    binding_id = "workspace:sha256:" + "a" * 24
    service = factory("alice")
    service.register_agent(
        "win-light",
        agent_id="win-light",
        capabilities=["source.workspace.read", "build.selena", "artifact.validate", "artifact.upload"],
        metadata={
            "node_kind": "windows_agent",
            "windows_mode": "light",
            "workspace_bindings": [
                {"id": binding_id, "project": "bydod25", "healthy": True, "configured": True}
            ],
        },
    )
    api = ApiV1Service(
        control_service_factory=factory,
        source_resolution_provider=lambda owner, parsed: source_inputs(
            owner=owner,
            project=parsed.project,
            workspace_binding_id=binding_id,
        ),
    )
    payload = spec_dict(
        selena={
            "mode": "current_workspace",
            "branch": "",
            "artifact": "",
            "auto_build": True,
            "build_mode": "Release",
        }
    )

    job = api.submit_job("alice", spec_payload=payload)
    stages = {stage["stage_type"]: stage for stage in job["stages"]}

    assert job["status"] == "queued"
    assert job["resolved_spec"]["status"] == "pending_node"
    assert job["metadata"]["source_resolution"] == {
        "status": "pending_node",
        "code": "workspace_snapshot_pending",
    }
    assert stages["resolve_spec"]["status"] == "skipped"
    assert stages["environment_check"]["assigned_agent_id"] == "win-light"
    assert stages["environment_check"]["required_agent_id"] == "win-light"
    assert stages["environment_check"]["payload"]["workspace_binding_id"] == binding_id
    assert stages["prepare_source"]["status"] == "skipped"
    assert stages["build_selena"]["assigned_agent_id"] == V1_SCHEDULER_AGENT_ID
    assert service.claim_next_task("win-light")["stage_type"] == "environment_check"


def test_offline_matching_agent_can_bind_pending_environment_on_later_poll(tmp_path):
    api, services = make_api(tmp_path)
    binding_id = "workspace:sha256:" + "b" * 24
    api = ApiV1Service(
        control_service_factory=api.control_service_factory,
        source_resolution_provider=lambda owner, parsed: source_inputs(
            owner=owner,
            project=parsed.project,
            workspace_binding_id=binding_id,
        ),
    )
    payload = spec_dict(
        selena={"mode": "current_workspace", "auto_build": True, "build_mode": "Release"}
    )
    job = api.submit_job("alice", spec_payload=payload)
    service = services["alice"]
    environment = {stage["stage_type"]: stage for stage in job["stages"]}["environment_check"]
    assert environment["assigned_agent_id"] == V1_SCHEDULER_AGENT_ID

    service.register_agent(
        "late-agent",
        agent_id="late-agent",
        capabilities=["source.workspace.read", "build.selena", "artifact.validate", "artifact.upload"],
        metadata={
            "node_kind": "windows_agent",
            "workspace_bindings": [{"id": binding_id, "project": "bydod25", "healthy": True}],
        },
    )
    bound = service.bind_pending_environment_stage("late-agent")
    assert bound["stage_id"] == environment["stage_id"]
    assert bound["required_agent_id"] == "late-agent"
    assert service.claim_next_task("late-agent")["stage_type"] == "environment_check"


def test_current_workspace_without_logical_binding_remains_real_user_input(tmp_path):
    api, services = make_api(tmp_path)
    api = ApiV1Service(
        control_service_factory=api.control_service_factory,
        source_resolution_provider=lambda owner, parsed: source_inputs(owner=owner, project=parsed.project),
    )
    payload = spec_dict(
        selena={"mode": "current_workspace", "auto_build": True, "build_mode": "Release"}
    )
    job = api.submit_job("alice", spec_payload=payload)
    assert job["status"] == "needs_input"
    assert job["resolved_spec"]["code"] == "workspace_binding_required"
    assert all(stage["status"] == "blocked" for stage in job["stages"])
    assert services["alice"].list_agents() == []


def test_minimal_auto_spec_with_binding_uses_machine_pending_instead_of_candidate_prompt(tmp_path):
    api, services = make_api(tmp_path)
    binding_id = "workspace:sha256:" + "d" * 24
    api = ApiV1Service(
        control_service_factory=api.control_service_factory,
        source_resolution_provider=lambda owner, parsed: source_inputs(
            owner=owner,
            project=parsed.project,
            workspace_binding_id=binding_id,
        ),
    )
    job = api.submit_job(
        "alice",
        spec_payload={"project": "bydod25", "data": {"path": "D:/measurement/CBNA_0117"}},
    )
    stages = {stage["stage_type"]: stage for stage in job["stages"]}
    assert job["resolved_spec"]["status"] == "pending_node"
    assert job["metadata"]["source_resolution"]["code"] == "workspace_snapshot_pending"
    assert stages["environment_check"]["payload"]["workspace_binding_id"] == binding_id
    assert stages["environment_check"]["assigned_agent_id"] == V1_SCHEDULER_AGENT_ID
    assert stages["prepare_source"]["status"] == "skipped"


def test_v1_submit_resolved_artifact_persists_resolution_and_dynamic_stage_skip(tmp_path):
    services: dict[str, ControlService] = {}

    def factory(owner: str) -> ControlService:
        services.setdefault(owner, ControlService(tmp_path / f"{owner}.db"))
        return services[owner]

    def provider(owner: str, parsed_spec: SimulationSpec) -> SourceResolutionInputs:
        return source_inputs(
            owner=owner,
            project=parsed_spec.project,
            artifacts=(artifact(project=parsed_spec.project, owner=owner),),
        )

    api = ApiV1Service(control_service_factory=factory, source_resolution_provider=provider)
    job = api.submit_job("alice", spec_payload=spec_dict())
    persisted = services["alice"].get_job(job["id"])
    by_type = {stage["stage_type"]: stage for stage in job["stages"]}

    assert job["resolved_spec"]["status"] == "partial"
    assert job["resolved_spec"]["decisions"]["selena"]["artifact_id"] == "artifact-cluster"
    assert job["metadata"]["source_resolution"] == {"status": "resolved", "code": "selena_artifact_resolved"}
    assert persisted["resolved_spec"] == job["resolved_spec"]
    assert by_type["prepare_source"]["status"] == "skipped"
    assert by_type["build_selena"]["status"] == "skipped"
    assert by_type["register_artifact"]["status"] == "skipped"
    assert by_type["prepare_source"]["assigned_agent_id"] == V1_SCHEDULER_AGENT_ID


@pytest.mark.parametrize(
    ("payload", "provider_inputs", "expected_status", "expected_code"),
    [
        (
            spec_dict(selena={"mode": "auto", "branch": "", "artifact": "", "auto_build": True, "build_mode": "Release"}),
            source_inputs(artifacts=(), workspace_binding_id=""),
            "needs_input",
            "selena_candidate_required",
        ),
        (
            spec_dict(
                selena={
                    "mode": "existing",
                    "branch": "",
                    "artifact": "debug-artifact",
                    "auto_build": False,
                    "build_mode": "Release",
                }
            ),
            source_inputs(artifacts=(artifact(id="debug-artifact", binary_checksum=SHA_B, build_mode="Debug"),)),
            "impossible",
            "artifact_build_mode_incompatible",
        ),
    ],
)
def test_v1_submit_unresolved_outcomes_are_observable_but_not_executable(
    tmp_path,
    payload,
    provider_inputs,
    expected_status,
    expected_code,
):
    api, services = make_api(tmp_path)
    api = ApiV1Service(
        control_service_factory=api.control_service_factory,
        source_resolution_provider=lambda owner, parsed_spec: provider_inputs,
    )

    job = api.submit_job("alice", spec_payload=payload)
    assert job["status"] == "needs_input"
    assert job["resolved_spec"]["status"] == expected_status
    assert job["resolved_spec"]["code"] == expected_code
    assert job["metadata"]["source_resolution"] == {"status": expected_status, "code": expected_code}
    assert all(stage["status"] == "blocked" for stage in job["stages"])
    assert all(stage["assigned_agent_id"] == V1_SCHEDULER_AGENT_ID for stage in job["stages"])
    assert all(stage["error"]["code"] == expected_code for stage in job["stages"])
    events = services["alice"].list_events(job["id"], since=0, limit=100)["events"]
    assert any(event["event"] == "job.status" and event["status"] == "needs_input" for event in events)
    assert any(event["event"] == "stage.blocked" and event["code"] == expected_code for event in events)

    services["alice"].register_internal_agent("scheduler", agent_id=V1_SCHEDULER_AGENT_ID, capabilities=["*"])
    assert services["alice"].claim_next_task(V1_SCHEDULER_AGENT_ID) is None

    cancelled = api.cancel_job("alice", job["id"])
    assert cancelled["status"] == "cancelled"
    assert all(stage["status"] in {"cancelled", "skipped"} for stage in cancelled["stages"])


def test_v1_idempotency_replay_does_not_call_source_provider_again(tmp_path):
    calls = []

    def provider(owner: str, parsed_spec: SimulationSpec) -> SourceResolutionInputs:
        calls.append((owner, parsed_spec.fingerprint()))
        return source_inputs(owner=owner, project=parsed_spec.project, artifacts=(artifact(project=parsed_spec.project),))

    api, _ = make_api(tmp_path)
    api = ApiV1Service(control_service_factory=api.control_service_factory, source_resolution_provider=provider)

    first = api.submit_job("alice", spec_payload=spec_dict(), idempotency_key="same")
    second = api.submit_job("alice", spec_payload=spec_dict(), idempotency_key="same")

    assert second["id"] == first["id"]
    assert len(calls) == 1


def test_v1_provider_exception_is_stable_and_does_not_leak_paths(tmp_path):
    def provider(owner: str, parsed_spec: SimulationSpec) -> SourceResolutionInputs:
        raise RuntimeError(r"D:\secret\workspace\selena.exe")

    api, _ = make_api(tmp_path)
    api = ApiV1Service(control_service_factory=api.control_service_factory, source_resolution_provider=provider)

    with pytest.raises(ApiV1Error) as excinfo:
        api.submit_job("alice", spec_payload=spec_dict())

    body = {
        "code": excinfo.value.code,
        "message": excinfo.value.message,
        "detail": excinfo.value.detail,
        "actions": excinfo.value.actions,
    }
    dumped = json.dumps(body, sort_keys=True)
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "source_resolution_unavailable"
    assert "D:\\secret" not in dumped
    assert "selena.exe" not in dumped
    assert excinfo.value.actions[0]["type"] == "retry_source_resolution"


def test_v1_provider_declared_error_uses_fixed_public_text_without_path_leak(tmp_path):
    def provider(owner: str, parsed_spec: SimulationSpec) -> SourceResolutionInputs:
        raise SourceResolutionProviderError(
            "source_config_invalid",
            r"D:\secret\local.yaml is invalid",
            action_label=r"Open D:\secret\local.yaml",
        )

    api, _ = make_api(tmp_path)
    api = ApiV1Service(control_service_factory=api.control_service_factory, source_resolution_provider=provider)
    with pytest.raises(ApiV1Error) as excinfo:
        api.submit_job("alice", spec_payload=spec_dict())

    dumped = json.dumps(
        {"message": excinfo.value.message, "detail": excinfo.value.detail, "actions": excinfo.value.actions},
        sort_keys=True,
    )
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "source_config_invalid"
    assert "D:\\secret" not in dumped
    assert "local.yaml" not in dumped


def test_v1_rejects_provider_snapshot_for_different_owner(tmp_path):
    api, _ = make_api(tmp_path)
    api = ApiV1Service(
        control_service_factory=api.control_service_factory,
        source_resolution_provider=lambda owner, parsed_spec: source_inputs(
            owner="bob",
            project=parsed_spec.project,
            artifacts=(artifact(owner="bob", visibility="private"),),
        ),
    )

    with pytest.raises(ApiV1Error) as excinfo:
        api.submit_job("alice", spec_payload=spec_dict())
    assert excinfo.value.code == "source_resolution_owner_mismatch"
    assert excinfo.value.status_code == 409


def test_events_are_structured_job_events_not_log_inferred_status(tmp_path):
    api, services = make_api(tmp_path)
    job = api.submit_job("alice", spec_payload=spec_dict())
    stage_id = job["stages"][0]["stage_id"]
    services["alice"].report_stage_progress(stage_id, progress=0.25, message="quarter", code="P25")
    services["alice"].append_logs(stage_id, ["line"])

    page = api.events("alice", job["id"], since=0, limit=50)
    interesting = [event for event in page["events"] if event["event"] in {"stage.progress", "log"}]
    assert [event["event"] for event in interesting] == ["stage.progress", "log"]
    assert interesting[0]["stage_id"] == stage_id
    assert interesting[0]["progress"] == 0.25
    assert page["status"] == "queued"


def test_retry_stage_api_preserves_attempt_history(tmp_path):
    api, services = make_api(tmp_path)
    job = api.submit_job("alice", spec_payload=spec_dict())
    service = services["alice"]
    service.register_internal_agent("scheduler", agent_id=V1_SCHEDULER_AGENT_ID, capabilities=["*"])
    stage = service.claim_next_task(V1_SCHEDULER_AGENT_ID)
    service.submit_task_result(stage["stage_id"], agent_id=V1_SCHEDULER_AGENT_ID, status="failed", returncode=1)

    retried = api.retry_stage("alice", job["id"], stage["stage_id"])
    assert retried["status"] == "queued"
    assert retried["stages"][0]["status"] == "queued"

    next_stage = service.claim_next_task(V1_SCHEDULER_AGENT_ID)
    assert next_stage["stage_id"] == stage["stage_id"]
    service.submit_task_result(next_stage["stage_id"], agent_id=V1_SCHEDULER_AGENT_ID, returncode=0)
    assert [attempt["attempt"] for attempt in service.list_attempts(stage["stage_id"])] == [1, 2]


def test_invalid_spec_uses_stable_error_shape(tmp_path):
    api, _ = make_api(tmp_path)
    with pytest.raises(ApiV1Error) as excinfo:
        api.validate({"schema_version": "1.0"})
    assert excinfo.value.code == "invalid_spec"
    assert excinfo.value.status_code == 422
    assert excinfo.value.actions[0]["type"] == "fix_spec"


def test_old_db_migration_and_legacy_create_job_caller(tmp_path):
    db_path = tmp_path / "old.db"
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

    service = ControlService(db_path)
    job = service.create_job("local.check", payload={"project": "ovrs25"})
    assert job["owner"] == ""
    assert job["idempotency_key"] == ""
    assert job["request_hash"] == ""
    assert service.get_job_by_idempotency("alice", "missing") is None

    columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(jobs)").fetchall()}
    assert {"owner", "idempotency_key", "request_hash"} <= columns


def test_old_db_migration_is_safe_with_concurrent_control_services(tmp_path):
    db_path = tmp_path / "old-concurrent.db"
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
        """
    )
    conn.close()
    barrier = threading.Barrier(2)

    def init_service():
        barrier.wait(timeout=5)
        return ControlService(db_path).list_jobs()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: init_service(), range(2))) == [[], []]

    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(jobs)").fetchall()}
    finally:
        conn.close()
    assert {"owner", "idempotency_key", "request_hash"} <= columns
    assert "idx_jobs_owner_idempotency_key" in indexes


def test_v1_logical_jobs_are_not_claimed_by_regular_agents(tmp_path):
    api, services = make_api(tmp_path)
    api.submit_job("alice", spec_payload=spec_dict())
    service = services["alice"]

    for agent_id, caps in {
        "empty": [],
        "wildcard": ["*"],
        "exact": ["simulation.v1"],
    }.items():
        service.register_agent(agent_id, agent_id=agent_id, capabilities=caps)
        assert service.claim_next_task(agent_id) is None


def test_external_agent_cannot_spoof_v1_scheduler_identity(tmp_path):
    api, services = make_api(tmp_path)
    api.submit_job("alice", spec_payload=spec_dict())
    service = services["alice"]

    with pytest.raises(ValueError, match="reserved"):
        service.register_agent(
            "spoofed-scheduler",
            agent_id=V1_SCHEDULER_AGENT_ID,
            capabilities=["*"],
        )
    assert service.list_agents() == []


def test_api_v1_service_is_framework_agnostic():
    import core.api_v1 as api_v1

    source = api_v1.__loader__.get_source(api_v1.__name__)
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ["fastapi", "httpx", "uvicorn"]:
        assert forbidden not in imported


def test_legacy_imports_do_not_require_wp2_dependencies():
    script = r"""
import builtins
blocked = {"fastapi", "httpx", "uvicorn"}
real_import = builtins.__import__
def guard(name, *args, **kwargs):
    if name.split(".")[0] in blocked:
        raise ModuleNotFoundError(name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = guard
import core.config
import core.control_service
print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "ok"
