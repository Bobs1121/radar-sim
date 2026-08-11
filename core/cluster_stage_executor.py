"""Trusted v2 Cluster Stage execution over logical catalog references.

Physical dataset, Runtime Bundle, configuration asset, workspace and credential
paths remain inside this module and the private lease stores.  Public Stage
results contain only logical references and path-free summaries.
"""

from __future__ import annotations

import copy
import logging
import ntpath
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from core.artifact_store import ArtifactStore
from core.cluster_runs import ClusterResultRef, ClusterRunRef, ClusterRunStore
from core.config_assets import ConfigAssetStore, is_config_asset_ref
from core.datasets import (
    DatasetCatalog,
    DatasetError,
    DatasetFileRef,
    DatasetRef,
    dataset_fingerprint,
    dataset_id_from_uri,
    resolve_data_reference,
)
from core.runtime_bundle_archive import extract_runtime_bundle_archive
from core.runtime_bundle_catalog import RuntimeBundleCatalog, RuntimeBundleRecord
from core.shared_namespace import SharedNamespaceRegistry, looks_like_shared_path
from core.simulation import normalize_radar_metadata
from core.user import normalize_user
from core.local_results import ResultCatalog
from core.agent_policy import (
    LINUX_EXECUTOR_CAPABILITIES,
    PLATFORM_GATEWAY_CAPABILITIES,
    NODE_KIND_LINUX_EXECUTOR,
    NODE_KIND_PLATFORM_GATEWAY,
)

LINUX_STAGE_AGENT_ID = "linux-v2-stage-executor"
CLUSTER_GATEWAY_AGENT_ID = "cluster-v2-platform-gateway"
_CLUSTER_WORKER_SUFFIX = "-worker-"
_DEFAULT_ROLE_WORKERS = 2
_MAX_ROLE_WORKERS = 16
_LOG = logging.getLogger(__name__)


