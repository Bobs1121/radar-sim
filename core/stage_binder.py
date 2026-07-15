"""Restricted v2 Stage binding rules.

Every handoff is backed by a trusted Stage result and keeps node-local leases
on the Agent that created them.  Cluster Stages use their dedicated central
executors; Windows-full local Stages are bound here only after build, data and
asset prerequisites are available on the same Agent.
"""

from __future__ import annotations

import time
import re
from typing import Callable

from core.control_service import ControlService, INTERNAL_V1_SCHEDULER_AGENT_ID
from core.datasets import classify_data_path
from core.environment_snapshot import EnvironmentSnapshot, EnvironmentSnapshotError


class StageBindingError(ValueError):
    """Stable refusal to bind a Stage without sufficient evidence."""


_BUILD_REQUIREMENTS = (
    "workspace_binding",
    "selena_build_toolchain",
    "artifact_local_staging",
)


def _selected_execution_target(job: dict) -> str:
    """Return the scheduler-selected route without changing the public YAML."""
    selected = str(
        (((job.get("resolved_spec") or {}).get("decisions") or {}).get("execution") or {})
        .get("selected_target")
        or ""
    ).strip()
    if selected in {"local", "cluster"}:
        return selected
    requested = str(((job.get("spec") or {}).get("simulation") or {}).get("target") or "auto")
    return requested


def bind_run_config_environment(
    control: ControlService,
    job_id: str,
    resolution_stage_id: str,
) -> dict:
    """Handoff a project-free workspace result to the same Windows Agent."""
    job = control.get_job(job_id)
    stages = {str(item.get("stage_type") or ""): item for item in job.get("stages") or []}
    resolution = stages.get("resolve_spec")
    environment = stages.get("environment_check")
    if not resolution or resolution.get("stage_id") != resolution_stage_id or resolution.get("status") != "succeeded":
        raise StageBindingError("resolve_spec stage has not succeeded")
    if not environment or environment.get("status") != "queued":
        raise StageBindingError("environment_check stage is not queued")
    if str(environment.get("assigned_agent_id") or "") != INTERNAL_V1_SCHEDULER_AGENT_ID:
        raise StageBindingError("environment_check stage assignment changed")
    agent_id = str(resolution.get("required_agent_id") or resolution.get("assigned_agent_id") or "")
    result = dict((resolution.get("result") or {}).get("recognition") or {})
    internal_project = str(result.get("internal_project") or "").strip()
    binding_id = str(result.get("workspace_binding_id") or "").strip()
    adapter_key = str(result.get("adapter_key") or "").strip()
    asset_bindings = dict(result.get("asset_bindings") or {})
    selected_data_binding_id = str(result.get("data_binding_id") or "").strip()
    selena_script_ref = str(result.get("selena_build_script_ref") or "").strip()
    package_script_ref = str(result.get("package_build_script_ref") or "").strip()
    if (
        not agent_id
        or agent_id == INTERNAL_V1_SCHEDULER_AGENT_ID
        or result.get("status") != "resolved"
        or not internal_project
        or not adapter_key
        or not binding_id.startswith("workspace:sha256:")
    ):
        raise StageBindingError("resolve_spec result is not trusted")
    if str((job.get("metadata") or {}).get("contract") or "") == "user-run-config/2.0":
        if set(asset_bindings) != {"runtime_xml"} or any(
            not str(value or "").startswith("asset-root:sha256:")
            for value in asset_bindings.values()
        ):
            raise StageBindingError("Runtime XML is not authorized")
    spec = dict(job.get("spec") or {})
    selena = dict(spec.get("selena") or {})
    resolved_spec = dict(job.get("resolved_spec") or {})
    decisions = dict(resolved_spec.get("decisions") or {})
    # Public snapshot records only the outcome. Internal adapter/project values
    # travel in the bound Stage payload and are sanitized from the v2 API.
    decisions["recognition"] = {
        "status": "resolved",
        "confidence": float(result.get("confidence") or 0.0),
        "evidence": list(result.get("evidence") or []),
    }
    resolved_spec["decisions"] = decisions
    resolved_spec["status"] = "partial"
    control.update_resolved_spec(job_id, resolved_spec)
    data_stage = stages.get("prepare_data")
    data_path = str((spec.get("data") or {}).get("path") or "")
    target = _selected_execution_target(job)
    if (
        data_stage
        and data_stage.get("status") == "queued"
        and str(data_stage.get("assigned_agent_id") or "") == INTERNAL_V1_SCHEDULER_AGENT_ID
    ):
        from core.agent_data_bindings import candidate_data_binding_ids

        agent = next(
            (item for item in control.list_agents() if str(item.get("agent_id") or "") == agent_id),
            None,
        )
        advertised = {
            str(item.get("id") or "")
            for item in dict((agent or {}).get("metadata") or {}).get("data_bindings") or []
            if isinstance(item, dict)
            and item.get("healthy") is True
            and str(item.get("project") or "") == internal_project
        }
        binding = (
            selected_data_binding_id
            if selected_data_binding_id.startswith("data-root:sha256:")
            else next(
                (item for item in candidate_data_binding_ids(internal_project, data_path) if item in advertised),
                "",
            )
        )
        if binding:
            control.bind_stage_to_agent(
                str(data_stage["stage_id"]),
                agent_id=agent_id,
                expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
                payload_patch={
                    "dispatch_scope": "local_data" if target == "local" else "data_upload",
                    "contract": "user-run-config/2.0",
                    "project": internal_project,
                    "data_path": data_path,
                    "data_binding_id": binding,
                    "required_signals": [],
                },
            )
    return control.bind_stage_to_agent(
        str(environment["stage_id"]),
        agent_id=agent_id,
        expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
        payload_patch={
            "dispatch_scope": "selena_build",
            "contract": "user-run-config/2.0",
            "project": internal_project,
            "workspace_binding_id": binding_id,
            "build_mode": str(selena.get("build_mode") or "RelWithDebInfo"),
            "profile": "default",
            "clean": False,
            "adapter_key": adapter_key,
            "selena_build_script_ref": selena_script_ref,
            "package_build_script_ref": package_script_ref,
            "asset_bindings": asset_bindings,
            "runtime_xml": str(selena.get("runtime_xml") or ""),
        },
    )


