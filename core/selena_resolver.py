"""Pure Selena source resolver for SimulationSpec v1.

``resolve_selena`` consumes only immutable snapshots supplied by callers. Any
workspace inspection or Git ref resolution is kept in explicitly named boundary
helpers so the business resolver remains deterministic and side-effect free.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional

from core.artifacts import SelenaArtifact
from core.repo import WorkspaceFingerprint
from core.spec.model import SimulationSpec
from core.stages import PlannedStage, StagePlan

ResolutionStatus = str
ResolutionKind = str

_ARTIFACT_SKIP_REASON = "selena_artifact_already_resolved"
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _read(obj: Any, name: str, default: Any = "") -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass(frozen=True)
class SourceResolutionContext:
    project_revision: str
    owner: str = ""
    evaluated_at: float = 0.0
    workspace_binding_id: str = ""
    workspace_project: str = ""
    workspace_fingerprint: Optional[WorkspaceFingerprint] = None
    branch_commits: Mapping[str, str] = field(default_factory=dict)
    artifacts: tuple[SelenaArtifact, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_revision", str(self.project_revision or "").strip())
        object.__setattr__(self, "owner", str(self.owner or "").strip())
        try:
            evaluated_at = float(self.evaluated_at or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("evaluated_at must be numeric") from exc
        if evaluated_at < 0 or not math.isfinite(evaluated_at):
            raise ValueError("evaluated_at must be finite and non-negative")
        object.__setattr__(self, "evaluated_at", evaluated_at)
        object.__setattr__(self, "workspace_binding_id", str(self.workspace_binding_id or "").strip())
        object.__setattr__(self, "workspace_project", str(self.workspace_project or "").strip())
        if self.workspace_fingerprint is not None and not _FULL_SHA_RE.fullmatch(self.workspace_fingerprint.commit):
            raise ValueError("workspace fingerprint must contain an exact commit SHA")
        branch_commits = {str(key).strip(): str(value).strip() for key, value in self.branch_commits.items()}
        for branch, commit in branch_commits.items():
            if not branch or not _FULL_SHA_RE.fullmatch(commit):
                raise ValueError(f"branch_commits must contain exact commit SHAs: {branch}")
        object.__setattr__(
            self,
            "branch_commits",
            MappingProxyType(branch_commits),
        )
        artifacts = tuple(self.artifacts or ())
        if any(not isinstance(artifact, SelenaArtifact) for artifact in artifacts):
            raise TypeError("artifacts must contain SelenaArtifact snapshots")
        object.__setattr__(self, "artifacts", artifacts)

    def artifact_snapshot(self) -> tuple[dict[str, Any], ...]:
        return tuple(artifact.to_dict() for artifact in self.artifacts)


@dataclass(frozen=True)
class SelenaResolutionOutcome:
    status: ResolutionStatus
    code: str
    action: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    resolution: ResolutionKind = ""
    artifact_id: str = ""
    workspace_binding_id: str = ""
    branch: str = ""
    commit: str = ""
    dirty: bool = False
    dirty_fingerprint: str = ""
    build_mode: str = ""

    def __post_init__(self) -> None:
        status = str(self.status or "").strip()
        if status not in {"resolved", "needs_input", "impossible"}:
            raise ValueError(f"Unsupported resolution status: {status}")
        resolution = str(self.resolution or "").strip()
        if resolution and resolution not in {"workspace_build", "branch_build", "artifact"}:
            raise ValueError(f"Unsupported Selena resolution: {resolution}")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "code", str(self.code or "").strip())
        object.__setattr__(self, "action", str(self.action or "").strip())
        object.__setattr__(self, "evidence", _freeze(self.evidence or {}))
        object.__setattr__(self, "resolution", resolution)
        object.__setattr__(self, "artifact_id", str(self.artifact_id or "").strip())
        object.__setattr__(self, "workspace_binding_id", str(self.workspace_binding_id or "").strip())
        object.__setattr__(self, "branch", str(self.branch or "").strip())
        object.__setattr__(self, "commit", str(self.commit or "").strip())
        object.__setattr__(self, "dirty", bool(self.dirty))
        object.__setattr__(self, "dirty_fingerprint", str(self.dirty_fingerprint or "").strip())
        object.__setattr__(self, "build_mode", str(self.build_mode or "").strip())
        if status == "resolved" and not resolution:
            raise ValueError("resolved Selena outcome requires a resolution")
        if resolution == "artifact" and not self.artifact_id:
            raise ValueError("artifact resolution requires artifact_id")
        if resolution == "workspace_build" and (not self.workspace_binding_id or not self.commit):
            raise ValueError("workspace_build resolution requires workspace binding and commit")
        if resolution == "branch_build" and (
            not self.workspace_binding_id or not self.branch or not self.commit
        ):
            raise ValueError("branch_build resolution requires workspace binding, branch, and commit")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "action": self.action,
            "evidence": _thaw(self.evidence),
            "resolution": self.resolution,
            "artifact_id": self.artifact_id,
            "workspace_binding_id": self.workspace_binding_id,
            "branch": self.branch,
            "commit": self.commit,
            "dirty": self.dirty,
            "dirty_fingerprint": self.dirty_fingerprint,
            "build_mode": self.build_mode,
        }

    def to_resolved_spec(self, *, project_revision: str) -> dict[str, Any]:
        return apply_resolution_to_resolved_spec({}, self, project_revision)


@dataclass(frozen=True)
class SelenaResolutionApplication:
    """Atomic pure result for persisting one Selena resolution decision."""

    resolved_spec: Mapping[str, Any]
    stage_plan: StagePlan
    mutated_stages: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolved_spec", _freeze(self.resolved_spec or {}))
        object.__setattr__(self, "mutated_stages", tuple(self.mutated_stages or ()))

    def resolved_spec_dict(self) -> dict[str, Any]:
        return _thaw(self.resolved_spec)


def resolve_selena(
    spec: SimulationSpec,
    project_catalog: Any,
    user_bindings: Any,
    context: SourceResolutionContext,
) -> SelenaResolutionOutcome:
    """Resolve the Selena source from supplied snapshots only."""

    project_check = _validate_project(spec, project_catalog, user_bindings, context)
    if project_check is not None:
        return project_check
    target_check = _validate_catalog_target(spec, project_catalog)
    if target_check is not None:
        return target_check

    mode = spec.selena.mode
    if mode == "current_workspace":
        return _resolve_current_workspace(spec, user_bindings, context)
    if mode == "branch":
        return _resolve_branch(spec, user_bindings, context)
    if mode == "existing":
        return _resolve_existing(spec, context, explicit_artifact_id=spec.selena.artifact)
    if mode == "auto":
        if spec.selena.auto_build and _workspace_authorized(spec, user_bindings, context) and context.workspace_fingerprint is not None:
            return _workspace_outcome(spec, context, reason="auto_workspace_preferred")
        artifact = _recommend_from_context(spec, context)
        if artifact is not None:
            return _artifact_outcome(spec, artifact, reason="auto_artifact_fallback")
        return _needs_input(
            "selena_candidate_required",
            "Provide an authorized workspace fingerprint or a compatible registered Selena artifact.",
            mode=mode,
            target=spec.simulation.target,
        )
    return _impossible("unsupported_selena_mode", "Use a supported Selena mode.", mode=mode)


def apply_resolution_to_resolved_spec(
    pending: Mapping[str, Any],
    outcome: SelenaResolutionOutcome,
    project_revision: str,
) -> dict[str, Any]:
    resolved = dict(pending or {})
    resolved["status"] = outcome.status
    resolved["project_revision"] = str(project_revision or "")
    resolved["decisions"] = dict(resolved.get("decisions") or {})
    resolved["decisions"]["selena"] = _resolution_decision(outcome)
    if outcome.status != "resolved":
        resolved["status"] = outcome.status
        resolved["code"] = outcome.code
        resolved["action"] = outcome.action
    else:
        # Selena is only one part of ResolvedSimulationSpec.  Never advertise
        # the complete job as resolved before data and execution routing finish.
        resolved["status"] = "partial"
        resolved.pop("code", None)
        resolved.pop("action", None)
    return resolved


def apply_resolution_to_stage_plan(plan: StagePlan, outcome: SelenaResolutionOutcome) -> StagePlan:
    revision = str(plan.resolved_spec.get("project_revision") or "")
    resolved_spec = apply_resolution_to_resolved_spec(plan.resolved_spec, outcome, revision)
    if outcome.status != "resolved" or outcome.resolution != "artifact":
        return StagePlan(stages=tuple(plan.stages), resolved_spec=resolved_spec)

    stages: list[PlannedStage] = []
    for stage in plan.stages:
        if stage.stage_type in {"prepare_source", "build_selena", "register_artifact"}:
            stages.append(
                dataclasses.replace(
                    stage,
                    initial_status="skipped",
                    skip_reason=_ARTIFACT_SKIP_REASON,
                )
            )
        else:
            stages.append(stage)
    return StagePlan(stages=tuple(stages), resolved_spec=resolved_spec)


def apply_selena_resolution(
    plan: StagePlan,
    outcome: SelenaResolutionOutcome,
    *,
    project_revision: str,
) -> SelenaResolutionApplication:
    """Return resolved snapshot and stage mutations as one atomic pure value."""

    resolved_spec = apply_resolution_to_resolved_spec(plan.resolved_spec, outcome, project_revision)
    updated_plan = apply_resolution_to_stage_plan(
        StagePlan(stages=tuple(plan.stages), resolved_spec=resolved_spec),
        outcome,
    )
    mutated = tuple(
        stage.stage_type
        for original, stage in zip(plan.stages, updated_plan.stages)
        if original != stage
    )
    return SelenaResolutionApplication(
        resolved_spec=updated_plan.resolved_spec,
        stage_plan=updated_plan,
        mutated_stages=mutated,
    )


def build_source_resolution_context_from_io(
    *,
    project_revision: str,
    owner: str = "",
    evaluated_at: float | None = None,
    workspace_binding_id: str = "",
    workspace_project: str = "",
    workspace_path: str = "",
    branch_refs: tuple[str, ...] = (),
    artifacts: tuple[SelenaArtifact, ...] = (),
) -> SourceResolutionContext:
    """I/O boundary: inspect workspace and resolve Git refs before pure resolve."""

    import time

    from core.repo import inspect_workspace, resolve_git_ref

    fingerprint = inspect_workspace(workspace_path) if workspace_path else None
    branch_commits = {
        str(branch): resolve_git_ref(workspace_path, str(branch))
        for branch in branch_refs
        if workspace_path and str(branch).strip()
    }
    return SourceResolutionContext(
        project_revision=project_revision,
        owner=owner,
        evaluated_at=time.time() if evaluated_at is None else evaluated_at,
        workspace_binding_id=workspace_binding_id,
        workspace_project=workspace_project,
        workspace_fingerprint=fingerprint,
        branch_commits=branch_commits,
        artifacts=artifacts,
    )


def _validate_project(
    spec: SimulationSpec,
    project_catalog: Any,
    user_bindings: Any,
    context: SourceResolutionContext,
) -> SelenaResolutionOutcome | None:
    catalog_project = str(_read(project_catalog, "project", "") or "").strip()
    bindings_project = str(_read(user_bindings, "project", "") or "").strip()
    catalog_revision = str(_read(project_catalog, "revision", "") or "").strip()
    if catalog_project and catalog_project != spec.project:
        return _impossible(
            "project_catalog_mismatch",
            "Choose a ProjectCatalog for the requested project.",
            spec_project=spec.project,
            catalog_project=catalog_project,
        )
    if bindings_project and bindings_project != spec.project:
        return _impossible(
            "user_bindings_mismatch",
            "Choose UserBindings for the requested project.",
            spec_project=spec.project,
            bindings_project=bindings_project,
        )
    if context.project_revision and catalog_revision and context.project_revision != catalog_revision:
        return _impossible(
            "project_revision_mismatch",
            "Refresh the project catalog snapshot before resolving the job.",
            expected_revision=catalog_revision,
            context_revision=context.project_revision,
        )
    return None


def _validate_catalog_target(spec: SimulationSpec, project_catalog: Any) -> SelenaResolutionOutcome | None:
    profiles = tuple(_read(project_catalog, "profiles", ()) or ())
    matching = [
        profile
        for profile in profiles
        if str(_read(profile, "name", "") or "").strip() == spec.simulation.profile
    ]
    if profiles and not matching:
        return _impossible(
            "simulation_profile_unknown",
            "Choose a profile present in the current ProjectCatalog revision.",
            profile=spec.simulation.profile,
        )
    profile_target = str(_read(matching[0], "target", "") or "").strip() if matching else ""
    requested = spec.simulation.target
    if requested != "auto" and profile_target and profile_target != requested:
        return _impossible(
            "simulation_target_incompatible",
            "Choose a profile compatible with the requested simulation target.",
            requested_target=requested,
            profile_target=profile_target,
        )
    return None


def _workspace_authorized(spec: SimulationSpec, user_bindings: Any, context: SourceResolutionContext) -> bool:
    if not context.workspace_binding_id:
        return False
    if str(_read(user_bindings, "project", "") or "").strip() != spec.project:
        return False
    if context.workspace_project and context.workspace_project != spec.project:
        return False
    allowed = _read(user_bindings, "authorized_workspace_binding_ids", None)
    if allowed is not None:
        return context.workspace_binding_id in set(str(item) for item in allowed)
    return bool(str(_read(user_bindings, "workspace_path", "") or "").strip())


def _resolve_current_workspace(
    spec: SimulationSpec,
    user_bindings: Any,
    context: SourceResolutionContext,
) -> SelenaResolutionOutcome:
    if not _workspace_authorized(spec, user_bindings, context):
        return _needs_input(
            "workspace_binding_required",
            "Select an authorized Selena workspace binding before building the current workspace.",
            mode=spec.selena.mode,
        )
    if context.workspace_fingerprint is None:
        return _needs_input(
            "workspace_fingerprint_required",
            "Inspect the authorized workspace and provide its fingerprint snapshot.",
            workspace_binding_id=context.workspace_binding_id,
        )
    return _workspace_outcome(spec, context, reason="current_workspace_requested")


def _resolve_branch(
    spec: SimulationSpec,
    user_bindings: Any,
    context: SourceResolutionContext,
) -> SelenaResolutionOutcome:
    if not _workspace_authorized(spec, user_bindings, context):
        return _needs_input(
            "workspace_binding_required",
            "Select an authorized workspace binding before resolving a branch build.",
            branch=spec.selena.branch,
        )
    commit = _branch_commit(context, spec.selena.branch)
    if not commit:
        return _needs_input(
            "branch_commit_required",
            "Resolve the requested branch to an exact commit before scheduling a branch build.",
            branch=spec.selena.branch,
        )
    return SelenaResolutionOutcome(
        status="resolved",
        code="selena_branch_build",
        action="build_branch",
        resolution="branch_build",
        workspace_binding_id=context.workspace_binding_id,
        branch=spec.selena.branch,
        commit=commit,
        dirty=False,
        dirty_fingerprint="",
        build_mode=spec.selena.build_mode,
        evidence={
            "branch": spec.selena.branch,
            "commit": commit,
            "build_mode": spec.selena.build_mode,
            "target_decision": _target_decision(spec, None),
        },
    )


def _branch_commit(context: SourceResolutionContext, branch: str) -> str:
    for key in (branch, f"refs/heads/{branch}", f"origin/{branch}", f"refs/remotes/origin/{branch}"):
        commit = str(context.branch_commits.get(key, "") or "").strip()
        if commit:
            return commit
    return ""


def _resolve_existing(
    spec: SimulationSpec,
    context: SourceResolutionContext,
    *,
    explicit_artifact_id: str,
) -> SelenaResolutionOutcome:
    explicit = str(explicit_artifact_id or "").strip()
    if explicit:
        artifact = next((item for item in context.artifacts if item.id == explicit), None)
        if artifact is None:
            return _needs_input(
                "artifact_snapshot_required",
                "Provide a catalog snapshot containing the requested Selena artifact.",
                artifact_id=explicit,
            )
        incompatible = _artifact_incompatibility(spec, artifact, context, for_recommendation=False)
        if incompatible:
            return _impossible(incompatible, "Choose a Selena artifact compatible with the project, build mode, and target.", artifact_id=explicit)
        return _artifact_outcome(spec, artifact, reason="explicit_artifact_requested")

    artifact = _recommend_from_context(spec, context)
    if artifact is None:
        return _needs_input(
            "artifact_candidate_required",
            "Select or register a compatible ready Selena artifact.",
            mode=spec.selena.mode,
            target=spec.simulation.target,
        )
    return _artifact_outcome(spec, artifact, reason="existing_artifact_recommended")


def _recommend_from_context(spec: SimulationSpec, context: SourceResolutionContext) -> SelenaArtifact | None:
    candidates = [
        artifact
        for artifact in context.artifacts
        if not _artifact_incompatibility(spec, artifact, context, for_recommendation=True)
    ]
    candidates.sort(key=lambda item: (-float(item.created_at), item.binary_checksum, item.id))
    return candidates[0] if candidates else None


def _artifact_incompatibility(
    spec: SimulationSpec,
    artifact: SelenaArtifact,
    context: SourceResolutionContext,
    *,
    for_recommendation: bool,
) -> str:
    if artifact.project != spec.project:
        return "artifact_project_incompatible"
    if artifact.visibility != "shared" and artifact.owner != context.owner:
        return "artifact_visibility_incompatible"
    if artifact.build_mode != spec.selena.build_mode:
        return "artifact_build_mode_incompatible"
    if artifact.health != "ready":
        return "artifact_health_not_ready"
    if artifact.source_changed_during_build:
        return "artifact_not_shareable"
    if for_recommendation and artifact.dirty:
        return "artifact_not_shareable"
    if context.evaluated_at and artifact.retain_until and artifact.retain_until < context.evaluated_at:
        return "artifact_retention_expired"
    if not _target_accessible(spec.simulation.target, artifact.accessibility):
        return "artifact_target_incompatible"
    return ""


def _target_accessible(target: str, accessibility: str) -> bool:
    if target == "auto":
        return accessibility in {"local", "cluster", "shared"}
    if target == "local":
        return accessibility == "local"
    if target == "cluster":
        return accessibility in {"cluster", "shared"}
    return False


def _workspace_outcome(spec: SimulationSpec, context: SourceResolutionContext, *, reason: str) -> SelenaResolutionOutcome:
    fingerprint = context.workspace_fingerprint
    if fingerprint is None:
        raise ValueError("workspace outcome requires a workspace fingerprint")
    dirty_fingerprint = fingerprint.sha256 if fingerprint.dirty else ""
    return SelenaResolutionOutcome(
        status="resolved",
        code="selena_workspace_build",
        action="build_current_workspace",
        resolution="workspace_build",
        workspace_binding_id=context.workspace_binding_id,
        branch=fingerprint.branch,
        commit=fingerprint.commit,
        dirty=fingerprint.dirty,
        dirty_fingerprint=dirty_fingerprint,
        build_mode=spec.selena.build_mode,
        evidence={
            "reason": reason,
            "workspace_binding_id": context.workspace_binding_id,
            "fingerprint": fingerprint.to_dict(),
            "build_mode": spec.selena.build_mode,
            "target_decision": _target_decision(spec, None),
        },
    )


def _artifact_outcome(spec: SimulationSpec, artifact: SelenaArtifact, *, reason: str) -> SelenaResolutionOutcome:
    return SelenaResolutionOutcome(
        status="resolved",
        code="selena_artifact_resolved",
        action="use_registered_artifact",
        resolution="artifact",
        artifact_id=artifact.id,
        branch=artifact.branch,
        commit=artifact.commit,
        dirty=artifact.dirty,
        dirty_fingerprint=artifact.dirty_fingerprint,
        build_mode=artifact.build_mode,
        evidence={
            "reason": reason,
            "artifact": {
                "id": artifact.id,
                "project": artifact.project,
                "branch": artifact.branch,
                "commit": artifact.commit,
                "build_mode": artifact.build_mode,
                "binary_checksum": artifact.binary_checksum,
                "accessibility": artifact.accessibility,
                "visibility": artifact.visibility,
                "health": artifact.health,
            },
            "target_decision": _target_decision(spec, artifact),
        },
    )


def _target_decision(spec: SimulationSpec, artifact: SelenaArtifact | None) -> dict[str, str]:
    requested = spec.simulation.target
    if requested != "auto":
        return {"requested": requested, "selected": requested, "reason": "explicit_target"}
    if artifact is None:
        return {"requested": "auto", "selected": "auto", "reason": "build_resolution_keeps_target_auto"}
    selected = "local" if artifact.accessibility == "local" else "cluster"
    return {"requested": "auto", "selected": selected, "reason": f"artifact_accessibility:{artifact.accessibility}"}


def _resolution_decision(outcome: SelenaResolutionOutcome) -> dict[str, Any]:
    data = outcome.to_dict()
    allowed = {
        "status",
        "code",
        "action",
        "evidence",
        "resolution",
        "artifact_id",
        "workspace_binding_id",
        "branch",
        "commit",
        "dirty",
        "dirty_fingerprint",
        "build_mode",
    }
    return {key: value for key, value in data.items() if key in allowed and value not in ("", {}, [])}


def _needs_input(code: str, action: str, **evidence: Any) -> SelenaResolutionOutcome:
    return SelenaResolutionOutcome(status="needs_input", code=code, action=action, evidence=evidence)


def _impossible(code: str, action: str, **evidence: Any) -> SelenaResolutionOutcome:
    return SelenaResolutionOutcome(status="impossible", code=code, action=action, evidence=evidence)


__all__ = [
    "SourceResolutionContext",
    "SelenaResolutionApplication",
    "SelenaResolutionOutcome",
    "apply_selena_resolution",
    "apply_resolution_to_resolved_spec",
    "apply_resolution_to_stage_plan",
    "build_source_resolution_context_from_io",
    "resolve_selena",
]
