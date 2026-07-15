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
from typing import Any, Callable, Mapping

from core.agent_bindings import AgentBindingStore
from core.agent_build_stage import AgentBuildStageError, prepare_selena_build
from core.agent_policy import NODE_KIND_WINDOWS_AGENT, NODE_KIND_WINDOWS_FULL


class EnvironmentSnapshotError(ValueError):
    """Stable validation error for public environment evidence."""


_LOGICAL_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_BINDING_RE = re.compile(r"^workspace:sha256:[0-9a-f]{24}$")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


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
) -> EnvironmentSnapshot:
    """Inspect the authorized build boundary without starting a subprocess."""
    if not isinstance(payload, Mapping):
        raise EnvironmentSnapshotError("environment payload must be a mapping")
    project = str(payload.get("project") or "").strip()
    binding_id = str(payload.get("workspace_binding_id") or "").strip()
    created_at = float(now_fn())
    try:
        prepared = prepare_fn(payload, binding_store)
    except (AgentBuildStageError, ValueError, TypeError, OSError) as exc:
        checks = (
            EnvironmentCheckResult(
                requirement_id="selena_build_environment",
                capability="build.selena",
                status="failed",
                code="selena_build_environment_unavailable",
                message=str(exc) or "Selena build environment is unavailable",
                action="Repair the Windows Agent project binding or build dependencies, then retry.",
            ),
        )
    else:
        before = getattr(prepared, "before", None)
        workspace = before.to_dict() if before is not None and hasattr(before, "to_dict") else None
        checks_list = [
            EnvironmentCheckResult("workspace_binding", "source.workspace.read", "passed"),
            EnvironmentCheckResult("selena_build_toolchain", "build.selena", "passed"),
            EnvironmentCheckResult("artifact_local_staging", "artifact.validate", "passed"),
        ]
        package_script = getattr(prepared, "package_build_script_path", None)
        if package_script is not None:
            from core.tcc import auto_repair_environment, derive_dependencies_from_build_script

            dependency_config = {"build": {"env_build_script": str(package_script)}}
            dependencies = derive_dependencies_from_build_script(dependency_config)
            managed = [item for item in dependencies if item.get("kind") == "toolcollection"]
            if managed:
                report = auto_repair_environment(dependency_config)
                checks_list.append(
                    EnvironmentCheckResult(
                        "package_build_dependencies",
                        "build.dependencies",
                        "passed" if report.ok else "failed",
                        code="" if report.ok else "package_build_dependencies_unavailable",
                        message=report.summary,
                        action="" if report.ok else "Repair the package build dependencies, then retry.",
                    )
                )
            else:
                checks_list.append(
                    EnvironmentCheckResult(
                        "package_build_dependencies",
                        "build.dependencies",
                        "passed",
                        message=f"{len(dependencies)} dependency hints inspected",
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