def bind_current_workspace_build(
    control: ControlService,
    job_id: str,
    environment_stage_id: str,
    *,
    now_fn: Callable[[], float] = time.time,
) -> dict:
    """Bind ``build_selena`` to the Agent that produced a ready snapshot.

    A7b intentionally allows only ``current_workspace`` here. Branch builds
    require a detached source lease/worktree adapter; binding them to the
    user's dirty checkout would violate the product contract.
    """
    job = control.get_job(job_id)
    stages = {str(item.get("stage_type") or ""): item for item in job.get("stages") or []}
    environment = stages.get("environment_check")
    build = stages.get("build_selena")
    source = stages.get("prepare_source")
    if not environment or environment.get("stage_id") != environment_stage_id:
        raise StageBindingError("environment_check stage does not belong to this job")
    if environment.get("status") != "succeeded":
        raise StageBindingError("environment_check stage has not succeeded")
    if not build or build.get("status") != "queued":
        raise StageBindingError("build_selena stage is not queued")
    if str(build.get("assigned_agent_id") or "") != INTERNAL_V1_SCHEDULER_AGENT_ID:
        raise StageBindingError("build_selena stage assignment changed")
    if not source or source.get("status") not in {"succeeded", "skipped"}:
        raise StageBindingError("prepare_source stage has not completed")

    spec = dict(job.get("spec") or {})
    selena = dict(spec.get("selena") or {})
    is_run_config = str(spec.get("schema_version") or "") == "2.0"
    mode = str(selena.get("mode") or "auto")
    if is_run_config:
        if str(selena.get("source") or "") != "build":
            raise StageBindingError("run config does not request a build")
        if str(selena.get("branch") or "").strip():
            raise StageBindingError("isolated branch worktree executor is not yet bound")
    elif mode not in {"current_workspace", "auto"} or not bool(selena.get("auto_build", True)):
        raise StageBindingError("only current_workspace build binding is available")
    raw_snapshot = dict((environment.get("result") or {}).get("environment_snapshot") or {})
    try:
        snapshot = EnvironmentSnapshot.from_dict(raw_snapshot)
    except EnvironmentSnapshotError as exc:
        raise StageBindingError("environment snapshot is invalid") from exc
    assigned_agent = str(environment.get("assigned_agent_id") or "")
    if not assigned_agent or snapshot.agent_id != assigned_agent:
        raise StageBindingError("environment snapshot agent mismatch")
    expected_project = snapshot.project if is_run_config else str(spec.get("project") or "")
    if snapshot.project != expected_project:
        raise StageBindingError("environment snapshot project mismatch")
    if snapshot.expires_at <= float(now_fn()):
        raise StageBindingError("environment snapshot has expired")
    if not snapshot.satisfies(_BUILD_REQUIREMENTS):
        raise StageBindingError("environment snapshot does not satisfy build requirements")
    if not snapshot.workspace:
        raise StageBindingError("environment snapshot has no workspace fingerprint")

    workspace = dict(snapshot.workspace)
    resolved_spec = dict(job.get("resolved_spec") or {})
    decisions = dict(resolved_spec.get("decisions") or {})
    decisions["selena"] = {
        "status": "resolved",
        "code": "selena_workspace_build",
        "action": "build_current_workspace",
        "resolution": "workspace_build",
        "workspace_binding_id": snapshot.workspace_binding_id,
        "branch": str(workspace.get("branch") or ""),
        "commit": str(workspace.get("commit") or ""),
        "dirty": bool(workspace.get("dirty")),
        "dirty_fingerprint": str(workspace.get("sha256") or "") if workspace.get("dirty") else "",
        "build_mode": str(selena.get("build_mode") or ("RelWithDebInfo" if is_run_config else "Release")),
        "evidence": {"reason": "node_local_environment_snapshot"},
    }
    resolved_spec["decisions"] = decisions
    resolved_spec["status"] = "partial"
    resolved_spec.pop("code", None)
    resolved_spec.pop("action", None)
    control.update_resolved_spec(job_id, resolved_spec)

    return control.bind_stage_to_agent(
        str(build["stage_id"]),
        agent_id=assigned_agent,
        expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
        payload_patch={
            "project": snapshot.project,
            "workspace_binding_id": snapshot.workspace_binding_id,
            "build_mode": str(selena.get("build_mode") or ("RelWithDebInfo" if is_run_config else "Release")),
            "profile": str((spec.get("simulation") or {}).get("profile") or "default"),
            "clean": False,
            "environment_snapshot_ref": f"{environment_stage_id}:{int(environment.get('attempt_count') or 0)}",
            "asset_bindings": dict(environment.get("payload", {}).get("asset_bindings") or {}),
            "adapter_key": str(environment.get("payload", {}).get("adapter_key") or ""),
            "runtime_xml": str(selena.get("runtime_xml") or ""),
        },
    )