def _bounded_worker_count(value: int) -> int:
    """Normalize a role pool size to a small, explicit deployment bound."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = _DEFAULT_ROLE_WORKERS
    return min(max(count, 1), _MAX_ROLE_WORKERS)


def _role_worker_id(role_id: str, index: int) -> str:
    """Return a stable worker identity while retaining role worker zero."""
    index = int(index)
    return str(role_id) if index == 0 else f"{role_id}{_CLUSTER_WORKER_SUFFIX}{index}"


class ClusterStageExecutionError(RuntimeError):
    """Stable execution refusal without exposing private paths."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "cluster_stage_failed",
        actions: tuple[dict[str, str], ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = str(code or "cluster_stage_failed")
        self.actions = tuple(dict(item) for item in actions)


@dataclass(frozen=True)
class ClusterStageContext:
    runtime_catalog: RuntimeBundleCatalog
    runtime_store: ArtifactStore
    dataset_catalog: DatasetCatalog
    config_assets: ConfigAssetStore
    run_store: ClusterRunStore
    work_root: Path
    config_loader: Callable[[str], dict[str, Any]]
    result_catalog: ResultCatalog | None = None
    now_fn: Callable[[], float] = time.time
    # Deployment-only Linux namespace used to probe completed direct-transfer
    # objects.  It is never serialized into a public Stage result.
    server_probe_root: str | Path = ""
    # Optional owner-aware resolver (normally backed by TransferService).  It
    # may return a probe Path for one storage_ref after validating owner and
    # manifest binding.  The executor only performs stat/size checks on it.
    storage_ref_resolver: Callable[..., Any] | None = None
    # The production server may inject the TransferService itself; deriving
    # both callbacks here prevents a context construction site from silently
    # dropping owner-aware storage resolution.
    transfer_service: Any | None = None

    def __post_init__(self) -> None:
        root = Path(self.work_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        object.__setattr__(self, "work_root", root)
        service = self.transfer_service
        if service is not None:
            if self.storage_ref_resolver is None:
                resolver = getattr(service, "resolve_storage_ref", None)
                if callable(resolver):
                    object.__setattr__(self, "storage_ref_resolver", resolver)
            if not str(self.server_probe_root or "").strip():
                probe_root = getattr(service, "server_probe_root", "")
                if str(probe_root or "").strip():
                    object.__setattr__(self, "server_probe_root", str(probe_root))


class ClusterStageExecutor:
    """Two-role in-process executor for one explicit ControlService database."""

    def __init__(
        self,
        control,
        context: ClusterStageContext,
        *,
        poll_interval: float = 1.0,
        heartbeat_interval: float = 10.0,
        linux_worker_count: int = _DEFAULT_ROLE_WORKERS,
        gateway_worker_count: int = _DEFAULT_ROLE_WORKERS,
    ) -> None:
        self.control = control
        self.context = context
        self.poll_interval = max(float(poll_interval), 0.05)
        self.heartbeat_interval = max(float(heartbeat_interval), 0.05)
        self.linux_worker_count = _bounded_worker_count(linux_worker_count)
        self.gateway_worker_count = _bounded_worker_count(gateway_worker_count)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        self._threads = []
        # The role IDs remain the stable affinity IDs written by ApiV1/stage
        # binding.  Worker 0 uses that root ID for backwards compatibility;
        # additional workers have deterministic IDs and therefore retain
        # their current_task_id across a connector/service restart.
        roles = (
            (
                LINUX_STAGE_AGENT_ID,
                "Linux v2 stage executor",
                "linux",
                list(LINUX_EXECUTOR_CAPABILITIES),
                NODE_KIND_LINUX_EXECUTOR,
                self.linux_worker_count,
            ),
            (
                CLUSTER_GATEWAY_AGENT_ID,
                "Cluster v2 platform gateway",
                "gateway",
                list(PLATFORM_GATEWAY_CAPABILITIES),
                NODE_KIND_PLATFORM_GATEWAY,
                self.gateway_worker_count,
            ),
        )
        worker_ids: list[str] = []
        for role_id, role_name, platform, capabilities, node_kind, count in roles:
            for index in range(count):
                worker_id = _role_worker_id(role_id, index)
                register_worker = getattr(self.control, "register_cluster_worker", None)
                if callable(register_worker):
                    register_worker(
                        f"{role_name} worker {index}", role_id=role_id,
                        worker_id=worker_id, worker_index=index,
                        worker_count=count, platform=platform,
                        capabilities=list(capabilities), node_kind=node_kind,
                    )
                else:
                    # Keep small test/dry-run control doubles compatible.  The
                    # production ControlService exposes the server-owned
                    # registration primitive above.
                    self.control.register_agent(
                        f"{role_name} worker {index}", agent_id=worker_id,
                        platform=platform, capabilities=list(capabilities),
                        metadata={
                            "cluster_role": role_id,
                            "claim_group": role_id,
                            "cluster_worker_index": index,
                            "cluster_worker_count": count,
                        },
                        node_kind=node_kind,
                    )
                worker_ids.append(worker_id)
        for worker_id in worker_ids:
            thread = threading.Thread(
                target=self._loop, args=(worker_id,), daemon=True, name=worker_id
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
        """Run a Stage while keeping the shared Cluster role visibly online.

        Cluster stages can spend minutes preparing data or waiting for results.
        The executor loop cannot heartbeat during that blocking call, so a
        separate heartbeat is required.  Otherwise the capability endpoint
        mistakes a busy shared worker for an offline one and rejects another
        user's submission instead of queueing it.
        """
        task_id = str(task.get("task_id") or "")
        stop_heartbeat = threading.Event()

        def heartbeat() -> None:
            while not stop_heartbeat.is_set() and not self._stop.is_set():
                try:
                    self.control.heartbeat(
                        agent_id,
                        status="busy",
                        current_task_id=task_id,
                    )
                except Exception:
                    _LOG.warning(
                        "Cluster Stage heartbeat failed: agent=%s task=%s",
                        agent_id,
                        task_id,
                        exc_info=True,
                    )
                if stop_heartbeat.wait(self.heartbeat_interval):
                    break

        thread = threading.Thread(
            target=heartbeat,
            daemon=True,
            name=f"{agent_id}-heartbeat",
        )
        thread.start()
        try:
            return self._execute_one(agent_id, task)
        finally:
            stop_heartbeat.set()
            thread.join(timeout=max(1.0, self.heartbeat_interval * 2.0))

    def _execute_one(self, agent_id: str, task: dict[str, Any]) -> None:
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
                if result.get("dataset"):
                    # All-shared jobs may skip prepare_data.  Persist the
                    # metadata-only DatasetRef before later finalize/manifest
                    # stages consume the resolved specification.
                    self._record_dataset(job, result)
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
                collected = _stage_result(job, "collect_results")
                cluster_result_ref = str(
                    collected.get("cluster_result_ref")
                    or collected.get("result_ref")
                    or ""
                )
                cluster_result = self.context.run_store.get_result(
                    cluster_result_ref, owner=_owner(job)
                )
                result = {
                    "manifest": build_public_run_manifest(
                        job,
                        cluster_result,
                        result_ref=str(collected.get("result_ref") or ""),
                    )
                }
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
            _LOG.exception(
                "Cluster stage execution failed: job=%s stage=%s attempt=%s",
                str(task.get("job_id") or ""),
                stage_type,
                int(task.get("attempt_count") or 0),
            )
            expected = isinstance(exc, ClusterStageExecutionError)
            message = str(exc) if expected else "Cluster stage execution failed"
            code = exc.code if expected else "cluster_stage_failed"
            actions = list(exc.actions) if expected else []
            self.control.append_logs(task_id, [f"[executor] {stage_type} failed"], stream="stderr")
            self.control.submit_task_result(
                task_id, agent_id=agent_id, status="failed", returncode=-1,
                result={
                    "error": message,
                    "code": code,
                    "actions": actions,
                    "error_json": {"code": code, "message": message, "actions": actions},
                },
            )

    def _record_dataset(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        resolved = dict(job.get("resolved_spec") or {})
        decisions = dict(resolved.get("decisions") or {})
        route = str(result.get("data_route") or "central").strip().lower()
        code = "shared_dataset_resolved" if route == "shared" else "central_dataset_resolved"
        decisions["data"] = {
            "status": "resolved", "code": code, "route": route or "central",
            "action": "", "dataset": dict(result.get("dataset") or {}),
            "evidence": {"reason": "metadata_only_cluster_resolution" if route == "shared" else "trusted_central_resolution"},
        }
        resolved["decisions"] = decisions
        resolved["status"] = "resolved" if str((decisions.get("selena") or {}).get("status") or "") == "resolved" else "partial"
        self.control.update_resolved_spec(str(job.get("job_id") or ""), resolved)


def resolve_cluster_data(context: ClusterStageContext, job: dict[str, Any]) -> dict[str, Any]:
    """Resolve a dataset URI/shared path centrally; local drives require upload."""
    owner = _owner(job)
    spec = dict(job.get("spec") or {})
    data_path = str((spec.get("data") or {}).get("path") or "")
    if data_path.lower().startswith("dataset://"):
        try:
            dataset = context.dataset_catalog.get(dataset_id_from_uri(data_path), owner=owner)
        except (DatasetError, AttributeError):
            # A direct-transfer synthetic DatasetRef may be path-free and not
            # inserted into the legacy catalog; rehydrate its metadata instead
            # of inventing a physical catalog location.
            dataset = _dataset(context, job, owner=owner)
        return {
            "dataset": dataset.to_dict(),
            "dataset_id": dataset.id,
            "evidence_ref": "central-dataset-resolution",
        }
    project = _data_project(context, job)
    config = context.config_loader(project)
    outcome = resolve_data_reference(
        context.dataset_catalog,
        SharedNamespaceRegistry.from_config(config),
        owner=owner,
        project=project,
        data_path=data_path,
        required_signals=(),
        # Linux Cluster resolution is metadata-only.  The worker-side
        # direct-transfer/data-plane contract permits existence/size probes
        # but forbids opening/parsing MF4 bytes during prepare_data.
        metadata_only=True,
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
    transfer_resources = _transfer_resources(job)
    bundle = None
    if transfer_resources or _direct_transfer_environment_expected(job):
        # A completed direct transfer is already the Selena/data identity for
        # this Stage.  Do not require a RuntimeBundle catalog row (or inspect
        # an archive) just to check the deployment's Cluster manager.
        project = "run-config-v2"
        config = context.config_loader(project)
        bundle_id = str(
            (((job.get("resolved_spec") or {}).get("decisions") or {}).get("selena") or {})
            .get("runtime_bundle", {})
            .get("id")
            or ""
        )
    else:
        bundle = _bundle(context, job)
        project = bundle.internal_project
        bundle_id = bundle.manifest.id
        config = context.config_loader(project)
    from core.cluster import check_cluster_environment

    checks = check_cluster_environment(config)
    superseded = {"Profile Selena executable", "Profile runtime XML"}
    failed = [
        item for item in checks
        if item.name not in superseded
        and not bool(item.ok)
        and str(getattr(item, "severity", "error") or "error") == "error"
    ]
    if failed:
        for item in failed:
            _LOG.error(
                "Cluster environment dependency unavailable: name=%s detail=%s",
                item.name,
                str(getattr(item, "detail", "") or "unavailable"),
            )
        detail = "; ".join(
            f"{_public_environment_item_name(item)}: {_public_environment_error_detail(item)}"
            for item in failed
        )
        raise ClusterStageExecutionError(
            "Cluster environment is temporarily unavailable: " + detail,
            code="CLUSTER_ENVIRONMENT_UNAVAILABLE",
            actions=({"type": "retry_stage", "label": "Retry environment check"},),
        )
    return {
        "environment_snapshot": {
            "status": "ready",
            "node_kind": "linux_executor",
            "requirements": [
                {"name": item.name, "ok": bool(item.ok)}
                for item in checks if item.name not in superseded
            ],
            "runtime_bundle_id": bundle_id,
        }
    }


def _direct_transfer_environment_expected(job: dict[str, Any]) -> bool:
    """Recognize a project-free direct route before its first byte is copied."""

    for stage in list(job.get("stages") or job.get("tasks") or []):
        if not isinstance(stage, dict):
            continue
        if str(stage.get("stage_type") or stage.get("task_type") or "") != "environment_check":
            continue
        payload = dict(stage.get("payload") or {})
        if str(payload.get("dispatch_scope") or "") == "direct_transfer_environment":
            return True
    return False


def _public_environment_error_detail(item: Any) -> str:
    """Keep deployment paths private while preserving actionable OS errors."""
    detail = str(getattr(item, "detail", "") or "").strip()
    if (
        str(getattr(item, "name", "") or "").strip() == "Cluster submission credential"
        and detail.casefold() == "not configured"
    ):
        # The check item deliberately exposes only configured/not configured;
        # add the deployment action here without ever including the secret.
        from core.cluster import CLUSTER_KILL_PASSWORD_ENV

        return f"not configured; set {CLUSTER_KILL_PASSWORD_ENV} in the deployment environment"
    marker = "(unavailable after "
    index = detail.find(marker)
    if index >= 0:
        return detail[index + 1 :].rstrip(")")
    return "unavailable"


def _public_environment_item_name(item: Any) -> str:
    """Remove deployment paths embedded in legacy check labels."""
    name = str(getattr(item, "name", "") or "Cluster dependency").strip()
    if name.casefold().startswith("worker dependency path:"):
        return "Worker dependency path"
    return name


def execute_cluster_preflight(context: ClusterStageContext, job: dict[str, Any]) -> dict[str, Any]:
    owner = _owner(job)
    transfer_resources = _transfer_resources(job)
    # ``prepare_data`` is intentionally skippable for an all-shared job.  If
    # that leaves decisions.data absent, resolve the logical/shared reference
    # here before creating a Cluster run so every later Stage has a DatasetRef.
    resolved_data_result: dict[str, Any] | None = None
    decisions = dict((job.get("resolved_spec") or {}).get("decisions") or {})
    data_decision = dict(decisions.get("data") or {})
    data_path = str(((job.get("spec") or {}).get("data") or {}).get("path") or "").strip()
    if not str((data_decision.get("dataset") or {}).get("id") or ""):
        if data_path.lower().startswith("dataset://") or data_path.startswith(("//", "\\\\")):
            resolved_data_result = resolve_cluster_data(context, job)
            resolved = dict(job.get("resolved_spec") or {})
            resolved_decisions = dict(resolved.get("decisions") or {})
            resolved_decisions["data"] = {
                "status": "resolved",
                "code": "shared_dataset_resolved",
                "route": "shared" if not data_path.lower().startswith("dataset://") else "central",
                "action": "",
                "dataset": dict(resolved_data_result.get("dataset") or {}),
                "evidence": {"reason": "metadata_only_cluster_resolution"},
            }
            resolved["decisions"] = resolved_decisions
            job["resolved_spec"] = resolved
    dataset = _dataset(context, job, owner=owner)
    direct_runtime = bool(transfer_resources.get("runtime_bundle"))
    # Dataset/runtime_xml transfers do not invalidate a registered/shared
    # Selena bundle.  Only a direct runtime_bundle can be prepared without
    # consulting its catalog row.
    bundle = None if direct_runtime else _bundle(context, job)
    if bundle is not None:
        project = bundle.internal_project
        bundle_source = getattr(getattr(bundle, "manifest", None), "source", None)
        bundle_branch = str(getattr(bundle_source, "branch", "") or "")
        bundle_id = str(getattr(getattr(bundle, "manifest", None), "id", "") or "")
        bundle_storage_ref = str(getattr(bundle, "storage_ref", "") or "")
    else:
        # Direct resource manifests are sufficient to run this adapter.  The
        # catalog may not contain a RuntimeBundle row yet; use deployment-wide
        # Cluster infrastructure as the project-independent config identity.
        project = "run-config-v2"
        bundle_decision = dict(
            (((job.get("resolved_spec") or {}).get("decisions") or {}).get("selena") or {})
            .get("runtime_bundle", {})
        )
        bundle_branch = str(
            (dict(bundle_decision.get("source") or {}).get("branch") or "")
        ).strip()
        bundle_id = str(bundle_decision.get("id") or "").strip()
        bundle_storage_ref = str(bundle_decision.get("storage_ref") or "").strip()
    config = copy.deepcopy(context.config_loader(project))
    # Project adapters and legacy profiles may carry a historical source such
    # as RadarFC.  The public v1 YAML has no source field, so that value is not
    # user intent and must not outrank MF4 acquisition metadata.
    config["_cluster_source_explicit"] = False
    # V2 run parameters belong to the submitted task, never to product
    # recognition.  Keep the task's explicit runtime XML/Selena path before
    # removing legacy project defaults below: a shared Selena source may be
    # selected without a direct-transfer runtime_bundle resource.
    spec = dict(job.get("spec") or {})
    spec_selena = dict(spec.get("selena") or {})
    simulation = dict(spec.get("simulation") or {})
    resolved_assets = dict(
        ((job.get("resolved_spec") or {}).get("decisions") or {}).get("simulation_assets") or {}
    )
    project_simulation = config.setdefault("simulation", {})
    configured_runtime_xml = str(
        spec_selena.get("runtime_xml")
        or resolved_assets.get("runtime_xml")
        or simulation.get("runtime_xml")
        or project_simulation.get("runtime_xml")
        or dict(config.get("assets") or {}).get("runtime_xml")
        or dict(config.get("cluster") or {}).get("runtime_xml")
        or ""
    ).strip()
    configured_selena_exe = str(
        dict(config.get("cluster") or {}).get("selena_exe")
        or dict(config.get("selena") or {}).get("exe")
        or ""
    ).strip()
    for key in (
        "source",
        "mounting_position",
        "auto_detect_radar",
        "runtime_xml",
        "adapter_file",
        "matfilefilter",
        "config_template",
    ):
        project_simulation.pop(key, None)
    project_assets = config.setdefault("assets", {})
    for key in ("runtime_xml", "adapter_file", "matfilefilter", "config_template"):
        project_assets.pop(key, None)
    job_id = str(job.get("job_id") or "")
    registry = SharedNamespaceRegistry.from_config(config)
    transfer_resources = _transfer_resources(job)
    direct_refs = bool(transfer_resources)

    # A completed direct transfer is already a Cluster-visible runtime/data
    # tree.  Validate its references against the deployment probe namespace by
    # stat/size only, then hand worker-visible paths to the mature Cluster
    # packager with both copy switches disabled.
    transfer_paths: dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]] = {}
    if direct_refs:
        transfer_paths = _validate_transfer_resources(context, job, config)

    if not direct_refs:
        # Compatibility path for pre-transfer jobs.  New Cluster jobs must
        # never enter this branch: it reads/extracts the archive on Linux.
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
    else:
        # Each resource role is independent.  A direct dataset may use a
        # registered/shared Selena directory, and a direct Selena bundle may
        # use a shared/registered dataset.  Do not make the presence of one
        # transfer role force all other roles through the transfer path.
        runtime_entries = transfer_paths.get("runtime_bundle", [])
        entrypoint = None
        if runtime_entries:
            entrypoint = next(
                (item for item in runtime_entries if _resource_entry_role(item[1]) == "entrypoint"),
                None,
            )
            # The transfer resource entries may not carry a role; align them
            # directly by filename.  Direct runtime resources are complete
            # enough to execute even when no catalog RuntimeBundle exists.
            # A manifest can be supplied without role annotations.  Selena's
            # executable is selected case-insensitively from all entries.
            if entrypoint is None:
                exe_candidates = [
                    item for item in runtime_entries
                    if Path(str(_entry_value(item[1], "relative_path"))).name.casefold() == "selena.exe"
                ]
                if exe_candidates:
                    entrypoint = exe_candidates[0]
            if entrypoint is None:
                raise ClusterStageExecutionError(
                    "Runtime Bundle transfer manifest has no Selena executable",
                    code="CLUSTER_RUNTIME_BUNDLE_REF_UNAVAILABLE",
                )
            exe = Path(entrypoint[2])
        elif configured_selena_exe:
            # Shared/registered Selena is already worker-visible.  Keep this
            # path as a worker reference; do not resolve or archive its bytes
            # on Linux.
            exe = Path(configured_selena_exe)
        else:
            raise ClusterStageExecutionError(
                "Runtime Bundle reference is unavailable",
                code="CLUSTER_RUNTIME_BUNDLE_REF_UNAVAILABLE",
            )

        # Runtime XML is a first-class resource, independent of the Selena
        # executable directory.  Prefer its own direct-transfer entry; old
        # manifests that bundled runtime_config remain a compatibility
        # fallback only.
        runtime_xml_entries = transfer_paths.get("runtime_xml", [])
        if runtime_xml_entries:
            runtime_xml = Path(runtime_xml_entries[0][2])
        elif configured_runtime_xml:
            runtime_xml = Path(configured_runtime_xml)
        else:
            runtime_xml_entry = next(
                (
                    item for item in runtime_entries
                    if _resource_entry_role(item[1]) == "runtime_config"
                    or str(_entry_value(item[1], "relative_path")).casefold().endswith(".xml")
                ),
                None,
            ) if runtime_entries else None
            if runtime_xml_entry is None:
                raise ClusterStageExecutionError(
                    "Runtime XML reference is unavailable",
                    code="CLUSTER_RUNTIME_XML_REF_UNAVAILABLE",
                )
            runtime_xml = Path(runtime_xml_entry[2])

    adapter_value = str(
        resolved_assets.get("adapter_file") or simulation.get("adapter_file") or ""
    ).strip()
    if transfer_paths.get("adapter"):
        adapter = Path(transfer_paths["adapter"][0][2])
    else:
        adapter = (
            _resolve_config_asset(context, registry, owner, "adapter", adapter_value)
            if adapter_value
            else None
        )
    mat_filter_value = (
        resolved_assets["mat_filter"]
        if "mat_filter" in resolved_assets
        else simulation.get("mat_filter", "")
    )
    if transfer_paths.get("mat_filter"):
        mat_filter = Path(transfer_paths["mat_filter"][0][2])
    else:
        mat_filter = (
            _resolve_config_asset(
                context,
                registry,
                owner,
                "mat_filter",
                mat_filter_value,
            )
            if str(mat_filter_value or "").strip()
            else None
        )
    direct_dataset = bool(transfer_paths.get("dataset"))
    if direct_dataset:
        data_location = _dataset_worker_root(transfer_paths["dataset"])
    else:
        data_location = context.dataset_catalog.resolve_location(dataset.id, owner=owner)

    config.setdefault("_meta", {})["project"] = project
    # The bundle identity is trace metadata only.  It must not activate
    # config/projects/<name>/signals.yaml or any other product contract.
    config["_project_independent_execution"] = True
    config.setdefault("paths", {})["build_output"] = str(exe.parent)
    config.setdefault("selena", {})["exe_pattern"] = "{executable_name}"
    config["selena"]["executable_name"] = exe.name
    if direct_refs:
        config.setdefault("cluster", {})["selena_exe"] = str(exe)
    config.setdefault("build", {})["selena_branch"] = bundle_branch
    sim = config.setdefault("simulation", {})
    dataset_radar = _dataset_radar_metadata(transfer_resources.get("dataset", []))
    if dataset_radar:
        # Direct-transfer metadata was derived on the data-owning SDK/Agent.
        # Linux only projects the validated values into Config.cfg; it never
        # opens the worker-visible MF4 to rediscover them.
        sim["source"] = dataset_radar["source"]
        sim["mounting_position"] = dataset_radar["mounting_position"]
    sim["runtime_xml"] = str(runtime_xml)
    sim["adapter_file"] = str(adapter) if adapter is not None else ""
    sim["matfilefilter"] = str(mat_filter) if mat_filter is not None else ""
    sim["input_mf4"] = str(data_location)
    if direct_refs:
        # Tell the existing Cluster adapter that all bytes are already in the
        # worker-visible data plane.  It must not copy assets, infer a radar
        # source by opening MF4, or inspect a project branch on Linux.
        config["_cluster_zero_copy"] = True
        config["_cluster_skip_mf4_probe"] = True
        config["_cluster_source_explicit"] = True

    if direct_refs:
        # Direct-transfer references are already validated by the manifest and
        # bounded probe.  Running the legacy preflight would read MF4 bytes (or
        # inspect project branches) on Linux, so keep only an explicit
        # metadata-level diagnostic marker.
        preflight = type("TransferPreflight", (), {"ok": True, "checks": ()})()
    else:
        from core.preflight import run_preflight
        # Compatibility path only.  New Cluster direct-transfer jobs skip this
        # body entirely so Linux never parses a potentially huge MF4.
        preflight = run_preflight(config)

    from core.cluster import prepare_cluster_job
    package = prepare_cluster_job(
        config,
        input_path=str(data_location),
        run_id=_safe_token(job_id),
        copy_data=False if direct_dataset or dataset.source_kind == "shared_path" else True,
        copy_selena=False if direct_refs else True,
    )
    local_job_root = Path(package.manifest_path).parent
    if not bundle_id:
        # A direct transfer can arrive before the catalog synthetic decision
        # is persisted.  Keep the private ClusterRun lease valid without
        # inventing a public RuntimeBundle body.
        transfer_id = str(
            (transfer_resources.get("runtime_bundle") or transfer_resources.get("runtime_xml") or [{}])[0]
            .get("transfer_id")
            or "direct-runtime"
        )
        token = re.sub(r"[^A-Za-z0-9_.:-]+", "-", transfer_id).strip("-.")[:120] or "direct-runtime"
        bundle_id = "direct-runtime:" + token
    if not bundle_storage_ref:
        bundle_storage_ref = "shared://selena-bundles/direct/" + re.sub(
            r"[^A-Za-z0-9_.-]+", "-", bundle_id
        ).strip("-.")
    run = context.run_store.create_run(
        owner=owner,
        control_job_id=job_id,
        project=project,
        dataset_id=dataset.id,
        artifact_id=bundle_id,
        artifact_storage_ref=bundle_storage_ref,
        profile=package.profile,
        job_dir=str(local_job_root),
        config_path=package.config_path,
        output_location=str(local_job_root / "output"),
    )
    result_payload = {
        "cluster_run": run.to_dict(),
        "cluster_run_ref": run.ref,
        "preflight": {
            # Static inspection is diagnostic only.  Selena/Cluster result
            # collection is the execution truth and decides the terminal job
            # status, so a best-effort mismatch never blocks dispatch here.
            "ok": True,
            "diagnostic_ok": bool(preflight.ok),
            "dispatch_blocked": False,
            "checks": [
                {
                    "name": item.name,
                    "level": item.level,
                    "passed": bool(item.passed),
                    "detail": item.detail,
                }
                for item in preflight.checks
            ],
        },
    }
    if resolved_data_result is not None:
        # Path-free DatasetRef metadata lets the owning executor persist the
        # data decision when prepare_data was skipped by the all-shared DAG.
        result_payload["dataset"] = dict(resolved_data_result.get("dataset") or {})
        result_payload["data_route"] = "shared" if not data_path.lower().startswith("dataset://") else "central"
    return result_payload


def execute_cluster_submit(context: ClusterStageContext, job: dict[str, Any], run_ref: str) -> dict[str, Any]:
    owner = _owner(job)
    lease = context.run_store.resolve_private(run_ref, owner=owner)
    config = context.config_loader(lease.public.project)
    from core.cluster import submit_cluster_job

    submitted = submit_cluster_job(lease.config_path, config, dry_run=False)
    if int(submitted.returncode or 0) != 0:
        # A transport or manager refusal happened before we received a durable
        # external job id.  Keep the private run in ``prepared`` so retrying
        # this Stage never rebuilds Selena or repackages the job.
        detail = str(submitted.stderr or submitted.stdout or "").strip()
        _LOG.error(
            "Cluster submission did not return a job id: run=%s mode=%s returncode=%s detail=%s",
            run_ref,
            str(submitted.mode or ""),
            int(submitted.returncode or 0),
            detail or "(empty)",
        )
        lowered = detail.casefold()
        transport_tokens = (
            "timed out", "timeout", "connection refused", "network is unreachable",
            "no route to host", "name or service not known", "temporary failure",
            "urlopen error", "连接超时", "无法访问",
        )
        unavailable = any(token in lowered for token in transport_tokens)
        raise ClusterStageExecutionError(
            (
                "Cluster gateway is temporarily unavailable; retry this stage without recompiling"
                if unavailable
                else "Cluster rejected the submission; retry this stage after the gateway is available"
            ),
            code=("CLUSTER_GATEWAY_UNREACHABLE" if unavailable else "CLUSTER_SUBMISSION_REJECTED"),
            actions=({"type": "retry_stage", "label": "Retry Cluster submission"},),
        )
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
    # XML-RPC addSimulation returns the number of created tasks on this
    # deployment (for example ``12``), not the durable Cluster job id.  The
    # generated Config path is unique and lets the official status page resolve
    # the actual job (for example ``10357``) without exposing that detail to the
    # user contract.
    query = str(PureWindowsPath(lease.config_path).parent)
    expected_count = _expected_cluster_task_count(job)
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
        # Always probe the controlled job directory when the web status looks
        # terminal *or* when it has no task rows.  The Cluster manager may
        # report all tasks as "finished" even when Selena returned a non-zero
        # exit code; result.ini is the authoritative success indicator.
        should_probe = (
            state == "running" and not list(info.get("tasks") or [])
        ) or state == "succeeded"
        if should_probe:
            inspected_probe = inspect_cluster_job(lease.job_dir)
            inspected_state = str(inspected_probe.get("state") or "")
            finished_probe = int(inspected_probe.get("success_count") or 0) + int(
                inspected_probe.get("fail_count") or 0
            )
            complete_probe = not expected_count or finished_probe >= expected_count
            if inspected_state == "finished-success" and complete_probe:
                state = "succeeded"
            elif inspected_state == "finished-failed" and complete_probe:
                state = "failed"
            elif state == "succeeded" and inspected_state == "finished-failed":
                # Web status said succeeded but result.ini says failed.
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
    finished_count = int(inspected.get("success_count") or 0) + int(
        inspected.get("fail_count") or 0
    )
    if expected_count and finished_count < expected_count:
        raise ClusterStageExecutionError(
            "Cluster results are incomplete; collection can be retried without rerunning simulation"
        )
    files = [str(item.get("relative_path") or "") for item in inspected.get("files", [])]
    if not files:
        files = [str(item.get("relative_path") or "") for key in ("output_mf4", "logs", "result_files") for item in inspected.get(key, [])]
    files = _dedupe_relative_paths(files)
    errors = list(inspected.get("error_summary") or [])[:6]
    output_files = [
        str(item.get("relative_path") or "")
        for item in inspected.get("output_mf4", [])
        if int(item.get("size") or 0) > 0
    ]
    if state == "succeeded" and not output_files:
        state = "failed"
        message = "Cluster worker produced no simulation output MF4"
        if message not in errors:
            errors.append(message)
    # The Cluster manager may report all tasks as "finished" even when Selena
    # itself returned a non-zero exit code (e.g. signal-not-found errors that
    # still produce a partial MF4).  Trust the authoritative result.ini-based
    # inspection over the coarse web-status polling verdict.
    inspected_state = str(inspected.get("state") or "")
    inspected_fail_count = int(inspected.get("fail_count") or 0)
    if state == "succeeded" and (
        inspected_state == "finished-failed" or inspected_fail_count > 0
    ):
        state = "failed"
        success_count = int(inspected.get("success_count") or 0)
        message = (
            f"Cluster workers finished but {inspected_fail_count} of "
            f"{success_count + inspected_fail_count} tasks reported simulation failure"
        )
        if message not in errors:
            errors.append(message)
    public_result_ref = ""
    if state in {"succeeded", "failed"} and context.result_catalog is not None and files:
        # Publish diagnostics for both successful and failed simulations before
        # making the Cluster run terminal.  A failed archive intentionally
        # contains result.ini/log/partial output so users can diagnose the
        # failure without exposing the private Cluster workspace.  If a source
        # file changes while archiving, the Stage remains retryable.
        published = context.result_catalog.publish(
            owner=owner,
            run_ref=run_ref,
            source_root=lease.job_dir,
            files=[item for item in files if item],
        )
        public_result_ref = published.ref
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
            "succeeded_input_count": int(inspected.get("success_count") or 0),
            "failed_input_count": int(inspected.get("fail_count") or 0),
            "total_input_count": int(inspected.get("success_count") or 0)
            + int(inspected.get("fail_count") or 0),
            "input_results": _cluster_input_results(inspected, lease.job_dir),
            "errors": errors,
        },
        physical_root=lease.job_dir,
    )
    if state == "succeeded" and not public_result_ref:
        public_result_ref = result.ref
    return {
        "cluster_run_ref": run_ref,
        "cluster_result_ref": result.ref,
        "result": result.to_dict(),
        "result_ref": public_result_ref,
    }


def _expected_cluster_task_count(job: dict[str, Any]) -> int:
    decisions = dict((job.get("resolved_spec") or {}).get("decisions") or {})
    dataset = dict((decisions.get("data") or {}).get("dataset") or {})
    try:
        return max(int(dataset.get("file_count") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _dedupe_relative_paths(values: list[str]) -> list[str]:
    """Keep one portable result path when inspection categories overlap."""
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        unique.append(text)
    return unique


def _cluster_input_results(inspected: dict[str, Any], job_root: str) -> list[dict[str, Any]]:
    """Build path-safe per-input outcomes from Cluster result.ini files.

    Cluster workers do not consistently write the original MF4 name into
    ``result.ini``.  When it is absent, the result-directory relative path is
    the stable logical task identity; it is preferable to guessing a source
    filename or leaking a private worker path.
    """
    raw_results = [item for item in inspected.get("task_results") or [] if isinstance(item, dict)]
    if not raw_results:
        return []
    output_files = [
        str(item.get("relative_path") or "").replace("\\", "/").strip("/")
        for item in inspected.get("output_mf4") or []
        if str(item.get("relative_path") or "").strip()
    ]
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_results, 1):
        result_relative = _cluster_logical_relative_path(
            raw.get("relative_path") or raw.get("path"),
            job_root,
            fallback=f"cluster-task-{index}/result.ini",
        )
        task_relative = result_relative.rsplit("/", 1)[0] if "/" in result_relative else result_relative
        source_hint = _cluster_source_hint(raw, job_root)
        input_relative = source_hint or task_relative or f"cluster-task-{index}"
        output_relative = _matching_cluster_output(task_relative, output_files)
        success_value = str(raw.get("successfull") or raw.get("successful") or "").strip().lower()
        status = "succeeded" if success_value in {"1", "true", "success", "succeeded"} else (
            "failed" if success_value in {"0", "false", "failure", "failed"} else "unknown"
        )
        returncode = _cluster_returncode(raw)
        error_code = str(raw.get("error_code") or "").strip()
        if status == "failed" and not error_code:
            error_code = "cluster_simulation_failed"
        rows.append(
            {
                "index": index,
                "input_relative_path": input_relative,
                "result_relative_path": result_relative,
                "output_relative_path": output_relative,
                "status": status,
                "returncode": returncode,
                "error_code": error_code,
            }
        )
    return rows[:200]


def _cluster_logical_relative_path(value: object, job_root: str, *, fallback: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return fallback
    try:
        candidate = Path(text)
        root = Path(str(job_root or "")).resolve(strict=False)
        if candidate.is_absolute():
            relative = candidate.resolve(strict=False).relative_to(root)
            normalized = relative.as_posix().strip("/")
            if normalized and ".." not in normalized.split("/"):
                return normalized
    except (OSError, ValueError):
        pass
    # A non-private relative path from the Cluster inspector is safe to keep.
    if not PureWindowsPath(text).is_absolute() and not Path(text).is_absolute() and ".." not in text.split("/"):
        return text.strip("/") or fallback
    return fallback


def _cluster_source_hint(raw: dict[str, Any], job_root: str) -> str:
    for key in ("input_relative_path", "input", "input_path", "data_path", "source"):
        value = str(raw.get(key) or "").strip()
        if not value:
            continue
        text = value.replace("\\", "/")
        if Path(text).is_absolute() or PureWindowsPath(text).is_absolute():
            # Keep only the filename as a user-facing hint; never return a
            # drive, UNC root or private Cluster path.
            name = PureWindowsPath(text).name or Path(text).name
            return name[:240]
        if ".." not in text.split("/"):
            return text.strip("/")[:240]
    return ""


def _matching_cluster_output(task_relative: str, output_files: list[str]) -> str:
    parent = task_relative.rsplit("/", 1)[0].casefold() if "/" in task_relative else ""
    for candidate in output_files:
        candidate_parent = candidate.rsplit("/", 1)[0].casefold() if "/" in candidate else ""
        if parent and candidate_parent == parent:
            return candidate
    return ""


def _cluster_returncode(raw: dict[str, Any]) -> int | None:
    for key in ("returncode", "Returncode", "return_code", "ReturnCode"):
        value = raw.get(key)
        if value in (None, ""):
            continue
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None
    return None


def build_public_run_manifest(
    job: dict[str, Any],
    result: ClusterResultRef,
    *,
    result_ref: str | None = None,
) -> dict[str, Any]:
    decisions = dict((job.get("resolved_spec") or {}).get("decisions") or {})
    dataset = dict((decisions.get("data") or {}).get("dataset") or {})
    bundle = dict((decisions.get("selena") or {}).get("runtime_bundle") or {})
    if not str(dataset.get("id") or "").startswith("dataset:sha256:"):
        raise ClusterStageExecutionError("DatasetRef is unavailable for manifest")
    if not str(bundle.get("id") or "").startswith("selena-bundle:sha256:"):
        raise ClusterStageExecutionError("Runtime Bundle reference is unavailable for manifest")
    status = result.state
    summary = dict(result.summary)
    input_results = list(summary.pop("input_results", []) or [])
    if _summary_is_partial(summary, input_results, result.files):
        status = "partial"
    elif status == "succeeded" and _summary_reports_failure(summary):
        # Historical ClusterResult rows are immutable.  If an older collector
        # persisted a contradictory state, the public manifest must still tell
        # the truth using the structured worker counts.
        status = "failed"
    return {
        "schema_version": "radar-sim.run-manifest/2.0",
        "job_id": str(job.get("job_id") or ""),
        "status": status,
        "config_fingerprint": str((job.get("payload") or {}).get("spec_hash") or ""),
        "runtime_bundle_id": str(bundle["id"]),
        "dataset_id": str(dataset["id"]),
        "cluster_run_ref": result.run_ref,
        "result_ref": result.ref if result_ref is None else result_ref,
        "files": list(result.files),
        "summary": summary,
        "input_results": input_results,
        "created_at": result.created_at,
    }


def _summary_reports_failure(summary: dict[str, Any]) -> bool:
    """Return true only for explicit positive structured failure counts."""
    for key in ("fail_count", "failed_count", "failed_input_count"):
        value = summary.get(key)
        try:
            if int(value) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _summary_is_partial(
    summary: dict[str, Any], input_results: list[dict[str, Any]], files: tuple[str, ...]
) -> bool:
    try:
        succeeded = int(summary.get("succeeded_input_count") or summary.get("success_count") or 0)
        failed = int(summary.get("failed_input_count") or summary.get("fail_count") or 0)
    except (TypeError, ValueError):
        succeeded = failed = 0
    return succeeded > 0 and failed > 0 and bool(files) and bool(input_results or succeeded + failed)


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


def _data_project(context: ClusterStageContext, job: dict[str, Any]) -> str:
    """Resolve the hidden project before a concurrent Selena build finishes."""
    if _transfer_resources(job):
        # Direct resources do not require a catalog RuntimeBundle row.  Shared
        # namespace resolution still uses deployment-wide infrastructure.
        return "run-config-v2"
    decision = dict(((job.get("resolved_spec") or {}).get("decisions") or {}).get("selena") or {})
    if str((decision.get("runtime_bundle") or {}).get("id") or ""):
        return _project(context, job)
    recognition = dict(_stage_result(job, "resolve_spec").get("recognition") or {})
    project = str(recognition.get("internal_project") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project):
        raise ClusterStageExecutionError("Dataset project is not resolved")
    return project


def _dataset(context: ClusterStageContext, job: dict[str, Any], *, owner: str) -> DatasetRef:
    decision = dict(((job.get("resolved_spec") or {}).get("decisions") or {}).get("data") or {})
    metadata = dict(decision.get("dataset") or {})
    dataset_id = str(metadata.get("id") or "")
    if not dataset_id:
        data_path = str(((job.get("spec") or {}).get("data") or {}).get("path") or "")
        if data_path.startswith("dataset://"):
            dataset_id = dataset_id_from_uri(data_path)
    if not dataset_id:
        metadata = {}
    if dataset_id:
        try:
            return context.dataset_catalog.get(dataset_id, owner=owner)
        except (DatasetError, AttributeError):
            # Direct-transfer synthetic decisions intentionally do not insert
            # a fake physical location into the legacy catalog.  Rehydrate
            # their path-free DatasetRef metadata directly instead.
            if metadata:
                try:
                    return _dataset_ref_from_metadata(metadata, owner=owner)
                except ClusterStageExecutionError:
                    # A partial decision may carry only the logical id while
                    # the completed transfer entries contain the file list.
                    # Fall through and reconstruct from those entries.
                    pass
    transfer_resources = _transfer_resources(job)
    if transfer_resources.get("dataset"):
        return _dataset_ref_from_transfer_resources(
            transfer_resources["dataset"],
            owner=owner,
            project="run-config-v2",
            dataset_id=dataset_id,
        )
    raise ClusterStageExecutionError("DatasetRef is not resolved")


def _dataset_ref_from_metadata(metadata: dict[str, Any], *, owner: str) -> DatasetRef:
    raw_files = list(metadata.get("files") or ())
    files: list[DatasetFileRef] = []
    for raw in raw_files:
        if isinstance(raw, DatasetFileRef):
            files.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        checksum = str(raw.get("checksum") or raw.get("sha256") or "").strip().lower()
        if checksum and not checksum.startswith("sha256:"):
            checksum = "sha256:" + checksum
        files.append(
            DatasetFileRef(
                relative_path=str(raw.get("relative_path") or ""),
                size=int(raw.get("size") or 0),
                checksum=checksum,
                signal_status=str(raw.get("signal_status") or "not-scanned"),
                mtime_ns=int(raw.get("mtime_ns") or 0),
                storage_ref=str(raw.get("storage_ref") or raw.get("target_logical_ref") or ""),
            )
        )
    if not files:
        raise ClusterStageExecutionError("DatasetRef metadata is incomplete")
    fingerprint = str(metadata.get("source_fingerprint") or dataset_fingerprint(files)).lower()
    dataset_id = str(metadata.get("id") or "").strip()
    if not dataset_id:
        import hashlib
        dataset_id = "dataset:sha256:" + hashlib.sha256(
            (str(owner) + "\0" + fingerprint).encode("utf-8")
        ).hexdigest()
    storage_ref = str(metadata.get("storage_ref") or "").strip()
    if not storage_ref:
        storage_ref = "cluster-staging://dataset/sha256/" + fingerprint.split(":", 1)[-1]
    return DatasetRef(
        id=dataset_id,
        project=str(metadata.get("project") or "run-config-v2"),
        owner=str(metadata.get("owner") or owner),
        source_kind=str(metadata.get("source_kind") or "direct_transfer"),
        accessibility=str(metadata.get("accessibility") or "cluster"),
        storage_ref=storage_ref,
        files=tuple(files),
        created_at=float(metadata.get("created_at") or time.time()),
        source_fingerprint=fingerprint,
    )


def _dataset_ref_from_transfer_resources(
    resources: list[dict[str, Any]],
    *,
    owner: str,
    project: str,
    dataset_id: str = "",
) -> DatasetRef:
    raw_files: list[dict[str, Any]] = []
    for resource in resources:
        for raw in list(resource.get("entries") or []):
            if isinstance(raw, dict):
                raw_files.append(raw)
    metadata = {
        "id": dataset_id,
        "project": project,
        "owner": owner,
        "source_kind": "direct_transfer",
        "accessibility": "cluster",
        "files": raw_files,
        "created_at": time.time(),
    }
    return _dataset_ref_from_metadata(metadata, owner=owner)


def _dataset_radar_metadata(resources: list[dict[str, Any]]) -> dict[str, str]:
    """Return the first validated radar projection from dataset resources."""

    for resource in resources:
        if not isinstance(resource, dict):
            continue
        for key in ("radar", "radar_metadata", "source_fingerprints"):
            metadata = normalize_radar_metadata(resource.get(key))
            if metadata:
                return metadata
    return {}


_TRANSFER_RESOURCE_ROLES = ("dataset", "runtime_bundle", "runtime_xml", "mat_filter", "adapter")


def _transfer_resources(job: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Read the bounded ``resolved_spec.decisions.transfers`` shape.

    The control plane owns conversion from TransferManifest to this shape.  A
    resource is either one mapping or a list of mappings (for multiple assets
    with the same role); arbitrary nested dictionaries are intentionally not
    interpreted here.
    """

    decisions = dict((job.get("resolved_spec") or {}).get("decisions") or {})
    transfers = decisions.get("transfers")
    if not isinstance(transfers, dict):
        return {}
    raw_resources = transfers.get("resources")
    if not isinstance(raw_resources, dict):
        return {}
    resources: dict[str, list[dict[str, Any]]] = {}
    for role in _TRANSFER_RESOURCE_ROLES:
        raw = raw_resources.get(role)
        if isinstance(raw, dict):
            resources[role] = [dict(raw)]
        elif isinstance(raw, list):
            values = [dict(item) for item in raw if isinstance(item, dict)]
            if values:
                resources[role] = values
    return resources


def _transfer_probe_root(context: ClusterStageContext, config: dict[str, Any]) -> str:
    value = str(context.server_probe_root or "").strip()
    if value:
        return value
    cluster = dict(config.get("cluster") or {})
    return str(cluster.get("server_probe_root") or cluster.get("probe_root") or "").strip()


def _resource_relative_root(resource: dict[str, Any]) -> str:
    return str(
        resource.get("relative_root")
        or resource.get("probe_relative_root")
        or resource.get("target_relative_root")
        or ""
    ).strip().replace("\\", "/").strip("/")


def _join_resource_path(root: str, relative: str) -> str:
    base = str(root or "").strip()
    suffix = str(relative or "").strip().replace("\\", "/").strip("/")
    if not base:
        return ""
    if not suffix:
        return base
    separator = "\\" if "\\" in base and "/" not in base else "/"
    return base.rstrip("\\/") + separator + suffix.replace("/", separator)


def _entry_value(entry: dict[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        value = entry.get(name)
        if value not in (None, ""):
            return value
    return default


def _resolve_probe_entry(
    context: ClusterStageContext,
    config: dict[str, Any],
    job: dict[str, Any],
    resource: dict[str, Any],
    entry: dict[str, Any],
) -> Path:
    owner = _owner(job)
    storage_ref = str(_entry_value(entry, "storage_ref", "target_logical_ref")).strip()
    expected_size = int(_entry_value(entry, "size", default=0) or 0)
    if not storage_ref:
        raise ClusterStageExecutionError(
            "Transfer manifest entry has no storage reference",
            code="CLUSTER_STORAGE_REF_INVALID",
        )
    resolver = context.storage_ref_resolver
    if resolver is not None:
        try:
            try:
                resolved = resolver(
                    storage_ref,
                    owner=owner,
                    expected_size=expected_size,
                    require_exists=True,
                )
            except TypeError:
                try:
                    # TransferService.resolve_storage_ref intentionally
                    # accepts only (storage_ref, *, owner, require_exists).
                    resolved = resolver(
                        storage_ref,
                        owner=owner,
                        require_exists=True,
                    )
                except TypeError:
                    try:
                        resolved = resolver(storage_ref, owner=owner)
                    except TypeError:
                        # Keep compatibility with small test/deployment
                        # resolvers that expose positional owner/size args.
                        resolved = resolver(storage_ref, owner, expected_size)
        except Exception as exc:
            raise ClusterStageExecutionError(
                "Cluster storage reference is unavailable",
                code="CLUSTER_STORAGE_UNAVAILABLE",
            ) from exc
        candidate = Path(str(resolved))
    else:
        root = _transfer_probe_root(context, config)
        relative_root = _resource_relative_root(resource)
        relative_path = str(_entry_value(entry, "relative_path")).strip().replace("\\", "/")
        if not root or not relative_path:
            raise ClusterStageExecutionError(
                "Cluster storage probe is not configured",
                code="CLUSTER_STORAGE_PROBE_UNAVAILABLE",
            )
        candidate = Path(_join_resource_path(_join_resource_path(root, relative_root), relative_path))

    # Probe is deliberately bounded to existence/size metadata.  Do not hash,
    # parse or archive the object on Linux.
    try:
        root_text = _transfer_probe_root(context, config)
        if root_text:
            root_path = Path(root_text).resolve(strict=False)
            candidate.resolve(strict=False).relative_to(root_path)
        if candidate.is_symlink() or not candidate.is_file():
            raise OSError("not a regular file")
        if int(candidate.stat().st_size) != expected_size:
            raise OSError("size mismatch")
    except (OSError, ValueError) as exc:
        raise ClusterStageExecutionError(
            "Cluster storage object is unavailable or has an unexpected size",
            code="CLUSTER_STORAGE_UNAVAILABLE",
        ) from exc
    return candidate


def _resolve_worker_entry(
    resource: dict[str, Any],
    entry: dict[str, Any],
    probe_path: Path,
    *,
    config: dict[str, Any] | None = None,
) -> str:
    relative = str(_entry_value(entry, "relative_path")).strip().replace("\\", "/")
    # Deployment may provide a worker-visible root alongside the probe root.
    # Keep this private and use it only to render Config.cfg for Windows
    # workers; it is never returned in the public Stage result.
    worker_root = str(
        resource.get("worker_root")
        or resource.get("client_target_root")
        or resource.get("target_root")
        or ""
    ).strip()
    if worker_root:
        return _join_resource_path(_join_resource_path(worker_root, _resource_relative_root(resource)), relative)
    worker_path = str(resource.get("worker_path") or resource.get("target_path") or "").strip()
    if worker_path and len(resource.get("entries") or []) == 1:
        return worker_path
    # A shared mount can use the probe path directly when the same path is
    # visible to workers.  This fallback is intentionally private and only
    # affects the generated Config.cfg.
    storage_ref = str(_entry_value(entry, "storage_ref", "target_logical_ref")).strip()
    probe_text = str(probe_path)
    # Never leak a Linux-only probe path into Config.cfg.  A UNC probe is also
    # worker-visible and can safely be reused; otherwise retain the logical ref
    # until the deployment-specific Cluster adapter expands it.
    if probe_text.startswith("\\\\") or probe_text.startswith("//"):
        return probe_text
    # The Linux executor may have a deployment mount map that translates its
    # probe root back to the UNC root consumed by Cluster Windows workers.
    # Reconstruct this mapping for every runtime entry so exe and DLLs remain
    # in one worker-visible directory even when no resource worker_root is
    # serialized in the public manifest.
    cluster = dict((config or {}).get("cluster") or {})
    for unc_prefix, mount in dict(cluster.get("linux_mount_map") or {}).items():
        mount_text = str(mount or "").rstrip("\\/")
        if mount_text and probe_text.casefold().startswith(mount_text.casefold() + os.sep):
            suffix = probe_text[len(mount_text):].replace("/", "\\")
            return str(unc_prefix).rstrip("\\/") + suffix
        if mount_text and probe_text.casefold() == mount_text.casefold():
            return str(unc_prefix).rstrip("\\/")
    return storage_ref


def _validate_transfer_resources(
    context: ClusterStageContext,
    job: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]]:
    paths: dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]] = {}
    for role, resources in _transfer_resources(job).items():
        for resource in resources:
            if str(resource.get("status") or "resolved").strip().lower() != "resolved":
                raise ClusterStageExecutionError(
                    f"Transfer manifest for {role} is not resolved",
                    code="CLUSTER_STORAGE_REF_UNAVAILABLE",
                )
            entries = resource.get("entries")
            if not isinstance(entries, list) or not entries:
                raise ClusterStageExecutionError(
                    f"Transfer manifest for {role} is empty",
                    code="CLUSTER_STORAGE_REF_INVALID",
                )
            for raw_entry in entries:
                if not isinstance(raw_entry, dict):
                    raise ClusterStageExecutionError(
                        f"Transfer manifest entry for {role} is invalid",
                        code="CLUSTER_STORAGE_REF_INVALID",
                    )
                entry = dict(raw_entry)
                probe = _resolve_probe_entry(context, config, job, resource, entry)
                worker = _resolve_worker_entry(resource, entry, probe, config=config)
                paths.setdefault(role, []).append((resource, entry, worker))
    return paths


def _resource_entry_role(entry: dict[str, Any]) -> str:
    return str(entry.get("role") or "").strip().lower()


def _match_runtime_manifest_entry(
    bundle: RuntimeBundleRecord,
    runtime_entries: list[tuple[dict[str, Any], dict[str, Any], str]],
    role: str,
) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    manifest_file = next((item for item in bundle.manifest.files if item.role == role), None)
    if manifest_file is None:
        return None
    for item in runtime_entries:
        if str(_entry_value(item[1], "relative_path")).replace("\\", "/").casefold() == manifest_file.relative_path.casefold():
            return item
    return None


def _dataset_worker_root(
    entries: list[tuple[dict[str, Any], dict[str, Any], str]],
) -> str:
    if not entries:
        return ""
    roots: list[str] = []
    first_style = str(entries[0][2] or "")
    for _resource, entry, path in entries:
        worker_path = str(path or "").strip()
        relative = str(_entry_value(entry, "relative_path")).replace("\\", "/").strip("/")
        normalized = worker_path.replace("\\", "/")
        suffix = "/" + relative
        if relative and normalized.casefold().endswith(suffix.casefold()):
            roots.append(normalized[: -len(suffix)] or "/")
        else:
            roots.append(normalized.rsplit("/", 1)[0] if "/" in normalized else normalized)
    if not roots:
        return ""
    # A direct dataset is a collection.  Use the common root of *all* entries
    # so prepare_cluster_job receives one directory containing every MF4,
    # rather than silently packaging only the first manifest entry.
    # ``os.path`` follows the executor host.  On Linux it treats a Windows
    # UNC path as a POSIX path and collapses the required two leading
    # separators (``//server/share`` -> ``/server/share``).  Select the
    # Windows path implementation explicitly for UNC/drive roots so the
    # value written to Config.cfg remains worker-valid on every host.
    windows_style = [
        path.startswith(("\\\\", "//")) or bool(ntpath.splitdrive(path.replace("/", "\\"))[0])
        for path in (str(item[2] or "").strip() for item in entries)
    ]
    if any(windows_style):
        if not all(windows_style):
            raise ValueError("worker paths mix Windows and POSIX path styles")
        common = ntpath.commonpath([root.replace("/", "\\") for root in roots])
        # ntpath keeps a trailing separator for a UNC share root.  It is not
        # part of the directory identity and would make the rendered root
        # differ from the path used for a nested entry.
        unc_drive, unc_tail = ntpath.splitdrive(common)
        if unc_drive.startswith("\\\\"):
            common = common.rstrip("\\/")
        elif not (len(unc_drive) == 2 and unc_drive[1] == ":" and unc_tail in {"\\", "/"}):
            common = common.rstrip("\\/")
        if first_style.startswith("//") or "/" in first_style and "\\" not in first_style:
            common = common.replace("\\", "/")
    else:
        try:
            common = os.path.commonpath(roots)
        except ValueError:
            common = roots[0]
        common = common.replace("\\", "/")
    return common or roots[0]


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
