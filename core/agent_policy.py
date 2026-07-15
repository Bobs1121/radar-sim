"""Windows Agent deployment-mode policy (v5 light/full boundary).

This module is the single source of truth for the Windows Agent node-kind /
deployment-mode capability contract defined in ``PRD.md`` §7.3 and
``docs/DETAILED_DESIGN.md`` §4.4.

A Windows Agent registers with an explicit ``node_kind`` carried in its
registration metadata:

- ``windows_agent``  — light Agent. Authorized workspace Selena compile,
  artifact register/validate/upload and local data inspect/validate/upload
  only. It must NEVER declare or receive local simulation, cluster
  simulation/gateway, cluster run/collect/finalize or the legacy
  ``cluster.run`` capability (INV-13 / PRD §14.4).
- ``windows_full``   — Windows full deployment. May additionally perform
  local compile + local simulation. It is distinct from
  ``platform_gateway`` and does not silently receive cluster-gateway
  capability (PRD §7.3 capability-ownership table).

The other node kinds (``linux_executor``, ``platform_gateway``) are part of
the same vocabulary but are not produced by the Windows Agent CLI; they are
accepted here so the policy can reason about them without special-casing.

This is a small, focused runtime-policy slice. It does not implement
artifact upload, scheduler replacement, data resolver or UI.
"""

from __future__ import annotations

from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Node-kind vocabulary (DETAILED_DESIGN §4.4 ExecutionNode.kind)
# ---------------------------------------------------------------------------

NODE_KIND_WINDOWS_AGENT = "windows_agent"
NODE_KIND_WINDOWS_FULL = "windows_full"
NODE_KIND_LEGACY = "legacy"
NODE_KIND_LINUX_EXECUTOR = "linux_executor"
NODE_KIND_PLATFORM_GATEWAY = "platform_gateway"

KNOWN_NODE_KINDS = frozenset({
    NODE_KIND_WINDOWS_AGENT,
    NODE_KIND_WINDOWS_FULL,
    NODE_KIND_LEGACY,
    NODE_KIND_LINUX_EXECUTOR,
    NODE_KIND_PLATFORM_GATEWAY,
})

# Windows Agent CLI deployment modes -> node kind.
MODE_LIGHT = "light"
MODE_FULL = "full"
WINDOWS_MODES = frozenset({MODE_LIGHT, MODE_FULL})

MODE_TO_NODE_KIND = {
    MODE_LIGHT: NODE_KIND_WINDOWS_AGENT,
    MODE_FULL: NODE_KIND_WINDOWS_FULL,
}

# ---------------------------------------------------------------------------
# Capability vocabulary (PRD §7.3 / DETAILED_DESIGN §4.4)
# ---------------------------------------------------------------------------

# Capabilities a light Windows Agent (windows_agent) is permitted to declare.
# Source/workspace checks, Selena compile, artifact register/validate/upload
# and local data inspect/validate/upload (PRD §14.4). The ``local.*`` entries
# are compatibility aliases for the existing local build/check task types so
# legacy build/check jobs still route to a light Agent.
LIGHT_AGENT_CAPABILITIES = frozenset({
    "source.workspace.recognize",
    # Existing-folder validation is input preparation, not simulation.
    "artifact.runtime.resolve",
    "source.workspace.read",
    "source.git.worktree",
    "build.selena",
    "data.local.read",
    "data.upload",
    "artifact.register",
    "artifact.validate",
    "artifact.upload",
    # Compatibility aliases for existing local build/check tasks.
    "local.check",
    "local.build_selena",
})

# Capabilities forbidden for a light Windows Agent — local simulation, cluster
# simulation/gateway, cluster run/collect/finalize and the legacy
# ``cluster.run``. Wildcards that would implicitly include any of these are
# also forbidden: a wildcard must not bypass light policy (PRD §14.4,
# DETAILED_DESIGN §4.4/§21.2).
FORBIDDEN_FOR_LIGHT_CAPABILITIES = frozenset({
    # Local simulation.
    "local.run_sim",
    "simulation.local",
    # Cluster simulation / gateway / run / collect / finalize.
    "simulation.cluster",
    "cluster.gateway",
    "cluster.run",
    "cluster.collect",
    "cluster.finalize",
    "result.collect",
    # Wildcards that would bypass the light boundary.
    "*",
    "local.*",
    "simulation.*",
    "cluster.*",
})

