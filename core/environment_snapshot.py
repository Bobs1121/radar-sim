"""Node-local, path-free environment evidence for v1 Stage dispatch.

The Windows Agent is the only process allowed to inspect its workspace and
toolchain paths. This module turns that local inspection into a small public
snapshot that the Linux control plane may persist and use for scheduling.
Absolute paths and credentials are rejected at the boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from core.agent_bindings import AgentBindingStore
from core.agent_build_stage import AgentBuildStageError, prepare_selena_build
from core.agent_policy import NODE_KIND_WINDOWS_AGENT, NODE_KIND_WINDOWS_FULL
from core.build_script_policy import (
    BuildScriptPolicyError,
    adapt_build_script_for_incremental,
    has_existing_build_artifact,
)
from core.windows_toolchain import (
    WindowsToolchainError,
    adapt_selena_script_visual_studio,
)


class EnvironmentSnapshotError(ValueError):
    """Stable validation error for public environment evidence."""


_LOGICAL_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_BINDING_RE = re.compile(r"^workspace:sha256:[0-9a-f]{24}$")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _report_progress(callback: Callable[[str], None] | None, message: str) -> None:
    """Emit a best-effort, path-free environment-check progress update."""
    if callback is None:
        return
    try:
        callback(message)
    except Exception:
        # Progress reporting must never make a usable Windows environment fail.
        return


def _build_environment_failure(exc: Exception) -> tuple[str, str, str]:
    """Translate stable local failures into a Chinese, actionable diagnosis."""
    raw = str(exc or "").strip()
    if isinstance(exc, BuildScriptPolicyError) or "incremental build policy failed" in raw:
        return (
            "selena_incremental_build_policy_failed",
            "无法安全改写所选 Selena 编译脚本中的清理命令，系统已阻止启动编译。",
            "确认编译脚本可写且未被其他进程锁定；修复后重新执行环境检查。",
        )
    if raw == "binding not found":
        return (
            "workspace_binding_missing",
            "这台 Windows 电脑尚未登记当前代码仓（原始检查：binding not found）。",
            "在 Web 中重新执行一次“连接这台电脑”，选择当前代码仓后重试。",
        )
    if raw == "binding project mismatch":
        return (
            "workspace_binding_project_mismatch",
            "当前代码仓登记信息与自动识别出的 Selena 产品不一致（原始检查：binding project mismatch）。",
            "在 Web 中重新连接这台电脑并选择当前代码仓后重试。",
        )
    if "build script is missing" in raw or "build script is not accessible" in raw:
        return (
            "selena_build_script_unavailable",
            "找不到或无法读取所选的 Selena 编译脚本。",
            "确认 YAML 中的 Selena 编译脚本路径属于已选择的代码仓，然后重试。",
        )
    if "package build script" in raw:
        return (
            "package_build_script_unavailable",
            "找不到或无法读取所选的软件包编译脚本。",
            "确认 YAML 中的软件包编译脚本路径属于已选择的代码仓，然后重试。",
        )
    if "workspace identity check failed" in raw or "branch repository is unavailable" in raw:
        return (
            "selena_branch_identity_unavailable",
            "无法在短时检查内读取所选 Selena 子仓的分支或提交。",
            "确认 Selena 子仓可正常执行 Git 分支与提交查询后重试；系统不会扫描本地 diff 或未跟踪文件。",
        )
    return (
        "selena_build_environment_unavailable",
        "Windows 本机编译环境检查未通过（原始检查：" + (raw or "unknown") + "）。",
        "检查代码仓授权、编译脚本和输出目录后重试。",
    )


@dataclass(frozen=True)
class EnvironmentCheckResult:
    requirement_id: str
    capability: str
    status: str
    code: str = ""
    message: str = ""
    action: str = ""

    def __post_init__(self) -> None:
        for name in ("requirement_id", "capability"):
            value = str(getattr(self, name) or "").strip()
            if not value or not _LOGICAL_TOKEN_RE.fullmatch(value):
                raise EnvironmentSnapshotError(f"{name} must be a logical token")
            object.__setattr__(self, name, value)
        status = str(self.status or "").strip().lower()
        if status not in {"passed", "failed", "deferred"}:
            raise EnvironmentSnapshotError("environment check status is invalid")
        object.__setattr__(self, "status", status)
        for name in ("code", "message", "action"):
            value = str(getattr(self, name) or "").strip()
            _assert_public(value, name)
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EnvironmentSnapshot:
    agent_id: str
    node_kind: str
    project: str
    workspace_binding_id: str
    scope: str
    checks: tuple[EnvironmentCheckResult, ...]
    created_at: float
    expires_at: float
    workspace: dict[str, Any] | None = None
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        agent_id = str(self.agent_id or "").strip()
        project = str(self.project or "").strip()
        scope = str(self.scope or "").strip()
        node_kind = str(self.node_kind or "").strip().lower()
        binding_id = str(self.workspace_binding_id or "").strip()
        if not agent_id or not _LOGICAL_TOKEN_RE.fullmatch(agent_id):
            raise EnvironmentSnapshotError("agent_id must be a logical token")
        if not project or not _LOGICAL_TOKEN_RE.fullmatch(project):
            raise EnvironmentSnapshotError("project must be a logical token")
        if not scope or not _LOGICAL_TOKEN_RE.fullmatch(scope):
            raise EnvironmentSnapshotError("scope must be a logical token")
        if node_kind not in {NODE_KIND_WINDOWS_AGENT, NODE_KIND_WINDOWS_FULL}:
            raise EnvironmentSnapshotError("environment snapshot requires a Windows node")
        if not _BINDING_RE.fullmatch(binding_id):
            raise EnvironmentSnapshotError("workspace_binding_id is invalid")
        checks = tuple(self.checks or ())
        if not checks or any(not isinstance(item, EnvironmentCheckResult) for item in checks):
            raise EnvironmentSnapshotError("environment snapshot checks are required")
        requirement_ids = [item.requirement_id for item in checks]
        if len(set(requirement_ids)) != len(requirement_ids):
            raise EnvironmentSnapshotError("environment snapshot checks must be unique")
        try:
            created_at = float(self.created_at)
            expires_at = float(self.expires_at)
        except (TypeError, ValueError) as exc:
            raise EnvironmentSnapshotError("environment snapshot timestamps are invalid") from exc
        if (
            not math.isfinite(created_at)
            or not math.isfinite(expires_at)
            or created_at < 0
            or expires_at <= created_at
        ):
            raise EnvironmentSnapshotError("environment snapshot timestamps are invalid")
        object.__setattr__(self, "agent_id", agent_id)
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "node_kind", node_kind)
        object.__setattr__(self, "workspace_binding_id", binding_id)
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        workspace = dict(self.workspace or {})
        if workspace:
            commit = str(workspace.get("commit") or "").strip()
            sha256 = str(workspace.get("sha256") or "").strip().lower()
            branch = str(workspace.get("branch") or "").strip()
            dirty = workspace.get("dirty")
            if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
                raise EnvironmentSnapshotError("workspace commit is invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise EnvironmentSnapshotError("workspace fingerprint is invalid")
            if not isinstance(dirty, bool):
                raise EnvironmentSnapshotError("workspace dirty state is invalid")
            _assert_public(branch, "workspace.branch")
            workspace = {"branch": branch, "commit": commit, "dirty": dirty, "sha256": sha256}
        object.__setattr__(self, "workspace", workspace or None)

    @property
    def status(self) -> str:
        if any(item.status == "failed" for item in self.checks):
            return "blocked"
        if any(item.status == "deferred" for item in self.checks):
            return "partial"
        return "ready"

    @property
    def snapshot_id(self) -> str:
        raw = json.dumps(self._body(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "environment:sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def satisfies(self, requirement_ids: tuple[str, ...] | list[str]) -> bool:
        passed = {item.requirement_id for item in self.checks if item.status == "passed"}
        return all(str(item) in passed for item in requirement_ids)

    def _body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "node_kind": self.node_kind,
            "project": self.project,
            "workspace_binding_id": self.workspace_binding_id,
            "scope": self.scope,
            "status": self.status,
            "checks": [item.to_dict() for item in self.checks],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "workspace": dict(self.workspace or {}),
        }

    def to_dict(self) -> dict[str, Any]:
        result = {"snapshot_id": self.snapshot_id, **self._body()}
        _assert_public(result, "environment_snapshot")
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EnvironmentSnapshot":
        if not isinstance(value, Mapping):
            raise EnvironmentSnapshotError("environment snapshot must be an object")
        raw_checks = value.get("checks")
        if not isinstance(raw_checks, (list, tuple)):
            raise EnvironmentSnapshotError("environment snapshot checks are required")
        if any(not isinstance(item, Mapping) for item in raw_checks):
            raise EnvironmentSnapshotError("environment snapshot check is invalid")
        try:
            checks = tuple(
                EnvironmentCheckResult(
                    requirement_id=item["requirement_id"],
                    capability=item["capability"],
                    status=item["status"],
                    code=item.get("code", ""),
                    message=item.get("message", ""),
                    action=item.get("action", ""),
                )
                for item in raw_checks
                if isinstance(item, Mapping)
            )
            snapshot = cls(
                agent_id=value.get("agent_id", ""),
                node_kind=value.get("node_kind", ""),
                project=value.get("project", ""),
                workspace_binding_id=value.get("workspace_binding_id", ""),
                scope=value.get("scope", ""),
                checks=checks,
                created_at=value.get("created_at", 0),
                expires_at=value.get("expires_at", 0),
                workspace=dict(value.get("workspace") or {}),
                schema_version=str(value.get("schema_version") or "1.0"),
            )
        except (KeyError, TypeError) as exc:
            raise EnvironmentSnapshotError("environment snapshot is invalid") from exc
        supplied_id = str(value.get("snapshot_id") or "").strip()
        if supplied_id and supplied_id != snapshot.snapshot_id:
            raise EnvironmentSnapshotError("environment snapshot id mismatch")
        return snapshot


def inspect_selena_build_environment(
    payload: Mapping[str, Any],
    binding_store: AgentBindingStore,
    *,
    agent_id: str,
    node_kind: str,
    now_fn: Callable[[], float] = time.time,
    ttl_seconds: float = 300.0,
    prepare_fn: Callable[..., Any] = prepare_selena_build,
    vs_adapter: Callable[..., Any] = adapt_selena_script_visual_studio,
    incremental_adapter: Callable[..., Any] = adapt_build_script_for_incremental,
    generated_dependency_preparer: Callable[..., Any] | None = None,
    progress_fn: Callable[[str], None] | None = None,
) -> EnvironmentSnapshot:
    """Inspect the authorized build boundary without starting a subprocess.

    The user-facing flow is intentionally bounded: selected scripts, output
    location, Visual Studio and selected Selena child-repository branch/HEAD.
    It never scans the product root for local diffs or untracked files.
    """
    if not isinstance(payload, Mapping):
        raise EnvironmentSnapshotError("environment payload must be a mapping")
    project = str(payload.get("project") or "").strip()
    binding_id = str(payload.get("workspace_binding_id") or "").strip()
    created_at = float(now_fn())
    adaptation = None
    incremental_adaptation = None
    incremental_existing_build = False

    def prepare_without_script_mutation() -> Any:
        # The default v5 preparer supports an explicit lock-safe mode.  Keep
        # injected legacy/test preparers on their historical two-argument
        # contract; the generic policy below still runs while the workspace
        # lock is held.
        if prepare_fn is prepare_selena_build:
            return prepare_fn(
                payload,
                binding_store,
                enforce_incremental_policy=False,
            )
        return prepare_fn(payload, binding_store)

    try:
        _report_progress(progress_fn, "正在确认代码仓、编译脚本和 Selena 输出位置")
        prepared = prepare_without_script_mutation()
        _report_progress(progress_fn, "已确认所选 Selena 子仓的分支与提交")
        build_script = getattr(prepared, "build_script_path", None)
        if build_script is not None:
            _report_progress(progress_fn, "正在检查 Visual Studio 与 Selena 编译脚本")
            build_script_path = Path(str(build_script))
            # VS argument adaptation and generic clean-command suppression
            # both edit the selected wrapper.  Serialize that small mutation
            # window with the same per-workspace OS lock used by the build
            # executor; otherwise two users can restore/suppress the same
            # line concurrently and capture mismatched checksums.
            from core.build_lock import WorkspaceBuildLock

            authorized = getattr(prepared, "authorized", None)
            lock_root = getattr(authorized, "workspace_root", None) or build_script_path.parent
            script_lock = WorkspaceBuildLock(lock_root).acquire(wait=True)
            try:
                adaptation = vs_adapter(build_script)
                if bool(getattr(adaptation, "changed", False)):
                    _report_progress(progress_fn, "已适配 Visual Studio 编译参数")
                # A project may use a completely different build wrapper.  The
                # policy is script-semantic, not project/path based: detect and
                # disable active clean commands before the immutable build-stage
                # checksum is captured for the final handoff.
                if build_script_path.is_file():
                    output_roots = getattr(authorized, "output_roots", ()) or ()
                    incremental_existing_build = has_existing_build_artifact(
                        getattr(prepared, "artifact_path", None),
                        output_roots,
                    )
                    incremental_adaptation = incremental_adapter(
                        build_script,
                        existing_build=incremental_existing_build,
                        allow_clean=bool(getattr(prepared, "clean", False)),
                    )
                    if bool(getattr(incremental_adaptation, "changed", False)):
                        _report_progress(progress_fn, "已禁用编译脚本中的清理命令，后续使用增量编译")
                if bool(getattr(adaptation, "changed", False)) or bool(
                    getattr(incremental_adaptation, "changed", False)
                ):
                    # Both adaptations are intentional current-workspace
                    # changes. Re-prepare while holding the lock so the
                    # returned checksum and command evidence match the exact
                    # script that the next handoff sees.
                    _report_progress(progress_fn, "正在确认适配后的 Selena 编译脚本")
                    prepared = prepare_without_script_mutation()
                    _report_progress(progress_fn, "已确认适配后的 Selena 子仓分支与提交")
            finally:
                script_lock.release()
    except WindowsToolchainError as exc:
        checks = (
            EnvironmentCheckResult(
                requirement_id="visual_studio_toolchain",
                capability="build.selena",
                status="failed",
                code="visual_studio_toolchain_unavailable",
                message=str(exc) or "A compatible Visual Studio C++ compiler is unavailable.",
                action="Install the required Visual Studio C++ environment, then retry. The Agent does not install Visual Studio.",
            ),
        )
    except (AgentBuildStageError, ValueError, TypeError, OSError) as exc:
        code, message, action = _build_environment_failure(exc)
        checks = (
            EnvironmentCheckResult(
                requirement_id="selena_build_environment",
                capability="build.selena",
                status="failed",
                code=code,
                message=message,
                action=action,
            ),
        )
    else:
        # Selena branch identity can live in a nested Git repository while
        # the authorized build workspace remains its outer product checkout.
        # The Agent chooses it from the selected script and exposes only the
        # path-free fingerprint as the public source evidence.
        before = getattr(prepared, "branch_before", None) or getattr(prepared, "before", None)
        workspace = before.to_dict() if before is not None and hasattr(before, "to_dict") else None
        checks_list = [
            EnvironmentCheckResult(
                "workspace_binding", "source.workspace.read", "passed",
                message="已确认本机代码仓授权、所选 Selena 编译脚本和输出位置。",
            ),
            EnvironmentCheckResult(
                "selena_build_toolchain", "build.selena", "passed",
                message="已完成 Selena 子仓分支与提交的有界身份检查。",
            ),
            EnvironmentCheckResult("artifact_local_staging", "artifact.validate", "passed"),
            EnvironmentCheckResult(
                "workspace_local_changes", "source.workspace.read", "passed",
                code="workspace_local_changes_not_scanned",
                message="按当前配置不扫描本地 diff 或未跟踪文件；现有本地修改会保留并直接参与编译。",
            ),
        ]
        if adaptation is not None:
            installation = getattr(adaptation, "installation", None)
            year = str(getattr(installation, "year", "") or "")
            tag = str(getattr(installation, "tag", "") or "")
            toolset = str(getattr(installation, "toolset", "") or "")
            changed = bool(getattr(adaptation, "changed", False))
            checks_list.append(
                EnvironmentCheckResult(
                    "visual_studio_toolchain",
                    "build.selena",
                    "passed",
                    code="selena_build_script_vs_adapted" if changed else "",
                    message=(
                        f"Selena build script adapted to Visual Studio {year} ({tag}, {toolset})."
                        if changed
                        else f"Visual Studio {year} ({tag}, {toolset}) matches the Selena build script."
                    ),
                )
            )
        if incremental_adaptation is not None:
            clean_lines = tuple(
                int(item)
                for item in getattr(incremental_adaptation, "clean_command_lines", ()) or ()
            )
            suppressed = bool(getattr(incremental_adaptation, "changed", False))
            explicitly_allowed = bool(
                getattr(incremental_adaptation, "explicit_clean_requested", False)
            )
            full_rebuild_required = bool(getattr(prepared, "full_rebuild_required", False))
            if full_rebuild_required and not clean_lines:
                raise AgentBuildStageError(
                    "full rebuild is required but the selected build script has no recognized clean command"
                )
            if full_rebuild_required:
                policy_code = "selena_full_rebuild_required"
                policy_message = (
                    "The existing Selena artifact belongs to a different or unproven branch; "
                    "a full clean build is required before execution."
                )
            elif suppressed:
                policy_code = "selena_clean_commands_suppressed"
                policy_message = (
                    "Detected active clean commands in the selected build script and disabled them; "
                    "the build will reuse the local workspace incrementally."
                )
            elif explicitly_allowed and clean_lines:
                policy_code = "selena_clean_explicitly_allowed"
                policy_message = (
                    "An explicit clean build was requested; detected clean commands remain enabled."
                )
            elif clean_lines:
                policy_code = "selena_clean_commands_present"
                policy_message = (
                    "Clean commands were detected but were not changed by the current policy."
                )
            elif incremental_existing_build:
                policy_code = "selena_incremental_build"
                policy_message = "An existing Selena artifact was found; the next build will be incremental."
            else:
                policy_code = "selena_incremental_build"
                policy_message = "The selected build uses incremental mode unless clean=true is explicitly requested."
            checks_list.append(
                EnvironmentCheckResult(
                    "incremental_build_policy",
                    "build.selena",
                    "passed",
                    code=policy_code,
                    message=policy_message
                    + (f" Detected script lines: {', '.join(str(item) for item in clean_lines)}." if clean_lines else ""),
                )
            )
        if getattr(prepared, "package_build_script_path", None) is not None:
            checks_list.append(
                EnvironmentCheckResult(
                    "package_build_script", "build.dependencies", "passed",
                    message="已确认软件包编译脚本位于当前代码仓；脚本依赖将在实际编译前准备。",
                )
            )
        expected_branch = str(payload.get("expected_branch") or "").strip()
        actual_branch = str((workspace or {}).get("branch") or "").strip()
        nested_selena_repo = bool(str(payload.get("branch_repo_ref") or "").strip())
        mismatch = bool(expected_branch and expected_branch != actual_branch)
        checks_list.append(
            EnvironmentCheckResult(
                "workspace_branch_expectation",
                "source.workspace.read",
                "passed",
                code="workspace_branch_mismatch" if mismatch else "",
                message=(
                    f"期望 Selena 子仓分支 '{expected_branch}'，当前子仓分支为 '{actual_branch}'。"
                    "将编译当前工作区，不会切换分支。"
                    if mismatch and nested_selena_repo
                    else
                    f"Expected branch '{expected_branch}', current branch is '{actual_branch}'. "
                    "The current workspace will be compiled unchanged."
                    if mismatch
                    else "Current Selena 子仓分支为 '" + actual_branch + "'."
                    if nested_selena_repo
                    else f"Current branch is '{actual_branch}'."
                ),
                action=(
                    "请确认 Selena 子仓分支及本地修改后再使用该构建产物。"
                    if mismatch and nested_selena_repo
                    else
                    "Confirm the branch and local modifications before relying on this build."
                    if mismatch
                    else ""
                ),
            )
        )
        checks = tuple(checks_list)
    return EnvironmentSnapshot(
        agent_id=agent_id,
        node_kind=node_kind,
        project=project,
        workspace_binding_id=binding_id,
        scope="selena_build",
        checks=checks,
        created_at=created_at,
        expires_at=created_at + float(ttl_seconds),
        workspace=workspace if "workspace" in locals() else None,
    )


def _assert_public(value: Any, context: str) -> None:
    """Reject path/credential-shaped values before they leave the Agent."""
    if isinstance(value, str):
        text = value.strip()
        lowered = text.lower()
        if _WINDOWS_ABS_RE.match(text) or text.startswith(("/", "\\\\")):
            raise EnvironmentSnapshotError(f"absolute path detected in {context}")
        if any(token in lowered for token in ("password=", "token=", "secret=")):
            raise EnvironmentSnapshotError(f"credential detected in {context}")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _assert_public(str(key), context)
            _assert_public(item, context)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_public(item, context)


__all__ = [
    "EnvironmentCheckResult",
    "EnvironmentSnapshot",
    "EnvironmentSnapshotError",
    "inspect_selena_build_environment",
]