def bind_branch_source(
    control: ControlService,
    job_id: str,
    environment_stage_id: str,
    *,
    now_fn: Callable[[], float] = time.time,
) -> dict:
    """Bind ``prepare_source`` to the same authorized Windows Agent."""
    job = control.get_job(job_id)
    stages = {str(item.get("stage_type") or ""): item for item in job.get("stages") or []}
    environment = stages.get("environment_check")
    source = stages.get("prepare_source")
    if not environment or environment.get("stage_id") != environment_stage_id or environment.get("status") != "succeeded":
        raise StageBindingError("environment_check stage has not succeeded")
    if not source or source.get("status") != "queued" or str(source.get("assigned_agent_id") or "") != INTERNAL_V1_SCHEDULER_AGENT_ID:
        raise StageBindingError("prepare_source stage is not queued")
    spec = dict(job.get("spec") or {})
    selena = dict(spec.get("selena") or {})
    branch = str(selena.get("branch") or "").strip()
    if str(selena.get("source") or "") != "build" or not branch:
        raise StageBindingError("run config does not request an isolated branch build")
    raw_snapshot = dict((environment.get("result") or {}).get("environment_snapshot") or {})
    try:
        snapshot = EnvironmentSnapshot.from_dict(raw_snapshot)
    except EnvironmentSnapshotError as exc:
        raise StageBindingError("environment snapshot is invalid") from exc
    agent_id = str(environment.get("assigned_agent_id") or "")
    if snapshot.agent_id != agent_id or snapshot.expires_at <= float(now_fn()):
        raise StageBindingError("environment snapshot is stale or belongs to another Agent")
    if not snapshot.satisfies(_BUILD_REQUIREMENTS):
        raise StageBindingError("environment snapshot does not satisfy source requirements")
    payload = dict(environment.get("payload") or {})
    return control.bind_stage_to_agent(
        str(source["stage_id"]),
        agent_id=agent_id,
        expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
        payload_patch={
            "contract": "user-run-config/2.0",
            "project": snapshot.project,
            "workspace_binding_id": snapshot.workspace_binding_id,
            "branch": branch,
            "asset_bindings": dict(payload.get("asset_bindings") or {}),
            "adapter_key": str(payload.get("adapter_key") or ""),
        },
    )