# Task / stage types a light Windows Agent must never claim, even if its
# stored record is corrupt or carries a wildcard. Used at claim time as the
# hard server-side boundary (DETAILED_DESIGN §4.4: light Agent 不领取
# preflight / run_simulation / collect_results / finalize_manifest).
FORBIDDEN_FOR_LIGHT_TASK_TYPES = frozenset({
    # Legacy task types.
    "local.run_sim",
    "cluster.run",
    "cluster.collect",
    "cluster.finalize",
    "result.collect",
    # v5 capability-style task types.
    "simulation.local",
    "simulation.cluster",
    "cluster.gateway",
    # v5 stage types the light Agent must not pick up.
    "run_simulation",
    "collect_results",
    "finalize_manifest",
    "preflight",
})

# Default capabilities advertised by each Windows Agent mode.
DEFAULT_LIGHT_CAPABILITIES = list(LIGHT_AGENT_CAPABILITIES)

# Full mode = light set + local simulation. Cluster execution remains a
# separate central/Gateway responsibility unless a later scheduler assigns an
# explicit adapter capability; it is never granted by this default.
DEFAULT_FULL_CAPABILITIES = list(LIGHT_AGENT_CAPABILITIES) + [
    "local.run_sim",
    "simulation.local",
    "manifest.finalize",
]

# Cluster execution is deliberately split across two non-Windows roles.  The
# Linux executor may inspect/prepare inputs and collect results, while the
# platform gateway is the only role allowed to submit or control a Cluster
# run.  Stage names are not capabilities; claim-time matching below translates
# stages to these semantic capabilities.
LINUX_EXECUTOR_CAPABILITIES = frozenset({
    "environment.cluster.check",
    "data.resolve",
    "cluster.prepare",
    "result.collect",
    "manifest.finalize",
})

PLATFORM_GATEWAY_CAPABILITIES = frozenset({
    "simulation.cluster",
    "cluster.gateway",
    # Compatibility for the legacy cluster.run queue while v1 migrates.
    "cluster.run",
})

LIGHT_AGENT_TASK_TYPES = frozenset({
    "local.check",
    "local.build_selena",
    "source.workspace.read",
    "source.git.worktree",
    "source.resolve",
    "resolve_spec",
    "data.local.read",
    "data.resolve",
    "data.upload",
    "build.selena",
    "artifact.register",
    "artifact.validate",
    "artifact.upload",
    "environment_check",
    "prepare_source",
    "prepare_data",
    "build_selena",
    "register_artifact",
})

FULL_AGENT_TASK_TYPES = LIGHT_AGENT_TASK_TYPES | frozenset({
    "local.run_sim",
    "simulation.local",
    "preflight",
    "run_simulation",
    "collect_results",
    "finalize_manifest",
})

LINUX_EXECUTOR_TASK_TYPES = frozenset({
    "environment_check",
    "prepare_data",
    "preflight",
    "collect_results",
    "finalize_manifest",
})

PLATFORM_GATEWAY_TASK_TYPES = frozenset({
    "run_simulation",
    # Compatibility for the legacy cluster.run queue.
    "cluster.run",
})

TASK_CAPABILITY_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "resolve_spec": ("source.workspace.recognize",),
    "environment_check": ("source.workspace.read", "build.selena", "artifact.validate"),
    "prepare_source": ("source.workspace.read",),
    "build_selena": ("build.selena",),
    "register_artifact": ("artifact.upload",),
    "prepare_data": ("data.local.read", "data.upload"),
}

NODE_TASK_CAPABILITY_REQUIREMENTS: dict[str, dict[str, tuple[str, ...]]] = {
    NODE_KIND_WINDOWS_FULL: {
        "preflight": ("local.check",),
        "run_simulation": ("simulation.local",),
        "collect_results": ("local.run_sim",),
        "finalize_manifest": ("manifest.finalize",),
    },
    NODE_KIND_LINUX_EXECUTOR: {
        "environment_check": ("environment.cluster.check",),
        "prepare_data": ("data.resolve",),
        "preflight": ("cluster.prepare",),
        "collect_results": ("result.collect",),
        "finalize_manifest": ("manifest.finalize",),
    },
    NODE_KIND_PLATFORM_GATEWAY: {
        "run_simulation": ("simulation.cluster",),
        "cluster.run": ("cluster.run",),
    },
}


