"""Explicit I/O boundary for v1 Selena source resolution inputs.

The pure resolver consumes immutable snapshots only. This module is the small
runtime edge that reads legacy project config and artifact catalog snapshots
before handing sanitized inputs to the application service.
"""

from __future__ import annotations

import math
from typing import Any, Callable

from core.api_v1 import SourceResolutionInputs, SourceResolutionProviderError
from core.artifacts import ArtifactCatalog, SelenaArtifact
from core.selena_resolver import SourceResolutionContext, build_source_resolution_context_from_io
from core.spec import ProjectCatalog, SimulationSpec, UserBindings


CatalogFactory = Callable[[str], ArtifactCatalog]
ConfigLoader = Callable[[str, str, str], Any]
NowFn = Callable[[], float]


def build_legacy_source_resolution_inputs(
    owner: str,
    spec: SimulationSpec,
    *,
    catalog_factory: CatalogFactory,
    config_loader: ConfigLoader,
    now_fn: NowFn,
    inspect_local_workspace: bool = False,
) -> SourceResolutionInputs:
    """Build immutable resolver inputs from legacy config and artifact storage.

    Central/Linux callers must keep ``inspect_local_workspace`` at its default
    ``False`` so Windows workspace paths are never inspected or probed. Windows
    full deployments may opt in, which explicitly calls the lower-level I/O
    context builder with the local workspace path from ``UserBindings``.
    """

    owner = str(owner or "").strip()
    try:
        bundle = config_loader(spec.project, spec.simulation.profile, spec.data.path)
    except SourceResolutionProviderError:
        raise
    except (FileNotFoundError, ValueError) as exc:
        raise SourceResolutionProviderError(
            "source_config_invalid",
            "Source resolution configuration is invalid or unavailable",
            status_code=422,
            action_type="fix_project_config",
            action_label="Fix the project configuration and retry",
        ) from exc
    except Exception as exc:
        raise SourceResolutionProviderError(
            "source_config_unavailable",
            "Source resolution configuration is unavailable",
            status_code=409,
            action_type="retry_source_resolution",
            action_label="Retry source resolution after configuration service recovery",
        ) from exc

    project_catalog = _require_type(getattr(bundle, "project_catalog", None), ProjectCatalog, "project_catalog")
    user_bindings = _require_type(getattr(bundle, "user_bindings", None), UserBindings, "user_bindings")
    evaluated_at = _now(now_fn)
    artifacts = _artifact_snapshot(catalog_factory, owner=owner, project=spec.project)
    workspace_binding_id = logical_workspace_binding_id(user_bindings)

    if inspect_local_workspace:
        context = _context_from_local_io(
            spec,
            project_catalog=project_catalog,
            user_bindings=user_bindings,
            owner=owner,
            evaluated_at=evaluated_at,
            workspace_binding_id=workspace_binding_id,
            artifacts=artifacts,
        )
    else:
        context = SourceResolutionContext(
            project_revision=project_catalog.revision,
            owner=owner,
            evaluated_at=evaluated_at,
            workspace_binding_id=workspace_binding_id,
            workspace_project=user_bindings.project if workspace_binding_id else "",
            workspace_fingerprint=None,
            branch_commits={},
            artifacts=artifacts,
        )
    return SourceResolutionInputs(
        project_catalog=project_catalog,
        user_bindings=user_bindings,
        context=context,
    )


def logical_workspace_binding_id(user_bindings: UserBindings) -> str:
    """Return a stable logical workspace id without exposing absolute paths.

    Backward-compatible facade over :func:`core.agent_bindings.make_workspace_binding_id`.
    """
    from core.agent_bindings import make_workspace_binding_id

    return make_workspace_binding_id(
        str(user_bindings.project or "").strip(),
        str(user_bindings.workspace_path or "").strip(),
    )


def _context_from_local_io(
    spec: SimulationSpec,
    *,
    project_catalog: ProjectCatalog,
    user_bindings: UserBindings,
    owner: str,
    evaluated_at: float,
    workspace_binding_id: str,
    artifacts: tuple[SelenaArtifact, ...],
) -> SourceResolutionContext:
    try:
        return build_source_resolution_context_from_io(
            project_revision=project_catalog.revision,
            owner=owner,
            evaluated_at=evaluated_at,
            workspace_binding_id=workspace_binding_id,
            workspace_project=user_bindings.project if workspace_binding_id else "",
            workspace_path=user_bindings.workspace_path if workspace_binding_id else "",
            branch_refs=_branch_refs(spec),
            artifacts=artifacts,
        )
    except Exception as exc:
        raise SourceResolutionProviderError(
            "source_workspace_unavailable",
            "Authorized workspace snapshot is unavailable",
            status_code=409,
            action_type="inspect_workspace",
            action_label="Inspect the authorized workspace and retry",
        ) from exc


def _artifact_snapshot(catalog_factory: CatalogFactory, *, owner: str, project: str) -> tuple[SelenaArtifact, ...]:
    try:
        catalog = catalog_factory(owner)
        return tuple(catalog.list(project=project, owner=owner, include_private=True))
    except Exception as exc:
        raise SourceResolutionProviderError(
            "source_artifact_catalog_unavailable",
            "Selena artifact catalog snapshot is unavailable",
            status_code=409,
            action_type="retry_source_resolution",
            action_label="Retry after artifact catalog recovery",
        ) from exc


def _branch_refs(spec: SimulationSpec) -> tuple[str, ...]:
    if spec.selena.mode == "branch" and spec.selena.branch:
        return (spec.selena.branch,)
    return ()


def _now(now_fn: NowFn) -> float:
    try:
        value = float(now_fn())
    except (TypeError, ValueError) as exc:
        raise SourceResolutionProviderError(
            "source_clock_unavailable",
            "Source resolution clock is unavailable",
            status_code=409,
            action_type="retry_source_resolution",
            action_label="Retry source resolution",
        ) from exc
    if value < 0 or not math.isfinite(value):
        raise SourceResolutionProviderError(
            "source_clock_invalid",
            "Source resolution clock is invalid",
            status_code=409,
            action_type="retry_source_resolution",
            action_label="Retry source resolution",
        )
    return value


def _require_type(value: Any, expected_type: type, label: str) -> Any:
    if not isinstance(value, expected_type):
        raise SourceResolutionProviderError(
            "source_config_invalid",
            "Source resolution configuration is invalid or unavailable",
            status_code=422,
            action_type="fix_project_config",
            action_label="Fix the project configuration and retry",
        )
    return value


__all__ = [
    "build_legacy_source_resolution_inputs",
    "logical_workspace_binding_id",
]