def bind_branch_worktree_build(control: ControlService, job_id: str, source_stage_id: str) -> dict:
    """Handoff a trusted isolated Source Lease to ``build_selena``."""
    job = control.get_job(job_id)
    stages = {str(item.get("stage_type") or ""): item for item in job.get("stages") or []}
    source = stages.get("prepare_source")
    build = stages.get("build_selena")
    environment = stages.get("environment_check")
    if not source or source.get("stage_id") != source_stage_id or source.get("status") != "succeeded":
        raise StageBindingError("prepare_source stage has not succeeded")
    if not build or build.get("status") != "queued" or str(build.get("assigned_agent_id") or "") != INTERNAL_V1_SCHEDULER_AGENT_ID:
        raise StageBindingError("build_selena stage is not queued")
    agent_id = str(source.get("required_agent_id") or source.get("assigned_agent_id") or "")
    if not environment or str(environment.get("assigned_agent_id") or "") != agent_id:
        raise StageBindingError("source and environment Agent mismatch")
    source_result = dict((source.get("result") or {}).get("source_lease") or {})
    source_lease_ref = str(source_result.get("lease_id") or "")
    if (
        not source_lease_ref.startswith("source-lease:sha256:")
        or source_result.get("source_kind") != "branch_worktree"
        or not re.fullmatch(r"[0-9a-f]{40}", str(source_result.get("commit") or ""))
    ):
        raise StageBindingError("prepare_source result has no trusted Source Lease")
    spec = dict(job.get("spec") or {})
    selena = dict(spec.get("selena") or {})
    env_payload = dict(environment.get("payload") or {})
    decisions = dict((job.get("resolved_spec") or {}).get("decisions") or {})
    decisions["selena"] = {
        "status": "resolved",
        "code": "selena_branch_worktree",
        "action": "build_isolated_branch",
        "resolution": "workspace_build",
        "workspace_binding_id": str(source_result.get("workspace_binding_id") or ""),
        "branch": str(source_result.get("branch") or ""),
        "commit": str(source_result.get("commit") or ""),
        "dirty": False,
        "dirty_fingerprint": "",
        "build_mode": str(selena.get("build_mode") or "RelWithDebInfo"),
        "evidence": {"reason": "agent_source_lease", "ref": str(source_result.get("source_evidence_ref") or "")},
    }
    resolved_spec = dict(job.get("resolved_spec") or {})
    resolved_spec["decisions"] = decisions
    resolved_spec["status"] = "partial"
    control.update_resolved_spec(job_id, resolved_spec)
    return control.bind_stage_to_agent(
        str(build["stage_id"]),
        agent_id=agent_id,
        expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
        payload_patch={
            "contract": "user-run-config/2.0",
            "project": str(source_result.get("project") or ""),
            "workspace_binding_id": str(source_result.get("workspace_binding_id") or ""),
            "build_mode": str(selena.get("build_mode") or "RelWithDebInfo"),
            "profile": "default",
            "clean": False,
            "branch": str(source_result.get("branch") or ""),
            "commit": str(source_result.get("commit") or ""),
            "source_lease_ref": source_lease_ref,
            "source_evidence_ref": str(source_result.get("source_evidence_ref") or ""),
            "asset_bindings": dict(env_payload.get("asset_bindings") or {}),
            "adapter_key": str(env_payload.get("adapter_key") or ""),
            "runtime_xml": str(selena.get("runtime_xml") or ""),
            "adapter_file": str((spec.get("simulation") or {}).get("adapter_file") or ""),
            "mat_filter": str((spec.get("simulation") or {}).get("mat_filter") or ""),
        },
    )


def bind_register_artifact(
    control: ControlService,
    job_id: str,
    build_stage_id: str,
) -> dict:
    """Bind upload/registration to the same Agent as one successful build."""
    job = control.get_job(job_id)
    stages = {str(item.get("stage_type") or ""): item for item in job.get("stages") or []}
    build = stages.get("build_selena")
    register = stages.get("register_artifact")
    if not build or build.get("stage_id") != build_stage_id or build.get("status") != "succeeded":
        raise StageBindingError("build_selena stage has not succeeded")
    if not register or register.get("status") != "queued":
        raise StageBindingError("register_artifact stage is not queued")
    if str(register.get("assigned_agent_id") or "") != INTERNAL_V1_SCHEDULER_AGENT_ID:
        raise StageBindingError("register_artifact stage assignment changed")
    agent_id = str(build.get("required_agent_id") or build.get("assigned_agent_id") or "")
    if not agent_id or agent_id == INTERNAL_V1_SCHEDULER_AGENT_ID:
        raise StageBindingError("build_selena stage has no required Windows Agent")
    result = dict(build.get("result") or {})
    lease_ref = str(result.get("artifact_lease_ref") or "").strip()
    runtime_bundle_lease_ref = str(result.get("runtime_bundle_lease_ref") or "").strip()
    is_v2 = str((job.get("metadata") or {}).get("contract") or "") == "user-run-config/2.0"
    if not is_v2 and not lease_ref.startswith("artifact-lease:sha256:"):
        raise StageBindingError("build_selena stage has no artifact lease")
    if is_v2 and not runtime_bundle_lease_ref.startswith(
        "runtime-bundle-lease:sha256:"
    ):
        raise StageBindingError("build_selena stage has no Runtime Bundle lease")
    attempt = int(build.get("attempt_count") or 0)
    if attempt < 1:
        raise StageBindingError("build_selena attempt is invalid")
    spec = dict(job.get("spec") or {})
    publish_path = str((spec.get("selena") or {}).get("publish_path") or "")
    return control.bind_stage_to_agent(
        str(register["stage_id"]),
        agent_id=agent_id,
        expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
        payload_patch={
            "project": str(spec.get("project") or ""),
            "workspace_binding_id": str(result.get("workspace_binding_id") or ""),
            "artifact_lease_ref": lease_ref,
            "runtime_bundle_lease_ref": runtime_bundle_lease_ref,
            "build_evidence_ref": f"{build_stage_id}:{attempt}",
            "publish_path": publish_path,
        },
    )


