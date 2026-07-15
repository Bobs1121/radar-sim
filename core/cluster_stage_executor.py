"""Trusted v2 Cluster Stage execution over logical catalog references.

Physical dataset, Runtime Bundle, configuration asset, workspace and credential
paths remain inside this module and the private lease stores.  Public Stage
results contain only logical references and path-free summaries.
"""

from __future__ import annotations

import copy
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.artifact_store import ArtifactStore
from core.cluster_runs import ClusterResultRef, ClusterRunRef, ClusterRunStore
from core.config_assets import ConfigAssetStore, is_config_asset_ref
from core.datasets import DatasetCatalog, DatasetRef, dataset_id_from_uri, resolve_data_reference
from core.runtime_bundle_archive import extract_runtime_bundle_archive
from core.runtime_bundle_catalog import RuntimeBundleCatalog, RuntimeBundleRecord
from core.shared_namespace import SharedNamespaceRegistry, looks_like_shared_path
from core.user import normalize_user
from core.agent_policy import (
    LINUX_EXECUTOR_CAPABILITIES,
    PLATFORM_GATEWAY_CAPABILITIES,
    NODE_KIND_LINUX_EXECUTOR,
    NODE_KIND_PLATFORM_GATEWAY,
)

LINUX_STAGE_AGENT_ID = "linux-v2-stage-executor"
CLUSTER_GATEWAY_AGENT_ID = "cluster-v2-platform-gateway"


class ClusterStageExecutionError(RuntimeError):
    """Stable execution refusal without exposing private paths."""


