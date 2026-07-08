"""Unified profile model for local + cluster simulation.

A profile bundles one combination of (Selena source, data policy, backend)
so users switch a whole runtime assumption with ``--profile <name>`` instead
of editing config or passing many flags.

Profile shape (top-level ``profiles`` list in a project config):

    profiles:
      - name: local-build
        description: "..."
        backend: local            # local | cluster
        selena:
          source: build            # build = local compile artifact, path = existing exe
          exe: ""                  # used when source=path
        data:
          copy: false              # stage data locally before run?
          required_signals: []     # optional scan-time validation
        # cluster-only fields (backend=cluster):
        cluster:
          group: Radar
          subgroup: PSS1
          simulation_prio: 1
        # asset/sim overrides (optional, apply to both backends):
        runtime_xml: "..."
        matfilefilter: "..."
        adapter_file: "..."
        source: RadarFC
        mounting_position: ""

Backwards compatibility: when a project has only the legacy ``cluster.profiles``
flat list (no top-level ``profiles``), it is converted on the fly so existing
ovrs25 configs keep working unchanged.
"""

from __future__ import annotations

import copy
from typing import Any

from core.config import resolve_selena_executable
from core.simulation import get_simulation_config


DEFAULT_PROFILE_NAME = "default"
DEFAULT_BACKEND = "local"


