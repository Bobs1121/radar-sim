"""Framework-agnostic v5 `/api/v1` application service.

This module owns only API contract orchestration around ``SimulationSpec`` and
the existing ``ControlService`` store. HTTP, FastAPI, SSE transport, SDK
transport, subprocess, Git, Cluster routing, and Web concerns stay outside.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from pydantic import ValidationError

from core.control_service import ControlService, INTERNAL_V1_SCHEDULER_AGENT_ID
from core.artifact_upload_service import ArtifactUploadService, ArtifactUploadServiceError
from core.dataset_upload_service import DatasetUploadService, DatasetUploadServiceError
from core.runtime_bundle_upload_service import RuntimeBundleUploadService, RuntimeBundleUploadServiceError
from core.config_assets import ConfigAssetError, ConfigAssetStore
from core.datasets import DataResolution
from core.selena_resolver import SourceResolutionContext, apply_selena_resolution, resolve_selena
from core.spec import ProjectCatalog, SimulationSpec, UserBindings
from core.stages import (
    PlannedStage,
    StagePlan,
    plan_simulation_stages,
    plan_user_environment_requirements,
    plan_user_run_stages,
)
from core.user_config import UserRunConfig
from core.user import control_db_path_for_user, current_user, normalize_user
from core.datasets import classify_data_path
from core.cluster_stage_executor import LINUX_STAGE_AGENT_ID, CLUSTER_GATEWAY_AGENT_ID
from core.local_results import ResultCatalog, ResultCatalogError

API_VERSION = "v1"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
V1_SCHEDULER_AGENT_ID = INTERNAL_V1_SCHEDULER_AGENT_ID


class ApiV1Error(RuntimeError):
    """Stable application error mapped by HTTP/SDK adapters."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        detail: Any = None,
        actions: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = int(status_code)
        self.detail = detail if detail is not None else {}
        self.actions = list(actions or [])


@dataclass(frozen=True)
class SourceResolutionInputs:
    """Immutable application-layer snapshot for pure Selena source resolution."""

    project_catalog: ProjectCatalog
    user_bindings: UserBindings
    context: SourceResolutionContext

    def __post_init__(self) -> None:
        if not isinstance(self.project_catalog, ProjectCatalog):
            raise TypeError("project_catalog must be a ProjectCatalog")
        if not isinstance(self.user_bindings, UserBindings):
            raise TypeError("user_bindings must be UserBindings")
        if not isinstance(self.context, SourceResolutionContext):
            raise TypeError("context must be SourceResolutionContext")


SourceResolutionProvider = Callable[[str, SimulationSpec], SourceResolutionInputs]
DataResolutionProvider = Callable[[str, SimulationSpec], DataResolution]
ProjectNamesProvider = Callable[[], Iterable[str]]