@dataclass(frozen=True)
class ClusterStageContext:
    runtime_catalog: RuntimeBundleCatalog
    runtime_store: ArtifactStore
    dataset_catalog: DatasetCatalog
    config_assets: ConfigAssetStore
    run_store: ClusterRunStore
    work_root: Path
    config_loader: Callable[[str], dict[str, Any]]
    now_fn: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        root = Path(self.work_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        object.__setattr__(self, "work_root", root)


class ClusterStageExecutor:
    """Two-role in-process executor for one explicit ControlService database."""

    def __init__(self, control, context: ClusterStageContext, *, poll_interval: float = 1.0) -> None:
        self.control = control
        self.context = context
        self.poll_interval = max(float(poll_interval), 0.05)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self.control.register_agent(
            "Linux v2 stage executor", agent_id=LINUX_STAGE_AGENT_ID,
            platform="linux", capabilities=list(LINUX_EXECUTOR_CAPABILITIES),
            metadata={}, node_kind=NODE_KIND_LINUX_EXECUTOR,
        )
        self.control.register_agent(
            "Cluster v2 platform gateway", agent_id=CLUSTER_GATEWAY_AGENT_ID,
            platform="gateway", capabilities=list(PLATFORM_GATEWAY_CAPABILITIES),
            metadata={}, node_kind=NODE_KIND_PLATFORM_GATEWAY,
        )
        for agent_id in (LINUX_STAGE_AGENT_ID, CLUSTER_GATEWAY_AGENT_ID):
            thread = threading.Thread(
                target=self._loop, args=(agent_id,), daemon=True, name=agent_id
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)

    def _loop(self, agent_id: str) -> None:
        while not self._stop.is_set():
            try:
                self.control.heartbeat(agent_id, status="idle", current_task_id="")
                task = self.control.claim_next_task(agent_id)
                if task is None:
                    self._stop.wait(self.poll_interval)
                    continue
                self._run_one(agent_id, task)
            except Exception:
                self._stop.wait(self.poll_interval)

    def _run_one(self, agent_id: str, task: dict[str, Any]) -> None:
        task_id = str(task.get("task_id") or "")
        stage_type = str(task.get("stage_type") or task.get("task_type") or "")
        job = self.control.get_job(str(task.get("job_id") or ""))
        self.control.append_logs(task_id, [f"[executor] {stage_type} started"])
        try:
            if stage_type == "environment_check":
                result = execute_cluster_environment(self.context, job)
            elif stage_type == "prepare_data":
                result = resolve_cluster_data(self.context, job)
                self._record_dataset(job, result)
            elif stage_type == "preflight":
                result = execute_cluster_preflight(self.context, job)
            elif stage_type == "run_simulation":
                run_ref = str(_stage_result(job, "preflight").get("cluster_run_ref") or "")
                result = execute_cluster_submit(self.context, job, run_ref)
            elif stage_type == "collect_results":
                run_ref = str(_stage_result(job, "run_simulation").get("cluster_run_ref") or "")
                result = execute_cluster_collect(
                    self.context, job, run_ref,
                    cancelled=lambda: bool(
                        self.control.heartbeat(
                            agent_id, status="busy", current_task_id=task_id
                        ).get("cancel_requested")
                    ),
                    sleep_fn=lambda seconds: self._stop.wait(min(float(seconds), 15.0)),
                )
            elif stage_type == "finalize_manifest":
                result_ref = str(_stage_result(job, "collect_results").get("result_ref") or "")
                cluster_result = self.context.run_store.get_result(result_ref, owner=_owner(job))
                result = {"manifest": build_public_run_manifest(job, cluster_result)}
            else:
                raise ClusterStageExecutionError("Stage is not supported by this executor")
            completed = self.control.submit_task_result(
                task_id, agent_id=agent_id, status="succeeded", returncode=0, result=result
            )
            self.control.append_logs(task_id, [f"[executor] {stage_type} completed"])
            return completed
        except Exception as exc:
            # Keep the public error stable and path-free; detailed deployment
            # diagnostics belong in trusted server logs, not the task payload.
            message = str(exc) if isinstance(exc, ClusterStageExecutionError) else "Cluster stage execution failed"
            self.control.append_logs(task_id, [f"[executor] {stage_type} failed"], stream="stderr")
            self.control.submit_task_result(
                task_id, agent_id=agent_id, status="failed", returncode=-1,
                result={"error": message, "code": "cluster_stage_failed"},
            )

    def _record_dataset(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        resolved = dict(job.get("resolved_spec") or {})
        decisions = dict(resolved.get("decisions") or {})
        decisions["data"] = {
            "status": "resolved", "code": "central_dataset_resolved", "route": "central",
            "action": "", "dataset": dict(result.get("dataset") or {}),
            "evidence": {"reason": "trusted_central_resolution"},
        }
        resolved["decisions"] = decisions
        resolved["status"] = "resolved" if str((decisions.get("selena") or {}).get("status") or "") == "resolved" else "partial"
        self.control.update_resolved_spec(str(job.get("job_id") or ""), resolved)


def resolve_cluster_data(context: ClusterStageContext, job: dict[str, Any]) -> dict[str, Any]:
    """Resolve a dataset URI/shared path centrally; local drives require upload."""
    owner = _owner(job)
    project = _project(context, job)
    spec = dict(job.get("spec") or {})
    data_path = str((spec.get("data") or {}).get("path") or "")
    if data_path.lower().startswith("dataset://"):
        dataset = context.dataset_catalog.get(dataset_id_from_uri(data_path), owner=owner)
        return {
            "dataset": dataset.to_dict(),
            "dataset_id": dataset.id,
            "evidence_ref": "central-dataset-resolution",
        }
    config = context.config_loader(project)
    outcome = resolve_data_reference(
        context.dataset_catalog,
        SharedNamespaceRegistry.from_config(config),
        owner=owner,
        project=project,
        data_path=data_path,
        required_signals=(),
    )
    if outcome.status != "resolved" or outcome.dataset is None:
        raise ClusterStageExecutionError(outcome.action or "Dataset must be uploaded before Cluster execution")
    return {
        "dataset": outcome.dataset.to_dict(),
        "dataset_id": outcome.dataset.id,
        "evidence_ref": "central-dataset-resolution",
    }


def execute_cluster_environment(context: ClusterStageContext, job: dict[str, Any]) -> dict[str, Any]:
    """Check only central/Gateway prerequisites; Linux never checks build tools."""
    bundle = _bundle(context, job)
    project = bundle.internal_project
    config = context.config_loader(project)
    from core.cluster import check_cluster_environment

    checks = check_cluster_environment(config)
    superseded = {"Profile Selena executable", "Profile runtime XML"}
    failed = [
        item.name for item in checks
        if item.name not in superseded
        and not bool(item.ok)
        and str(getattr(item, "severity", "error") or "error") == "error"
    ]
    if failed:
        raise ClusterStageExecutionError("Cluster environment is unavailable: " + ", ".join(failed))
    return {
        "environment_snapshot": {
            "status": "ready",
            "node_kind": "linux_executor",
            "requirements": [
                {"name": item.name, "ok": bool(item.ok)}
                for item in checks if item.name not in superseded
            ],
            "runtime_bundle_id": bundle.manifest.id,
        }
    }


def execute_cluster_preflight(context: ClusterStageContext, job: dict[str, Any]) -> dict[str, Any]:
    owner = _owner(job)
    bundle = _bundle(context, job)
    dataset = _dataset(context, job, owner=owner)
    project = bundle.internal_project
    config = copy.deepcopy(context.config_loader(project))
    job_id = str(job.get("job_id") or "")
    private_root = context.work_root / _safe_token(job_id)
    runtime_root = private_root / "runtime-bundle"
    archive = context.runtime_store.resolve_location(bundle.storage_ref)
    extracted = extract_runtime_bundle_archive(
        archive,
        runtime_root,
        manifest=bundle.manifest,
        archive_checksum=bundle.archive_checksum,
    )
    entrypoint_ref = next(
        (item for item in bundle.manifest.files if item.role == "entrypoint"),
        None,
    )
    runtime_ref = next(
        (item for item in bundle.manifest.files if item.role == "runtime_config"),
        None,
    )
    exe = extracted.get(entrypoint_ref.relative_path) if entrypoint_ref is not None else None
    runtime_xml = extracted.get(runtime_ref.relative_path) if runtime_ref is not None else None
    if exe is None or runtime_xml is None:
        raise ClusterStageExecutionError("Runtime Bundle is incomplete")

    simulation = dict((job.get("spec") or {}).get("simulation") or {})
    registry = SharedNamespaceRegistry.from_config(config)
    adapter_value = str(simulation.get("adapter_file") or "").strip()
    adapter = (
        _resolve_config_asset(context, registry, owner, "adapter", adapter_value)
        if adapter_value
        else None
    )
    mat_filter = _resolve_config_asset(context, registry, owner, "mat_filter", simulation.get("mat_filter"))
    data_location = context.dataset_catalog.resolve_location(dataset.id, owner=owner)

    config.setdefault("_meta", {})["project"] = project
    config.setdefault("paths", {})["build_output"] = str(exe.parent)
    config.setdefault("selena", {})["exe_pattern"] = "{executable_name}"
    config["selena"]["executable_name"] = exe.name
    config.setdefault("build", {})["selena_branch"] = bundle.manifest.source.branch
    sim = config.setdefault("simulation", {})
    sim["runtime_xml"] = str(runtime_xml)
    sim["adapter_file"] = str(adapter) if adapter is not None else ""
    sim["matfilefilter"] = str(mat_filter)
    sim["input_mf4"] = str(data_location)
    _apply_existing_cluster_profile_defaults(config, job)

    from core.preflight import run_preflight
    preflight = run_preflight(config)
    if not preflight.ok:
        raise ClusterStageExecutionError("Preflight compatibility validation failed")

    from core.cluster import prepare_cluster_job
    package = prepare_cluster_job(
        config,
        input_path=str(data_location),
        run_id=_safe_token(job_id),
        copy_data=dataset.source_kind != "shared_path",
        copy_selena=True,
    )
    local_job_root = Path(package.manifest_path).parent
    run = context.run_store.create_run(
        owner=owner,
        control_job_id=job_id,
        project=project,
        dataset_id=dataset.id,
        artifact_id=bundle.manifest.id,
        artifact_storage_ref=bundle.storage_ref,
        profile=package.profile,
        job_dir=str(local_job_root),
        config_path=package.config_path,
        output_location=str(local_job_root / "output"),
    )
    return {
        "cluster_run": run.to_dict(),
        "cluster_run_ref": run.ref,
        "preflight": {
            "ok": True,
            "checks": [
                {"name": item.name, "level": item.level, "passed": bool(item.passed)}
                for item in preflight.checks
            ],
        },
    }


def execute_cluster_submit(context: ClusterStageContext, job: dict[str, Any], run_ref: str) -> dict[str, Any]:
    owner = _owner(job)
    lease = context.run_store.resolve_private(run_ref, owner=owner)
    config = context.config_loader(lease.public.project)
    from core.cluster import submit_cluster_job

    submitted = submit_cluster_job(lease.config_path, config, dry_run=False)
    if int(submitted.returncode or 0) != 0:
        context.run_store.update_state(run_ref, owner=owner, state="failed")
        raise ClusterStageExecutionError("Cluster submission failed")
    external = _external_job_id(str(submitted.stdout or ""), run_ref)
    run = context.run_store.mark_submitted(
        run_ref, owner=owner, external_job_id=external, submit_mode=str(submitted.mode or "")
    )
    return {"cluster_run": run.to_dict(), "cluster_run_ref": run.ref, "state": "submitted"}


def execute_cluster_collect(
    context: ClusterStageContext,
    job: dict[str, Any],
    run_ref: str,
    *,
    cancelled: Callable[[], bool] = lambda: False,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    owner = _owner(job)
    lease = context.run_store.resolve_private(run_ref, owner=owner)
    config = context.config_loader(lease.public.project)
    timeout = int(((job.get("spec") or {}).get("simulation") or {}).get("timeout_minutes") or 0)
    if timeout <= 0:
        timeout = int((config.get("cluster") or {}).get("timeout_min") or 120)
    deadline = context.now_fn() + max(timeout + 10, 15) * 60
    query = lease.public.external_job_id or run_ref
    state = "running"
    summary: dict[str, Any] = {}
    from core.cluster import get_cluster_web_status, inspect_cluster_job

    context.run_store.update_state(run_ref, owner=owner, state="running")
    while context.now_fn() < deadline:
        if cancelled():
            context.run_store.update_state(run_ref, owner=owner, state="cancelled")
            result = context.run_store.finalize_result(
                run_ref, owner=owner, state="cancelled", files=(),
                summary={"status": "cancelled"}, physical_root=lease.output_location,
            )
            return {"cluster_run_ref": run_ref, "result": result.to_dict(), "result_ref": result.ref}
        info = get_cluster_web_status(config, query)
        state = _terminal_state(info)
        # Some Cluster V2.0 deployments return a submission success flag
        # (commonly ``1``) instead of the durable job id, and the official
        # page may already have removed the task row by the next poll.  The
        # controlled job directory is still authoritative: result.ini is
        # written only after the worker finishes its task.
        if state == "running" and not list(info.get("tasks") or []):
            inspected_probe = inspect_cluster_job(lease.job_dir)
            inspected_state = str(inspected_probe.get("state") or "")
            if inspected_state == "finished-success":
                state = "succeeded"
            elif inspected_state == "finished-failed":
                state = "failed"
            if state in {"succeeded", "failed"}:
                summary = {
                    "task_count": int(inspected_probe.get("success_count") or 0)
                    + int(inspected_probe.get("fail_count") or 0),
                    "finished_count": int(inspected_probe.get("success_count") or 0),
                    "failed_count": int(inspected_probe.get("fail_count") or 0),
                }
        if state in {"succeeded", "failed"}:
            if not summary:
                summary = _public_cluster_summary(info)
            break
        sleep_fn(15.0)
    else:
        # A local polling deadline must never lie that the remote job failed.
        raise ClusterStageExecutionError("Cluster job is still running after the observation window")

    inspected = inspect_cluster_job(lease.job_dir)
    files = [str(item.get("relative_path") or "") for item in inspected.get("files", [])]
    if not files:
        files = [str(item.get("relative_path") or "") for key in ("output_mf4", "logs", "result_files") for item in inspected.get(key, [])]
    result = context.run_store.finalize_result(
        run_ref,
        owner=owner,
        state=state,
        files=[item for item in files if item],
        summary={
            **summary,
            "file_count": int(inspected.get("file_count") or 0),
            "success_count": int(inspected.get("success_count") or 0),
            "fail_count": int(inspected.get("fail_count") or 0),
            "errors": list(inspected.get("error_summary") or [])[:6],
        },
        physical_root=lease.output_location,
    )
    return {"cluster_run_ref": run_ref, "result": result.to_dict(), "result_ref": result.ref}


def _apply_existing_cluster_profile_defaults(
    config: dict[str, Any], job: dict[str, Any]
) -> None:
    """Apply hidden radar/mounting defaults for one exact existing runtime.

    These are administrator-owned project adaptation details. They must not
    become user YAML fields, but an existing Selena folder and Runtime pair
    can deterministically select the matching legacy Cluster profile.
    """
    selena = dict((job.get("spec") or {}).get("selena") or {})
    if str(selena.get("source") or "") != "existing":
        return
    existing = _normalized_path(str(selena.get("existing_path") or ""))
    runtime = _normalized_path(str(selena.get("runtime_xml") or ""))
    if not existing or not runtime:
        return
    matches: list[dict[str, Any]] = []
    for raw_profile in (config.get("cluster") or {}).get("profiles") or []:
        profile = dict(raw_profile or {})
        profile_exe = _normalized_path(str(profile.get("selena_exe") or ""))
        profile_runtime = _normalized_path(str(profile.get("runtime_xml") or ""))
        profile_folder = profile_exe.rsplit("/", 1)[0] if "/" in profile_exe else ""
        if profile_folder == existing and profile_runtime == runtime:
            matches.append(profile)
    if len(matches) > 1:
        raise ClusterStageExecutionError(
            "Existing Selena matches multiple internal Cluster profiles"
        )
    if not matches:
        return
    profile = matches[0]
    simulation = config.setdefault("simulation", {})
    if str(profile.get("source") or "").strip():
        simulation["source"] = str(profile["source"])
    if str(profile.get("mounting_position") or "").strip():
        simulation["mounting_position"] = str(profile["mounting_position"])


def _normalized_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/").casefold()


def build_public_run_manifest(job: dict[str, Any], result: ClusterResultRef) -> dict[str, Any]:
    decisions = dict((job.get("resolved_spec") or {}).get("decisions") or {})
    dataset = dict((decisions.get("data") or {}).get("dataset") or {})
    bundle = dict((decisions.get("selena") or {}).get("runtime_bundle") or {})
    if not str(dataset.get("id") or "").startswith("dataset:sha256:"):
        raise ClusterStageExecutionError("DatasetRef is unavailable for manifest")
    if not str(bundle.get("id") or "").startswith("selena-bundle:sha256:"):
        raise ClusterStageExecutionError("Runtime Bundle reference is unavailable for manifest")
    return {
        "schema_version": "radar-sim.run-manifest/2.0",
        "job_id": str(job.get("job_id") or ""),
        "status": result.state,
        "config_fingerprint": str((job.get("payload") or {}).get("spec_hash") or ""),
        "runtime_bundle_id": str(bundle["id"]),
        "dataset_id": str(dataset["id"]),
        "cluster_run_ref": result.run_ref,
        "result_ref": result.ref,
        "files": list(result.files),
        "summary": dict(result.summary),
        "created_at": result.created_at,
    }


def _owner(job: dict[str, Any]) -> str:
    return normalize_user(str(job.get("owner") or (job.get("metadata") or {}).get("owner") or ""))


def _bundle(context: ClusterStageContext, job: dict[str, Any]) -> RuntimeBundleRecord:
    decision = dict(((job.get("resolved_spec") or {}).get("decisions") or {}).get("selena") or {})
    bundle_id = str((decision.get("runtime_bundle") or {}).get("id") or "")
    if not bundle_id:
        raise ClusterStageExecutionError("Runtime Bundle is not resolved")
    return context.runtime_catalog.get(bundle_id)


def _project(context: ClusterStageContext, job: dict[str, Any]) -> str:
    return _bundle(context, job).internal_project


def _dataset(context: ClusterStageContext, job: dict[str, Any], *, owner: str) -> DatasetRef:
    decision = dict(((job.get("resolved_spec") or {}).get("decisions") or {}).get("data") or {})
    dataset_id = str((decision.get("dataset") or {}).get("id") or "")
    if not dataset_id:
        data_path = str(((job.get("spec") or {}).get("data") or {}).get("path") or "")
        if data_path.startswith("dataset://"):
            dataset_id = dataset_id_from_uri(data_path)
    if not dataset_id:
        raise ClusterStageExecutionError("DatasetRef is not resolved")
    return context.dataset_catalog.get(dataset_id, owner=owner)


def _resolve_config_asset(
    context: ClusterStageContext,
    registry: SharedNamespaceRegistry,
    owner: str,
    kind: str,
    value: object,
) -> Path:
    text = str(value or "").strip()
    if is_config_asset_ref(text):
        return context.config_assets.resolve_location(text, owner=owner, kind=kind)
    if looks_like_shared_path(text):
        path = Path(registry.resolve(text).central_probe_path)
        if path.is_file():
            return path
    raise ClusterStageExecutionError(f"{kind} must be uploaded or selected from an authorized shared path")


def _safe_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip(".-")
    if not token:
        raise ClusterStageExecutionError("control job identity is invalid")
    return token[:96]


def _external_job_id(stdout: str, fallback: str) -> str:
    lines = [line.strip() for line in str(stdout or "").splitlines() if line.strip()]
    value = lines[-1] if lines else ""
    if value.lower().startswith("value="):
        value = value.split("=", 1)[1].strip()
    value = re.sub(r"[^A-Za-z0-9_.:-]+", "", value)
    return value[:200] or fallback


def _terminal_state(info: dict[str, Any]) -> str:
    tasks = list(info.get("tasks") or [])
    states = [str(item.get("simulation_state") or "").strip().lower() for item in tasks]
    if states and all(item == "finished" for item in states):
        return "succeeded"
    if states and all(item in {"finished", "failed", "error", "aborted", "cancelled"} for item in states):
        return "failed"
    state = str(info.get("state") or "").strip().lower()
    if state in {"finished", "succeeded", "success"}:
        return "succeeded"
    if state in {"failed", "error", "aborted", "cancelled"}:
        return "failed"
    return "running"


def _public_cluster_summary(info: dict[str, Any]) -> dict[str, Any]:
    tasks = list(info.get("tasks") or [])
    states = [str(item.get("simulation_state") or "").strip().lower() for item in tasks]
    return {
        "task_count": len(tasks),
        "finished_count": sum(item == "finished" for item in states),
        "failed_count": sum(item in {"failed", "error", "aborted", "cancelled"} for item in states),
    }


def _stage_result(job: dict[str, Any], stage_type: str) -> dict[str, Any]:
    for stage in job.get("stages") or job.get("tasks") or []:
        if str(stage.get("stage_type") or "") == stage_type:
            result = dict(stage.get("result") or {})
            if str(stage.get("status") or "") not in {"succeeded", "skipped"}:
                raise ClusterStageExecutionError(f"{stage_type} stage is incomplete")
            return result
    raise ClusterStageExecutionError(f"{stage_type} stage is unavailable")


__all__ = [
    "CLUSTER_GATEWAY_AGENT_ID", "LINUX_STAGE_AGENT_ID", "ClusterStageContext",
    "ClusterStageExecutionError", "ClusterStageExecutor", "build_public_run_manifest",
    "execute_cluster_collect", "execute_cluster_environment", "execute_cluster_preflight",
    "execute_cluster_submit", "resolve_cluster_data",
]