def complete_data_resolution(control: ControlService, job_id: str, data_stage_id: str) -> dict:
    """Persist one successful Agent-uploaded DatasetRef and release Agent affinity."""
    job = control.get_job(job_id)
    stages = {str(item.get("stage_type") or ""): item for item in job.get("stages") or []}
    stage = stages.get("prepare_data")
    if not stage or stage.get("stage_id") != data_stage_id or stage.get("status") != "succeeded":
        raise StageBindingError("prepare_data stage has not succeeded")
    agent_id = str(stage.get("required_agent_id") or stage.get("assigned_agent_id") or "")
    if not agent_id or agent_id == INTERNAL_V1_SCHEDULER_AGENT_ID:
        raise StageBindingError("prepare_data stage has no required Windows Agent")
    attempt = int(stage.get("attempt_count") or 0)
    result = dict(stage.get("result") or {})
    dataset = dict(result.get("dataset") or {})
    dataset_id = str(dataset.get("id") or "")
    storage_ref = str(dataset.get("storage_ref") or "")
    evidence_ref = str(result.get("evidence_ref") or "")
    data_lease_ref = str(result.get("data_lease_ref") or "")
    local_route = str((stage.get("payload") or {}).get("dispatch_scope") or "") == "local_data"
    if local_route:
        if (
            not data_lease_ref.startswith("data-lease:sha256:")
            or not dataset_id.startswith("dataset:sha256:")
            or dataset.get("source_kind") != "agent_local"
            or evidence_ref != f"{data_stage_id}:{attempt}"
        ):
            raise StageBindingError("prepare_data result has no trusted local Data Lease")
        resolved_spec = dict(job.get("resolved_spec") or {})
        decisions = dict(resolved_spec.get("decisions") or {})
        decisions["data"] = {
            "status": "resolved",
            "code": "agent_local_data_leased",
            "route": "local",
            "action": "",
            "dataset": dataset,
            "evidence": {"reason": "agent_local_data_lease", "ref": evidence_ref},
        }
        resolved_spec["decisions"] = decisions
        selena_status = str((decisions.get("selena") or {}).get("status") or "")
        resolved_spec["status"] = "resolved" if selena_status == "resolved" else "partial"
        resolved_spec.pop("code", None)
        resolved_spec.pop("action", None)
        return control.update_resolved_spec(job_id, resolved_spec)
    if (
        not dataset_id.startswith("dataset:sha256:")
        or not storage_ref.startswith("shared://datasets/")
        or dataset.get("source_kind") != "agent_upload"
        or evidence_ref != f"{data_stage_id}:{attempt}"
    ):
        raise StageBindingError("prepare_data result has no trusted DatasetRef")
    resolved_spec = dict(job.get("resolved_spec") or {})
    decisions = dict(resolved_spec.get("decisions") or {})
    decisions["data"] = {
        "status": "resolved",
        "code": "agent_dataset_uploaded",
        "route": "central",
        "action": "",
        "dataset": dataset,
        "evidence": {"reason": "agent_prepare_data_attempt", "ref": evidence_ref},
    }
    resolved_spec["decisions"] = decisions
    selena_status = str((decisions.get("selena") or {}).get("status") or "")
    resolved_spec["status"] = "resolved" if selena_status == "resolved" else "partial"
    resolved_spec.pop("code", None)
    resolved_spec.pop("action", None)
    return control.update_resolved_spec(job_id, resolved_spec)