def required_capabilities_for_task(
    task_type: object,
    stage_type: object = None,
    node_kind: object = None,
) -> tuple[str, ...]:
    """Return formal v5 capabilities for a known Stage alias."""
    kind = normalize_node_kind(node_kind)
    node_requirements = NODE_TASK_CAPABILITY_REQUIREMENTS.get(kind, {})
    for value in (stage_type, task_type):
        token = str(value or "").strip().lower()
        if token in node_requirements:
            return node_requirements[token]
        if token in TASK_CAPABILITY_REQUIREMENTS:
            return TASK_CAPABILITY_REQUIREMENTS[token]
    return ()


class AgentPolicyError(ValueError):
    """Raised when a Windows Agent declares capabilities its mode forbids.

    The message is stable and free of local path detail so it can surface
    directly as an HTTP 400 / CLI error without leaking runtime paths.
    """


def normalize_node_kind(value: object) -> str:
    """Normalize a node_kind string (strip + lowercase) or return ``""``.

    Empty / missing node_kind means a legacy caller that does not declare a
    v5 node kind; such callers keep their pre-v5 behavior and are not subject
    to the light-Agent policy (this does not open the light-Agent bypass
    because they are not registered as ``windows_agent``).
    """
    if value is None:
        return ""
    text = str(value).strip().lower()
    return text


def normalize_windows_mode(value: object) -> str:
    """Normalize a Windows Agent ``--windows-mode`` value, defaulting to light."""
    text = str(value or "").strip().lower() or MODE_LIGHT
    return text


def is_light_node_kind(node_kind: object) -> bool:
    """Return True if the (possibly raw) node_kind identifies a light Agent."""
    return normalize_node_kind(node_kind) == NODE_KIND_WINDOWS_AGENT


def is_windows_node_kind(node_kind: object) -> bool:
    """Return True if the node_kind is a Windows Agent (light or full)."""
    normalized = normalize_node_kind(node_kind)
    return normalized in (NODE_KIND_WINDOWS_AGENT, NODE_KIND_WINDOWS_FULL)


def normalize_capabilities(capabilities: Optional[Iterable[str]]) -> list[str]:
    """Return canonical lowercase capabilities, de-duplicated in first order."""
    if isinstance(capabilities, (str, bytes)):
        raise AgentPolicyError("capabilities must be a list of strings")
    result: list[str] = []
    seen: set[str] = set()
    for item in capabilities or []:
        if not isinstance(item, str):
            raise AgentPolicyError("capabilities must be a list of strings")
        text = item.strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def forbidden_light_capabilities(capabilities: Optional[Iterable[str]]) -> list[str]:
    """Return the capabilities in ``capabilities`` forbidden for a light Agent.

    Matching is case-insensitive on the capability token. Wildcards that would
    bypass the light boundary (``*``, ``local.*``, ``simulation.*``,
    ``cluster.*``) are reported as forbidden.
    """
    forbidden: list[str] = []
    lower_set = {str(item).strip().lower() for item in (capabilities or []) if str(item).strip()}
    for cap in lower_set:
        if cap in FORBIDDEN_FOR_LIGHT_CAPABILITIES:
            forbidden.append(cap)
    return forbidden


def validate_light_capabilities(capabilities: Optional[Iterable[str]]) -> list[str]:
    """Validate capabilities for a light Windows Agent.

    Returns the normalized capability list on success. Raises
    :class:`AgentPolicyError` with a stable message if any forbidden capability
    (or bypass wildcard) is present — the CLI / server fail fast rather than
    silently dropping a misconfigured capability.
    """
    normalized = normalize_capabilities(capabilities)
    forbidden = forbidden_light_capabilities(normalized)
    if forbidden:
        raise AgentPolicyError(
            "windows_agent light node may not declare forbidden capability: "
            + ", ".join(sorted(forbidden))
        )
    unsupported = sorted(set(normalized) - LIGHT_AGENT_CAPABILITIES)
    if unsupported:
        raise AgentPolicyError(
            "windows_agent light node may not declare unsupported capability: "
            + ", ".join(unsupported)
        )
    return normalized