class SourceResolutionProviderError(RuntimeError):
    """Stable provider failure that adapters can expose without leaking paths."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        action_type: str = "retry_source_resolution",
        action_label: str = "Retry source resolution",
    ) -> None:
        super().__init__(message)
        self.code = str(code or "source_resolution_unavailable")
        self.message = str(message or "Source resolution inputs are unavailable")
        self.status_code = int(status_code)
        self.action_type = str(action_type or "retry_source_resolution")
        self.action_label = str(action_label or "Retry source resolution")


@dataclass(frozen=True)
class ApiV1Service:
    """Application service for one user-visible `/api/v1` request scope."""

    control_service_factory: Callable[[str], ControlService] | None = None
    source_resolution_provider: SourceResolutionProvider | None = None
    data_resolution_provider: DataResolutionProvider | None = None
    artifact_upload_service_factory: Callable[[str], ArtifactUploadService] | None = None
    dataset_upload_service_factory: Callable[[str], DatasetUploadService] | None = None
    runtime_bundle_upload_service_factory: Callable[[str], RuntimeBundleUploadService] | None = None
    config_asset_store: ConfigAssetStore | None = None
    result_catalog: ResultCatalog | None = None
    project_names_provider: ProjectNamesProvider | None = None
    now_fn: Callable[[], float] = time.time

    def health(self) -> dict[str, Any]:
        return {"ok": True, "api_version": API_VERSION}

    def simulation_spec_schema(self) -> dict[str, Any]:
        return SimulationSpec.json_schema()

    def user_run_config_schema(self) -> dict[str, Any]:
        """The only new-user YAML contract; legacy SimulationSpec stays compatible."""
        return UserRunConfig.json_schema()

    def import_user_run_config_yaml(self, yaml_content: str) -> dict[str, Any]:
        try:
            config = UserRunConfig.from_yaml(str(yaml_content or ""))
        except Exception as exc:
            raise ApiV1Error(
                "invalid_run_config",
                "Simulation YAML validation failed",
                status_code=422,
                detail={"error": str(exc)},
                actions=[{"type": "fix_config", "label": "Fix the YAML fields shown in detail"}],
            ) from exc
        return {
            "valid": True,
            "config": config.to_dict(),
            "yaml_content": config.to_yaml(),
            "fingerprint": config.fingerprint(),
        }

    def export_user_run_config_yaml(self, config_payload: dict[str, Any]) -> dict[str, Any]:
        config = self._parse_user_run_config(config_payload)
        return {"yaml_content": config.to_yaml(), "fingerprint": config.fingerprint()}

    def validate_user_run_config(self, config_payload: dict[str, Any]) -> dict[str, Any]:
        config = self._parse_user_run_config(config_payload)
        return {
            "valid": True,
            "config": config.to_dict(),
            "fingerprint": config.fingerprint(),
            "environment_plan": plan_user_environment_requirements(config),
        }

    def submit_user_run(
        self,
        owner: str,
        *,
        config_payload: dict[str, Any],
        dry_run: bool = False,
        idempotency_key: str = "",
        prepared_runtime_bundle_id: str = "",
    ) -> dict[str, Any]:
        """Persist a project-free job; recognition occurs on the execution node.

        This is deliberately separate from the legacy synchronous project
        resolver.  A Linux server cannot inspect a user's Windows code path,
        so the first Stage remains queued for a trusted Agent or local full
        deployment instead of asking the user for an internal project name.
        """
        owner = self._owner(owner)
        config = self._parse_user_run_config(config_payload)
        canonical = config.to_dict()
        config_hash = config.fingerprint()
        plan = plan_user_run_stages(config)
        prepared_bundle_id = str(prepared_runtime_bundle_id or "").strip()
        if prepared_bundle_id and config.selena.source != "existing":
            raise ApiV1Error(
                "invalid_prepared_selena",
                "Prepared existing Selena can only be used with selena.source=existing",
                status_code=422,
            )
        # A shared/cloud folder may be invisible to the caller but mounted on
        # the Linux control plane.  Import it here so the V1 flow does not
        # require a Windows Agent merely to recognize an existing runtime.
        if (
            not dry_run
            and not prepared_bundle_id
            and config.selena.source == "existing"
        ):
            existing_path = self._server_visible_path(config.selena.existing_path)
            runtime_path = self._server_visible_path(config.selena.runtime_xml)
            if (
                existing_path.is_dir()
                and runtime_path.is_file()
                and self.runtime_bundle_upload_service_factory is not None
            ):
                try:
                    from core.existing_selena import import_existing_selena

                    with tempfile.TemporaryDirectory(prefix="rsim-server-existing-") as temporary:
                        imported = import_existing_selena(
                            existing_path,
                            runtime_path,
                            staging_root=Path(temporary) / "staging",
                            created_at=0.0,
                        )
                        imported_record = self.runtime_bundle_upload_service_factory(owner).import_existing(
                            owner,
                            metadata={
                                "internal_project": imported.internal_project,
                                "adapter_key": imported.adapter_key,
                                "manifest": imported.bundle.manifest.to_dict(),
                                "archive_checksum": imported.archive.checksum,
                                "archive_size": imported.archive.size,
                            },
                            archive_bytes=imported.archive.path.read_bytes(),
                        )
                    prepared_bundle_id = str(
                        (imported_record.get("runtime_bundle") or {}).get("id") or ""
                    )
                except RuntimeBundleUploadServiceError as exc:
                    raise ApiV1Error(exc.code, exc.message, status_code=exc.status_code) from exc
                except (OSError, ValueError) as exc:
                    raise ApiV1Error(
                        "invalid_existing_selena",
                        "Existing Selena folder or Runtime XML is invalid",
                        status_code=422,
                        detail={"error": str(exc)},
                    ) from exc
        request_hash = self._request_hash(
            {**canonical, "_prepared_runtime_bundle_id": prepared_bundle_id},
            dry_run=bool(dry_run),
        )
        control = self._control(owner)
        key = str(idempotency_key or "").strip()
        if key:
            existing = control.get_job_by_idempotency(owner, key)
            if existing is not None:
                if str(existing.get("request_hash") or "") != request_hash:
                    self._raise_idempotency_conflict(key)
                return self._job_response(existing)
        task_specs = plan.task_specs()
        resolved_spec = dict(plan.resolved_spec)
        requested_target = config.simulation.target
        selected_target = requested_target
        route_reason = "explicit_user_selection"
        if requested_target == "auto":
            capabilities = self.execution_capabilities(owner)["capabilities"]
            data_path = str(config.data.path)
            data_is_cluster_ready = (
                data_path.lower().startswith("dataset://")
                or classify_data_path(data_path) in {"shared", "central"}
            )
            if data_is_cluster_ready:
                # Browser/SDK uploads and shared paths are already reachable
                # from the Linux control plane.  Choosing a Windows-local run
                # here would introduce an unnecessary reverse transfer.
                selected_target = "cluster"
                route_reason = "cluster_accessible_data"
            elif capabilities["windows_full"]["available"]:
                selected_target = "local"
                route_reason = "windows_full_available"
            else:
                # A light Agent can compile/upload but never simulates locally.
                # Existing bundles also need no Windows node at all, so Cluster
                # is the safe auto fallback while its executors come online.
                selected_target = "cluster"
                route_reason = (
                    "windows_light_build_then_cluster"
                    if capabilities["windows_light"]["available"] and config.selena.source == "build"
                    else "cluster_fallback"
                )
        decisions = dict(resolved_spec.get("decisions") or {})
        decisions["execution"] = {
            "status": "selected",
            "requested_target": requested_target,
            "selected_target": selected_target,
            "reason": route_reason,
        }
        resolved_spec["decisions"] = decisions
        recognition_status = "pending_node"
        selected_runtime_bundle: dict[str, Any] | None = None
        selected_runtime_project = ""
        # Compatibility only: a trusted internal caller may still submit an
        # already registered logical id. A normal existing_path is a folder
        # and is resolved on the node that can access it.
        bundle_selector = prepared_bundle_id or (
            config.selena.existing_path
            if config.selena.existing_path.startswith("selena-bundle:sha256:")
            else ""
        )
        if config.selena.source == "existing" and bundle_selector:
            if self.runtime_bundle_upload_service_factory is None:
                raise ApiV1Error("runtime_bundle_catalog_unavailable", "Runtime Bundle catalog is unavailable", status_code=503)
            try:
                selected_record = self.runtime_bundle_upload_service_factory(owner).resolve_bundle(owner, bundle_selector)
            except RuntimeBundleUploadServiceError as exc:
                raise ApiV1Error(exc.code, exc.message, status_code=exc.status_code) from exc
            selected_runtime_bundle = selected_record.public_dict
            selected_runtime_project = selected_record.internal_project
            decisions = dict(resolved_spec.get("decisions") or {})
            decisions["selena"] = {
                "status": "resolved",
                "code": "registered_runtime_bundle_selected",
                "action": "use_runtime_bundle",
                "runtime_bundle": selected_runtime_bundle,
                "evidence": {"reason": "shared_runtime_bundle_catalog"},
            }
            resolved_spec["decisions"] = decisions
            resolved_spec["status"] = "partial"
            recognition_status = "registered_bundle"
        for task in task_specs:
            stage_type = str(task.get("stage_type") or "")
            if task.get("stage_type") == "resolve_spec":
                payload = dict(task.get("payload") or {})
                payload.update(
                    {
                        "contract": "user-run-config/2.0",
                        "source": config.selena.source,
                        "target": requested_target,
                        "selected_target": selected_target,
                    }
                )
                task["payload"] = payload
                if selected_runtime_bundle is not None:
                    task["status"] = "skipped"
                    task["initial_status"] = "skipped"
                    task["skip_reason"] = "registered_runtime_bundle_selected"
            if (
                config.selena.source == "existing"
                and stage_type == "register_artifact"
                and (
                    selected_runtime_bundle is not None
                    or selected_target == "local"
                )
            ):
                task["status"] = "skipped"
                task["initial_status"] = "skipped"
                task["skip_reason"] = (
                    "registered_runtime_bundle_selected"
                    if selected_runtime_bundle is not None
                    else "existing_selena_kept_on_local_full_agent"
                )
            if selected_runtime_project:
                payload = dict(task.get("payload") or {})
                payload["internal_project"] = selected_runtime_project
                task["payload"] = payload
            if (
                selected_runtime_bundle is not None
                and selected_target == "local"
            ):
                if stage_type == "environment_check":
                    payload = dict(task.get("payload") or {})
                    payload.update(
                        {
                            "dispatch_scope": "runtime_bundle_cache",
                            "contract": "user-run-config/2.0",
                            "project": selected_runtime_project,
                            "runtime_bundle": selected_runtime_bundle,
                            "runtime_bundle_id": str(selected_runtime_bundle.get("id") or ""),
                            "archive_checksum": str(selected_runtime_bundle.get("archive_checksum") or ""),
                            "archive_size": int(selected_runtime_bundle.get("archive_size") or 0),
                        }
                    )
                    task["payload"] = payload
                elif stage_type == "prepare_data":
                    payload = dict(task.get("payload") or {})
                    payload.update(
                        {
                            "dispatch_scope": "local_data",
                            "contract": "user-run-config/2.0",
                            "project": selected_runtime_project,
                            "data_path": str(config.data.path),
                            "required_signals": [],
                        }
                    )
                    task["payload"] = payload
            if (
                selected_runtime_bundle is not None
                and selected_target == "cluster"
                and stage_type == "prepare_data"
                and not str(config.data.path).lower().startswith("dataset://")
                and classify_data_path(config.data.path) not in {"shared", "central"}
            ):
                # The Web/YAML still carries only data.path. A matching Windows
                # Agent turns this stage into a central Dataset upload.
                payload = dict(task.get("payload") or {})
                payload.update(
                    {
                        "dispatch_scope": "data_upload",
                        "contract": "user-run-config/2.0",
                        "project": selected_runtime_project,
                        "data_path": str(config.data.path),
                        "required_signals": [],
                    }
                )
                task["payload"] = payload
            cluster_route = selected_target == "cluster"
            if cluster_route:
                if (
                    stage_type == "environment_check"
                    and config.selena.source == "existing"
                    and selected_runtime_bundle is not None
                ):
                    task["assigned_agent_id"] = LINUX_STAGE_AGENT_ID
                    task["required_agent_id"] = LINUX_STAGE_AGENT_ID
                elif stage_type == "prepare_data" and (
                    str(config.data.path).lower().startswith("dataset://")
                    or classify_data_path(config.data.path) in {"shared", "central"}
                ):
                    # Data preparation is independent of Selena packaging.
                    # Shared/uploaded data belongs to the Linux control plane
                    # from job creation even while a Windows Agent is still
                    # compiling the Runtime Bundle.
                    task["assigned_agent_id"] = LINUX_STAGE_AGENT_ID
                    task["required_agent_id"] = LINUX_STAGE_AGENT_ID
                elif stage_type in {"preflight", "collect_results", "finalize_manifest"}:
                    task["assigned_agent_id"] = LINUX_STAGE_AGENT_ID
                    task["required_agent_id"] = LINUX_STAGE_AGENT_ID
                elif stage_type == "run_simulation":
                    task["assigned_agent_id"] = CLUSTER_GATEWAY_AGENT_ID
                    task["required_agent_id"] = CLUSTER_GATEWAY_AGENT_ID
        if dry_run:
            # UserRunConfig dry-run is plan-only: it must never switch branches,
            # compile, upload data, launch Selena or submit a Cluster job.
            for task in task_specs:
                task["status"] = "skipped"
                task["initial_status"] = "skipped"
                task["skip_reason"] = "dry_run_plan_only"
                task["required_agent_id"] = ""
            resolved_spec["status"] = "planned"
        job_type = "simulation.run_config.v2.dry_run" if dry_run else "simulation.run_config.v2"
        metadata = {
            "api_version": API_VERSION,
            "contract": "user-run-config/2.0",
            "owner": owner,
            "dry_run": bool(dry_run),
            "idempotency": {"key": key, "request_hash": request_hash},
            "recognition": {"status": recognition_status},
        }
        try:
            job = control.create_job(
                job_type,
                payload={"spec": canonical, "spec_hash": config_hash},
                tasks=task_specs,
                metadata=metadata,
                assigned_agent_id=V1_SCHEDULER_AGENT_ID,
                owner=owner,
                idempotency_key=key,
                request_hash=request_hash,
                spec=canonical,
                resolved_spec=resolved_spec,
            )
        except sqlite3.IntegrityError:
            if key:
                existing = control.get_job_by_idempotency(owner, key)
                if existing is not None and str(existing.get("request_hash") or "") == request_hash:
                    return self._job_response(existing)
            self._raise_idempotency_conflict(key)
        return self._job_response(job)

    def _server_visible_path(self, value: str) -> Path:
        """Resolve a raw or administrator-authorized shared path on Linux."""
        candidate = Path(str(value or "")).expanduser()
        if candidate.exists() or self.project_names_provider is None:
            return candidate
        try:
            from core.config import load_config
            from core.shared_namespace import (
                SharedNamespaceError,
                SharedNamespaceRegistry,
                looks_like_shared_path,
            )

            if not looks_like_shared_path(str(value or "")):
                return candidate
            # Existing Selena is packaged before its internal project is
            # known. Try each administrator-owned namespace and accept only a
            # mount path that really exists on this control plane.
            for project in self.project_names_provider():
                try:
                    resolved = SharedNamespaceRegistry.from_config(
                        load_config(str(project))
                    ).resolve(str(value))
                except (OSError, ValueError, SharedNamespaceError):
                    continue
                central = Path(resolved.central_probe_path)
                if central.exists():
                    return central
        except (ImportError, OSError, TypeError, ValueError):
            pass
        return candidate

    def execution_capabilities(self, owner: str) -> dict[str, Any]:
        """Return a path-free availability snapshot for Web guidance.

        This is advisory only. The scheduler revalidates capabilities at claim
        time, so a stale browser snapshot can never authorize execution.
        """
        owner = self._owner(owner)
        now = float(self.now_fn())
        summary = {
            "windows_full": {"available": False, "count": 0},
            "windows_light": {"available": False, "count": 0},
            "cluster": {
                "available": False,
                "count": 0,
                "linux_executor_count": 0,
                "platform_gateway_count": 0,
            },
        }
        for agent in self._control(owner).list_agents():
            last = float(agent.get("last_heartbeat") or 0.0)
            if last <= 0 or now - last > 120 or str(agent.get("status") or "") == "offline":
                continue
            metadata = dict(agent.get("metadata") or {})
            node_kind = str(metadata.get("node_kind") or metadata.get("node.kind") or "")
            key = ""
            if node_kind == "windows_full":
                key = "windows_full"
            elif node_kind == "windows_agent":
                key = "windows_light"
            elif node_kind == "linux_executor":
                summary["cluster"]["linux_executor_count"] += 1
            elif node_kind == "platform_gateway":
                summary["cluster"]["platform_gateway_count"] += 1
            if key:
                summary[key]["count"] += 1
                summary[key]["available"] = True
        summary["cluster"]["count"] = min(
            summary["cluster"]["linux_executor_count"],
            summary["cluster"]["platform_gateway_count"],
        )
        summary["cluster"]["available"] = summary["cluster"]["count"] > 0
        return {"capabilities": summary, "observed_at": now}

    def list_projects(self) -> dict[str, Any]:
        """Return public project identifiers only, never project adapter paths."""
        if self.project_names_provider is None:
            return {"projects": [], "count": 0}
        try:
            projects = sorted(
                {
                    str(item or "").strip()
                    for item in self.project_names_provider()
                    if str(item or "").strip()
                },
                key=str.casefold,
            )
        except Exception as exc:
            raise ApiV1Error(
                "project_catalog_unavailable",
                "Project catalog is unavailable",
                status_code=503,
                actions=[{"type": "retry", "label": "Retry loading projects"}],
            ) from exc
        return {"projects": projects, "count": len(projects)}

    def import_spec_yaml(self, yaml_content: str) -> dict[str, Any]:
        try:
            spec = SimulationSpec.from_yaml(str(yaml_content or ""))
        except Exception as exc:
            raise ApiV1Error(
                "invalid_spec",
                "SimulationSpec YAML validation failed",
                status_code=422,
                detail={"error": str(exc)},
                actions=[{"type": "fix_spec", "label": "Fix the YAML fields shown in detail"}],
            ) from exc
        return {
            "valid": True,
            "spec": spec.to_dict(),
            "yaml_content": spec.to_yaml(),
            "fingerprint": spec.fingerprint(),
        }

    def export_spec_yaml(self, spec_payload: dict[str, Any]) -> dict[str, Any]:
        spec = self._parse_spec(spec_payload)
        return {
            "yaml_content": spec.to_yaml(),
            "fingerprint": spec.fingerprint(),
        }

    def validate(self, spec_payload: dict[str, Any]) -> dict[str, Any]:
        spec = self._parse_spec(spec_payload)
        from core.environment_contract import plan_environment_requirements
        return {
            "valid": True,
            "spec": spec.to_dict(),
            "fingerprint": spec.fingerprint(),
            "environment_plan": plan_environment_requirements(spec),
        }

    def submit_job(
        self,
        owner: str,
        *,
        spec_payload: dict[str, Any],
        dry_run: bool = False,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        owner = self._owner(owner)
        spec = self._parse_spec(spec_payload)
        canonical_spec = spec.to_dict()
        spec_hash = spec.fingerprint()
        stage_plan = plan_simulation_stages(spec)
        request_hash = self._request_hash(canonical_spec, dry_run=bool(dry_run))
        control = self._control(owner)
        key = str(idempotency_key or "").strip()

        if key:
            existing = control.get_job_by_idempotency(owner, key)
            if existing is not None:
                if str(existing.get("request_hash") or "") != request_hash:
                    self._raise_idempotency_conflict(key)
                return self._job_response(existing)

        job_type = "simulation.v1.dry_run" if dry_run else "simulation.v1"
        resolution_metadata = {"status": "pending", "code": ""}
        data_resolution_metadata = {"status": "pending", "code": ""}
        task_specs: list[dict[str, Any]] | None = None
        if self.source_resolution_provider is not None:
            try:
                inputs = self.source_resolution_provider(owner, spec)
                if inputs.context.owner != owner:
                    raise ApiV1Error(
                        "source_resolution_owner_mismatch",
                        "Source resolution snapshot does not belong to the request owner",
                        status_code=409,
                        actions=[{"type": "retry_source_resolution", "label": "Refresh source resolution inputs"}],
                    )
                outcome = resolve_selena(spec, inputs.project_catalog, inputs.user_bindings, inputs.context)
                application = apply_selena_resolution(
                    stage_plan,
                    outcome,
                    project_revision=inputs.project_catalog.revision,
                )
                stage_plan = application.stage_plan
                resolved_spec = dict(stage_plan.resolved_spec)
                environment_plan = dict(resolved_spec.get("environment_plan") or {})
                environment_plan["project_adapter"] = inputs.project_catalog.adapter
                resolved_spec["environment_plan"] = environment_plan
                stage_plan = StagePlan(stages=stage_plan.stages, resolved_spec=resolved_spec)
                machine_pending = _is_windows_workspace_machine_pending(spec, outcome, inputs.context)
                if machine_pending:
                    pending = dict(stage_plan.resolved_spec)
                    pending["status"] = "pending_node"
                    pending["code"] = "workspace_snapshot_pending"
                    pending["action"] = "Wait for the configured Windows Agent to inspect the workspace."
                    stage_plan = StagePlan(stages=stage_plan.stages, resolved_spec=pending)
                    selected_agent = _matching_windows_agent(
                        control,
                        project=spec.project,
                        binding_id=outcome.workspace_binding_id or inputs.context.workspace_binding_id,
                    )
                    task_specs = _current_workspace_task_specs(
                        stage_plan,
                        spec,
                        binding_id=outcome.workspace_binding_id or inputs.context.workspace_binding_id,
                        agent_id=selected_agent if not dry_run else "",
                        dispatch_scope="selena_build" if not dry_run else "plan_only",
                    )
                    resolution_metadata = {
                        "status": "pending_node",
                        "code": "workspace_snapshot_pending",
                    }
                elif outcome.status != "resolved":
                    stage_plan = _blocked_stage_plan(stage_plan, status=outcome.status, code=outcome.code, action=outcome.action)
                    resolution_metadata = {"status": outcome.status, "code": outcome.code}
                else:
                    task_specs = _resolved_submission_task_specs(stage_plan)
                    resolution_metadata = {"status": outcome.status, "code": outcome.code}
            except ApiV1Error:
                raise
            except SourceResolutionProviderError as exc:
                raise _provider_api_error(exc) from exc
            except Exception as exc:
                raise ApiV1Error(
                    "source_resolution_unavailable",
                    "Source resolution inputs are unavailable",
                    status_code=409,
                    detail={"provider_error": type(exc).__name__},
                    actions=[{"type": "retry_source_resolution", "label": "Retry source resolution"}],
                ) from exc

        if self.data_resolution_provider is not None:
            try:
                data_outcome = self.data_resolution_provider(owner, spec)
                if not isinstance(data_outcome, DataResolution):
                    raise TypeError("data resolution provider returned an invalid result")
                stage_plan = _apply_data_resolution(stage_plan, data_outcome)
                if task_specs is None:
                    task_specs = stage_plan.task_specs()
                task_specs = _apply_data_resolution_to_task_specs(task_specs, data_outcome, spec)
                data_resolution_metadata = {
                    "status": data_outcome.status,
                    "code": data_outcome.code,
                    "route": data_outcome.route,
                }
            except ApiV1Error:
                raise
            except Exception as exc:
                raise ApiV1Error(
                    "data_resolution_unavailable",
                    "Data resolution service is unavailable",
                    status_code=409,
                    detail={"provider_error": type(exc).__name__},
                    actions=[{"type": "retry_data_resolution", "label": "Retry data resolution"}],
                ) from exc

        metadata = {
            "api_version": API_VERSION,
            "owner": owner,
            "dry_run": bool(dry_run),
            "idempotency": {"key": key, "request_hash": request_hash},
            "source_resolution": resolution_metadata,
            "data_resolution": data_resolution_metadata,
        }
        payload = {"spec": canonical_spec, "spec_hash": spec_hash}
        try:
            job = control.create_job(
                job_type,
                payload=payload,
                tasks=task_specs if task_specs is not None else stage_plan.task_specs(),
                metadata=metadata,
                assigned_agent_id=V1_SCHEDULER_AGENT_ID,
                owner=owner,
                idempotency_key=key,
                request_hash=request_hash,
                spec=canonical_spec,
                resolved_spec=stage_plan.resolved_spec,
            )
        except sqlite3.IntegrityError:
            if key:
                existing = control.get_job_by_idempotency(owner, key)
                if existing is not None and str(existing.get("request_hash") or "") == request_hash:
                    return self._job_response(existing)
            self._raise_idempotency_conflict(key)
        return self._job_response(job)

    def get_job(self, owner: str, job_id: str) -> dict[str, Any]:
        job = self._get_owned_job(owner, job_id)
        return self._job_response(job)

    def list_jobs(self, owner: str, *, status: str = "", limit: int = 50) -> dict[str, Any]:
        """Return the current user's v1 jobs for the Web/SDK task center."""
        owner = self._owner(owner)
        control = self._control(owner)
        safe_limit = max(1, min(int(limit or 50), 100))
        requested_status = str(status or "").strip()
        summaries = control.list_jobs(
            # v1 status can be derived from Stage state (for example a queued
            # control job with a blocked Stage is ``needs_input``), so filtering
            # the raw control status would return incorrect task-center pages.
            limit=100,
            owner=owner,
            status="",
            job_type_prefix="simulation.",
        )
        jobs = [self._job_response(control.get_job(item["job_id"])) for item in summaries]
        if requested_status:
            jobs = [item for item in jobs if item["status"] == requested_status]
        jobs = jobs[:safe_limit]
        return {"jobs": jobs, "count": len(jobs)}

    def cancel_job(self, owner: str, job_id: str) -> dict[str, Any]:
        owner = self._owner(owner)
        self._get_owned_job(owner, job_id)
        job = self._control(owner).cancel_job(job_id)
        return self._job_response(job)

    def retry_stage(self, owner: str, job_id: str, stage_id: str) -> dict[str, Any]:
        owner = self._owner(owner)
        self._get_owned_job(owner, job_id)
        try:
            job = self._control(owner).retry_stage(job_id, stage_id)
        except ValueError as exc:
            raise ApiV1Error(
                "invalid_stage_retry",
                str(exc),
                status_code=409,
                detail={"job_id": job_id, "stage_id": stage_id},
                actions=[{"type": "choose_failed_stage", "label": "Retry a failed or cancelled stage"}],
            ) from exc
        return self._job_response(job)

    def manifest(self, owner: str, job_id: str) -> dict[str, Any]:
        job = self._get_owned_job(owner, job_id)
        manifest = None
        if isinstance(job.get("result"), dict):
            manifest = job["result"].get("manifest")
        if manifest is None and isinstance(job.get("metadata"), dict):
            manifest = job["metadata"].get("manifest")
        return {
            "job_id": job["job_id"],
            "available": manifest is not None,
            "manifest": manifest,
        }

    def events(self, owner: str, job_id: str, *, since: int = 0, limit: int = 200) -> dict[str, Any]:
        job = self._get_owned_job(owner, job_id)
        safe_limit = min(max(int(limit or 200), 1), 1000)
        cursor = max(int(since or 0), 0)
        page = self._control(self._owner(owner)).list_events(job_id, since=cursor, limit=safe_limit)
        events = list(page.get("events") or [])
        current = self._get_owned_job(owner, job_id)
        return {
            "job_id": job["job_id"],
            "status": self._v1_status(current),
            "events": events,
            "next_cursor": int(page.get("next_cursor") or cursor),
            "terminal": self._v1_status(current) in TERMINAL_STATUSES,
        }

    def list_results(self, owner: str) -> dict[str, Any]:
        owner = self._owner(owner)
        catalog = self._result_catalog()
        return {"items": [item.public_dict for item in catalog.list(owner=owner)]}

    def get_result(self, owner: str, result_ref: str) -> dict[str, Any]:
        owner = self._owner(owner)
        try:
            return self._result_catalog().get(result_ref, owner=owner).public_dict
        except ResultCatalogError as exc:
            raise ApiV1Error("result_unavailable", str(exc), status_code=404) from exc

    def register_agent(
        self, owner: str, *, name: str, agent_id: str, hostname: str,
        platform: str, capabilities: list[str], metadata: dict[str, Any],
    ) -> dict[str, Any]:
        control = self._control(self._owner(owner))
        return control.register_agent(
            name, agent_id=agent_id, hostname=hostname, platform=platform,
            capabilities=capabilities, metadata=metadata,
            node_kind=str(metadata.get("node_kind") or metadata.get("node.kind") or ""),
        )

    def poll_agent(self, owner: str, agent_id: str) -> dict[str, Any]:
        control = self._control(self._owner(owner))
        control.bind_pending_run_config_resolution(agent_id)
        control.bind_pending_runtime_bundle_cache(agent_id)
        control.bind_pending_environment_stage(agent_id)
        control.bind_pending_data_stage(agent_id)
        task = control.claim_next_task(agent_id)
        if task is not None:
            # Agent-side transfers must act for the user who submitted the job,
            # not for the Windows login account running the Agent.  This value
            # is only returned after ControlService has assigned the task to
            # this Agent; it is not part of the public job/config contract.
            job = control.get_job(str(task.get("job_id") or ""))
            task["owner"] = self._owner(str(job.get("owner") or ""))
        return {"task": task}

    def heartbeat_agent(
        self, owner: str, agent_id: str, *, status: str, current_task_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._control(self._owner(owner)).heartbeat(
            agent_id, status=status, current_task_id=current_task_id,
            metadata=dict(metadata or {}),
        )

    def append_agent_logs(
        self, owner: str, task_id: str, *, lines: list[str], stream: str = "stdout",
        agent_id: str = "",
    ) -> dict[str, Any]:
        control = self._control(self._owner(owner))
        if agent_id:
            try:
                task = control.get_task(task_id)
            except KeyError as exc:
                raise ApiV1Error("task_not_found", "Task is unavailable", status_code=404) from exc
            assigned = str(task.get("assigned_agent_id") or "")
            required = str(task.get("required_agent_id") or "")
            if str(agent_id) not in {assigned, required}:
                raise ApiV1Error(
                    "agent_task_mismatch",
                    "Authenticated Agent is not assigned to this task",
                    status_code=403,
                )
        return control.append_logs(task_id, lines, stream=stream)

    def report_agent_progress(
        self,
        owner: str,
        task_id: str,
        *,
        agent_id: str,
        progress: float,
        message: str = "",
    ) -> dict[str, Any]:
        control = self._control(self._owner(owner))
        try:
            task = control.get_task(task_id)
        except KeyError as exc:
            raise ApiV1Error("task_not_found", "Task is unavailable", status_code=404) from exc
        assigned = str(task.get("assigned_agent_id") or "")
        required = str(task.get("required_agent_id") or "")
        if str(agent_id) not in {assigned, required}:
            raise ApiV1Error(
                "agent_task_mismatch",
                "Authenticated Agent is not assigned to this task",
                status_code=403,
            )
        if str(task.get("status") or "") != "running":
            raise ApiV1Error(
                "task_not_running",
                "Task progress can only be reported while it is running",
                status_code=409,
            )
        return control.report_stage_progress(
            task_id,
            progress=max(0.0, min(float(progress), 1.0)),
            message=str(message or ""),
        )

    def submit_agent_result(
        self, owner: str, task_id: str, *, agent_id: str, status: str,
        returncode: int, result: dict[str, Any],
    ) -> dict[str, Any]:
        control = self._control(self._owner(owner))
        completed = control.submit_task_result(
            task_id, agent_id=agent_id, status=status,
            returncode=returncode, result=result,
        )
        try:
            from core.stage_binder import StageBindingError, advance_after_stage_result

            stage = next(
                (item for item in completed.get("stages") or [] if str(item.get("stage_id") or "") == task_id),
                {},
            )
            handoff = advance_after_stage_result(control, stage)
            if handoff is not None:
                completed["handoff"] = {
                    "status": "bound", "stage_id": handoff["stage_id"],
                    "stage_type": handoff["stage_type"],
                }
        except StageBindingError as exc:
            completed["handoff"] = {"status": "blocked", "message": str(exc)}
        return completed

    def result_archive(self, owner: str, result_ref: str):
        """Trusted HTTP adapter hook; physical location is never serialized."""
        owner = self._owner(owner)
        try:
            return self._result_catalog().resolve_archive(result_ref, owner=owner)
        except ResultCatalogError as exc:
            raise ApiV1Error("result_unavailable", str(exc), status_code=404) from exc

    def create_artifact_upload(
        self,
        owner: str,
        *,
        build_evidence_ref: str,
        publish_path: str = "",
    ) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._upload_call(
            owner,
            lambda service: service.create(
                owner,
                evidence_ref=str(build_evidence_ref or "").strip(),
                publish_path=str(publish_path or "").strip(),
            )
        )

    def get_artifact_upload(self, owner: str, session_id: str) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._upload_call(owner, lambda service: service.get(owner, session_id))

    def append_artifact_upload(
        self,
        owner: str,
        session_id: str,
        *,
        offset: int,
        data: bytes,
    ) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._upload_call(
            owner,
            lambda service: service.append(owner, session_id, offset=int(offset), data=data)
        )

    def finalize_artifact_upload(self, owner: str, session_id: str) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._upload_call(owner, lambda service: service.finalize(owner, session_id))

    def create_runtime_bundle_upload(
        self,
        owner: str,
        *,
        build_evidence_ref: str,
        publish_path: str = "",
    ) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._runtime_bundle_upload_call(
            owner,
            lambda service: service.create(
                owner,
                evidence_ref=str(build_evidence_ref or "").strip(),
                publish_path=str(publish_path or "").strip(),
            ),
        )

    def list_runtime_bundles(self, owner: str) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._runtime_bundle_upload_call(owner, lambda service: service.list_bundles(owner))

    def get_runtime_bundle(self, owner: str, bundle_id: str) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._runtime_bundle_upload_call(owner, lambda service: service.get_bundle(owner, bundle_id))

    def runtime_bundle_archive(self, owner: str, bundle_id: str):
        """Trusted HTTP adapter hook for a shared Bundle archive download."""
        owner = self._owner(owner)
        return self._runtime_bundle_upload_call(
            owner,
            lambda service: service.resolve_archive(owner, bundle_id)[1],
        )

    def get_runtime_bundle_upload(self, owner: str, session_id: str) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._runtime_bundle_upload_call(owner, lambda service: service.get(owner, session_id))

    def append_runtime_bundle_upload(
        self,
        owner: str,
        session_id: str,
        *,
        offset: int,
        data: bytes,
    ) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._runtime_bundle_upload_call(
            owner, lambda service: service.append(owner, session_id, offset=int(offset), data=bytes(data))
        )

    def finalize_runtime_bundle_upload(self, owner: str, session_id: str) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._runtime_bundle_upload_call(owner, lambda service: service.finalize(owner, session_id))

    def import_existing_selena(
        self,
        owner: str,
        *,
        metadata: dict[str, Any],
        archive_bytes: bytes,
    ) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._runtime_bundle_upload_call(
            owner,
            lambda service: service.import_existing(
                owner,
                metadata=dict(metadata or {}),
                archive_bytes=bytes(archive_bytes),
            ),
        )

    def upload_config_asset(
        self, owner: str, *, kind: str, filename: str, content: bytes
    ) -> dict[str, Any]:
        owner = self._owner(owner)
        if self.config_asset_store is None:
            raise ApiV1Error("config_asset_store_unavailable", "Configuration asset store is unavailable", status_code=503)
        try:
            return self.config_asset_store.put(
                owner=owner, kind=kind, filename=filename, content=bytes(content)
            ).public_dict
        except ConfigAssetError as exc:
            raise ApiV1Error("invalid_config_asset", str(exc), status_code=422) from exc

    def list_config_assets(self, owner: str, *, kind: str = "") -> dict[str, Any]:
        owner = self._owner(owner)
        if self.config_asset_store is None:
            raise ApiV1Error("config_asset_store_unavailable", "Configuration asset store is unavailable", status_code=503)
        try:
            return {"items": [item.public_dict for item in self.config_asset_store.list(owner=owner, kind=kind)]}
        except ConfigAssetError as exc:
            raise ApiV1Error("invalid_config_asset", str(exc), status_code=422) from exc

    def get_config_asset(self, owner: str, asset_id: str, *, kind: str) -> dict[str, Any]:
        owner = self._owner(owner)
        if self.config_asset_store is None:
            raise ApiV1Error("config_asset_store_unavailable", "Configuration asset store is unavailable", status_code=503)
        try:
            return self.config_asset_store.get(asset_id, owner=owner, kind=kind).public_dict
        except ConfigAssetError as exc:
            raise ApiV1Error("config_asset_unavailable", str(exc), status_code=404) from exc

    def config_asset_content(self, owner: str, asset_id: str, *, kind: str):
        """Trusted HTTP-adapter hook for an owner-scoped asset download.

        Physical storage locations never enter the public JSON contract.  The
        HTTP adapter may use this hook to stream the file to an authenticated
        user or to that user's authenticated Windows Agent.
        """
        owner = self._owner(owner)
        if self.config_asset_store is None:
            raise ApiV1Error(
                "config_asset_store_unavailable",
                "Configuration asset store is unavailable",
                status_code=503,
            )
        try:
            record = self.config_asset_store.get(asset_id, owner=owner, kind=kind)
            location = self.config_asset_store.resolve_location(
                asset_id, owner=owner, kind=kind
            )
            return record, location
        except ConfigAssetError as exc:
            raise ApiV1Error("config_asset_unavailable", str(exc), status_code=404) from exc

    def create_dataset_upload(
        self,
        owner: str,
        *,
        project: str,
        files: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._dataset_upload_call(
            owner,
            lambda service: service.create(owner, project=str(project or ""), files=files),
        )

    def create_agent_dataset_upload(
        self,
        owner: str,
        *,
        project: str,
        files: Iterable[dict[str, Any]],
        evidence_ref: str,
        agent_id: str,
    ) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._dataset_upload_call(
            owner,
            lambda service: service.create_agent_from_evidence(
                owner,
                project=str(project or ""),
                files=files,
                evidence_ref=str(evidence_ref or ""),
                requesting_agent_id=str(agent_id or ""),
            ),
        )

    def get_dataset_upload(self, owner: str, session_id: str) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._dataset_upload_call(owner, lambda service: service.get(owner, session_id))

    def append_dataset_upload(
        self,
        owner: str,
        session_id: str,
        file_id: str,
        *,
        offset: int,
        data: bytes,
    ) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._dataset_upload_call(
            owner,
            lambda service: service.append(
                owner, session_id, file_id, offset=int(offset), data=bytes(data)
            ),
        )

    def finalize_dataset_upload(self, owner: str, session_id: str) -> dict[str, Any]:
        owner = self._owner(owner)
        return self._dataset_upload_call(owner, lambda service: service.finalize(owner, session_id))

    def _control(self, owner: str) -> ControlService:
        factory = self.control_service_factory or _default_control_service
        return factory(owner)

    def _result_catalog(self) -> ResultCatalog:
        if self.result_catalog is None:
            raise ApiV1Error(
                "result_service_unavailable",
                "Local result service is unavailable",
                status_code=503,
            )
        return self.result_catalog

    def _upload_call(
        self,
        owner: str,
        callback: Callable[[ArtifactUploadService], dict[str, Any]],
    ) -> dict[str, Any]:
        if self.artifact_upload_service_factory is None:
            raise ApiV1Error(
                "artifact_upload_unavailable",
                "Artifact upload service is unavailable",
                status_code=503,
                actions=[{"type": "retry", "label": "Retry after the upload service is configured"}],
            )
        try:
            service = self.artifact_upload_service_factory(owner)
            return callback(service)
        except ArtifactUploadServiceError as exc:
            raise ApiV1Error(exc.code, exc.message, status_code=exc.status_code) from exc

    def _dataset_upload_call(
        self,
        owner: str,
        callback: Callable[[DatasetUploadService], dict[str, Any]],
    ) -> dict[str, Any]:
        if self.dataset_upload_service_factory is None:
            raise ApiV1Error(
                "dataset_upload_unavailable",
                "Dataset upload service is unavailable",
                status_code=503,
                actions=[{"type": "retry", "label": "Retry after the upload service is configured"}],
            )
        try:
            service = self.dataset_upload_service_factory(owner)
            return callback(service)
        except DatasetUploadServiceError as exc:
            raise ApiV1Error(exc.code, exc.message, status_code=exc.status_code) from exc

    def _runtime_bundle_upload_call(
        self,
        owner: str,
        callback: Callable[[RuntimeBundleUploadService], dict[str, Any]],
    ) -> dict[str, Any]:
        if self.runtime_bundle_upload_service_factory is None:
            raise ApiV1Error(
                "runtime_bundle_upload_unavailable",
                "Runtime Bundle upload service is unavailable",
                status_code=503,
            )
        try:
            return callback(self.runtime_bundle_upload_service_factory(owner))
        except RuntimeBundleUploadServiceError as exc:
            raise ApiV1Error(exc.code, exc.message, status_code=exc.status_code) from exc

    def _get_owned_job(self, owner: str, job_id: str) -> dict[str, Any]:
        owner = self._owner(owner)
        try:
            job = self._control(owner).get_job(job_id)
        except KeyError as exc:
            raise ApiV1Error(
                "not_found",
                "Job not found",
                status_code=404,
                detail={"job_id": job_id},
            ) from exc
        job_owner = str(job.get("owner") or job.get("metadata", {}).get("owner") or "")
        if job_owner and job_owner != owner:
            raise ApiV1Error(
                "not_found",
                "Job not found",
                status_code=404,
                detail={"job_id": job_id},
            )
        return job

    def _job_response(self, job: dict[str, Any]) -> dict[str, Any]:
        raw_stages = list(job.get("stages") or job.get("tasks") or [])
        is_run_config = str((job.get("metadata") or {}).get("contract") or "") == "user-run-config/2.0"
        stages = [self._public_run_stage(item) for item in raw_stages] if is_run_config else raw_stages
        status = self._v1_status(job)
        return {
            "id": job["job_id"],
            "job_id": job["job_id"],
            "type": job["job_type"],
            "status": status,
            "spec_hash": (job.get("payload") or {}).get("spec_hash", ""),
            "dry_run": bool((job.get("metadata") or {}).get("dry_run", False)),
            "created_at": job.get("created_at", 0.0),
            "updated_at": job.get("updated_at", 0.0),
            "completed_at": job.get("completed_at", 0.0),
            "started_at": job.get("started_at", 0.0),
            "finished_at": job.get("finished_at", 0.0),
            "cancel_requested": bool(job.get("cancel_requested", False)),
            "spec": dict(job.get("spec") or (job.get("payload") or {}).get("spec") or {}),
            "resolved_spec": dict(job.get("resolved_spec") or {}),
            "progress": self._job_progress(stages, str(job.get("status") or "")),
            "current_stage": self._current_stage(stages),
            "available_actions": self._available_actions(str(job["job_id"]), status, stages),
            "stages": stages,
            "tasks": stages if is_run_config else list(job.get("tasks") or []),
            "metadata": dict(job.get("metadata") or {}),
        }

    @staticmethod
    def _public_run_stage(stage: dict[str, Any]) -> dict[str, Any]:
        """Remove node-local paths, internal adapters and Agent identities."""
        allowed = {
            "task_id",
            "stage_id",
            "job_id",
            "task_type",
            "stage_type",
            "order_index",
            "status",
            "initial_status",
            "dependencies",
            "progress",
            "error",
            "skip_reason",
            "attempt_count",
            "created_at",
            "updated_at",
            "completed_at",
        }
        return {key: value for key, value in stage.items() if key in allowed}

    @staticmethod
    def _job_progress(stages: list[dict[str, Any]], status: str) -> float:
        if not stages:
            return 1.0 if status in TERMINAL_STATUSES else 0.0
        values: list[float] = []
        for stage in stages:
            try:
                value = float(stage.get("progress") or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            values.append(min(max(value, 0.0), 1.0))
        return round(sum(values) / len(values), 4)

    @staticmethod
    def _current_stage(stages: list[dict[str, Any]]) -> str:
        for desired in ("running", "cancel_requested", "blocked", "queued"):
            for stage in stages:
                if str(stage.get("status") or "") == desired:
                    return str(stage.get("stage_type") or stage.get("task_type") or "")
        return ""

    @staticmethod
    def _available_actions(job_id: str, status: str, stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        if status in {"queued", "running", "needs_input"}:
            actions.append({"type": "cancel_job", "label": "Cancel job", "job_id": job_id})
        for stage in stages:
            stage_status = str(stage.get("status") or "")
            if stage_status == "blocked":
                error = dict(stage.get("error") or {})
                for item in error.get("actions") or []:
                    action = dict(item) if isinstance(item, dict) else {}
                    if action and action not in actions:
                        actions.append(action)
            if stage_status == "failed":
                actions.append(
                    {
                        "type": "retry_stage",
                        "label": "Retry failed stage",
                        "job_id": job_id,
                        "stage_id": str(stage.get("stage_id") or stage.get("task_id") or ""),
                    }
                )
                break
        return actions

    def _v1_status(self, job: dict[str, Any]) -> str:
        status = str(job.get("status") or "")
        if status == "cancel_requested":
            return "cancelling"
        stages = list(job.get("stages") or job.get("tasks") or [])
        if status not in TERMINAL_STATUSES and any(
            str(stage.get("status") or "") == "blocked" for stage in stages
        ):
            return "needs_input"
        return status

    def _parse_spec(self, spec_payload: dict[str, Any]) -> SimulationSpec:
        if not isinstance(spec_payload, dict):
            raise ApiV1Error(
                "invalid_spec",
                "SimulationSpec body must be a JSON object",
                status_code=422,
                detail={"loc": ["body"]},
            )
        try:
            return SimulationSpec.from_dict(spec_payload)
        except ValidationError as exc:
            raise ApiV1Error(
                "invalid_spec",
                "SimulationSpec validation failed",
                status_code=422,
                detail={"errors": json.loads(exc.json(include_url=False))},
                actions=[{"type": "fix_spec", "label": "Fix the SimulationSpec fields shown in detail"}],
            ) from exc
        except ValueError as exc:
            raise ApiV1Error(
                "invalid_spec",
                str(exc),
                status_code=422,
                detail={"loc": ["body"]},
            ) from exc

    def _parse_user_run_config(self, config_payload: dict[str, Any]) -> UserRunConfig:
        if not isinstance(config_payload, dict):
            raise ApiV1Error(
                "invalid_run_config",
                "Simulation config body must be a JSON object",
                status_code=422,
                detail={"loc": ["body"]},
            )
        try:
            return UserRunConfig.from_dict(config_payload)
        except ValidationError as exc:
            raise ApiV1Error(
                "invalid_run_config",
                "Simulation YAML validation failed",
                status_code=422,
                detail={"errors": json.loads(exc.json(include_url=False))},
                actions=[{"type": "fix_config", "label": "Fix the simulation fields shown in detail"}],
            ) from exc
        except ValueError as exc:
            raise ApiV1Error(
                "invalid_run_config",
                str(exc),
                status_code=422,
                detail={"loc": ["body"]},
            ) from exc

    @staticmethod
    def _request_hash(canonical_spec: dict[str, Any], *, dry_run: bool) -> str:
        body = json.dumps(
            {"spec": canonical_spec, "dry_run": bool(dry_run)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()

    @staticmethod
    def _log_to_event(job_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        event_id = int(entry["log_id"])
        return {
            "id": event_id,
            "event": "log",
            "job_id": job_id,
            "task_id": entry["task_id"],
            "sequence": event_id,
            "timestamp": entry["created_at"],
            "level": "info" if entry["stream"] == "stdout" else "error",
            "stream": entry["stream"],
            "message": entry["message"],
            "data": {
                "job_id": job_id,
                "task_id": entry["task_id"],
                "stream": entry["stream"],
                "message": entry["message"],
                "created_at": entry["created_at"],
            },
        }

    @staticmethod
    def _owner(owner: str) -> str:
        return normalize_user(owner or current_user())

    @staticmethod
    def _raise_idempotency_conflict(idempotency_key: str) -> None:
        raise ApiV1Error(
            "idempotency_conflict",
            "Idempotency-Key was already used with a different request",
            status_code=409,
            detail={"idempotency_key": idempotency_key},
            actions=[{"type": "change_idempotency_key", "label": "Use a new Idempotency-Key"}],
        )


def format_error_envelope(
    code: str,
    message: str,
    *,
    request_id: str,
    detail: Any = None,
    actions: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "detail": make_json_safe(detail if detail is not None else {}),
        "actions": make_json_safe(list(actions or [])),
        "request_id": request_id,
    }


def make_json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def iter_sse(events: Iterable[dict[str, Any]]) -> Iterable[str]:
    """Yield standard SSE frames for already-materialized v1 events."""
    for event in events:
        event_id = event.get("id", event.get("sequence", ""))
        event_name = str(event.get("event") or "message")
        data = json.dumps(event, ensure_ascii=False, sort_keys=True)
        yield f"id: {event_id}\n"
        yield f"event: {event_name}\n"
        for line in data.splitlines() or [""]:
            yield f"data: {line}\n"
        yield "\n"


def _default_control_service(owner: str) -> ControlService:
    return ControlService(control_db_path_for_user(owner))


def _provider_api_error(exc: SourceResolutionProviderError) -> ApiV1Error:
    public = {
        "source_config_invalid": (
            "Source resolution configuration is invalid or unavailable",
            422,
            "fix_project_config",
            "Fix the project configuration and retry",
        ),
        "source_config_unavailable": (
            "Source resolution configuration is unavailable",
            409,
            "retry_source_resolution",
            "Retry source resolution after configuration service recovery",
        ),
        "source_workspace_unavailable": (
            "Authorized workspace snapshot is unavailable",
            409,
            "inspect_workspace",
            "Inspect the authorized workspace and retry",
        ),
        "source_artifact_catalog_unavailable": (
            "Selena artifact catalog snapshot is unavailable",
            409,
            "retry_source_resolution",
            "Retry after artifact catalog recovery",
        ),
        "source_clock_unavailable": (
            "Source resolution clock is unavailable",
            409,
            "retry_source_resolution",
            "Retry source resolution",
        ),
        "source_clock_invalid": (
            "Source resolution clock is invalid",
            409,
            "retry_source_resolution",
            "Retry source resolution",
        ),
    }
    message, status_code, action_type, action_label = public.get(
        exc.code,
        (
            "Source resolution inputs are unavailable",
            409,
            "retry_source_resolution",
            "Retry source resolution",
        ),
    )
    return ApiV1Error(
        exc.code,
        message,
        status_code=status_code,
        detail={"provider_error": exc.code},
        actions=[{"type": action_type, "label": action_label}],
    )


def _is_windows_workspace_machine_pending(
    spec: SimulationSpec,
    outcome: Any,
    context: SourceResolutionContext,
) -> bool:
    status = str(getattr(outcome, "status", "") or "")
    code = str(getattr(outcome, "code", "") or "")
    if status != "needs_input" or not context.workspace_binding_id:
        return False
    if spec.selena.mode == "current_workspace":
        return code == "workspace_fingerprint_required"
    # Minimal two-field YAML defaults to auto. When auto-build is enabled and
    # a logical Windows binding exists, the missing fingerprint is machine
    # work, not a reason to ask the user for a Selena candidate.
    return spec.selena.mode == "auto" and spec.selena.auto_build and code == "selena_candidate_required"


def _matching_windows_agent(control: ControlService, *, project: str, binding_id: str) -> str:
    """Return the newest connected Agent advertising one exact logical binding."""
    binding_id = str(binding_id or "").strip()
    if not binding_id:
        return ""
    for agent in control.list_agents():
        metadata = dict(agent.get("metadata") or {})
        if str(metadata.get("node_kind") or "") not in {"windows_agent", "windows_full"}:
            continue
        bindings = metadata.get("workspace_bindings") or []
        if not isinstance(bindings, list):
            continue
        if any(
            isinstance(item, dict)
            and item.get("healthy") is True
            and str(item.get("project") or "") == project
            and str(item.get("id") or "") == binding_id
            for item in bindings
        ):
            return str(agent.get("agent_id") or "")
    return ""


def _resolved_submission_task_specs(plan: StagePlan) -> list[dict[str, Any]]:
    """Mark the synchronously executed catalog/source resolution as visible."""
    tasks = plan.task_specs()
    for task in tasks:
        if task.get("stage_type") == "resolve_spec":
            task["status"] = "skipped"
            task["initial_status"] = "skipped"
            task["skip_reason"] = "resolved_during_submission"
    return tasks


def _current_workspace_task_specs(
    plan: StagePlan,
    spec: SimulationSpec,
    *,
    binding_id: str,
    agent_id: str,
    dispatch_scope: str,
) -> list[dict[str, Any]]:
    """Prepare the path-free current-workspace handoff without releasing later Stages."""
    tasks = _resolved_submission_task_specs(plan)
    for task in tasks:
        stage_type = str(task.get("stage_type") or "")
        if stage_type == "environment_check":
            payload = dict(task.get("payload") or {})
            payload.update(
                {
                    "dispatch_scope": dispatch_scope,
                    "project": spec.project,
                    "workspace_binding_id": str(binding_id or ""),
                    "build_mode": spec.selena.build_mode,
                    "profile": spec.simulation.profile,
                    "clean": False,
                }
            )
            task["payload"] = payload
            if agent_id:
                task["assigned_agent_id"] = agent_id
                task["required_agent_id"] = agent_id
        elif stage_type == "prepare_source":
            task["status"] = "skipped"
            task["initial_status"] = "skipped"
            task["skip_reason"] = "current_workspace_verified_by_environment_check"
    return tasks


def _blocked_stage_plan(plan: StagePlan, *, status: str, code: str, action: str) -> StagePlan:
    stages = tuple(
        replace(
            stage,
            initial_status="blocked",
            skip_reason=action,
            error={
                "code": code,
                "status": status,
                "message": action,
                "actions": [{"type": "resolve_source", "label": action}],
            },
        )
        for stage in plan.stages
    )
    return StagePlan(stages=stages, resolved_spec=dict(plan.resolved_spec))


def _apply_data_resolution(plan: StagePlan, outcome: DataResolution) -> StagePlan:
    stages: list[PlannedStage] = []
    for stage in plan.stages:
        if stage.stage_type != "prepare_data":
            stages.append(stage)
            continue
        if outcome.status == "resolved":
            stages.append(replace(stage, initial_status="skipped", skip_reason="data_resolved_during_submission"))
        elif outcome.status == "needs_input":
            stages.append(
                replace(
                    stage,
                    initial_status="blocked",
                    skip_reason=outcome.action,
                    error={
                        "code": outcome.code,
                        "status": outcome.status,
                        "message": outcome.action,
                        "actions": [{"type": "upload_data", "label": outcome.action}],
                    },
                )
            )
        else:
            stages.append(stage)
    resolved = dict(plan.resolved_spec)
    decisions = dict(resolved.get("decisions") or {})
    decisions["data"] = outcome.to_dict()
    resolved["decisions"] = decisions
    if outcome.status == "resolved":
        selena_status = str((decisions.get("selena") or {}).get("status") or "")
        resolved["status"] = "resolved" if selena_status == "resolved" else "partial"
        resolved.pop("code", None)
        resolved.pop("action", None)
    elif outcome.status == "requires_agent":
        if resolved.get("status") not in {"needs_input", "blocked"}:
            resolved["status"] = "pending_node"
            resolved["code"] = outcome.code
            resolved["action"] = outcome.action
    elif resolved.get("status") not in {"needs_input", "blocked"}:
        resolved["status"] = "needs_input"
        resolved["code"] = outcome.code
        resolved["action"] = outcome.action
    return StagePlan(stages=tuple(stages), resolved_spec=resolved)


def _apply_data_resolution_to_task_specs(
    tasks: list[dict[str, Any]], outcome: DataResolution, spec: SimulationSpec
) -> list[dict[str, Any]]:
    if outcome.status == "requires_agent":
        environment = next(
            (item for item in tasks if str(item.get("stage_type") or "") == "environment_check"),
            None,
        )
        environment_scope = str(((environment or {}).get("payload") or {}).get("dispatch_scope") or "")
        if environment is not None and environment_scope != "selena_build":
            environment["status"] = "skipped"
            environment["initial_status"] = "skipped"
            environment["skip_reason"] = "data_authorization_runs_in_prepare_data"
    for task in tasks:
        if str(task.get("stage_type") or "") != "prepare_data":
            continue
        if outcome.status == "resolved":
            task["status"] = "skipped"
            task["initial_status"] = "skipped"
            task["skip_reason"] = "data_resolved_during_submission"
            task["input_ref"] = {
                **dict(task.get("input_ref") or {}),
                "dataset_id": outcome.dataset.id if outcome.dataset else "",
            }
        elif outcome.status == "needs_input":
            task["status"] = "blocked"
            task["initial_status"] = "blocked"
            task["skip_reason"] = outcome.action
            task["error"] = {
                "code": outcome.code,
                "status": outcome.status,
                "message": outcome.action,
                "actions": [{"type": "upload_data", "label": outcome.action}],
            }
        else:
            payload = dict(task.get("payload") or {})
            payload.update(
                {
                    "dispatch_scope": "data_upload",
                    "project": spec.project,
                    "data_path": spec.data.path,
                    "required_signals": list(spec.data.required_signals),
                }
            )
            task["payload"] = payload
    return tasks


__all__ = [
    "API_VERSION",
    "ApiV1Error",
    "ApiV1Service",
    "SourceResolutionInputs",
    "DataResolutionProvider",
    "SourceResolutionProvider",
    "SourceResolutionProviderError",
    "format_error_envelope",
    "iter_sse",
    "make_json_safe",
    "V1_SCHEDULER_AGENT_ID",
]