def maybe_bind_local_preflight(control: ControlService, job_id: str) -> dict | None:
    """Bind local preflight once a node-local Bundle and data both exist."""
    job = control.get_job(job_id)
    spec = dict(job.get("spec") or {})
    if _selected_execution_target(job) != "local":
        return None
    stages = {str(item.get("stage_type") or ""): item for item in job.get("stages") or []}
    preflight = stages.get("preflight")
    register = stages.get("register_artifact")
    environment = stages.get("environment_check")
    data = stages.get("prepare_data")
    if not preflight or preflight.get("status") != "queued":
        return None
    if str(preflight.get("assigned_agent_id") or "") != INTERNAL_V1_SCHEDULER_AGENT_ID:
        return None
    if not register or register.get("status") not in {"succeeded", "skipped"} or not data or data.get("status") != "succeeded":
        return None
    existing_bundle = register.get("status") == "skipped"
    bundle_stage = environment if existing_bundle else register
    if not bundle_stage or bundle_stage.get("status") != "succeeded":
        return None
    agent_id = str(bundle_stage.get("required_agent_id") or bundle_stage.get("assigned_agent_id") or "")
    data_agent = str(data.get("required_agent_id") or data.get("assigned_agent_id") or "")
    if not agent_id or agent_id != data_agent or agent_id == INTERNAL_V1_SCHEDULER_AGENT_ID:
        raise StageBindingError("local Runtime Bundle and data must belong to the same Agent")
    agent = next(
        (item for item in control.list_agents() if str(item.get("agent_id") or "") == agent_id),
        None,
    )
    if str(((agent or {}).get("metadata") or {}).get("node_kind") or "") != "windows_full":
        raise StageBindingError("local simulation requires a Windows full deployment")
    register_payload = dict(bundle_stage.get("payload") or {})
    register_result = dict(bundle_stage.get("result") or {})
    data_result = dict(data.get("result") or {})
    runtime_bundle_lease_ref = str(
        register_result.get("runtime_bundle_lease_ref")
        or register_payload.get("runtime_bundle_lease_ref")
        or ""
    )
    data_lease_ref = str(data_result.get("data_lease_ref") or "")
    bundle = dict(
        register_result.get("runtime_bundle")
        or (((job.get("resolved_spec") or {}).get("decisions") or {}).get("selena") or {}).get("runtime_bundle")
        or {}
    )
    dataset = dict(data_result.get("dataset") or {})
    if (
        not runtime_bundle_lease_ref.startswith("runtime-bundle-lease:sha256:")
        or not str(bundle.get("id") or "").startswith("selena-bundle:sha256:")
        or not data_lease_ref.startswith("data-lease:sha256:")
        or not str(dataset.get("id") or "").startswith("dataset:sha256:")
    ):
        raise StageBindingError("local preflight prerequisites are invalid")
    simulation = dict(spec.get("simulation") or {})
    return control.bind_stage_to_agent(
        str(preflight["stage_id"]),
        agent_id=agent_id,
        expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
        payload_patch={
            "dispatch_scope": "local_simulation",
            "contract": "user-run-config/2.0",
            "project": str((register_payload.get("project") or data.get("payload", {}).get("project") or "")),
            "runtime_bundle_lease_ref": runtime_bundle_lease_ref,
            "runtime_bundle_id": str(bundle.get("id") or ""),
            "data_lease_ref": data_lease_ref,
            "dataset_id": str(dataset.get("id") or ""),
            "adapter_file": str(simulation.get("adapter_file") or ""),
            "mat_filter": str(simulation.get("mat_filter") or ""),
            "limit": int((spec.get("data") or {}).get("limit") or 0),
            "timeout_minutes": int(simulation.get("timeout_minutes") or 0),
            "owner": str(job.get("owner") or (job.get("metadata") or {}).get("owner") or ""),
            "retain_days": int((spec.get("result") or {}).get("retain_days") or 30),
            "config_fingerprint": str((job.get("resolved_spec") or {}).get("source_config_hash") or ""),
        },
    )


def maybe_bind_cluster_data_after_bundle(control: ControlService, job_id: str) -> dict | None:
    """Release centrally accessible data only after its Runtime Bundle reveals the project."""
    from core.cluster_stage_executor import LINUX_STAGE_AGENT_ID

    job = control.get_job(job_id)
    if _selected_execution_target(job) != "cluster":
        return None
    stages = {str(item.get("stage_type") or ""): item for item in job.get("stages") or []}
    data = stages.get("prepare_data")
    register = stages.get("register_artifact")
    if (
        not data
        or data.get("status") != "queued"
        or str(data.get("assigned_agent_id") or "") != INTERNAL_V1_SCHEDULER_AGENT_ID
        or not register
        or register.get("status") not in {"succeeded", "skipped"}
    ):
        return None
    data_path = str(((job.get("spec") or {}).get("data") or {}).get("path") or "")
    if not (
        data_path.lower().startswith("dataset://")
        or classify_data_path(data_path) in {"shared", "central"}
    ):
        return None
    project = str((register.get("payload") or {}).get("project") or "")
    return control.bind_stage_to_agent(
        str(data["stage_id"]),
        agent_id=LINUX_STAGE_AGENT_ID,
        expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
        payload_patch={
            "dispatch_scope": "central_data",
            "contract": "user-run-config/2.0",
            "project": project,
            "data_path": data_path,
            "required_signals": [],
        },
    )


