"""Framework-agnostic v5 SimulationSpec stage planner.

The planner only describes the business DAG and initial skipped stages. It does
not select nodes, inspect paths, access the network, or execute work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.environment_contract import plan_environment_requirements
from core.spec import SimulationSpec
from core.user_config import UserRunConfig
from core.datasets import classify_data_path

STAGE_TYPES: tuple[str, ...] = (
    "resolve_spec",
    "environment_check",
    "prepare_source",
    "prepare_data",
    "build_selena",
    "register_artifact",
    "preflight",
    "run_simulation",
    "collect_results",
    "finalize_manifest",
)

STAGE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "resolve_spec": (),
    "environment_check": ("resolve_spec",),
    "prepare_source": ("environment_check",),
    "prepare_data": ("environment_check",),
    "build_selena": ("prepare_source",),
    "register_artifact": ("build_selena",),
    # Keep the execution-node snapshot as an explicit gate even though the
    # source/data branches also descend from it.  This prevents a future
    # skipped source branch (for example existing Selena) from accidentally
    # making preflight claimable without current runtime evidence.
    "preflight": ("environment_check", "register_artifact", "prepare_data"),
    "run_simulation": ("preflight",),
    "collect_results": ("run_simulation",),
    "finalize_manifest": ("collect_results",),
}

_CAPABILITY_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "resolve_spec": (),
    "environment_check": (),
    "prepare_source": ("source.resolve",),
    "prepare_data": ("data.resolve",),
    "build_selena": ("build.selena",),
    "register_artifact": ("artifact.register",),
    "preflight": ("preflight",),
    "run_simulation": ("simulation.run",),
    "collect_results": ("result.collect",),
    "finalize_manifest": ("manifest.finalize",),
}

_EXISTING_SELENA_SKIP_REASON = "existing_selena_uses_registered_artifact"
_CLUSTER_VISIBLE_EXISTING_SKIP_REASON = "existing_selena_is_cluster_visible"


def _cluster_visible_path(value: object) -> bool:
    """Return whether a public path can be referenced without a Connector.

    The planner deliberately performs syntax classification only.  Whether a
    shared/central path is actually mounted is a Linux/Cluster preflight
    concern; this helper only decides if the Windows source-side stages are
    unnecessary for the route requested by the user.
    """

    text = str(value or "").strip()
    if not text:
        return False
    if text.casefold().startswith(("dataset://", "config-asset://", "config-asset:")):
        return True
    return classify_data_path(text) in {"shared", "central"}


def cluster_visible_existing_selena(config: UserRunConfig, *, target: str = "") -> bool:
    """Whether an existing Selena run can execute without a Windows node.

    This is intentionally independent of product names, profiles, or catalog
    adapters.  Every required public input must be a shared/central reference;
    an optional Adapter/MatFilter is included when supplied.  A local drive
    path therefore keeps the existing Connector direct-transfer route.
    """

    selected_target = str(target or config.simulation.target or "").strip().casefold()
    if selected_target != "cluster" or config.selena.source != "existing":
        return False
    required = (
        config.selena.existing_path,
        config.selena.runtime_xml,
        config.data.path,
    )
    optional = (
        config.simulation.adapter_file,
        config.simulation.mat_filter,
    )
    return all(_cluster_visible_path(value) for value in required) and all(
        not str(value or "").strip() or _cluster_visible_path(value)
        for value in optional
    )


@dataclass(frozen=True)
class PlannedStage:
    stage_type: str
    dependencies: tuple[str, ...] = ()
    initial_status: str = "queued"
    skip_reason: str = ""
    required_capabilities: tuple[str, ...] = field(default_factory=tuple)
    input_ref: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)

    def to_task_spec(self) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "task_type": self.stage_type,
            "stage_type": self.stage_type,
            "dependencies": list(self.dependencies),
            "status": self.initial_status,
            "initial_status": self.initial_status,
            "payload": {
                "stage_type": self.stage_type,
                "required_capabilities": list(self.required_capabilities),
            },
            "input_ref": dict(self.input_ref),
        }
        if self.error:
            spec["error"] = dict(self.error)
        if self.skip_reason:
            spec["skip_reason"] = self.skip_reason
        return spec


@dataclass(frozen=True)
class StagePlan:
    stages: tuple[PlannedStage, ...]
    resolved_spec: dict[str, Any]

    def task_specs(self) -> list[dict[str, Any]]:
        return [stage.to_task_spec() for stage in self.stages]


def plan_simulation_stages(spec: SimulationSpec) -> StagePlan:
    """Return the fixed v5 10-stage DAG for one SimulationSpec."""
    canonical_spec = spec.to_dict()
    spec_hash = spec.fingerprint()
    stages: list[PlannedStage] = []
    for stage_type in STAGE_TYPES:
        initial_status = "queued"
        skip_reason = ""
        if spec.selena.mode == "existing" and stage_type in {"prepare_source", "build_selena"}:
            initial_status = "skipped"
            skip_reason = _EXISTING_SELENA_SKIP_REASON
        stages.append(
            PlannedStage(
                stage_type=stage_type,
                dependencies=STAGE_DEPENDENCIES[stage_type],
                initial_status=initial_status,
                skip_reason=skip_reason,
                required_capabilities=_CAPABILITY_PLACEHOLDERS[stage_type],
                input_ref={"spec_hash": spec_hash},
            )
        )
    return StagePlan(
        stages=tuple(stages),
        resolved_spec=pending_resolved_spec(canonical_spec, spec_hash),
    )


def plan_user_run_stages(config: UserRunConfig) -> StagePlan:
    """Plan the project-free public contract before internal recognition.

    Project/recipe recognition is intentionally a Stage concern.  This lets a
    Linux control plane accept a Windows-local code path and hand the check to
    the configured Agent, instead of forcing the user to understand an
    internal project catalog.
    """
    canonical = config.to_dict()
    config_hash = config.fingerprint()
    stages: list[PlannedStage] = []
    for stage_type in STAGE_TYPES:
        status = "queued"
        reason = ""
        if config.selena.source == "build" and stage_type == "prepare_source":
            status = "skipped"
            reason = "current_workspace_selected"
        if config.selena.source == "existing" and stage_type in {"prepare_source", "build_selena"}:
            status = "skipped"
            reason = _EXISTING_SELENA_SKIP_REASON
        if cluster_visible_existing_selena(config) and stage_type in {
            "resolve_spec",
            "register_artifact",
        }:
            status = "skipped"
            reason = _CLUSTER_VISIBLE_EXISTING_SKIP_REASON
        # A source-side existing folder normally needs registration/upload for
        # Cluster.  The project-free shared/central route below is the one
        # exception: it keeps the worker-visible folder in place and skips the
        # Windows resolver/registration stages entirely.
        capabilities = _CAPABILITY_PLACEHOLDERS[stage_type]
        if stage_type == "resolve_spec":
            capabilities = (
                "source.workspace.recognize"
                if config.selena.source == "build"
                else "artifact.runtime.resolve"
            ,)
        stages.append(
            PlannedStage(
                stage_type=stage_type,
                dependencies=STAGE_DEPENDENCIES[stage_type],
                initial_status=status,
                skip_reason=reason,
                required_capabilities=capabilities,
                input_ref={"config_hash": config_hash},
            )
        )
    return StagePlan(
        stages=tuple(stages),
        resolved_spec={
            "status": "pending_recognition",
            "source_config_hash": config_hash,
            "decisions": {},
            "environment_plan": plan_user_environment_requirements(config),
        },
    )


def plan_user_environment_requirements(config: UserRunConfig) -> dict[str, Any]:
    """Return a project-free, path-free preview for Web and SDK."""
    requirements: list[dict[str, Any]] = [
        {
            "id": "data",
            "stage_type": "prepare_data",
            "description": "Locate shared data or upload local MF4 data for the selected execution route.",
        },
        {
            "id": "simulation_assets",
            "stage_type": "preflight",
            "description": "Verify this task's MatFilter and optional Adapter without project defaults.",
        },
    ]
    if config.selena.source == "build":
        requirements.extend(
            [
                {
                    "id": "workspace_recognition",
                    "stage_type": "resolve_spec",
                    "description": "Inspect the selected build scripts and validate the current Windows workspace.",
                },
                {
                    "id": "selena_build",
                    "stage_type": "build_selena",
                    "description": "Run the selected Selena build script on the connected Windows computer and verify Selena.exe with its DLLs.",
                },
            ]
        )
    else:
        requirements.append(
            {
                "id": "runtime_bundle",
                "stage_type": "resolve_spec",
                "description": "Verify the selected existing Selena folder, its DLLs, and Runtime XML.",
            }
        )
    requirements.append(
        {
            "id": "execution",
            "stage_type": "run_simulation",
            "description": (
                "Run on Windows full deployment."
                if config.simulation.target == "local"
                else "Run through the Linux-controlled Cluster route."
                if config.simulation.target == "cluster"
                else "Choose Windows full or Cluster from currently available capabilities."
            ),
        }
    )
    return {"status": "planned", "requirements": requirements}


def pending_resolved_spec(canonical_spec: dict[str, Any], spec_hash: str) -> dict[str, Any]:
    """Build the WP3 placeholder ResolvedSpec without pretending resolution ran."""
    spec = SimulationSpec.from_dict(canonical_spec)
    return {
        "status": "pending",
        "source_spec_hash": spec_hash,
        "project": str(canonical_spec.get("project") or ""),
        "decisions": {},
        "environment_plan": plan_environment_requirements(spec),
    }


__all__ = [
    "PlannedStage",
    "STAGE_DEPENDENCIES",
    "STAGE_TYPES",
    "StagePlan",
    "pending_resolved_spec",
    "plan_simulation_stages",
    "plan_user_environment_requirements",
    "plan_user_run_stages",
    "cluster_visible_existing_selena",
]
