"""Framework-agnostic v5 `/api/v1` application service.

This module owns only API contract orchestration around ``SimulationSpec`` and
the existing ``ControlService`` store. HTTP, FastAPI, SSE transport, SDK
transport, subprocess, Git, Cluster routing, and Web concerns stay outside.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

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
from core.simulation import normalize_radar_metadata
from core.agent_policy import (
    WINDOWS_CONNECTOR_CONTRACT_VERSION,
    windows_connector_contract_is_current,
)
from core.local_results import ResultCatalog, ResultCatalogError
from core.result_upload_service import ResultUploadService, ResultUploadServiceError
from core.transfer_service import (
    TransferError,
    TransferManifest,
    TransferManifestEntry,
    TransferPlanItem,
    TransferProgress,
    TransferService,
)
from core.business_progress import business_steps

API_VERSION = "v1"
TERMINAL_STATUSES = {"succeeded", "partial", "failed", "cancelled"}
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
    result_upload_service_factory: Callable[[str], ResultUploadService] | None = None
    # The deployment-owned transfer service signs target roots and stores only
    # plan/progress/manifest metadata.  It is intentionally optional so the
    # API can run in a control-only deployment and return the stable
    # ``cluster_direct_transfer_unavailable`` blocker instead of falling back
    # to an HTTP/body upload path.
    transfer_service: TransferService | None = None
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

    def validate_user_run_config(
        self,
        config_payload: dict[str, Any],
        *,
        owner: str = "",
    ) -> dict[str, Any]:
        config = self._parse_user_run_config(config_payload)
        plan = plan_user_run_stages(config)
        selected_target, route_reason = self._select_user_execution_target(owner, config)
        readiness = self._user_run_readiness(owner, config, selected_target)
        return {
            "valid": True,
            "config": config.to_dict(),
            "fingerprint": config.fingerprint(),
            "environment_plan": plan_user_environment_requirements(config),
            "execution": {
                "requested_target": config.simulation.target,
                "selected_target": selected_target,
                "reason": route_reason,
            },
            # A syntactically valid YAML is not necessarily runnable on the
            # current control plane. Keep the distinction explicit and
            # path-free so Web and SDK clients do not submit a job destined to
            # wait/fail for a known infrastructure reason.
            "readiness": readiness,
            "execution_plan": [
                {
                    "stage_type": stage.stage_type,
                    "status": stage.initial_status,
                    "skip_reason": stage.skip_reason,
                }
                for stage in plan.stages
            ],
        }

    def _user_run_readiness(
        self,
        owner: str,
        config: UserRunConfig,
        selected_target: str,
    ) -> dict[str, Any]:
        """Return submit-time capability guidance without exposing internals.

        This checks only information the Linux control plane can know safely:
        registered execution roles and whether a user input inherently needs a
        connected Windows computer. It never serializes agent IDs, local paths
        or the internally recognized product adapter.
        """
        capabilities = self.execution_capabilities(self._owner(owner))["capabilities"]
        blockers: list[dict[str, str]] = []
        notices: list[dict[str, str]] = []

        def block(code: str, message: str, action: str) -> None:
            blockers.append({"code": code, "message": message, "action": action})

        has_windows_build = bool(
            capabilities["windows_light"]["available"]
            or capabilities["windows_full"]["available"]
        )
        local_data = classify_data_path(config.data.path) == "agent"
        local_paths = any(
            classify_data_path(str(value or "")) == "agent"
            for value in (
                config.data.path,
                config.selena.code_path,
                config.selena.selena_build_script,
                config.selena.package_build_script,
                config.selena.existing_path,
                config.selena.runtime_xml,
                config.simulation.adapter_file,
                config.simulation.mat_filter,
            )
        )
        windows_needed = bool(
            selected_target == "local"
            or config.selena.source == "build"
            or local_paths
        )
        connector_update_required = bool(
            windows_needed
            and capabilities["windows_connector"]["update_required"]
            and not capabilities["windows"]["available"]
        )
        if connector_update_required:
            block(
                "windows_connector_update_required",
                "已安装的 Windows 连接组件版本过旧，不能执行当前任务。",
                "请从当前 Web 或 SDK 重新执行“一键连接/更新”；原 Agent ID、路径绑定和 YAML 配置会保留。",
            )
        if selected_target == "cluster" and not capabilities["cluster"]["available"]:
            block(
                "cluster_service_unavailable",
                "Linux 服务当前未连接到 Cluster 调度组件，暂不能提交云端仿真。",
                "请等待服务恢复，或联系部署方检查 Linux 调度与 Cluster 网关。",
            )
        if (
            selected_target == "local"
            and not capabilities["windows_full"]["available"]
            and not connector_update_required
        ):
            block(
                "windows_local_simulation_unavailable",
                "本地仿真需要已连接的完整 Windows 运行环境。",
                "在这台电脑完成一键连接，或将执行位置改为 Cluster。",
            )
        if (
            config.selena.source == "build"
            and not has_windows_build
            and not connector_update_required
        ):
            block(
                "windows_build_unavailable",
                "本地编译需要已连接的 Windows 电脑来访问代码和编译脚本。",
                "在代码所在电脑完成一键连接后重新检查配置。",
            )

        compatible_local_agent = False
        if local_paths:
            probe_job = {"owner": owner, "spec": config.to_dict()}
            compatible_local_agent = self._has_compatible_run_config_agent(
                probe_job,
                owner,
                selected_target=selected_target,
            )
        if (
            local_paths
            and config.selena.source == "existing"
            and selected_target == "cluster"
            and not compatible_local_agent
            and not connector_update_required
        ):
            block(
                "windows_path_access_required",
                "当前没有已连接且能访问本次配置本地路径的 Windows 电脑；已有 Selena + Cluster 不需要安装 Visual Studio 或编译依赖，只需要文件读取/上传连接。",
                "请在 Selena、Runtime、MatFilter 或数据所在电脑一键连接文件访问组件，或将这些路径放到 Cluster 可直接访问的共享位置。",
            )
        elif (
            local_paths
            and has_windows_build
            and not compatible_local_agent
            and not connector_update_required
        ):
            block(
                "windows_path_access_required",
                "当前已连接的 Windows 电脑无法确认能访问本次配置中的本地路径；请连接实际存放文件的电脑。",
                "请在这些文件所在电脑一键连接，或将输入放到当前执行节点可访问的位置。",
            )
        if selected_target == "cluster" and local_data and not has_windows_build and not local_paths:
            block(
                "windows_data_access_unavailable",
                "当前数据位于本机，云端仿真前需要 Windows 电脑验证并上传数据。",
                "在数据所在电脑完成一键连接，或填写 Cluster 可直接访问的数据路径。",
            )

        if config.selena.source == "existing":
            existing_visible = self._server_visible_path(config.selena.existing_path).is_dir()
            runtime_visible = self._server_visible_path(config.selena.runtime_xml).is_file()
            if not (existing_visible and runtime_visible) and not has_windows_build and not local_paths:
                block(
                    "windows_selena_access_unavailable",
                    "现有 Selena 产物和 Runtime XML 需要由已连接的 Windows 电脑读取并打包；这条路径不需要安装 Visual Studio 或编译依赖。",
                    "在保存该 Selena 文件夹的电脑一键连接文件访问组件，或改用 Linux/Cluster 可访问的共享位置。",
                )

        if selected_target == "cluster" and classify_data_path(config.data.path) == "shared":
            notices.append(
                {
                    "code": "shared_path_will_be_checked",
                    "message": "共享数据路径将在 Linux 调度节点按部署映射校验；校验失败时不会启动仿真。",
                    "action": "确认该共享路径已由部署方挂载到 Linux 服务。",
                }
            )
        return {
            "status": "ready" if not blockers else "blocked",
            "can_submit": not blockers,
            "blockers": blockers,
            "notices": notices,
        }

    def submit_user_run(
        self,
        owner: str,
        *,
        config_payload: dict[str, Any],
        dry_run: bool = False,
        idempotency_key: str = "",
        prepared_runtime_bundle_id: str = "",
        client_transfer_roles: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Persist a project-free job; recognition occurs on the execution node.

        This is deliberately separate from the legacy synchronous project
        resolver.  A Linux server cannot inspect a user's Windows code path,
        so the first Stage remains queued for a trusted Agent or local full
        deployment instead of asking the user for an internal project name.
        """
        owner = self._owner(owner)
        config = self._parse_user_run_config(config_payload)
        allowed_transfer_roles = {
            "dataset", "runtime_bundle", "runtime_xml", "mat_filter", "adapter"
        }
        sdk_transfer_roles = {
            str(role or "").strip().lower() for role in client_transfer_roles
            if str(role or "").strip()
        }
        invalid_transfer_roles = sorted(sdk_transfer_roles - allowed_transfer_roles)
        if invalid_transfer_roles:
            raise ApiV1Error(
                "invalid_client_transfer_roles",
                "Client direct-transfer roles are invalid",
                status_code=422,
                detail={"roles": invalid_transfer_roles},
            )
        if (
            config.selena.source == "existing"
            and config.selena.existing_path.startswith("selena-bundle:sha256:")
        ):
            raise ApiV1Error(
                "internal_selena_reference_not_allowed",
                "请填写 Selena 产物文件夹；公开 YAML 不能使用内部 Bundle ID。",
                status_code=422,
                actions=[
                    {
                        "type": "select_existing_selena_folder",
                        "label": "选择 Selena 产物文件夹",
                    }
                ],
            )
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
        # A Linux control plane may validate a logical/shared path, but it
        # never archives a user Selena directory.  Existing Selena folders
        # remain source-side inputs and are either referenced in place or
        # copied by a trusted Connector/SDK through the direct-transfer data
        # plane.  ``prepared_runtime_bundle_id`` is an explicit internal
        # selector supplied out-of-band by a trusted client; no implicit body
        # import is performed here.
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
        selected_target, route_reason = self._select_user_execution_target(owner, config)
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
        # Only the out-of-band SDK/API preparation field may carry an internal
        # logical id. Public existing_path remains a user-visible folder.
        bundle_selector = prepared_bundle_id
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

        # An existing Selena folder supplied from the source-side client is a
        # direct-transfer resource, not a Windows Agent resolution task.  Once
        # the Connector has copied the runtime bundle (and the other local
        # roles) to the Cluster data plane, Linux can validate the Cluster
        # environment and continue.  Keep this narrow: a build source still
        # needs Windows recognition/build, and a registered bundle keeps its
        # catalog/cache semantics below.
        direct_transfer_existing_cluster = bool(
            selected_target == "cluster"
            and config.selena.source == "existing"
            and selected_runtime_bundle is None
            and (
                classify_data_path(str(config.selena.existing_path or "")) == "agent"
                or "runtime_bundle" in sdk_transfer_roles
            )
        )
        for task in task_specs:
            stage_type = str(task.get("stage_type") or "")
            # Private Stage payload only: the Agent needs the authenticated
            # owner when creating owner/device-scoped data bindings.  Public
            # run-stage projections strip payloads entirely.
            task_payload = dict(task.get("payload") or {})
            task_payload.setdefault("owner", owner)
            task["payload"] = task_payload
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
                elif direct_transfer_existing_cluster:
                    # Recognition is represented by the source-side transfer
                    # manifests; there is no Windows resolver to wait for.
                    task["status"] = "skipped"
                    task["initial_status"] = "skipped"
                    task["skip_reason"] = "runtime_bundle_direct_transfer"
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
            elif direct_transfer_existing_cluster and stage_type == "register_artifact":
                # The direct-transfer manifest is the artifact registration
                # evidence for this route; do not release a Windows task.
                task["status"] = "skipped"
                task["initial_status"] = "skipped"
                task["skip_reason"] = "runtime_bundle_direct_transfer"
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
            cluster_route = selected_target == "cluster"
            if cluster_route:
                if (
                    stage_type == "environment_check"
                    and config.selena.source == "existing"
                    and (selected_runtime_bundle is not None or direct_transfer_existing_cluster)
                ):
                    task["assigned_agent_id"] = LINUX_STAGE_AGENT_ID
                    task["required_agent_id"] = LINUX_STAGE_AGENT_ID
                elif stage_type == "prepare_data" and (
                    not sdk_transfer_roles
                    and (
                        str(config.data.path).lower().startswith("dataset://")
                        or classify_data_path(config.data.path) in {"shared", "central"}
                    )
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

        if direct_transfer_existing_cluster:
            # Check shared Cluster infrastructure before copying hundreds of
            # megabytes from a user's computer.  The environment check needs
            # only deployment configuration for this project-free direct
            # route; prepare_data starts after it succeeds.  This also makes a
            # transient Cluster outage retryable without wasting a transfer.
            for task in task_specs:
                stage_type = str(task.get("stage_type") or "")
                if stage_type == "prepare_data":
                    task["dependencies"] = ["environment_check"]
                    payload = dict(task.get("payload") or {})
                    payload.setdefault("project", "run-config-v2")
                    task["payload"] = payload
                elif stage_type == "environment_check":
                    task["dependencies"] = ["resolve_spec"]
                    payload = dict(task.get("payload") or {})
                    payload["dispatch_scope"] = "direct_transfer_environment"
                    task["payload"] = payload
        # A cluster job with any Windows-local input is represented by the
        # existing ``prepare_data`` Stage, but its data-plane dispatch scope is
        # explicit and never the legacy Linux ``data_upload`` path.  The
        # actual TransferPlan is issued later, after ControlService has
        # persisted the Job/Stage and the Connector has enumerated metadata
        # items.  Shared/dataset references are zero-copy and skip the Stage.
        if selected_target == "cluster":
            self._apply_direct_transfer_stage(
                task_specs,
                config,
                selected_runtime_bundle=selected_runtime_bundle,
                selected_runtime_project=selected_runtime_project,
                client_transfer_roles=sdk_transfer_roles,
            )
        elif selected_target == "local":
            self._apply_source_to_local_stage(
                task_specs,
                config,
                owner=owner,
            )
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

    def _apply_direct_transfer_stage(
        self,
        task_specs: list[dict[str, Any]],
        config: UserRunConfig,
        *,
        selected_runtime_bundle: dict[str, Any] | None = None,
        selected_runtime_project: str = "",
        client_transfer_roles: Iterable[str] = (),
    ) -> None:
        """Annotate the project-free data Stage with the P0 data-plane route.

        This method is deliberately metadata-only.  It does not inspect any
        user path, enumerate a directory, open a file, or create a transfer
        plan.  A Connector/SDK performs that work after the Job and Stage have
        been persisted and sends only ``TransferPlanItem`` metadata back to the
        owner-authenticated API.
        """

        stage = next(
            (item for item in task_specs if str(item.get("stage_type") or "") == "prepare_data"),
            None,
        )
        if stage is None:
            return

        data_path = str(config.data.path or "")
        local_sources: list[dict[str, str]] = []
        hinted_roles = {str(role or "").strip().lower() for role in client_transfer_roles}

        def add_local(role: str, value: str) -> None:
            value = str(value or "").strip()
            if not value:
                return
            if classify_data_path(value) == "agent" or role in hinted_roles:
                local_sources.append({"source_role": role, "path": value})

        add_local("dataset", data_path)
        # A selected registered bundle is already a logical internal asset;
        # otherwise an existing Selena folder/Runtime XML must be supplied by
        # the source-side Connector and never archived by Linux.
        if selected_runtime_bundle is None:
            if config.selena.source == "existing":
                add_local("runtime_bundle", config.selena.existing_path)
                add_local("runtime_xml", config.selena.runtime_xml)
            elif config.selena.source == "build":
                # The workspace and build scripts remain on the build Agent.
                # Only the actual build output discovered by that Agent may
                # be emitted from the register_artifact handoff; never treat
                # the source code directory as a runtime bundle to copy.
                add_local("runtime_xml", config.selena.runtime_xml)
        add_local("adapter", config.simulation.adapter_file)
        add_local("mat_filter", config.simulation.mat_filter)

        required_source_roles = {item["source_role"] for item in local_sources}
        # An omitted MatFilter is still a required runtime resource when the
        # source Connector/SDK has repository evidence from which it can infer
        # one.  Declare the role before any plan is issued: otherwise the
        # control plane can mark prepare_data complete after the explicit
        # resources and reject the later inferred MatFilter as an unexpected
        # role.  Keep source_paths path-free for this role until the source
        # node has selected the unique candidate.
        if (
            not str(config.simulation.mat_filter or "").strip()
            and local_sources
            and any(
                str(value or "").strip()
                for value in (
                    config.selena.code_path,
                    config.selena.existing_path,
                    config.selena.selena_build_script,
                    config.selena.runtime_xml,
                )
            )
        ):
            required_source_roles.add("mat_filter")

        payload = dict(stage.get("payload") or {})
        payload.update(
            {
                "contract": "user-run-config/2.0",
                "transfer_mode": "shared_copy",
                "source_roles": sorted(required_source_roles),
                "resource_discovery": {
                    "code_path": str(config.selena.code_path or ""),
                    "existing_path": str(config.selena.existing_path or ""),
                    "selena_build_script": str(config.selena.selena_build_script or ""),
                    "runtime_xml": str(config.selena.runtime_xml or ""),
                },
            }
        )
        if local_sources:
            payload.update(
                {
                    "dispatch_scope": "direct_transfer",
                    "transfer_status": "waiting_for_local_connector",
                    "transfer_required": True,
                    # Paths are the user's own input references.  They remain
                    # private task payload and are never included by
                    # ``_public_run_stage`` or in a TransferPlan response.
                    "source_paths": local_sources,
                    "data_path": data_path,
                    "required_signals": [],
                }
            )
            if selected_runtime_bundle is not None:
                payload["project"] = selected_runtime_project
            # A preselected registered Bundle means the Selena resource is
            # already logical and this Stage is the only remaining source-side
            # data edge.  If deployment has no direct target at that point we
            # can fail closed immediately.  For build/unprepared existing
            # inputs, the Windows Connector itself is still the capability
            # being awaited, so keep the Job resumably queued.
            if not self._direct_transfer_available() and selected_runtime_bundle is not None:
                self._block_direct_transfer_tasks(
                    task_specs,
                    message=(
                        "Cluster direct transfer is unavailable; configure a deployment data-plane root "
                        "or connect a source-side Connector/SDK. Linux will not proxy file bytes."
                    ),
                )
                payload["transfer_status"] = "cluster_direct_transfer_unavailable"
            stage["payload"] = payload
            return

        # dataset://, central and shared paths are already visible to Cluster;
        # the Stage records zero-copy evidence but creates no TransferPlan.
        payload.update(
            {
                "dispatch_scope": "shared_reference",
                "transfer_status": "transfer_skipped_shared",
                "transfer_required": False,
            }
        )
        # Keep the Linux ``prepare_data`` Stage claimable for a bounded
        # shared-path/dataset existence check.  The transfer edge itself is
        # skipped (``transfer_status`` above), but central resolution still
        # records the DatasetRef needed by the Cluster executor's manifest.
        stage["payload"] = payload

    def _direct_transfer_available(self) -> bool:
        """Return whether deployment has a usable target namespace.

        Test doubles may intentionally omit ``client_target_root``; treating
        those as available keeps the scheduler tests focused on route
        selection.  A real ``TransferService`` without an injected root is a
        deterministic infrastructure blocker.
        """

        service = self.transfer_service
        if service is None:
            return False
        root = getattr(service, "client_target_root", None)
        return root is None or bool(str(root).strip())

    @staticmethod
    def _block_direct_transfer_tasks(task_specs: list[dict[str, Any]], *, message: str) -> None:
        """Stop a local-input Cluster DAG before any staging/submit Stage."""

        error = {
            "code": "cluster_direct_transfer_unavailable",
            "status": "needs_input",
            "message": message,
            "actions": [
                {
                    "type": "configure_direct_transfer",
                    "label": "Configure a Cluster direct-transfer root or connect the source client",
                }
            ],
        }
        for task in task_specs:
            status = str(task.get("status") or task.get("initial_status") or "queued")
            if status == "skipped":
                continue
            task["status"] = "blocked"
            task["initial_status"] = "blocked"
            task["skip_reason"] = "cluster_direct_transfer_unavailable"
            task["error"] = dict(error)
            payload = dict(task.get("payload") or {})
            payload["transfer_status"] = "cluster_direct_transfer_unavailable"
            task["payload"] = payload

    def _select_user_execution_target(
        self,
        owner: str,
        config: UserRunConfig,
    ) -> tuple[str, str]:
        requested = config.simulation.target
        if requested != "auto":
            return requested, "explicit_user_selection"
        owner = self._owner(owner)
        capabilities = self.execution_capabilities(owner)["capabilities"]
        cluster_available = bool(capabilities.get("cluster", {}).get("available"))
        resources = self._user_run_resource_paths(config)
        kinds = {
            "central"
            if value.lower().startswith(("dataset://", "shared://"))
            else classify_data_path(value)
            for _role, value in resources
        }
        has_cluster_side_input = bool(kinds.intersection({"shared", "central"}))
        local_full = self._compatible_local_execution_agents(owner, config)

        # A fully shared/registered job is the true zero-copy Cluster route.
        # Mixed local/shared input also stays on Cluster: choosing Windows
        # would assume that a particular laptop can read every remote share,
        # while local inputs already have the supported source->Cluster path.
        if has_cluster_side_input:
            data_value = str(config.data.path or "").strip().lower()
            if data_value.startswith(("dataset://", "shared://")):
                return "cluster", "cluster_accessible_data"
            return (
                "cluster",
                "cluster_zero_copy_inputs" if kinds.issubset({"shared", "central"})
                else "mixed_inputs_cluster_transfer",
            )

        # Select local only when one same-owner Windows execution node can
        # truthfully validate every caller-local resource.  A fresh unified
        # Connector with auto_configure is allowed to perform that validation
        # at claim time; no cross-machine source_to_local transfer is assumed.
        if local_full:
            return "local", "windows_full_available"

        if config.selena.source == "build" and (
            capabilities["windows_light"]["available"]
            or capabilities["windows_full"]["available"]
        ):
            return "cluster", "windows_light_build_then_cluster"
        return (
            "cluster",
            "cluster_available_local_inputs_require_transfer"
            if cluster_available
            else "cluster_fallback_waiting_for_capability",
        )

    @staticmethod
    def _user_run_resource_paths(config: UserRunConfig) -> list[tuple[str, str]]:
        """Return resource roles used by route planning (never public fields)."""
        values: list[tuple[str, str]] = [("dataset", str(config.data.path or ""))]
        if config.selena.source == "existing":
            values.extend(
                [
                    ("runtime_bundle", str(config.selena.existing_path or "")),
                    ("runtime_xml", str(config.selena.runtime_xml or "")),
                ]
            )
        else:
            values.extend(
                [
                    ("code_path", str(config.selena.code_path or "")),
                    ("selena_build_script", str(config.selena.selena_build_script or "")),
                    ("package_build_script", str(config.selena.package_build_script or "")),
                    ("runtime_xml", str(config.selena.runtime_xml or "")),
                ]
            )
        values.extend(
            [
                ("mat_filter", str(config.simulation.mat_filter or "")),
                ("adapter", str(config.simulation.adapter_file or "")),
            ]
        )
        return [(role, value.strip()) for role, value in values if value.strip()]

    def _compatible_local_execution_agents(
        self,
        owner: str,
        config: UserRunConfig,
    ) -> list[Mapping[str, Any]]:
        """Return online same-owner full Connectors for one local route."""

        owner_token = str(owner or "").strip().casefold()
        now = float(self.now_fn())
        result: list[Mapping[str, Any]] = []
        for agent in self._control(owner).list_agents():
            metadata = dict(agent.get("metadata") or {})
            if str(metadata.get("node_kind") or metadata.get("node.kind") or "") != "windows_full":
                continue
            registered_owner = str(metadata.get("user") or "").strip().casefold()
            if registered_owner and owner_token and registered_owner != owner_token:
                continue
            if str(agent.get("status") or "") == "offline":
                continue
            heartbeat = float(agent.get("last_heartbeat") or 0.0)
            if heartbeat and now - heartbeat > 120:
                continue
            if not windows_connector_contract_is_current(metadata):
                continue
            if metadata.get("auto_configure") is True or self._agent_can_read_local_resources(
                agent,
                config,
                owner=owner,
                require_full=True,
            ):
                result.append(agent)
        return result

    @staticmethod
    def _agent_can_read_local_resources(
        agent: Mapping[str, Any],
        config: UserRunConfig,
        *,
        owner: str,
        require_full: bool,
    ) -> bool:
        """Check advertised owner/device/root bindings without project filters."""
        metadata = dict(agent.get("metadata") or {})
        node_kind = str(metadata.get("node_kind") or metadata.get("node.kind") or "")
        if require_full and node_kind != "windows_full":
            return False
        if node_kind not in {"windows_agent", "windows_full"}:
            return False
        registered_owner = str(metadata.get("user") or "").strip().casefold()
        owner_token = str(owner or "").strip().casefold()
        if registered_owner and owner_token and registered_owner != owner_token:
            return False
        if str(agent.get("status") or "") == "offline":
            return False
        workspace_ids = {
            str(item.get("path_id") or "")
            for item in metadata.get("workspace_bindings") or []
            if isinstance(item, dict) and item.get("healthy") is True
        }
        asset_ids = {
            str(item.get("id") or "")
            for item in metadata.get("asset_bindings") or []
            if isinstance(item, dict) and item.get("healthy") is True
        }
        data_bindings = [
            item
            for item in metadata.get("data_bindings") or []
            if isinstance(item, dict) and item.get("healthy") is True
        ]
        from core.agent_bindings import make_workspace_path_id
        from core.agent_asset_bindings import candidate_asset_binding_ids
        from core.agent_data_bindings import candidate_data_binding_ids

        agent_id = str(agent.get("agent_id") or "")
        for role, value in ApiV1Service._user_run_resource_paths(config):
            kind = classify_data_path(value)
            if kind != "agent":
                # Shared/central references are not proven readable by a
                # particular Windows full node from control-plane metadata.
                continue
            if role == "dataset":
                candidates = set(
                    candidate_data_binding_ids(owner=owner, device_id=agent_id, data_path=value)
                )
                for item in data_bindings:
                    if str(item.get("project") or ""):
                        candidates.update(
                            candidate_data_binding_ids(str(item.get("project") or ""), value)
                        )
                if not candidates.intersection(
                    {str(item.get("id") or "") for item in data_bindings}
                ):
                    return False
            elif role == "code_path" and make_workspace_path_id(value) not in workspace_ids:
                return False
            elif role in {"selena_build_script", "package_build_script"}:
                # UserRunConfig requires build scripts to live below code_path;
                # the authorized workspace binding covers those files.
                continue
            elif role == "runtime_bundle" and not set(candidate_asset_binding_ids(value)).intersection(asset_ids):
                return False
            else:
                if not set(candidate_asset_binding_ids(value)).intersection(asset_ids):
                    return False
        return True

    def _apply_source_to_local_stage(
        self,
        task_specs: list[dict[str, Any]],
        config: UserRunConfig,
        *,
        owner: str,
    ) -> None:
        """Fail truthfully when local inputs are not co-located for execution.

        The current deployment TransferService has only a Cluster data-plane
        target root.  It cannot authorize a different Windows machine's cache,
        so this method must never issue a plan disguised as source_to_local.
        """
        stage = next(
            (item for item in task_specs if str(item.get("stage_type") or "") == "prepare_data"),
            None,
        )
        if stage is None:
            return
        local_sources = [
            {"source_role": role, "path": value}
            for role, value in self._user_run_resource_paths(config)
            if classify_data_path(value) == "agent"
        ]
        if not local_sources:
            return
        owner_token = self._owner(owner)
        if self._compatible_local_execution_agents(owner_token, config):
            return
        # A new user must be able to submit first and connect the unified
        # Windows component afterwards.  With no online full Connector this is
        # a normal resumable wait state, not a permanent routing failure.
        now = float(self.now_fn())
        online_full = [
            agent
            for agent in self._control(owner_token).list_agents()
            if str((agent.get("metadata") or {}).get("user") or "").strip().casefold()
            == owner_token.strip().casefold()
            and str(agent.get("mode") or "") == "windows_full"
            and windows_connector_contract_is_current(agent.get("metadata") or {})
            and now - float(agent.get("last_seen") or 0.0) <= 120.0
        ]
        if not online_full:
            return
        payload = dict(stage.get("payload") or {})
        payload.update(
            {
                "contract": "user-run-config/2.0",
                "dispatch_scope": "local_execution_unavailable",
                "transfer_status": "source_to_local_unavailable",
                "transfer_required": False,
                "source_roles": sorted({item["source_role"] for item in local_sources}),
                "source_paths": local_sources,
                "data_path": str(config.data.path or ""),
                "required_signals": [],
            }
        )
        stage["payload"] = payload
        # A source-side -> Windows cache adapter is not wired to a
        # target-specific root yet.  Block before any Agent can claim the
        # Stage; this is intentionally truthful rather than a Cluster-root
        # masquerade.
        self._block_source_to_local_tasks(task_specs)

    @staticmethod
    def _block_source_to_local_tasks(task_specs: list[dict[str, Any]]) -> None:
        error = {
            "code": "source_to_local_unavailable",
            "status": "needs_input",
            "message": "The connected simulation computer cannot read one or more configured inputs.",
            "actions": [
                {
                    "type": "use_co_located_inputs",
                    "label": "Use paths readable by the local simulation computer",
                }
            ],
        }
        for task in task_specs:
            if str(task.get("status") or task.get("initial_status") or "") == "skipped":
                continue
            task["status"] = "blocked"
            task["initial_status"] = "blocked"
            task["skip_reason"] = "source_to_local_unavailable"
            task["error"] = dict(error)
            payload = dict(task.get("payload") or {})
            payload["transfer_status"] = "source_to_local_unavailable"
            task["payload"] = payload

    def _server_visible_path(self, value: str) -> Path:
        """Resolve a raw or administrator-authorized shared path on Linux."""
        candidate = Path(str(value or "")).expanduser()
        if candidate.exists():
            return candidate
        try:
            from core.config import load_cluster_execution_config
            from core.shared_namespace import (
                SharedNamespaceError,
                SharedNamespaceRegistry,
                looks_like_shared_path,
            )

            if not looks_like_shared_path(str(value or "")):
                return candidate
            resolved = SharedNamespaceRegistry.from_config(
                load_cluster_execution_config("run-config-v2")
            ).resolve(str(value))
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
            # Public contract: one Windows connector.  The two legacy keys
            # below remain only so older clients can roll forward safely; Web
            # and SDK callers should consume ``windows``.
            "windows": {
                "available": False,
                "count": 0,
                "configured_count": 0,
                "reconnecting": False,
            },
            "windows_full": {
                "available": False,
                "count": 0,
                "configured_count": 0,
                "reconnecting": False,
                "_compatible_configured_count": 0,
            },
            "windows_light": {
                "available": False,
                "count": 0,
                "configured_count": 0,
                "reconnecting": False,
                "_compatible_configured_count": 0,
            },
            "cluster": {
                "available": False,
                "count": 0,
                "linux_executor_count": 0,
                "platform_gateway_count": 0,
            },
            "windows_connector": {
                "update_required": False,
                "outdated_count": 0,
                "required_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
            },
        }
        owner_token = str(owner or "").strip().casefold()
        for agent in self._control(owner).list_agents():
            metadata = dict(agent.get("metadata") or {})
            node_kind = str(metadata.get("node_kind") or metadata.get("node.kind") or "")
            key = ""
            if node_kind == "windows_full":
                key = "windows_full"
            elif node_kind == "windows_agent":
                key = "windows_light"
            # Windows connections are user-local.  The service intentionally
            # keeps one shared control DB for Cluster scheduling, so do not
            # expose another user's laptop as available to this scope.  Cluster
            # roles (Linux executor and gateway) remain shared infrastructure.
            if key:
                registered_user = str(metadata.get("user") or "").strip().casefold()
                if registered_user and owner_token and registered_user != owner_token:
                    continue
            if key:
                summary[key]["configured_count"] += 1
                if not windows_connector_contract_is_current(metadata):
                    summary["windows_connector"]["outdated_count"] += 1
                    summary["windows_connector"]["update_required"] = True
                    continue
                summary[key]["_compatible_configured_count"] += 1
            last = float(agent.get("last_heartbeat") or 0.0)
            if last <= 0 or now - last > 120 or str(agent.get("status") or "") == "offline":
                continue
            if node_kind == "linux_executor":
                summary["cluster"]["linux_executor_count"] += 1
            elif node_kind == "platform_gateway":
                summary["cluster"]["platform_gateway_count"] += 1
            if key:
                summary[key]["count"] += 1
                summary[key]["available"] = True
        for key in ("windows_full", "windows_light"):
            summary[key]["reconnecting"] = (
                summary[key]["_compatible_configured_count"] > 0
                and not summary[key]["available"]
            )
            summary[key].pop("_compatible_configured_count", None)
        summary["windows"] = {
            "available": any(summary[key]["available"] for key in ("windows_full", "windows_light")),
            "count": sum(summary[key]["count"] for key in ("windows_full", "windows_light")),
            "configured_count": sum(
                summary[key]["configured_count"] for key in ("windows_full", "windows_light")
            ),
            "reconnecting": any(
                summary[key]["reconnecting"] for key in ("windows_full", "windows_light")
            ),
        }
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
        # For the unfiltered task-center view the requested page size is also
        # the database query limit.  The previous implementation always read
        # 100 complete jobs, expanded every task and resolved Runtime Bundle,
        # then discarded all but ``safe_limit``.  On a shared service this
        # made the Web page appear stuck at "正在加载任务" while it serialized
        # megabytes of historical job data.  Status filters still scan the
        # larger window because needs_input is derived from Stage state rather
        # than the raw job status.
        summaries = control.list_jobs(
            # v1 status can be derived from Stage state (for example a queued
            # control job with a blocked Stage is ``needs_input``), so filtering
            # the raw control status would return incorrect task-center pages.
            limit=safe_limit if not requested_status else 100,
            owner=owner,
            status="",
            job_type_prefix="simulation.",
        )
        capabilities = self.execution_capabilities(owner)["capabilities"]
        jobs = [
            self._job_response(
                control.get_job(item["job_id"]),
                execution_capabilities=capabilities,
            )
            for item in summaries
        ]
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

    # ------------------------------------------------------------------
    # Control/data-plane transfer contract
    # ------------------------------------------------------------------

    def issue_transfer_plan(
        self,
        owner: str,
        *,
        job_id: str,
        stage_id: str,
        source_role: str,
        items: Sequence[Mapping[str, Any]],
        source_fingerprints: Optional[Mapping[str, Any]] = None,
        ttl_seconds: float = 86400.0,
    ) -> dict[str, Any]:
        """Issue a deployment-signed, metadata-only direct-transfer plan."""

        owner = self._owner(owner)
        transfer = self.transfer_service
        if transfer is None:
            raise ApiV1Error(
                "cluster_direct_transfer_unavailable",
                "Cluster direct transfer is not configured on this deployment",
                status_code=503,
                actions=[
                    {
                        "type": "configure_direct_transfer",
                        "label": "Configure a deployment-managed Cluster data-plane root",
                    }
                ],
            )
        job = self._get_owned_job(owner, str(job_id or ""))
        stage_key = str(stage_id or "").strip()
        stage = next(
            (
                item
                for item in list(job.get("stages") or job.get("tasks") or [])
                if str(item.get("stage_id") or item.get("task_id") or "") == stage_key
            ),
            None,
        )
        if stage is None:
            raise ApiV1Error(
                "transfer_stage_not_found",
                "Transfer Stage is not part of this Job",
                status_code=404,
                detail={"job_id": str(job_id), "stage_id": stage_key},
            )
        stage_payload = dict(stage.get("payload") or {})
        if str(stage_payload.get("dispatch_scope") or "") != "direct_transfer":
            raise ApiV1Error(
                "transfer_stage_not_direct",
                "Transfer plans are allowed only for a direct-transfer Stage",
                status_code=403,
                detail={"job_id": str(job_id), "stage_id": stage_key},
            )
        transfer_mode = str(stage_payload.get("transfer_mode") or "shared_copy").strip().lower()
        if transfer_mode == "source_to_local":
            raise ApiV1Error(
                "source_to_local_unavailable",
                "A target-specific Windows cache adapter is not configured",
                status_code=503,
                actions=[
                    {
                        "type": "use_co_located_inputs",
                        "label": "Use inputs readable by the local simulation computer",
                    }
                ],
            )
        if transfer_mode == "gateway_upload":
            raise ApiV1Error(
                "cluster_direct_transfer_unavailable",
                "gateway_upload is not available in the current transfer kernel",
                status_code=503,
                actions=[
                    {
                        "type": "use_shared_copy",
                        "label": "Use a deployment-managed direct-transfer root",
                    }
                ],
            )
        if transfer_mode != "shared_copy":
            raise ApiV1Error(
                "invalid_transfer_mode",
                "Unsupported transfer mode",
                status_code=422,
                detail={"mode": transfer_mode},
            )
        allowed_roles = {
            str(item).strip()
            for item in list(stage_payload.get("source_roles") or [])
            if str(item).strip()
        }
        requested_role = str(source_role or "").strip()
        if requested_role not in allowed_roles:
            raise ApiV1Error(
                "transfer_source_role_not_allowed",
                "source_role is not required by this direct-transfer Stage",
                status_code=422,
                detail={"source_role": requested_role},
            )
        if not items:
            raise ApiV1Error(
                "invalid_transfer_item",
                "At least one metadata item is required before issuing a transfer plan",
                status_code=422,
            )
        plan_items: list[TransferPlanItem] = []
        allowed_item_fields = {"source_role", "relative_path", "size", "checksum", "sha256", "mtime_ns"}
        try:
            for raw in items:
                if not isinstance(raw, Mapping):
                    raise ValueError("transfer item must be an object")
                unknown = set(raw) - allowed_item_fields
                if unknown:
                    raise ValueError("transfer item contains unsupported fields")
                item_role = str(raw.get("source_role") or requested_role or "").strip()
                if item_role != requested_role:
                    raise ValueError("item source_role must match the request source_role")
                plan_items.append(
                    TransferPlanItem(
                        source_role=item_role,
                        relative_path=str(raw.get("relative_path") or ""),
                        size=int(raw.get("size") or 0),
                        checksum=str(raw.get("checksum") or raw.get("sha256") or ""),
                        mtime_ns=(None if raw.get("mtime_ns") is None else int(raw.get("mtime_ns"))),
                    )
                )
        except (TypeError, ValueError) as exc:
            raise ApiV1Error(
                "invalid_transfer_item",
                "Transfer plan items must contain metadata only",
                status_code=422,
                detail={"error": str(exc)},
            ) from exc
        try:
            plan = transfer.issue_plan(
                owner=owner,
                job_id=str(job_id),
                stage_id=stage_key,
                # The Stage's server-side route selects the mode.  Clients
                # cannot override a Cluster shared-copy or local
                # source-to-local decision in the request body.
                mode=transfer_mode,
                source_role=requested_role,
                items=tuple(plan_items),
                source_fingerprints=source_fingerprints,
                ttl_seconds=float(ttl_seconds),
            )
        except TransferError as exc:
            raise ApiV1Error(
                exc.code,
                exc.message,
                status_code=exc.status_code,
                detail=exc.detail,
                actions=exc.actions,
            ) from exc
        return plan.to_dict()

    def get_transfer_plan(self, owner: str, transfer_id: str) -> dict[str, Any]:
        transfer = self._require_transfer_service()
        try:
            return transfer.get_plan(str(transfer_id or ""), owner=self._owner(owner)).to_dict()
        except TransferError as exc:
            raise ApiV1Error(exc.code, exc.message, status_code=exc.status_code, detail=exc.detail, actions=exc.actions) from exc

    def get_job_transfer_status(self, owner: str, job_id: str) -> dict[str, Any]:
        self._get_owned_job(owner, str(job_id or ""))
        transfer = self._require_transfer_service()
        try:
            result = transfer.get_job_transfer_status(self._owner(owner), str(job_id))
        except TransferError as exc:
            raise ApiV1Error(exc.code, exc.message, status_code=exc.status_code, detail=exc.detail, actions=exc.actions) from exc
        return {"job_id": str(job_id), **result}

    def report_transfer_progress(self, owner: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        transfer = self._require_transfer_service()
        try:
            progress = TransferProgress(
                transfer_id=str(payload.get("transfer_id") or ""),
                owner_scope=str(payload.get("owner_scope") or ""),
                bytes_transferred=int(payload.get("bytes_transferred") or 0),
                bytes_total=int(payload.get("bytes_total") or 0),
                current_file=str(payload.get("current_file") or ""),
                status=str(payload.get("status") or "in_progress"),
                updated_at=float(payload.get("updated_at") or self.now_fn()),
            )
            transfer.report_progress(progress, owner=self._owner(owner))
            plan = transfer.get_plan(progress.transfer_id, owner=self._owner(owner))
            return {"transfer_id": progress.transfer_id, "status": plan.status, "progress": progress.to_dict()}
        except TransferError as exc:
            raise ApiV1Error(exc.code, exc.message, status_code=exc.status_code, detail=exc.detail, actions=exc.actions) from exc

    def receive_transfer_manifest(self, owner: str, transfer_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        transfer = self._require_transfer_service()
        try:
            plan = transfer.get_plan(str(transfer_id or ""), owner=self._owner(owner))
            entries: list[TransferManifestEntry] = []
            for raw in payload.get("entries") or ():
                if not isinstance(raw, Mapping):
                    raise ValueError("manifest entry must be an object")
                entries.append(
                    TransferManifestEntry(
                        relative_path=str(raw.get("relative_path") or ""),
                        size=int(raw.get("size") or 0),
                        checksum=str(raw.get("checksum") or raw.get("sha256") or ""),
                        target_logical_ref=str(raw.get("target_logical_ref") or raw.get("storage_ref") or ""),
                        mtime_ns=int(raw.get("mtime_ns") or 0),
                        status=str(raw.get("status") or "completed"),
                        result=str(raw.get("result") or ""),
                        started_at=float(raw.get("started_at") or 0.0),
                        completed_at=float(raw.get("completed_at") or 0.0),
                    )
                )
            manifest = TransferManifest(
                transfer_id=str(payload.get("transfer_id") or transfer_id or ""),
                # The authenticated request owner is authoritative; a body
                # owner is ignored so clients cannot self-report another
                # user's scope.
                owner=self._owner(owner),
                owner_scope=str(payload.get("owner_scope") or plan.owner_scope),
                job_id=str(payload.get("job_id") or plan.job_id),
                entries=tuple(entries),
                total_bytes=(None if payload.get("total_bytes") is None else int(payload.get("total_bytes"))),
                started_at=float(payload.get("started_at") or 0.0),
                completed_at=float(payload.get("completed_at") or 0.0),
                status=str(payload.get("status") or "completed"),
            )
            if manifest.transfer_id != str(transfer_id or ""):
                raise ValueError("manifest transfer_id does not match route")
            result = transfer.receive_manifest(manifest, owner=self._owner(owner))
            radar_metadata = (
                normalize_radar_metadata(plan.source_fingerprints)
                if plan.source_role == "dataset"
                else {}
            )
            transfer_projection = {
                "transfer_id": manifest.transfer_id,
                "source_role": plan.source_role,
                "entries": [
                    {
                        "relative_path": entry.relative_path,
                        "size": entry.size,
                        "sha256": entry.sha256,
                        "storage_ref": entry.storage_ref,
                    }
                    for entry in manifest.entries
                ],
            }
            if radar_metadata:
                transfer_projection["radar"] = radar_metadata
            completed_job = self._control(self._owner(owner)).complete_transfer_stage(
                manifest.job_id or plan.job_id,
                plan.stage_id,
                owner=self._owner(owner),
                source_role=plan.source_role,
                transfer=transfer_projection,
            )
            completed_stage = next(
                (
                    item
                    for item in completed_job.get("stages") or []
                    if str(item.get("stage_id") or "") == plan.stage_id
                ),
                {},
            )
            output_ref = dict(completed_stage.get("output_ref") or {})
            stage_result = dict(completed_stage.get("result") or {})
            return {
                **result,
                "manifest": manifest.to_dict(),
                "job_id": plan.job_id,
                "stage_id": plan.stage_id,
                "stage_status": str(completed_stage.get("status") or ""),
                "transfer_status": str(output_ref.get("transfer_status") or result.get("status") or ""),
                "remaining_roles": list(stage_result.get("remaining_roles") or output_ref.get("remaining_roles") or []),
                "job_status": completed_job.get("status"),
            }
        except TransferError as exc:
            raise ApiV1Error(exc.code, exc.message, status_code=exc.status_code, detail=exc.detail, actions=exc.actions) from exc
        except (TypeError, ValueError) as exc:
            raise ApiV1Error("invalid_transfer_manifest", "Transfer manifest must contain metadata only", status_code=422, detail={"error": str(exc)}) from exc

    def cancel_transfer(self, owner: str, transfer_id: str) -> dict[str, Any]:
        transfer = self._require_transfer_service()
        try:
            return transfer.cancel_transfer(str(transfer_id or ""), owner=self._owner(owner))
        except TransferError as exc:
            raise ApiV1Error(exc.code, exc.message, status_code=exc.status_code, detail=exc.detail, actions=exc.actions) from exc

    def _require_transfer_service(self) -> TransferService:
        if self.transfer_service is None:
            raise ApiV1Error(
                "cluster_direct_transfer_unavailable",
                "Cluster direct transfer is not configured on this deployment",
                status_code=503,
                actions=[{"type": "configure_direct_transfer", "label": "Configure a deployment-managed Cluster data-plane root"}],
            )
        return self.transfer_service

    def diagnosis(self, owner: str, job_id: str) -> dict[str, Any]:
        """Return a stable, path-free diagnosis for Web, SDK and AI adapters.

        The Job is authoritative for orchestration failures while an explicit
        failed Manifest is authoritative for the simulation business outcome.
        This distinction intentionally allows a failed simulation to retain
        downloadable artifacts.
        """
        owner = self._owner(owner)
        job = self._get_owned_job(owner, job_id)
        status = self._job_response(job)["status"]
        manifest_payload = self.manifest(owner, job_id)
        manifest = (
            dict(manifest_payload["manifest"])
            if isinstance(manifest_payload.get("manifest"), dict)
            else {}
        )
        manifest_status = _normalize_manifest_status(manifest.get("status"))
        result_ref = _public_result_ref(manifest.get("result_ref"))
        artifacts_available = self._result_is_available(owner, result_ref)

        stages = list(job.get("stages") or job.get("tasks") or [])
        failed_stage = next(
            (stage for stage in stages if str(stage.get("status") or "") == "failed"),
            None,
        )
        blocked_stage = next(
            (stage for stage in stages if str(stage.get("status") or "") == "blocked"),
            None,
        )
        problem_stage = failed_stage or blocked_stage
        error = dict((problem_stage or {}).get("error") or {})
        job_result = dict(job.get("result") or {})
        source_code = _safe_diagnostic_code(
            error.get("code") or job_result.get("code")
        )
        stage_type = _safe_diagnostic_code(
            (problem_stage or {}).get("stage_type")
            or (problem_stage or {}).get("task_type")
        )
        stage_id = str(
            (problem_stage or {}).get("stage_id")
            or (problem_stage or {}).get("task_id")
            or ""
        )
        if not source_code and stage_id:
            # Older Agent versions recorded the structured failure only in the
            # immutable stage.failed event.  Recover its stable code for
            # diagnosis without exposing the historical result body or
            # changing the audit record in-place.
            source_code = _historical_stage_failure_code(
                self._control(owner),
                str(job["job_id"]),
                stage_id,
            )

        warnings: list[str] = []
        manifest_failed = manifest_status == "failed"
        manifest_partial = manifest_status == "partial"
        manifest_succeeded = manifest_status == "succeeded"
        if (
            (status == "succeeded" and manifest_failed)
            or (status == "failed" and manifest_succeeded)
        ):
            warnings.append("job_manifest_outcome_mismatch")
        if result_ref and not artifacts_available:
            warnings.append("result_reference_unavailable")

        if manifest_partial:
            summary_counts = dict(manifest.get("summary") or {})
            def count(value: Any) -> int:
                try:
                    return max(int(value or 0), 0)
                except (TypeError, ValueError):
                    return 0

            succeeded_count = count(
                summary_counts.get("succeeded_input_count")
                or summary_counts.get("success_count")
                or 0
            )
            failed_count = count(
                summary_counts.get("failed_input_count")
                or summary_counts.get("failed_count")
                or summary_counts.get("fail_count")
                or 0
            )
            total_count = count(
                summary_counts.get("total_input_count")
                or summary_counts.get("task_count")
                or succeeded_count + failed_count
            )
            outcome = "partial"
            code = "simulation_partial"
            category = "simulation"
            summary = (
                f"Simulation partially completed: {succeeded_count}/{total_count} inputs succeeded "
                f"and {failed_count} failed."
            )
        elif manifest_failed:
            outcome = "failed"
            code = "simulation_failed"
            category = "simulation"
            summary = (
                "Simulation completed with a failed outcome; result artifacts "
                "may still be available."
            )
        elif status == "failed":
            outcome = "failed"
            category = _diagnostic_category(source_code, stage_type)
            code = f"{category}_failed"
            summary = _failure_summary(category, stage_type)
        elif status == "succeeded":
            outcome = "succeeded"
            code = "job_succeeded"
            category = "none"
            summary = "Simulation completed successfully."
        elif status == "cancelled":
            outcome = "cancelled"
            code = "job_cancelled"
            category = "none"
            summary = "The task was cancelled."
        elif status == "needs_input":
            outcome = "needs_input"
            code = "job_needs_input"
            category = "configuration"
            summary = (
                "The task needs configuration or a connected execution "
                "resource before it can continue."
            )
        else:
            outcome = "pending"
            code = "job_running" if status in {"running", "cancelling"} else "job_queued"
            category = "none"
            summary = "The task is still running." if status == "running" else "The task is waiting to run."

        action = _diagnostic_action(
            outcome=outcome,
            category=category,
            job_id=str(job["job_id"]),
            stage_id=stage_id,
            result_ref=result_ref if artifacts_available else "",
            available_actions=self._available_actions(str(job["job_id"]), status, stages),
        )
        return {
            "schema_version": "radar-sim.job-diagnosis/1.0",
            "job_id": str(job["job_id"]),
            "status": status,
            "terminal": status in TERMINAL_STATUSES,
            "outcome": outcome,
            "code": code,
            "category": category,
            "summary": summary,
            "action": action,
            "artifacts_available": artifacts_available,
            "result_ref": result_ref,
            "evidence": {
                "job_status": status,
                "manifest_available": bool(manifest_payload["available"]),
                "manifest_status": manifest_status,
                "failed_stage": (
                    {
                        "stage_type": stage_type,
                        "stage_id": stage_id,
                        "source_code": source_code,
                    }
                    if problem_stage is not None
                    else None
                ),
            },
            "consistency": {
                "state": "warning" if warnings else "consistent",
                "warnings": warnings,
            },
        }

    def _result_is_available(self, owner: str, result_ref: str) -> bool:
        if not result_ref or self.result_catalog is None:
            return False
        try:
            self.result_catalog.get(result_ref, owner=owner)
            return True
        except ResultCatalogError:
            return False

    def events(
        self,
        owner: str,
        job_id: str,
        *,
        since: int = 0,
        limit: int = 200,
        tail: bool = False,
    ) -> dict[str, Any]:
        job = self._get_owned_job(owner, job_id)
        safe_limit = min(max(int(limit or 200), 1), 1000)
        cursor = max(int(since or 0), 0)
        page = self._control(self._owner(owner)).list_events(
            job_id,
            since=cursor,
            limit=safe_limit,
            tail=bool(tail),
        )
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
        owner = self._owner(owner)
        control = self._control(owner)
        trusted_metadata = dict(metadata or {})
        node_kind = str(
            trusted_metadata.get("node_kind")
            or trusted_metadata.get("node.kind")
            or ""
        )
        # Windows execution is user-local.  In the shared control database the
        # owner must come from the authenticated/pairing identity, never from a
        # client-supplied metadata field.
        if node_kind in {"windows_agent", "windows_full"}:
            trusted_metadata["user"] = owner
        return control.register_agent(
            name, agent_id=agent_id, hostname=hostname, platform=platform,
            capabilities=capabilities, metadata=trusted_metadata,
            node_kind=node_kind,
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
        try:
            completed = control.submit_task_result(
                task_id, agent_id=agent_id, status=status,
                returncode=returncode, result=result,
            )
        except ValueError as exc:
            # Direct-transfer manifests can complete a prepare_data Stage as
            # soon as the last resource is accepted.  The Windows Agent then
            # sends its ordinary task-result callback, which is a harmless
            # duplicate.  Treat only an assigned Agent's already-terminal
            # callback as idempotent; keep assignment and unknown-task errors
            # visible to callers.
            if not str(exc).startswith(f"task already completed: {task_id}"):
                raise
            try:
                task = control.get_task(task_id)
            except KeyError:
                raise exc
            assigned = {
                str(task.get("assigned_agent_id") or ""),
                str(task.get("required_agent_id") or ""),
            }
            if str(agent_id or "") not in assigned:
                raise exc
            completed = control.get_job(str(task.get("job_id") or ""))
            return completed
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

    def create_result_upload(
        self,
        owner: str,
        *,
        run_ref: str,
        archive_size: int,
        archive_checksum: str,
    ) -> dict[str, Any]:
        return self._result_upload_call(
            owner,
            lambda service: service.create(
                owner,
                run_ref=run_ref,
                archive_size=archive_size,
                archive_checksum=archive_checksum,
            ),
        )

    def get_result_upload(self, owner: str, session_id: str) -> dict[str, Any]:
        return self._result_upload_call(owner, lambda service: service.get(owner, session_id))

    def append_result_upload(
        self,
        owner: str,
        session_id: str,
        *,
        offset: int,
        data: bytes,
    ) -> dict[str, Any]:
        return self._result_upload_call(
            owner,
            lambda service: service.append(owner, session_id, offset=offset, data=data),
        )

    def finalize_result_upload(
        self,
        owner: str,
        session_id: str,
        *,
        files: Iterable[Mapping[str, Any]],
        retain_until: float = 0,
    ) -> dict[str, Any]:
        return self._result_upload_call(
            owner,
            lambda service: service.finalize(
                owner,
                session_id,
                files=files,
                retain_until=retain_until,
            ),
        )

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

    def _result_upload_call(
        self,
        owner: str,
        callback: Callable[[ResultUploadService], dict[str, Any]],
    ) -> dict[str, Any]:
        if self.result_upload_service_factory is None:
            raise ApiV1Error(
                "result_upload_unavailable",
                "Result upload service is unavailable",
                status_code=503,
                actions=[{"type": "retry", "label": "Retry after the result service is configured"}],
            )
        try:
            return callback(self.result_upload_service_factory(owner))
        except ResultUploadServiceError as exc:
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

    def _job_response(
        self,
        job: dict[str, Any],
        *,
        execution_capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_stages = list(job.get("stages") or job.get("tasks") or [])
        is_run_config = str((job.get("metadata") or {}).get("contract") or "") == "user-run-config/2.0"
        stages = [self._public_run_stage(item) for item in raw_stages] if is_run_config else raw_stages
        status = self._v1_status(job)
        response = {
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
            "progress": self._job_progress(stages, status),
            "current_stage": self._current_stage(stages),
            "available_actions": self._available_actions(str(job["job_id"]), status, stages),
            "business_steps": business_steps(stages) if is_run_config else [],
            "stages": stages,
            "tasks": stages if is_run_config else list(job.get("tasks") or []),
            "metadata": dict(job.get("metadata") or {}),
        }
        waiting = self._windows_waiting(
            job,
            raw_stages,
            status,
            execution_capabilities=execution_capabilities,
        ) if is_run_config else None
        response["waiting"] = waiting
        if waiting and waiting.get("reason") == "windows_connector_update_required":
            response["status"] = "needs_input"
            update_action = dict(waiting.get("action") or {})
            if update_action and update_action not in response["available_actions"]:
                response["available_actions"].append(update_action)
        return response

    def _windows_waiting(
        self,
        job: dict[str, Any],
        stages: list[dict[str, Any]],
        status: str,
        *,
        execution_capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Describe a path-free Windows connection wait for Web and SDK clients."""
        if status in TERMINAL_STATUSES or status == "cancelling":
            return None
        owner = str(job.get("owner") or (job.get("metadata") or {}).get("owner") or "")
        capabilities = execution_capabilities or self.execution_capabilities(owner)["capabilities"]
        current = next(
            (
                stage for stage in stages
                if str(stage.get("status") or "") == "queued"
                and str(stage.get("stage_type") or stage.get("task_type") or "")
                == self._current_stage(stages)
            ),
            None,
        )
        if current is None and bool(
            (capabilities.get("windows_connector") or {}).get("update_required")
        ):
            owner_token = owner.strip().casefold()
            outdated_agent_ids = {
                str(agent.get("agent_id") or "")
                for agent in self._control(owner).list_agents()
                if str((agent.get("metadata") or {}).get("user") or "").strip().casefold()
                == owner_token
                and not windows_connector_contract_is_current(agent.get("metadata") or {})
            }
            current = next(
                (
                    stage for stage in stages
                    if str(stage.get("status") or "") == "running"
                    and str(stage.get("assigned_agent_id") or "") in outdated_agent_ids
                ),
                None,
            )
        if current is None:
            return None

        stage_type = str(current.get("stage_type") or current.get("task_type") or "")
        spec = dict(job.get("spec") or (job.get("payload") or {}).get("spec") or {})
        selena = dict(spec.get("selena") or {})
        simulation = dict(spec.get("simulation") or {})
        data = dict(spec.get("data") or {})
        resolved = dict(job.get("resolved_spec") or {})
        decisions = dict(resolved.get("decisions") or {})
        execution = dict(decisions.get("execution") or {})
        target = str(execution.get("selected_target") or simulation.get("target") or "auto")
        source = str(selena.get("source") or selena.get("mode") or "auto")
        selena_decision = dict(decisions.get("selena") or {})
        # Filter local inputs independently by the direct-transfer role that
        # has already received a resolved Manifest.  A direct runtime bundle
        # decision itself is not enough: dataset/Runtime XML/MatFilter/Adapter
        # may still be local and must keep the source-side wait visible.
        transfer_resources = dict((decisions.get("transfers") or {}).get("resources") or {})

        def transfer_role_resolved(role: str) -> bool:
            value = transfer_resources.get(role)
            if isinstance(value, dict):
                return str(value.get("status") or "") == "resolved"
            if isinstance(value, list):
                return any(
                    isinstance(item, dict) and str(item.get("status") or "") == "resolved"
                    for item in value
                )
            return False

        local_inputs = [
            ("dataset", data.get("path")),
            ("adapter", simulation.get("adapter_file")),
            ("mat_filter", simulation.get("mat_filter")),
        ]
        catalog_runtime_bundle = bool(
            source == "existing"
            and (
                str(selena_decision.get("code") or "") == "registered_runtime_bundle_selected"
                or str(selena_decision.get("action") or "") == "use_runtime_bundle"
            )
        )
        if source == "existing" and not catalog_runtime_bundle:
            local_inputs.extend(
                [
                    ("runtime_bundle", selena.get("existing_path")),
                    ("runtime_xml", selena.get("runtime_xml")),
                ]
            )
        elif source == "build":
            # Build+Cluster still uses the Windows resolver/build branch, but
            # its explicitly selected Runtime XML can also be a direct local
            # input and must retain the old path-access check.
            local_inputs.append(("runtime_xml", selena.get("runtime_xml")))
        local_path_values = [
            value
            for role, value in local_inputs
            if classify_data_path(str(value or "")) == "agent"
            and not transfer_role_resolved(role)
        ]

        connector_state = dict(capabilities.get("windows_connector") or {})
        if (
            connector_state.get("update_required") is True
            and not bool(capabilities["windows"]["available"])
            and (target == "local" or source == "build" or bool(local_path_values))
        ):
            return {
                "reason": "windows_connector_update_required",
                "mode": "unified",
                "stage": stage_type,
                "missing_capability": "windows_connector_contract",
                "connection_state": "update_required",
                "message": "The installed Windows Connector is too old for this task contract.",
                "action": {
                    "type": "update_windows_connector",
                    "label": "Update this Windows computer",
                    "mode": "unified",
                },
            }

        # A Windows capability is not enough for a local-path task.  The
        # connected machine must either advertise the exact opaque binding or
        # be a genuinely fresh one-click Agent with no prior bindings.  This
        # prevents an unrelated configured laptop from looking "ready" and
        # then failing on the first folder existence check.
        if stage_type == "resolve_spec":
            owner = str(job.get("owner") or (job.get("metadata") or {}).get("owner") or "")
            if target == "local" and capabilities["windows_full"]["available"]:
                if not self._has_compatible_run_config_agent(job, owner, selected_target="local"):
                    return self._path_match_waiting(
                        mode="full",
                        capabilities=capabilities,
                    )
            elif (
                target != "local"
                and (
                    capabilities["windows_light"]["available"]
                    or capabilities["windows_full"]["available"]
                )
                and any(
                    classify_data_path(str(value or "")) == "agent"
                    for value in local_path_values
                )
                and not self._has_compatible_run_config_agent(job, owner, selected_target=target)
            ):
                return self._path_match_waiting(
                    mode="light",
                    capabilities=capabilities,
                )

        mode = ""
        message = ""
        if target == "local" and not capabilities["windows_full"]["available"]:
            mode = "full"
            message = "This task is waiting for a connected Windows computer with local simulation capability."
        elif (
            source == "build"
            and stage_type in {"resolve_spec", "environment_check", "prepare_source", "build_selena", "register_artifact"}
            and not (
                capabilities["windows_light"]["available"]
                or capabilities["windows_full"]["available"]
            )
        ):
            mode = "light"
            message = "This task is waiting for a connected Windows computer with build capability."
        elif (
            target != "local"
            and stage_type in {"resolve_spec", "environment_check", "prepare_data", "register_artifact"}
            and any(classify_data_path(str(value or "")) == "agent" for value in local_path_values)
            and not (
                capabilities["windows_light"]["available"]
                or capabilities["windows_full"]["available"]
            )
        ):
            mode = "light"
            message = "This task is waiting for a connected Windows computer that can access local files."
        if not mode:
            return None
        if mode == "full":
            reconnecting = bool(capabilities["windows_full"].get("configured_count"))
        else:
            reconnecting = bool(
                capabilities["windows_light"].get("configured_count")
                or capabilities["windows_full"].get("configured_count")
            )
        return {
            "reason": "windows_connection_required",
            "mode": mode,
            "stage": stage_type,
            "missing_capability": "windows_full" if mode == "full" else "windows_light",
            "connection_state": "reconnecting" if reconnecting else "not_configured",
            "message": (
                "This configured Windows computer is temporarily offline and should reconnect automatically."
                if reconnecting
                else message
            ),
            "action": {
                "type": "wait_windows_reconnect" if reconnecting else "connect_windows",
                "label": "Wait for automatic reconnection" if reconnecting else "Connect this Windows computer",
                "mode": mode,
            },
        }

    def _has_compatible_run_config_agent(
        self,
        job: dict[str, Any],
        owner: str,
        *,
        selected_target: str,
    ) -> bool:
        """Return whether an online Windows Agent can safely claim resolve_spec.

        Matching is deliberately path-free on the wire: the server computes
        the same opaque IDs as the Agent advertises.  A fresh one-click Agent
        is accepted only when it has no healthy bindings, so an old configured
        machine cannot claim another user's arbitrary local folders.
        """
        from core.agent_bindings import make_workspace_path_id
        from core.agent_asset_bindings import candidate_asset_binding_ids

        spec = dict(job.get("spec") or (job.get("payload") or {}).get("spec") or {})
        selena = dict(spec.get("selena") or {})
        source = str(selena.get("source") or selena.get("mode") or "")
        code_path = str(selena.get("code_path") or "").strip()
        runtime_xml = str(selena.get("runtime_xml") or "").strip()
        simulation = dict(spec.get("simulation") or {})
        asset_candidates: set[str] = set()
        for value in (
            runtime_xml,
            selena.get("existing_path"),
            simulation.get("adapter_file"),
            simulation.get("mat_filter"),
        ):
            asset_candidates.update(candidate_asset_binding_ids(str(value or "")))
        expected_path_id = make_workspace_path_id(code_path) if code_path else ""
        owner_token = str(owner or "").strip().casefold()
        now = float(self.now_fn())
        for agent in self._control(owner).list_agents():
            metadata = dict(agent.get("metadata") or {})
            node_kind = str(metadata.get("node_kind") or metadata.get("node.kind") or "")
            if node_kind not in {"windows_agent", "windows_full"}:
                continue
            if not windows_connector_contract_is_current(metadata):
                continue
            if selected_target == "local" and node_kind != "windows_full":
                continue
            if str(agent.get("status") or "") == "offline":
                continue
            last_heartbeat = float(agent.get("last_heartbeat") or 0.0)
            if last_heartbeat and now - last_heartbeat > 120:
                continue
            registered_user = str(metadata.get("user") or "").strip().casefold()
            if registered_user and owner_token and registered_user != owner_token:
                continue
            workspace_ids = {
                str(item.get("path_id") or "")
                for item in metadata.get("workspace_bindings") or []
                if isinstance(item, dict)
                and item.get("healthy") is True
            }
            asset_ids = {
                str(item.get("id") or "")
                for item in metadata.get("asset_bindings") or []
                if isinstance(item, dict)
                and item.get("healthy") is True
            }
            data_ids = {
                str(item.get("id") or "")
                for item in metadata.get("data_bindings") or []
                if isinstance(item, dict)
                and item.get("healthy") is True
            }
            has_bindings = bool(workspace_ids or asset_ids or data_ids)
            auto_configure = metadata.get("auto_configure") is True
            # A one-click Agent is owner-scoped.  Once it has successfully
            # configured one workspace/data root, the same owner may submit a
            # different local project or recording without reinstalling or
            # manually registering another binding.  The Agent still cannot
            # claim another owner's job because the metadata owner check above
            # is enforced before this branch.  Existing bindings remain useful
            # for path-specific matching, while auto-configuration is the
            # safe fallback for this owner's new local paths.
            if auto_configure and owner_token and registered_user == owner_token:
                return True
            if auto_configure and not has_bindings:
                return True
            # Legacy/manual Agents predate the binding advertisement.  Keep
            # their explicit registration compatible; the stricter guard is
            # for the one-click auto-configured pool that caused this failure.
            if not auto_configure and not has_bindings:
                return True
            if source == "existing":
                if expected_path_id and expected_path_id in workspace_ids:
                    return True
                if asset_candidates.intersection(asset_ids):
                    return True
                continue
            if source == "build":
                workspace_match = bool(expected_path_id and expected_path_id in workspace_ids)
                asset_match = bool(asset_candidates.intersection(asset_ids))
                if workspace_match and asset_match:
                    return True
        return False

    @staticmethod
    def _path_match_waiting(*, mode: str, capabilities: dict[str, Any]) -> dict[str, Any]:
        configured = bool(
            capabilities.get("windows_light", {}).get("configured_count")
            or capabilities.get("windows_full", {}).get("configured_count")
        )
        return {
            "reason": "windows_path_access_required",
            "mode": mode,
            "stage": "resolve_spec",
            "missing_capability": "windows_full" if mode == "full" else "windows_light",
            "connection_state": "connected_but_path_unavailable" if configured else "not_configured",
            "message": (
                "在线 Windows 连接已存在，但无法确认它能访问本任务的本地路径。"
                "请在文件所在电脑一键连接，或改用 Cluster 可直接访问的共享路径。"
            ),
            "action": {
                "type": "connect_windows",
                "label": "连接存放文件的 Windows 电脑",
                "mode": mode,
            },
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
        if status in TERMINAL_STATUSES:
            return 1.0
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
        # A queued stage may be listed before the actually claimable stage in
        # the fixed ten-stage representation.  Prefer a queued stage whose
        # explicit dependencies have reached a success state; this makes a
        # direct-transfer ``prepare_data`` barrier visible before the Linux
        # environment stage without changing ordinary resolver-first jobs.
        statuses = {
            str(stage.get("stage_id") or stage.get("task_id") or ""): str(stage.get("status") or "")
            for stage in stages
        }

        def ready(stage: dict[str, Any]) -> bool:
            dependencies = list(stage.get("dependencies") or [])
            if not dependencies:
                return True
            return all(statuses.get(str(dependency)) in {"succeeded", "skipped"} for dependency in dependencies)

        for desired in ("running", "cancel_requested", "blocked", "queued"):
            for stage in stages:
                if str(stage.get("status") or "") == desired and (
                    desired != "queued" or ready(stage)
                ):
                    return str(stage.get("stage_type") or stage.get("task_type") or "")
        # Preserve the previous fallback for malformed/legacy dependency data.
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
        manifest = None
        if isinstance(job.get("result"), dict):
            manifest = job["result"].get("manifest")
        if manifest is None and isinstance(job.get("metadata"), dict):
            manifest = job["metadata"].get("manifest")
        if (
            status in {"succeeded", "failed"}
            and isinstance(manifest, dict)
            and _normalize_manifest_status(manifest.get("status")) == "partial"
        ):
            return "partial"
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


def _normalize_manifest_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"succeeded", "success", "successful", "completed"}:
        return "succeeded"
    if status == "partial":
        return "partial"
    if status in {"failed", "failure", "cancelled", "canceled"}:
        return "failed"
    return "unknown" if status else ""


def _public_result_ref(value: Any) -> str:
    result_ref = str(value or "").strip().lower()
    prefix = "result:sha256:"
    digest = result_ref.removeprefix(prefix)
    if result_ref.startswith(prefix) and len(digest) == 64 and all(
        char in "0123456789abcdef" for char in digest
    ):
        return result_ref
    return ""


def _safe_diagnostic_code(value: Any) -> str:
    code = str(value or "").strip().lower()
    if not code or len(code) > 100:
        return ""
    if all(char.isalnum() or char in "._-" for char in code):
        return code
    return ""


def _historical_stage_failure_code(control: Any, job_id: str, stage_id: str) -> str:
    """Recover a path-free failure code from an older stage event, if present."""
    try:
        page = control.list_events(job_id, since=0, limit=1000, tail=False)
    except Exception:
        return ""
    events = page.get("events") if isinstance(page, dict) else []
    for event in reversed(events if isinstance(events, list) else []):
        if str(event.get("type") or event.get("event") or "") != "stage.failed":
            continue
        if str(event.get("stage_id") or "") != str(stage_id):
            continue
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        result = detail.get("result") if isinstance(detail.get("result"), dict) else {}
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        for candidate in (
            summary.get("error_code"),
            result.get("code"),
            event.get("code"),
        ):
            code = _safe_diagnostic_code(candidate)
            if code:
                return code
    return ""


def _diagnostic_category(source_code: str, stage_type: str) -> str:
    if source_code in {
        "simulation_failed",
        "selena_failed",
        "simulation_engine_failed",
        "engine_failed",
        "runtime_timeout",
    }:
        return "simulation"
    if source_code in {
        "selena_launch_failed",
        "runner_unavailable",
        "runner_contract_failed",
        "paramconfig_failed",
        "paramconfig_outside_lease",
        "unsafe_runtime_argument",
        "connector_dependency_missing",
    }:
        return "configuration"
    if any(
        token in source_code
        for token in (
            "gateway",
            "network",
            "transport",
            "connection",
            "offline",
            "unavailable",
            "timeout",
            "storage",
            "service",
            "agent_lost",
        )
    ):
        return "infrastructure"
    if (
        any(
            token in source_code
            for token in (
                "invalid",
                "missing",
                "required",
                "unsupported",
                "needs_input",
                "not_configured",
                "workspace_snapshot_pending",
            )
        )
        or stage_type in {"resolve_spec", "environment_check", "prepare_source", "prepare_data"}
    ):
        return "configuration"
    return "system"


def _failure_summary(category: str, stage_type: str) -> str:
    if category == "infrastructure":
        return (
            "The simulation service could not complete because an execution "
            "dependency was unavailable."
        )
    if category == "configuration":
        return "The task failed because required configuration or environment input was not ready."
    if category == "simulation":
        return "Simulation completed with a failed outcome."
    return f"The task failed in stage {stage_type}." if stage_type else "The task failed."


def _diagnostic_action(
    *,
    outcome: str,
    category: str,
    job_id: str,
    stage_id: str,
    result_ref: str,
    available_actions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if result_ref and outcome in {"succeeded", "partial", "failed"}:
        return {
            "type": "download_result",
            "label": "Download result artifacts",
            "result_ref": result_ref,
        }
    if outcome == "failed" and stage_id:
        return {
            "type": "retry_stage",
            "label": "Retry failed stage",
            "job_id": job_id,
            "stage_id": stage_id,
        }
    if outcome == "needs_input":
        allowed = {
            "connect_windows": "Connect this Windows computer",
            "upload_data": "Provide accessible simulation data",
            "resolve_source": "Resolve the Selena source",
        }
        for candidate in available_actions:
            action_type = str(candidate.get("type") or "")
            if action_type in allowed:
                return {"type": action_type, "label": allowed[action_type]}
        return {"type": "review_configuration", "label": "Review task configuration"}
    if outcome == "pending":
        return {"type": "wait_job", "label": "Wait for task completion", "job_id": job_id}
    if outcome == "failed" and category == "infrastructure":
        return {"type": "inspect_events", "label": "Inspect task events", "job_id": job_id}
    return None


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