def bind_existing_bundle_local_data(
    control: ControlService,
    job_id: str,
    environment_stage_id: str,
) -> dict:
    """Keep local data on the Agent that cached an existing shared Bundle."""
    job = control.get_job(job_id)
    stages = {str(item.get("stage_type") or ""): item for item in job.get("stages") or []}
    environment = stages.get("environment_check")
    data = stages.get("prepare_data")
    if (
        not environment
        or environment.get("stage_id") != environment_stage_id
        or environment.get("status") != "succeeded"
        or str((environment.get("payload") or {}).get("dispatch_scope") or "") != "runtime_bundle_cache"
    ):
        raise StageBindingError("Runtime Bundle cache Stage has not succeeded")
    if not data or data.get("status") != "queued":
        raise StageBindingError("prepare_data stage is not queued")
    if str(data.get("assigned_agent_id") or "") != INTERNAL_V1_SCHEDULER_AGENT_ID:
        raise StageBindingError("prepare_data stage assignment changed")
    agent_id = str(environment.get("required_agent_id") or environment.get("assigned_agent_id") or "")
    result = dict(environment.get("result") or {})
    lease_ref = str(result.get("runtime_bundle_lease_ref") or "")
    bundle = dict(result.get("runtime_bundle") or {})
    if (
        not agent_id
        or agent_id == INTERNAL_V1_SCHEDULER_AGENT_ID
        or not lease_ref.startswith("runtime-bundle-lease:sha256:")
        or not str(bundle.get("id") or "").startswith("selena-bundle:sha256:")
    ):
        raise StageBindingError("Runtime Bundle cache result is not trusted")
    payload = dict(environment.get("payload") or {})
    binding_id = str(payload.get("data_binding_id") or "")
    if not binding_id.startswith("data-root:sha256:"):
        raise StageBindingError("local data binding is unavailable on Runtime Bundle Agent")
    spec = dict(job.get("spec") or {})
    return control.bind_stage_to_agent(
        str(data["stage_id"]),
        agent_id=agent_id,
        expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
        payload_patch={
            "dispatch_scope": "local_data",
            "contract": "user-run-config/2.0",
            "project": str(payload.get("project") or ""),
            "data_path": str((spec.get("data") or {}).get("path") or ""),
            "data_binding_id": binding_id,
            "required_signals": [],
        },
    )


def bind_local_stage_after_result(control: ControlService, completed_stage: dict) -> dict:
    """Keep one local run lease on the same Windows-full Agent."""
    job_id = str(completed_stage.get("job_id") or "")
    job = control.get_job(job_id)
    spec = dict(job.get("spec") or {})
    if _selected_execution_target(job) != "local":
        raise StageBindingError("completed Stage is not a local run")
    stages = {str(item.get("stage_type") or ""): item for item in job.get("stages") or []}
    stage_type = str(completed_stage.get("stage_type") or "")
    next_type = {
        "preflight": "run_simulation",
        "run_simulation": "collect_results",
        "collect_results": "finalize_manifest",
    }.get(stage_type)
    if not next_type:
        raise StageBindingError("local Stage has no successor")
    successor = stages.get(next_type)
    if not successor or successor.get("status") != "queued":
        raise StageBindingError(f"{next_type} stage is not queued")
    agent_id = str(completed_stage.get("required_agent_id") or completed_stage.get("assigned_agent_id") or "")
    if not agent_id or agent_id == INTERNAL_V1_SCHEDULER_AGENT_ID:
        raise StageBindingError("local Stage has no Windows-full Agent")
    result = dict(completed_stage.get("result") or {})
    lease_ref = str(result.get("local_run_lease_ref") or (completed_stage.get("payload") or {}).get("local_run_lease_ref") or "")
    if not lease_ref.startswith("local-run-lease:sha256:"):
        raise StageBindingError("local run lease is unavailable")
    payload = {
        "dispatch_scope": "local_simulation",
        "contract": "user-run-config/2.0",
        "local_run_lease_ref": lease_ref,
        "owner": str(job.get("owner") or (job.get("metadata") or {}).get("owner") or ""),
        "job_id": job_id,
        "runtime_bundle_id": str(
            (((job.get("resolved_spec") or {}).get("decisions") or {}).get("selena") or {})
            .get("runtime_bundle", {}).get("id") or ""
        ),
        "dataset_id": str(
            (((job.get("resolved_spec") or {}).get("decisions") or {}).get("data") or {})
            .get("dataset", {}).get("id") or ""
        ),
        "retain_days": int((spec.get("result") or {}).get("retain_days") or 30),
        "config_fingerprint": str((job.get("resolved_spec") or {}).get("source_config_hash") or ""),
    }
    if stage_type == "collect_results":
        result_ref = str(result.get("result_ref") or "")
        if not result_ref.startswith("result:sha256:"):
            raise StageBindingError("local result reference is unavailable")
        payload["result_ref"] = result_ref
    return control.bind_stage_to_agent(
        str(successor["stage_id"]),
        agent_id=agent_id,
        expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
        payload_patch=payload,
    )