def filter_capabilities_for_node(
    node_kind: object,
    capabilities: Optional[Iterable[str]],
) -> tuple[list[str], list[str]]:
    """Return server-trusted capabilities and rejected self-declarations."""
    kind = normalize_node_kind(node_kind) or NODE_KIND_LEGACY
    normalized = normalize_capabilities(capabilities)
    if kind == NODE_KIND_LEGACY:
        return normalized, []
    if kind == NODE_KIND_WINDOWS_AGENT:
        allowed = LIGHT_AGENT_CAPABILITIES
    elif kind == NODE_KIND_WINDOWS_FULL:
        allowed = frozenset(DEFAULT_FULL_CAPABILITIES)
    elif kind == NODE_KIND_LINUX_EXECUTOR:
        allowed = LINUX_EXECUTOR_CAPABILITIES
    elif kind == NODE_KIND_PLATFORM_GATEWAY:
        allowed = PLATFORM_GATEWAY_CAPABILITIES
    else:
        raise AgentPolicyError("unsupported agent node kind")
    effective = [capability for capability in normalized if capability in allowed]
    rejected = [capability for capability in normalized if capability not in allowed]
    return effective, rejected


def default_capabilities_for_mode(mode: object) -> list[str]:
    """Return the default capability list for a Windows Agent mode."""
    normalized = normalize_windows_mode(mode)
    if normalized == MODE_FULL:
        return list(DEFAULT_FULL_CAPABILITIES)
    if normalized == MODE_LIGHT:
        return list(DEFAULT_LIGHT_CAPABILITIES)
    raise AgentPolicyError("unsupported windows-mode: expected 'light' or 'full'")


def node_kind_for_mode(mode: object) -> str:
    """Map a Windows Agent mode (light|full) to its node_kind."""
    normalized = normalize_windows_mode(mode)
    if normalized not in MODE_TO_NODE_KIND:
        raise AgentPolicyError(
            "unsupported windows-mode: expected 'light' or 'full'"
        )
    return MODE_TO_NODE_KIND[normalized]


def is_forbidden_light_task_type(task_type: object, stage_type: object = None) -> bool:
    """Return True if a task/stage type is forbidden for a light Agent.

    Checks both the legacy ``task_type`` and the v5 ``stage_type`` so a
    corrupt record that stores the forbidden value in either field is still
    rejected at claim time.
    """
    for value in (task_type, stage_type):
        text = str(value or "").strip().lower()
        if text and text in FORBIDDEN_FOR_LIGHT_TASK_TYPES:
            return True
    return False


def may_claim_task(node_kind: object, task_type: object, stage_type: object = None) -> bool:
    """Apply a second, claim-time node policy to persisted task records.

    Legacy records retain their historical capability-driven behavior. V5
    Windows nodes use strict task/stage allowlists so wildcard or corrupt
    capability rows cannot widen their execution boundary.
    """
    kind = normalize_node_kind(node_kind) or NODE_KIND_LEGACY
    if kind == NODE_KIND_LEGACY:
        return True
    tokens = [
        str(value or "").strip().lower()
        for value in (task_type, stage_type)
        if str(value or "").strip()
    ]
    if not tokens:
        return False
    if kind == NODE_KIND_WINDOWS_AGENT:
        return all(token in LIGHT_AGENT_TASK_TYPES for token in tokens)
    if kind == NODE_KIND_WINDOWS_FULL:
        return all(token in FULL_AGENT_TASK_TYPES for token in tokens)
    if kind == NODE_KIND_LINUX_EXECUTOR:
        return all(token in LINUX_EXECUTOR_TASK_TYPES for token in tokens)
    if kind == NODE_KIND_PLATFORM_GATEWAY:
        return all(token in PLATFORM_GATEWAY_TASK_TYPES for token in tokens)
    return False
