"""Pure environment-requirement plan for one SimulationSpec.

This module describes *what* must be checked and on which node kind.  It does
not inspect paths, tools, credentials, Agents, or Cluster services.  Concrete
project adapters and node-local checkers turn these logical requirements into
an EnvironmentSnapshot later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core.spec.model import SimulationSpec


@dataclass(frozen=True)
class EnvironmentRequirement:
    id: str
    stage_type: str
    capability: str
    node_kinds: tuple[str, ...]
    description: str
    user_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Public ResolvedSpec/API values are JSON-shaped.  Keep the immutable
        # tuple internally but serialize node kinds as a list so frozen/thawed
        # resolution snapshots compare exactly with the persisted StagePlan.
        data["node_kinds"] = list(self.node_kinds)
        return data


def plan_environment_requirements(
    spec: SimulationSpec,
    *,
    project_adapter: str = "",
) -> dict[str, Any]:
    """Return a path-free, deterministic environment plan for Web/SDK/Stage routing."""
    requirements: list[EnvironmentRequirement] = [
        EnvironmentRequirement(
            id="project_catalog",
            stage_type="resolve_spec",
            capability="project.catalog.read",
            node_kinds=("central",),
            description="Load the project profile and adapter contract.",
        ),
        EnvironmentRequirement(
            id="data_resolver",
            stage_type="prepare_data",
            capability="data.resolve",
            node_kinds=("central", "windows_agent", "windows_full"),
            description="Resolve the user data path as shared, node-local, or upload-required.",
            user_action="Select the data folder or provide one reusable data path.",
        ),
    ]

    if spec.selena.mode == "existing":
        requirements.append(
            EnvironmentRequirement(
                id="selena_artifact_access",
                stage_type="preflight",
                capability="artifact.read",
                node_kinds=("central", "windows_full", "linux_executor", "platform_gateway"),
                description="Verify that the selected Selena artifact is compatible and reachable by the execution target.",
                user_action="Choose an existing Selena artifact or reusable publish path.",
            )
        )
    else:
        requirements.extend(
            [
                EnvironmentRequirement(
                    id="workspace_binding",
                    stage_type="prepare_source",
                    capability="source.workspace.read",
                    node_kinds=("windows_agent", "windows_full"),
                    description="Resolve an authorized Windows workspace binding without exposing its path centrally.",
                    user_action="Authorize the project workspace once on the Windows machine.",
                ),
                EnvironmentRequirement(
                    id="selena_build_toolchain",
                    stage_type="build_selena",
                    capability="build.selena",
                    node_kinds=("windows_agent", "windows_full"),
                    description="Check the project adapter build script and its Windows toolchain dependencies.",
                    user_action="Run the guided dependency setup only when the Agent reports a missing build dependency.",
                ),
                EnvironmentRequirement(
                    id="artifact_publish",
                    stage_type="register_artifact",
                    capability="artifact.upload",
                    node_kinds=("windows_agent", "windows_full"),
                    description="Validate and upload the artifact produced by the same build attempt.",
                ),
            ]
        )

    if spec.simulation.target == "local":
        requirements.append(
            EnvironmentRequirement(
                id="local_simulation_runtime",
                stage_type="run_simulation",
                capability="simulation.local",
                node_kinds=("windows_full",),
                description="Check Selena runtime assets and local simulation dependencies.",
                user_action="Use Windows full deployment or change the target to Cluster.",
            )
        )
    elif spec.simulation.target == "cluster":
        requirements.append(
            EnvironmentRequirement(
                id="cluster_runtime",
                stage_type="run_simulation",
                capability="simulation.cluster",
                node_kinds=("linux_executor", "platform_gateway"),
                description="Check shared artifact/data access and the Cluster submission gateway.",
            )
        )
    else:
        requirements.append(
            EnvironmentRequirement(
                id="execution_route",
                stage_type="environment_check",
                capability="execution.route",
                node_kinds=("central",),
                description="Choose local or Cluster execution from available nodes and resolved resources.",
            )
        )

    return {
        "status": "planned",
        "project_adapter": str(project_adapter or ""),
        "requirements": [item.to_dict() for item in requirements],
    }


__all__ = ["EnvironmentRequirement", "plan_environment_requirements"]