def complete_runtime_bundle_registration(control: ControlService, job_id: str, register_stage_id: str) -> dict:
    """Persist the shared Runtime Bundle selected by one trusted upload attempt."""
    job = control.get_job(job_id)
    stages = {str(item.get("stage_type") or ""): item for item in job.get("stages") or []}
    stage = stages.get("register_artifact")
    if not stage or stage.get("stage_id") != register_stage_id or stage.get("status") != "succeeded":
        raise StageBindingError("register_artifact stage has not succeeded")
    result = dict(stage.get("result") or {})
    bundle = dict(result.get("runtime_bundle") or {})
    if (
        not str(bundle.get("id") or "").startswith("selena-bundle:sha256:")
        or not str(bundle.get("storage_ref") or "").startswith("shared://selena-bundles/")
    ):
        # Legacy single-executable registration remains unchanged.
        return job
    resolved_spec = dict(job.get("resolved_spec") or {})
    decisions = dict(resolved_spec.get("decisions") or {})
    current = dict(decisions.get("selena") or {})
    decisions["selena"] = {
        **current,
        "status": "resolved",
        "code": "runtime_bundle_registered",
        "action": "use_runtime_bundle",
        "runtime_bundle": bundle,
        "evidence": {
            "reason": "trusted_runtime_bundle_upload",
            "ref": str(result.get("build_evidence_ref") or ""),
        },
    }
    resolved_spec["decisions"] = decisions
    data_status = str((decisions.get("data") or {}).get("status") or "")
    resolved_spec["status"] = "resolved" if data_status == "resolved" else "partial"
    return control.update_resolved_spec(job_id, resolved_spec)


def advance_after_stage_result(control: ControlService, completed_stage: dict) -> dict | None:
    """Advance only handoffs backed by real A7b executors."""
    if (
        str(completed_stage.get("stage_type") or "") == "resolve_spec"
        and str(completed_stage.get("status") or "") == "succeeded"
    ):
        return bind_run_config_environment(
            control,
            str(completed_stage.get("job_id") or ""),
            str(completed_stage.get("stage_id") or completed_stage.get("task_id") or ""),
        )
    if (
        str(completed_stage.get("stage_type") or "") == "environment_check"
        and str(completed_stage.get("status") or "") == "succeeded"
    ):
        if str((completed_stage.get("payload") or {}).get("dispatch_scope") or "") == "runtime_bundle_cache":
            return bind_existing_bundle_local_data(
                control,
                str(completed_stage.get("job_id") or ""),
                str(completed_stage.get("stage_id") or completed_stage.get("task_id") or ""),
            )
        job = control.get_job(str(completed_stage.get("job_id") or ""))
        branch = str(((job.get("spec") or {}).get("selena") or {}).get("branch") or "").strip()
        binder = bind_branch_source if branch else bind_current_workspace_build
        return binder(
            control, str(completed_stage.get("job_id") or ""),
            str(completed_stage.get("stage_id") or completed_stage.get("task_id") or ""),
        )
    if (
        str(completed_stage.get("stage_type") or "") == "prepare_source"
        and str(completed_stage.get("status") or "") == "succeeded"
    ):
        return bind_branch_worktree_build(
            control, str(completed_stage.get("job_id") or ""),
            str(completed_stage.get("stage_id") or completed_stage.get("task_id") or ""),
        )
    if (
        str(completed_stage.get("stage_type") or "") == "build_selena"
        and str(completed_stage.get("status") or "") == "succeeded"
    ):
        return bind_register_artifact(
            control,
            str(completed_stage.get("job_id") or ""),
            str(completed_stage.get("stage_id") or completed_stage.get("task_id") or ""),
        )
    if (
        str(completed_stage.get("stage_type") or "") == "prepare_data"
        and str(completed_stage.get("status") or "") == "succeeded"
    ):
        complete_data_resolution(
            control,
            str(completed_stage.get("job_id") or ""),
            str(completed_stage.get("stage_id") or completed_stage.get("task_id") or ""),
        )
        return maybe_bind_local_preflight(control, str(completed_stage.get("job_id") or ""))
    if (
        str(completed_stage.get("stage_type") or "") == "register_artifact"
        and str(completed_stage.get("status") or "") == "succeeded"
    ):
        complete_runtime_bundle_registration(
            control,
            str(completed_stage.get("job_id") or ""),
            str(completed_stage.get("stage_id") or completed_stage.get("task_id") or ""),
        )
        job_id = str(completed_stage.get("job_id") or "")
        return maybe_bind_local_preflight(control, job_id) or maybe_bind_cluster_data_after_bundle(control, job_id)
    if (
        str(completed_stage.get("stage_type") or "") in {"preflight", "run_simulation", "collect_results"}
        and str(completed_stage.get("status") or "") == "succeeded"
    ):
        return bind_local_stage_after_result(control, completed_stage)
    return None


__all__ = [
    "StageBindingError",
    "advance_after_stage_result",
    "bind_run_config_environment",
    "bind_current_workspace_build",
    "bind_branch_source",
    "bind_branch_worktree_build",
    "bind_register_artifact",
    "complete_data_resolution",
    "complete_runtime_bundle_registration",
    "maybe_bind_local_preflight",
    "maybe_bind_cluster_data_after_bundle",
    "bind_local_stage_after_result",
    "bind_existing_bundle_local_data",
]
