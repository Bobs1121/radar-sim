"""Shared route contracts for stages whose executor depends on the target."""

from __future__ import annotations

from typing import Any, Mapping


def selected_execution_target(job: Mapping[str, Any]) -> str:
    """Return the scheduler-selected target without changing public config."""

    selected = str(
        (
            ((job.get("resolved_spec") or {}).get("decisions") or {})
            .get("execution")
            or {}
        ).get("selected_target")
        or ""
    ).strip().casefold()
    if selected in {"local", "cluster"}:
        return selected
    requested = str(
        ((job.get("spec") or {}).get("simulation") or {}).get("target") or ""
    ).strip().casefold()
    return requested


def register_artifact_dispatch_scope(job: Mapping[str, Any]) -> str:
    """Return the only valid executor scope for ``register_artifact``.

    Registration is not synonymous with Cluster transfer.  A local build
    only validates/reuses its Agent-local Runtime Bundle Lease; a Cluster
    build issues a signed direct-transfer plan.  Keeping this mapping in one
    place prevents a route-specific Stage from silently inheriting the wrong
    transport behavior.
    """

    target = selected_execution_target(job)
    if target == "local":
        return "local_runtime_registration"
    if target == "cluster":
        return "direct_transfer"
    raise ValueError("register_artifact execution target is unresolved")


__all__ = ["register_artifact_dispatch_scope", "selected_execution_target"]