def list_profiles(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all profiles in the unified shape, including the implicit default.

    The implicit ``default`` profile is synthesized from the project's base
    ``simulation``/``cluster``/``assets`` config so that omitting ``--profile``
    always works and behaves like the legacy "no profile" path.
    """
    sim = get_simulation_config(config)
    cluster = dict(config.get("cluster") or {})
    assets = config.get("assets") or {}

    default = {
        "name": DEFAULT_PROFILE_NAME,
        "description": "Project default runtime/Selena configuration",
        "backend": DEFAULT_BACKEND,
        "selena": {
            "source": "build",
            "exe": str(cluster.get("selena_exe") or resolve_selena_executable(config) or ""),
        },
        "data": {
            "copy": bool(cluster.get("copy_data", False)),
            "required_signals": list(cluster.get("required_input_signals") or []),
        },
        "runtime_xml": str(sim.get("runtime_xml") or assets.get("runtime_xml", "")),
        "matfilefilter": str(sim.get("matfilefilter") or assets.get("matfilefilter", "")),
        "adapter_file": str(sim.get("adapter_file") or assets.get("adapter_file", "")),
        "config_template": str(assets.get("config_template") or ""),
        "source": str(sim.get("source") or ""),
        "mounting_position": str(sim.get("mounting_position") or ""),
    }
    profiles = [default]

    # Merge top-level profiles with legacy cluster.profiles so configs that
    # migrated partially (top-level local-build + legacy cluster profiles) keep
    # all entries available. Top-level entries take precedence on name clashes.
    top_level = config.get("profiles") or []
    legacy = [_convert_legacy_cluster_profile(item) for item in (cluster.get("profiles") or [])]
    seen_names = {DEFAULT_PROFILE_NAME}
    for raw in list(top_level) + list(legacy):
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        if raw["name"] in seen_names:
            continue
        seen_names.add(raw["name"])
        profiles.append(_normalize_profile(raw, fallback_cluster=cluster))
    return profiles


def get_profile(config: dict[str, Any], name: str = "") -> dict[str, Any]:
    """Return a single profile by name (default if empty/missing)."""
    target = str(name or "").strip() or DEFAULT_PROFILE_NAME
    for profile in list_profiles(config):
        if profile.get("name") == target:
            return profile
    raise ValueError(f"Unknown profile: {target}")


def apply_profile(config: dict[str, Any], name: str = "") -> dict[str, Any]:
    """Return a config copy with the selected profile overlaid.

    Overlays asset/sim/cluster fields from the profile onto the config and
    records ``active_profile`` and ``active_backend`` for downstream code.
    """
    target = str(name or "").strip() or DEFAULT_PROFILE_NAME
    if target == DEFAULT_PROFILE_NAME:
        updated = copy.deepcopy(config)
        updated["active_profile"] = DEFAULT_PROFILE_NAME
        updated["active_backend"] = DEFAULT_BACKEND
        updated["cluster"] = dict(config.get("cluster") or {})
        updated["cluster"]["active_profile"] = DEFAULT_PROFILE_NAME
        return updated

    selected = get_profile(config, target)
    updated = copy.deepcopy(config)
    updated["cluster"] = dict(config.get("cluster") or {})
    updated["simulation"] = dict(config.get("simulation") or {})
    updated["assets"] = dict(config.get("assets") or {})
    updated["active_profile"] = target
    updated["active_backend"] = str(selected.get("backend") or DEFAULT_BACKEND)
    updated["cluster"]["active_profile"] = target

    # Flat override keys (legacy + convenience).
    sim_keys = {
        "source",
        "mounting_position",
        "tolerant",
        "enable_multibuffer_border",
        "enable_doorkeeper",
        "disable_sequence_check",
        "extra_args",
    }
    asset_keys = {"runtime_xml", "matfilefilter", "adapter_file", "config_template"}
    cluster_keys = {
        "selena_exe",
        "required_input_signals",
        "group",
        "subgroup",
        "simulation_prio",
        "timeout_min",
        "finalstep",
        "filter",
        "python_version",
        "extension",
        "skip_dir",
        "skip_filename",
    }

    for key, value in selected.items():
        if key in sim_keys:
            updated["simulation"][key] = value
        elif key in asset_keys:
            updated["assets"][key] = value
            if key in {"runtime_xml", "matfilefilter", "adapter_file"}:
                updated["simulation"][key] = value
        elif key in cluster_keys:
            updated["cluster"][key] = value

    selena = selected.get("selena") or {}
    if isinstance(selena, dict):
        if selena.get("exe"):
            updated["cluster"]["selena_exe"] = selena["exe"]
        # Record selena source so cluster prepare knows whether to copy runtime.
        updated["_profile_selena_source"] = str(selena.get("source") or "build")
        # Record expected selena branch so checks can warn on exe/branch mismatch.
        if selena.get("selena_branch"):
            updated["_profile_selena_branch"] = str(selena["selena_branch"])
            updated.setdefault("build", {})
            if isinstance(updated["build"], dict):
                updated["build"].setdefault("selena_branch", str(selena["selena_branch"]))
        # source=build leaves selena_exe to be resolved from build_output at runtime.

    data = selected.get("data") or {}
    if isinstance(data, dict):
        if "copy" in data:
            updated["cluster"]["copy_data"] = bool(data["copy"])
        if data.get("required_signals") is not None:
            updated["cluster"]["required_input_signals"] = list(data["required_signals"] or [])

    cluster_block = selected.get("cluster") or {}
    if isinstance(cluster_block, dict):
        for key, value in cluster_block.items():
            updated["cluster"][key] = value

    return updated


def resolve_selena_exe(config: dict[str, Any], profile: dict[str, Any] | None = None) -> str:
    """Resolve the selena.exe path for a profile.

    ``source=build`` (or unset) → derive from ``build.build_output`` via
    ``resolve_selena_executable``. ``source=path`` → use ``selena.exe``.
    Falls back to the legacy ``cluster.selena_exe`` if present.
    """
    profile = profile or {}
    selena = profile.get("selena") or {}
    source = str(selena.get("source") or "build").strip().lower()
    if source == "path" and selena.get("exe"):
        return str(selena["exe"])
    if source == "build":
        resolved = resolve_selena_executable(config)
        if resolved:
            return resolved
    # Fallbacks: profile exe, then legacy cluster.selena_exe.
    if selena.get("exe"):
        return str(selena["exe"])
    cluster = config.get("cluster") or {}
    return str(cluster.get("selena_exe") or "")


def active_backend(config: dict[str, Any]) -> str:
    """Return the backend recorded by apply_profile, defaulting to local."""
    return str(config.get("active_backend") or DEFAULT_BACKEND)


def active_profile_name(config: dict[str, Any]) -> str:
    """Return the profile name recorded by apply_profile, defaulting to default."""
    return str(config.get("active_profile") or DEFAULT_PROFILE_NAME)


# ---------------------------------------------------------------------------
# Internal normalization
# ---------------------------------------------------------------------------

def _normalize_profile(raw: dict[str, Any], *, fallback_cluster: dict[str, Any]) -> dict[str, Any]:
    profile = dict(raw)
    profile.setdefault("description", "")
    profile.setdefault("backend", DEFAULT_BACKEND)

    selena = profile.get("selena")
    if not isinstance(selena, dict):
        # Legacy flat profile carried selena_exe at top level.
        exe = str(profile.get("selena_exe") or "")
        profile["selena"] = {
            "source": "path" if exe else "build",
            "exe": exe,
            "selena_branch": "",
        }
    else:
        selena.setdefault("source", "build" if not selena.get("exe") else "path")
        selena.setdefault("exe", "")
        selena.setdefault("selena_branch", "")

    data = profile.get("data")
    if not isinstance(data, dict):
        profile["data"] = {
            "copy": bool(profile.get("copy_data", fallback_cluster.get("copy_data", False))),
            "required_signals": list(profile.get("required_input_signals") or fallback_cluster.get("required_input_signals") or []),
        }
    else:
        data.setdefault("copy", bool(fallback_cluster.get("copy_data", False)))
        data.setdefault("required_signals", list(fallback_cluster.get("required_input_signals") or []))

    return profile


def _convert_legacy_cluster_profile(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a legacy cluster.profiles entry (flat) into the unified shape."""
    converted: dict[str, Any] = {
        "name": item.get("name"),
        "description": item.get("description", ""),
        "backend": "cluster",
        "selena": {
            "source": "path" if item.get("selena_exe") else "build",
            "exe": str(item.get("selena_exe") or ""),
        },
        "data": {
            "copy": False,
            "required_signals": list(item.get("required_input_signals") or []),
        },
    }
    # Carry through flat override keys that apply_profile understands.
    for key in ("runtime_xml", "matfilefilter", "adapter_file", "source", "mounting_position"):
        if item.get(key) is not None:
            converted[key] = item[key]
    cluster_block: dict[str, Any] = {}
    for key in ("group", "subgroup", "simulation_prio", "timeout_min", "finalstep"):
        if item.get(key) is not None:
            cluster_block[key] = item[key]
    if cluster_block:
        converted["cluster"] = cluster_block
    return converted
