"""
Configuration loader with multi-project support and assets management.

v4 config system:
  config/default.yaml          — global defaults (AI, default project)
  config/projects/<name>/      — per-project configs
    config.yaml                — compile paths, environment
    signals.yaml               — signals to monitor
    rules.yaml                 — check rules
  assets/<name>/               — simulation resources
    runtime.xml
    selena_config.txt
    matfilefilter.txt

Backward-compatible: old single config.yaml still works.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import yaml

from core.simulation import build_paramconfig_placeholders, get_simulation_config

logger = logging.getLogger(__name__)


def get_radar_sim_root() -> Path:
    """Get the radar-sim project root directory (code location, not data)."""
    return Path(__file__).resolve().parent.parent


def get_data_root() -> Path:
    """Get the data root for results/DBs.

    Defaults to the project root (backward compatible). Set ``RSIM_HOME`` to
    redirect results, control DB, task store, and local.yaml to a per-user
    directory without moving the code install.
    """
    home = os.environ.get("RSIM_HOME", "").strip()
    return Path(home).expanduser() if home else get_radar_sim_root()


def get_config_dir() -> Path:
    """Get the config/ directory."""
    return get_radar_sim_root() / "config"


def get_projects_dir() -> Path:
    """Get config/projects/ directory."""
    d = get_config_dir() / "projects"
    return d


def local_yaml_path_for_project(project: str) -> Path:
    """Return the local.yaml path for a project.

    If ``RSIM_HOME`` is set, prefer ``$RSIM_HOME/config/projects/<project>/local.yaml``
    (per-user overrides, isolated from the shared code checkout). Otherwise fall
    back to the in-repo ``config/projects/<project>/local.yaml`` (legacy).
    The RSIM_HOME location is created on demand by the caller (save_local_config).
    """
    home = os.environ.get("RSIM_HOME", "").strip()
    if home:
        return Path(home).expanduser() / "config" / "projects" / project / "local.yaml"
    return get_projects_dir() / project / "local.yaml"


def get_recipes_dir() -> Path:
    """Get config/recipes/ directory."""
    return get_config_dir() / "recipes"


def get_assets_dir() -> Path:
    """Get assets/ directory."""
    return get_radar_sim_root() / "assets"


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _normalize_layer(layer: dict[str, Any]) -> dict[str, Any]:
    result = dict(layer or {})
    machine = dict(result.get("machine") or {})
    build = dict(result.get("build") or {})
    assets = dict(result.get("assets") or {})
    vs_debug = dict(result.get("vs_debug") or {})
    paths = dict(result.get("paths") or {})
    environment = dict(result.get("environment") or {})
    project = dict(result.get("project") or {})
    repos = dict(result.get("repos") or {})
    simulation = dict(result.get("simulation") or {})

    project_root = result.get("project_root") or paths.get("project_root") or repos.get("outer_repo_root")
    if project_root:
        machine.setdefault("project_root", project_root)
        paths.setdefault("project_root", project_root)
        paths.setdefault("source_root", project_root)
        repos.setdefault("outer_repo_root", project_root)

    binding = result.get("binding") or paths.get("binding")
    if binding:
        machine.setdefault("binding", binding)
        paths.setdefault("binding", binding)

    for key in ("build_mode", "build_config", "build_output", "r2d2_script"):
        value = result.get(key) or paths.get(key)
        if value:
            build.setdefault(key, value)
            paths.setdefault(key, value)

    if paths.get("selena_config") and not build.get("build_config"):
        build.setdefault("build_config", paths["selena_config"])
        paths.setdefault("build_config", paths["selena_config"])

    if paths.get("selena_paramconfig"):
        assets.setdefault("fixed_config_path", paths["selena_paramconfig"])

    paths_simulation = paths.get("simulation", {})
    if isinstance(paths_simulation, dict):
        simulation = _deep_merge(paths_simulation, simulation)

    for key in ("selena_build_script", "hex_build_script", "env_build_script"):
        value = result.get(key) or build.get(key)
        if value:
            build.setdefault(key, value)

    selena_branch = result.get("selena_branch") or build.get("selena_branch") or repos.get("inner_repo_branch")
    if selena_branch:
        build.setdefault("selena_branch", selena_branch)
        repos.setdefault("inner_repo_branch", selena_branch)

    inner_repo_root = result.get("inner_repo_root") or repos.get("inner_repo_root")
    if inner_repo_root:
        repos.setdefault("inner_repo_root", inner_repo_root)

    if result.get("results_dir") or paths.get("results_dir"):
        paths.setdefault("results_dir", result.get("results_dir") or paths.get("results_dir"))

    if result.get("assets_dir") or paths.get("assets_dir"):
        assets.setdefault("root", result.get("assets_dir") or paths.get("assets_dir"))
        paths.setdefault("assets_dir", assets.get("root"))

    for key in ("runtime_xml", "config_template", "fixed_config_path", "matfilefilter", "adapter_file"):
        value = result.get(key) or assets.get(key)
        if value:
            assets.setdefault(key, value)

    for key in (
        "python3_path",
        "boost_root",
        "qt_path",
        "matlab_root",
        "selena_env_path",
        "vs_version",
        "path_prefix",
    ):
        value = result.get(key) or environment.get(key)
        if value:
            environment.setdefault(key, value)

    paths_environment = paths.get("environment", {})
    if isinstance(paths_environment, dict):
        for key in (
            "python3_path",
            "boost_root",
            "qt_path",
            "matlab_root",
            "selena_env_path",
            "vs_version",
            "path_prefix",
        ):
            value = paths_environment.get(key)
            if value:
                environment.setdefault(key, value)

    compile_section = result.get("compile", {}) or {}
    if isinstance(compile_section, dict) and compile_section.get("vs_sln"):
        vs_debug.setdefault("solution", compile_section["vs_sln"])

    result["machine"] = machine
    result["build"] = build
    result["assets"] = assets
    result["vs_debug"] = vs_debug
    result["paths"] = paths
    result["environment"] = environment
    result["project"] = project
    result["repos"] = repos
    result["simulation"] = simulation
    paths["simulation"] = simulation
    return result


def _resolve_project_assets(project: str, config: dict[str, Any] | None = None) -> Path:
    """Resolve the assets directory: explicit assets.root wins, else standard layout."""
    if config:
        explicit = str((config.get("assets") or {}).get("root") or "")
        if explicit:
            return Path(explicit)
    return get_projects_dir() / project / "assets"


def _finalize_layered_config(project: str, config: dict[str, Any]) -> dict[str, Any]:
    result = dict(config)
    machine = dict(result.get("machine") or {})
    build = dict(result.get("build") or {})
    assets = dict(result.get("assets") or {})
    vs_debug = dict(result.get("vs_debug") or {})
    paths = dict(result.get("paths") or {})
    environment = dict(result.get("environment") or {})
    project_section = dict(result.get("project") or {})
    repos = dict(result.get("repos") or {})
    simulation = dict(result.get("simulation") or {})
    selena_build_script = (
        result.get("selena_build_script")
        or build.get("selena_build_script")
        or paths.get("selena_build_script")
        or ""
    )
    derived_from_script = derive_project_context_from_selena_script(selena_build_script)
    if derived_from_script.get("script_dependency_hints"):
        build["script_dependency_hints"] = list(derived_from_script["script_dependency_hints"])

    project_root = (
        result.get("project_root")
        or machine.get("project_root")
        or paths.get("project_root")
        or paths.get("source_root")
        or derived_from_script.get("project_root", "")
        or ""
    )
    if project_root:
        project_root = os.path.normpath(str(project_root))
        result["project_root"] = project_root
        machine["project_root"] = project_root
        paths["project_root"] = project_root
        paths.setdefault("source_root", project_root)
        repos["outer_repo_root"] = project_root

    binding = (
        result.get("binding")
        or machine.get("binding")
        or paths.get("binding")
        or derived_from_script.get("binding", "")
        or ""
    )
    if not binding and project_root:
        binding = detect_binding(project_root)
    if binding:
        result["binding"] = binding
        machine["binding"] = binding
        paths["binding"] = binding

    if not repos.get("inner_repo_root") and project_root:
        candidate_inner = Path(project_root) / "apl" / "byd"
        repos["inner_repo_root"] = os.path.normpath(
            str(candidate_inner if candidate_inner.exists() else Path(project_root))
        )

    selena_branch = build.get("selena_branch") or repos.get("inner_repo_branch") or result.get("selena_branch") or ""
    if selena_branch:
        build["selena_branch"] = selena_branch
        repos["inner_repo_branch"] = selena_branch

    machine["name"] = project_section.get("name") or machine.get("name") or project
    machine["platform"] = project_section.get("platform") or machine.get("platform") or "gen5_selena"
    project_section["name"] = machine["name"]
    project_section["platform"] = machine["platform"]

    assets_root = _resolve_project_assets(project, result)
    assets_root_str = os.path.normpath(str(assets_root))
    # Task-safe runtime dir: unique per load_config() call so concurrent threads
    # in the same process (ThreadingHTTPServer) don't collide on CRlog.log /
    # paramconfig. time+pid+uuid suffix is stable within one config object and
    # unique across calls.
    import time as _time, uuid as _uuid
    run_id = result.setdefault("_meta", {}).get("_run_id") or (
        f"{_time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_{_uuid.uuid4().hex[:6]}"
    )
    result["_meta"]["_run_id"] = run_id
    runtime_root = os.path.normpath(str(get_results_base_dir() / project / "_runtime" / run_id))
    assets["root"] = assets_root_str
    paths["assets_dir"] = assets_root_str
    assets.setdefault("config_template", os.path.join("selena", "selena_config_tmpl.txt"))
    assets.setdefault("fixed_config_path", os.path.join(runtime_root, f"{project}_selena_paramconfig.txt"))

    for key in ("runtime_xml", "config_template", "fixed_config_path", "matfilefilter", "adapter_file"):
        value = assets.get(key, "")
        if value and not os.path.isabs(value):
            base_dir = assets_root
            if key == "fixed_config_path":
                base_dir = Path(runtime_root)
            assets[key] = os.path.normpath(str(base_dir / value))

    if assets.get("fixed_config_path"):
        paths["selena_paramconfig"] = assets["fixed_config_path"]
    for key in ("runtime_xml", "matfilefilter", "adapter_file"):
        if assets.get(key):
            paths[key] = assets[key]

    simulation = _deep_merge(simulation, paths.get("simulation", {}) or {})
    simulation.setdefault("runtime_xml", assets.get("runtime_xml", ""))
    simulation.setdefault("matfilefilter", assets.get("matfilefilter", ""))
    simulation.setdefault("adapter_file", assets.get("adapter_file", ""))
    simulation.setdefault("log_file", os.path.normpath(str(Path(runtime_root) / "CRlog.log")))
    result["simulation"] = simulation
    paths["simulation"] = simulation

    build_mode = (
        build.get("build_mode")
        or result.get("build_mode")
        or derived_from_script.get("build_mode", "")
        or "RelWithDebInfo"
    )
    build["build_mode"] = build_mode
    result["build_mode"] = build_mode

    explicit_build_config = build.get("build_config") or paths.get("build_config") or ""
    derived_build_config = str(derived_from_script.get("build_config", "") or "")
    build_config = explicit_build_config or derived_build_config
    if explicit_build_config and not os.path.isabs(str(explicit_build_config)) and derived_build_config:
        build_config = derived_build_config
    if build_config:
        build["build_config"] = build_config
        paths["build_config"] = build_config
        result["build_config"] = build_config

    build_output = (
        build.get("build_output")
        or paths.get("build_output")
        or derived_from_script.get("build_output", "")
        or ""
    )
    if build_output:
        build_output = os.path.normpath(str(build_output))
        build["build_output"] = build_output
        paths["build_output"] = build_output
        result["build_output"] = build_output

    r2d2_script = (
        build.get("r2d2_script")
        or paths.get("r2d2_script")
        or derived_from_script.get("r2d2_script", "")
        or ""
    )
    if r2d2_script:
        r2d2_script = os.path.normpath(str(r2d2_script))
        build["r2d2_script"] = r2d2_script
        paths["r2d2_script"] = r2d2_script
        result["r2d2_script"] = r2d2_script

    if not selena_build_script and project_root and binding:
        selena_build_script = os.path.normpath(
            str(Path(project_root) / "apl" / "byd" / "bindings" / binding / "selena" / "jenkins_selena_build.bat")
        )
    if selena_build_script:
        selena_build_script = os.path.normpath(str(selena_build_script))
        result["selena_build_script"] = selena_build_script
        build["selena_build_script"] = selena_build_script

    hex_build_script = (
        result.get("hex_build_script")
        or build.get("hex_build_script")
        or derived_from_script.get("hex_build_script", "")
        or ""
    )
    if not hex_build_script and project_root and binding:
        hex_build_script = os.path.normpath(
            str(Path(project_root) / "apl" / "byd" / "bindings" / binding / "buildscripts" / "testbuild_BaseC0S_SINGLE.bat")
        )
    if hex_build_script:
        hex_build_script = os.path.normpath(str(hex_build_script))
        result["hex_build_script"] = hex_build_script
        build["hex_build_script"] = hex_build_script

    py3 = (
        environment.get("python3_path")
        or result.get("python3_path")
        or derived_from_script.get("python3_path", "")
        or _detect_python3()
        or ""
    )
    if py3:
        py3 = os.path.normpath(str(py3))
        environment["python3_path"] = py3
        result["python3_path"] = py3

    boost_root = (
        environment.get("boost_root")
        or result.get("boost_root")
        or derived_from_script.get("boost_root", "")
        or _detect_boost()
        or ""
    )
    if boost_root:
        boost_root = os.path.normpath(str(boost_root))
        environment["boost_root"] = boost_root
        result["boost_root"] = boost_root

    qt_path = (
        environment.get("qt_path")
        or result.get("qt_path")
        or derived_from_script.get("qt_path", "")
        or _detect_qt()
        or ""
    )
    if qt_path:
        qt_path = os.path.normpath(str(qt_path))
        environment["qt_path"] = qt_path
        result["qt_path"] = qt_path

    matlab_root = (
        environment.get("matlab_root")
        or result.get("matlab_root")
        or environment.get("matlab_path")
        or _detect_matlab()
        or ""
    )
    if matlab_root:
        matlab_root = os.path.normpath(str(matlab_root))
        environment["matlab_root"] = matlab_root
        result["matlab_root"] = matlab_root

    selena_env_path = (
        environment.get("selena_env_path")
        or result.get("selena_env_path")
        or derived_from_script.get("selena_env_path", "")
        or _detect_selena_env()
        or ""
    )
    if selena_env_path:
        selena_env_path = os.path.normpath(str(selena_env_path))
        environment["selena_env_path"] = selena_env_path
        result["selena_env_path"] = selena_env_path

    path_prefix = list(environment.get("path_prefix") or [])
    prefix_segments = []
    if selena_env_path:
        prefix_segments.append(os.path.normpath(str(Path(selena_env_path) / "MSYS" / "mingw64" / "bin")))
    if matlab_root:
        prefix_segments.append(os.path.normpath(str(Path(matlab_root) / "bin" / "win64")))
    if qt_path:
        prefix_segments.append(os.path.normpath(str(Path(qt_path) / "bin")))
        prefix_segments.append(os.path.normpath(str(Path(qt_path) / "lib")))
    if boost_root:
        prefix_segments.append(os.path.normpath(str(Path(boost_root) / "lib64-msvc-14.0")))
    for segment in prefix_segments:
        if segment not in path_prefix:
            path_prefix.append(segment)
    environment["path_prefix"] = path_prefix

    result["build"] = build
    result["paths"] = paths
    result["vs_debug"] = vs_debug
    resolved_vs_solution = get_selena_vs_solution(result)
    if resolved_vs_solution:
        build["vs_solution"] = resolved_vs_solution
        paths["vs_solution"] = resolved_vs_solution
        result["vs_solution"] = resolved_vs_solution
    vs_debug["solution"] = resolved_vs_solution
    vs_debug["environment_path"] = render_selena_environment_path(result)
    fixed_config_path = assets.get("fixed_config_path", "")
    if fixed_config_path:
        vs_debug["command_args"] = ["--paramconfig", os.path.normpath(str(fixed_config_path))]
    else:
        vs_debug["command_args"] = []
    vs_debug.setdefault("target_project", "selena")

    result["machine"] = machine
    result["build"] = build
    result["assets"] = assets
    result["vs_debug"] = vs_debug
    result["paths"] = paths
    result["environment"] = environment
    result["project"] = project_section
    result["repos"] = repos
    result["simulation"] = simulation
    result.setdefault("_meta", {})
    return result


# ============================================================
# Project discovery
# ============================================================

def list_projects() -> list[str]:
    """List all configured project names."""
    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        # Fall back: check if old config.yaml exists
        old = get_radar_sim_root() / "config.yaml"
        if old.exists():
            return ["default"]
        return []
    return sorted([
        d.name for d in projects_dir.iterdir()
        if d.is_dir() and (d / "config.yaml").exists()
    ])


def get_default_project() -> str:
    """Get the default project name."""
    default_cfg = get_config_dir() / "default.yaml"
    if default_cfg.exists():
        cfg = yaml.safe_load(default_cfg.read_text(encoding="utf-8")) or {}
        name = cfg.get("default_project", "")
        if name:
            projects = list_projects()
            if name in projects:
                return name
    # Fallback to first available
    projects = list_projects()
    if projects:
        return projects[0]
    return "default"


# ============================================================
# Config loading
# ============================================================

def load_config(project: Optional[str] = None) -> dict[str, Any]:
    """Load configuration for a project.

    Args:
        project: Project name. If None, uses default_project.

    Returns:
        Merged config dict with all derived paths.
    """
    if project is None:
        project = get_default_project()

    # Try new multi-project config first
    project_cfg_path = get_projects_dir() / project / "config.yaml"
    if project_cfg_path.exists():
        return _load_project_config(project, project_cfg_path)

    # Fall back to old single config.yaml
    old_cfg = get_radar_sim_root() / "config.yaml"
    if old_cfg.exists():
        return _load_legacy_config(old_cfg)

    raise FileNotFoundError(
        f"Project '{project}' not found. "
        f"Available: {list_projects() or 'none (run rsim init)'}"
    )


def load_simulation_spec_bundle(
    project: Optional[str] = None,
    *,
    profile: str | None = None,
    data_path: str | None = None,
) -> Any:
    """Load legacy config and map it to read-only v5 spec/catalog/bindings.

    Importing the v5 spec adapter stays lazy so legacy ``core.config`` remains
    importable without the optional ``v5-spec`` dependencies installed.
    """
    effective_config = load_config(project)
    from core.spec.legacy_adapter import adapt_legacy_config

    return adapt_legacy_config(effective_config, project=project, profile=profile, data_path=data_path)


def load_config_from_path(local_yaml_path: str | Path) -> dict[str, Any]:
    """Load config from a local.yaml path (any location), not a project name.

    Resolution:
      1. project name: inferred from path / local.yaml content
      2. config.yaml: same directory if present, else config/projects/<name>/config.yaml
      3. layers: global → platform (default gen5_selena) → recipe (if specified) → config.yaml → local.yaml
      4. _finalize_layered_config derives project_root/binding/selena_build_script from repos
    """
    local_path = Path(local_yaml_path)
    if not local_path.exists():
        raise FileNotFoundError(f"local.yaml not found: {local_path}")

    # Peek at local.yaml content to infer project name (project.name field wins).
    local_content = _load_yaml_file(local_path) or {}
    project = _infer_project_name_from_path(local_path, local_content)

    # config.yaml: same directory preferred, else standard layout.
    same_dir_config = local_path.parent / "config.yaml"
    if same_dir_config.exists():
        return _load_project_config(project, same_dir_config)
    standard_config = get_projects_dir() / project / "config.yaml"
    if standard_config.exists():
        return _load_project_config(project, standard_config)

    # No config.yaml: synthesize a minimal layer set (platform default, no recipe).
    platform_path = get_config_dir() / "platforms" / "gen5_selena.yaml"
    layers = [
        _normalize_layer(load_global_defaults()),
        _normalize_layer(_load_yaml_file(platform_path)),
        _normalize_layer(local_content),
    ]
    config: dict[str, Any] = {}
    for layer in layers:
        config = _deep_merge(config, layer)
    config = _finalize_layered_config(project, config)
    missing = _validate(config)
    if missing:
        raise ValueError(f"Config from '{local_path}' missing: {missing}")
    config.setdefault("_meta", {})
    config["_meta"]["project"] = project
    config["_meta"]["local_config_path"] = str(local_path)
    return config


def _infer_project_name_from_path(local_path: Path, local_content: dict | None = None) -> str:
    """Infer project name: local.yaml project.name > parent dir (if in projects/) > filename stem > default."""
    if local_content:
        name = str((local_content.get("project") or {}).get("name") or "")
        if name:
            return name
    try:
        rel = local_path.relative_to(get_projects_dir())
        if rel.parts:
            return str(rel.parts[0])
    except ValueError:
        pass
    stem = local_path.stem
    if stem in ("local", "local.yaml"):
        return "default"
    return stem or "default"


def load_signals(project: str) -> list[dict]:
    """Load signals.yaml for a project."""
    path = get_projects_dir() / project / "signals.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("signals", [])


def load_rules(project: str) -> list[dict]:
    """Load rules.yaml for a project."""
    path = get_projects_dir() / project / "rules.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("rules", [])


def load_global_defaults() -> dict:
    """Load global default.yaml."""
    path = get_config_dir() / "default.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ============================================================
# Project config loader (new v4 format)
# ============================================================

def _load_project_config(project: str, path: Path) -> dict[str, Any]:
    """Load and merge layered project config."""
    platform_path = get_config_dir() / "platforms" / "gen5_selena.yaml"
    # local.yaml: prefer RSIM_HOME (per-user), fall back to in-repo next to config.yaml.
    local_path = local_yaml_path_for_project(project)
    if not local_path.exists():
        local_path = path.parent / "local.yaml"
    project_layer = _load_yaml_file(path)
    recipe_name = (
        (project_layer.get("project") or {}).get("recipe")
        or project_layer.get("recipe")
        or ""
    )
    recipe_path = get_recipes_dir() / f"{recipe_name}.yaml" if recipe_name else None
    if recipe_name and recipe_path and not recipe_path.exists():
        raise FileNotFoundError(f"Recipe '{recipe_name}' not found: {recipe_path}")

    layers = [
        _normalize_layer(load_global_defaults()),
        _normalize_layer(_load_yaml_file(platform_path)),
    ]
    if recipe_path and recipe_path.exists():
        layers.append(_normalize_layer(_load_yaml_file(recipe_path)))
    layers.extend([
        _normalize_layer(project_layer),
        _normalize_layer(_load_yaml_file(local_path)),
    ])

    config: dict[str, Any] = {}
    for layer in layers:
        config = _deep_merge(config, layer)

    config = _finalize_layered_config(project, config)

    missing = _validate(config)
    if missing:
        raise ValueError(f"Config for project '{project}' missing: {missing}")

    config.setdefault("_meta", {})
    config["_meta"]["project"] = project
    config["_meta"]["recipe"] = recipe_name
    config["_meta"]["config_path"] = str(path)
    config["_meta"]["platform_config_path"] = str(platform_path)
    if recipe_path and recipe_path.exists():
        config["_meta"]["recipe_path"] = str(recipe_path)
    if local_path.exists():
        config["_meta"]["local_config_path"] = str(local_path)

    return config


# ============================================================
# Legacy config loader (backward-compatible)
# ============================================================

def _load_legacy_config(path: Path) -> dict[str, Any]:
    """Load old single-file config.yaml (backward-compatible)."""
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    if "project_root" not in config and "paths" in config:
        config = _migrate_old_config(config)

    config = _auto_derive(config)

    # Ensure results_dir exists
    results_dir = config.get("paths", {}).get("results_dir", "results")
    os.makedirs(results_dir, exist_ok=True)

    config.setdefault("_meta", {})
    config["_meta"]["project"] = "default"
    config["_meta"]["config_path"] = str(path)

    return config


def _migrate_old_config(config: dict) -> dict:
    """Migrate old-style config (paths/selena/environment) to new style."""
    paths = config.get("paths", {})
    selena = config.get("selena", {})
    env = config.get("environment", {})
    project = config.get("project", {})

    new_config = dict(config)
    new_config["project_root"] = paths.get("source_root", "")
    new_config.setdefault("runtime_xml", selena.get("runtime_xml", ""))
    new_config.setdefault("config_template", selena.get("config_template", ""))
    new_config.setdefault("boost_root", env.get("boost_root", ""))
    new_config.setdefault("results_dir", paths.get("results_dir", "results"))
    new_config.setdefault("project", project)

    for key in ("analysis", "logging"):
        if key in config:
            new_config.setdefault(key, config[key])

    return new_config


# ============================================================
# Auto-derivation
# ============================================================

def _auto_derive(config: dict) -> dict:
    """Auto-derive all paths from project_root."""
    root = config.get("project_root", "")
    if not root:
        return config

    root = os.path.normpath(root)

    # Detect binding
    binding = config.get("binding", "")
    if not binding:
        binding = detect_binding(root)
        config["binding"] = binding

    binding_dir = f"{root}/apl/byd/bindings/{binding}" if binding else ""

    # paths section
    paths = config.setdefault("paths", {})
    paths.setdefault("source_root", root)
    paths.setdefault("build_output", f"{root}/ip_dc/build/ROS_PER_SIT_RPM_FCT_RECR")
    paths.setdefault("r2d2_script", f"{root}/ip_dc/dc_tools/R2D2.py")
    paths.setdefault(
        "build_config",
        f"{root}/apl/byd/selena/cmake_build_cfg/ROS_PER_SIT_RPM_FCT_RECR.config",
    )
    paths.setdefault("results_dir", config.get("results_dir", "results"))

    # selena section
    selena = config.setdefault("selena", {})
    selena.setdefault("executable_name", "selena.exe")
    selena.setdefault("exe_pattern", "dc_tools/selena/core/{build_mode}/selena.exe")
    selena.setdefault("build_mode", config.get("build_mode", "RelWithDebInfo"))
    selena.setdefault("simulation_timeout", 600)

    # Auto-detect runtime_xml if not in assets
    if not selena.get("runtime_xml"):
        runtime_xml = _detect_runtime_xml(binding_dir)
        if runtime_xml:
            selena["runtime_xml"] = runtime_xml

    # Auto-detect config_template
    if not selena.get("config_template"):
        template = _detect_config_template(binding_dir)
        if template:
            selena["config_template"] = template

    # Build scripts
    if binding_dir:
        config.setdefault("selena_build_script", f"{binding_dir}/selena/jenkins_selena_build.bat")
        config.setdefault("hex_build_script", f"{binding_dir}/buildscripts/testbuild_BaseC0S_SINGLE.bat")

    # Environment
    env = config.setdefault("environment", {})
    py3 = config.get("python3_path") or env.get("python3_path")
    if not py3:
        py3 = _detect_python3()
    if py3:
        env["python3_path"] = py3
        config.setdefault("python3_path", py3)

    boost = config.get("boost_root") or env.get("boost_root")
    if not boost:
        boost = _detect_boost()
    if boost:
        env["boost_root"] = boost
        config.setdefault("boost_root", boost)

    path_prefix = env.get("path_prefix", [])
    if not path_prefix:
        qt = _detect_qt()
        if qt:
            path_prefix.append(f"{qt}/bin")
        matlab = _detect_matlab()
        if matlab:
            path_prefix.append(f"{matlab}/bin/win64")
        if boost:
            path_prefix.append(f"{boost}/lib64-msvc-14.0")
    env["path_prefix"] = path_prefix

    # VS detection
    vs_postfix = ""
    if os.path.exists(r"C:\Program Files (x86)\Microsoft Visual Studio\2019"):
        vs_postfix = "-vs vs16"
    config.setdefault("vs_postfix", vs_postfix)

    # Project info
    proj = config.setdefault("project", {})
    proj.setdefault("name", "BYD_OVS_CB")
    proj.setdefault("platform", "gen5_selena")

    return config


# ============================================================
# Auto-detection functions
# ============================================================

def detect_binding(root: str) -> str:
    """Detect binding name from project structure."""
    bindings_dir = os.path.join(root, "apl", "byd", "bindings")
    if not os.path.isdir(bindings_dir):
        return ""
    try:
        entries = os.listdir(bindings_dir)
        bindings = [e for e in entries if os.path.isdir(os.path.join(bindings_dir, e))]
        if len(bindings) == 1:
            return bindings[0]
        if "ovrs25" in bindings:
            return "ovrs25"
        if bindings:
            return sorted(bindings)[0]
    except OSError:
        pass
    return ""


def derive_project_context_from_selena_script(script_path: str) -> dict[str, Any]:
    """Derive project context from a Selena build script path and content."""
    if not script_path:
        return {}

    script = Path(os.path.normpath(str(script_path)))
    data: dict[str, Any] = {"selena_build_script": str(script)}

    parts_lower = [part.lower() for part in script.parts]
    if "apl" in parts_lower and "bindings" in parts_lower:
        apl_idx = parts_lower.index("apl")
        bindings_idx = parts_lower.index("bindings")
        if bindings_idx + 1 < len(script.parts):
            data["binding"] = script.parts[bindings_idx + 1]
        if apl_idx > 0:
            data["project_root"] = os.path.normpath(str(Path(*script.parts[:apl_idx])))
    elif "apl" in parts_lower and "byd" in parts_lower:
        apl_idx = parts_lower.index("apl")
        if apl_idx > 0:
            data["project_root"] = os.path.normpath(str(Path(*script.parts[:apl_idx])))

    if data.get("project_root") and data.get("binding"):
        project_root = Path(data["project_root"])
        binding = data["binding"]
        data.setdefault("r2d2_script", os.path.normpath(str(project_root / "ip_dc" / "dc_tools" / "R2D2.py")))
        data.setdefault(
            "hex_build_script",
            os.path.normpath(str(project_root / "apl" / "byd" / "bindings" / binding / "buildscripts" / "testbuild_BaseC0S_SINGLE.bat")),
        )
    elif data.get("project_root"):
        project_root = Path(data["project_root"])
        data.setdefault("r2d2_script", os.path.normpath(str(project_root / "ip_dc" / "dc_tools" / "R2D2.py")))

    if script.exists():
        try:
            text = script.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""

        patterns = {
            "build_mode": r"set\s+buildmode=([^\r\n]+)",
            "build_config": r"set\s+selena_config=([^\r\n]+)",
            "selena_env_path": r"set\s+SELENA_ENV_PATH=([^\r\n]+)",
            "boost_root": r"set\s+BOOST_ROOT=([^\r\n]+)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                data[key] = match.group(1).strip().strip('"')

        qt_match = re.search(
            r"([A-Za-z]:\\[^;\r\n!]+\\msvc\d+_64)\\bin;([A-Za-z]:\\[^;\r\n!]+\\msvc\d+_64)\\lib",
            text,
            flags=re.IGNORECASE,
        )
        if qt_match:
            data["qt_path"] = qt_match.group(1)

        dependency_hints = extract_build_script_dependency_hints(text)
        if dependency_hints:
            data["script_dependency_hints"] = dependency_hints

        if data.get("project_root"):
            project_root = str(data["project_root"])
            build_config_name = str(data.get("build_config", "")).strip()
            build_config_from_r2d2 = _extract_r2d2_build_config(text, project_root, build_config_name)
            if build_config_from_r2d2:
                data["build_config"] = build_config_from_r2d2
            build_output_from_script = _extract_r2d2_build_output(text, project_root)
            if build_output_from_script:
                data["build_output"] = build_output_from_script

    if data.get("selena_env_path"):
        data.setdefault(
            "python3_path",
            os.path.normpath(str(Path(data["selena_env_path"]) / "MSYS" / "mingw64" / "bin" / "python3.exe")),
        )

    if data.get("project_root") and data.get("build_config"):
        build_config_value = str(data["build_config"])
        build_output_name = Path(build_config_value).stem if os.path.isabs(build_config_value) else build_config_value
        data.setdefault(
            "build_output",
            os.path.normpath(str(Path(data["project_root"]) / "ip_dc" / "build" / build_output_name)),
        )

    return data


def extract_build_script_dependency_hints(text: str) -> list[str]:
    """Extract human-readable dependency/install hints from a Selena build script."""
    hints: list[str] = []
    if re.search(r"\bitc2\.exe\s+install\b", text, flags=re.IGNORECASE):
        hints.append("ITC2 tool collection install via C:/TCC/itc2/itc2.exe")
    collection_matches = re.findall(r"set\s+TOOLCOLLECTION=([^\r\n]+)", text, flags=re.IGNORECASE)
    for value in collection_matches:
        value = value.strip().strip('"')
        if value:
            hints.append(f"TCC tool collection: {value}")
    init_matches = re.findall(r"call\s+([^\r\n]*tcc_init[^\r\n]*init\.bat)", text, flags=re.IGNORECASE)
    for value in init_matches:
        hints.append(f"TCC init script: {value.strip()}")
    if re.search(r"\bpython3\s+.*R2D2\.py\b", text, flags=re.IGNORECASE):
        hints.append("Python3 runtime capable of running ip_dc/dc_tools/R2D2.py")
    if re.search(r"set\s+BOOST_ROOT=%TCCPATH_boost%", text, flags=re.IGNORECASE):
        hints.append("Boost is provided by TCCPATH_boost after TCC init")
    if re.search(r"%TCCPATH_selena_environment%", text, flags=re.IGNORECASE):
        hints.append("Selena environment is provided by TCCPATH_selena_environment after TCC init")
    return list(dict.fromkeys(hints))


def _script_var_to_path(value: str, project_root: str, build_config_name: str = "") -> str:
    result = value.strip().strip('"')
    replacements = {
        "%root_path%": project_root,
        "%APL_PATH%": os.path.normpath(str(Path(project_root) / "apl" / "byd")),
        "!selena_config!": build_config_name,
        "%selena_config%": build_config_name,
    }
    for token, replacement in replacements.items():
        result = result.replace(token, replacement)
    result = result.replace("/", os.sep)
    return os.path.normpath(result)


def _extract_r2d2_build_config(text: str, project_root: str, build_config_name: str) -> str:
    for line in text.splitlines():
        if line.lstrip().lower().startswith("rem") or "R2D2.py" not in line:
            continue
        match = re.search(r"R2D2\.py\s+-m\s+([^\s\r\n]+)", line, flags=re.IGNORECASE)
        if not match:
            continue
        config_path = _script_var_to_path(match.group(1), project_root, build_config_name)
        if "!selena_config!" not in match.group(1) and "%selena_config%" not in match.group(1):
            return config_path
        if build_config_name:
            return config_path
    return ""


def _extract_r2d2_build_output(text: str, project_root: str) -> str:
    for line in text.splitlines():
        if line.lstrip().lower().startswith("rem") or "R2D2.py" not in line:
            continue
        match = re.search(r"\s-B\s+([^\s\r\n]+)", line, flags=re.IGNORECASE)
        if match:
            return _script_var_to_path(match.group(1), project_root)
    return ""


def _detect_runtime_xml(binding_dir: str) -> str:
    """Find runtime XML in binding or C:/tools/."""
    runtime_dir = os.path.join(binding_dir, "selena", "config", "runtime")
    if os.path.isdir(runtime_dir):
        xmls = glob.glob(os.path.join(runtime_dir, "*_Runtime*.xml"))
        if not xmls:
            xmls = glob.glob(os.path.join(runtime_dir, "*.xml"))
        if xmls:
            return xmls[0]
    tools = "C:/tools"
    if os.path.isdir(tools):
        xmls = glob.glob(os.path.join(tools, "Runtime_*.xml"))
        if xmls:
            return xmls[0]
    return ""


def _detect_config_template(binding_dir: str) -> str:
    """Find selena config template."""
    tools = "C:/tools"
    if os.path.isdir(tools):
        txts = glob.glob(os.path.join(tools, "*Selena_Config*.txt"))
        if txts:
            return txts[0]
    selena_dir = os.path.join(binding_dir, "selena")
    if os.path.isdir(selena_dir):
        txts = glob.glob(os.path.join(selena_dir, "**/*Config*.txt"), recursive=True)
        if txts:
            return txts[0]
    return ""


def _detect_python3() -> str:
    """Detect Python3 path."""
    py3 = "C:/TCC/Tools/selena_environment/0.1.7_WIN64/MSYS/mingw64/bin/python3.exe"
    if os.path.exists(py3):
        return py3
    py = shutil.which("python3") or shutil.which("python")
    return py or ""


def _detect_selena_env() -> str:
    """Detect selena_environment installation."""
    candidates = [
        r"C:\TCC\Tools\selena_environment\0.1.7_WIN64",
        r"C:\TCC\Tools\selena_environment",
    ]
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return ""


def _detect_boost() -> str:
    """Detect Boost installation."""
    for c in [
        r"C:\TCC\Tools\boost\1.63.0_WIN64",
        r"C:\boost\1_63_0",
        r"C:\local\boost_1_63_0",
    ]:
        if os.path.isdir(c):
            return c
    br = os.environ.get("BOOST_ROOT", "")
    if br and os.path.isdir(br):
        return br
    return ""


def _detect_qt() -> str:
    """Detect Qt installation."""
    for c in [
        r"C:\TCC\Tools\qt\5.8.0_WIN64\5.8\msvc2015_64",
        r"C:\Qt\5.8\msvc2015_64",
    ]:
        if os.path.isdir(c):
            return c
    return ""


def _detect_matlab() -> str:
    """Detect MATLAB installation."""
    for c in [
        r"C:\Program Files\MATLAB\R2023b",
        r"C:\Program Files\MATLAB\R2022b",
        r"C:\Program Files\MATLAB\R2023a",
        r"C:\Program Files\MATLAB\R2022a",
    ]:
        if os.path.isdir(c):
            return c
    return ""


# ============================================================
# Validation
# ============================================================

def _validate(config: dict) -> list[str]:
    """Check required keys after auto-derivation."""
    missing = []
    if not config.get("project_root"):
        missing.append("project_root")
    return missing


# ============================================================
# Helpers
# ============================================================

def merge_cli_overrides(config: dict, overrides: dict[str, str]) -> dict:
    """Apply CLI --param k=v overrides into config dict."""
    result = dict(config)
    for k, v in overrides.items():
        parts = k.split(".")
        d = result
        for part in parts[:-1]:
            d = d.setdefault(part, {})
        d[parts[-1]] = v
    return result


def get_selena_exe(config: dict) -> str:
    """Locate the simulation executable from config paths."""
    build_output = config["paths"]["build_output"]
    pattern = config["selena"]["exe_pattern"]
    build_mode = config["selena"]["build_mode"]
    exe_name = config["selena"]["executable_name"]
    exe_dir = os.path.join(build_output, pattern.format(build_mode=build_mode))
    return os.path.join(exe_dir, exe_name)


def get_selena_vs_solution(config: dict) -> str:
    """Resolve the Selena Visual Studio solution path."""
    solution = config.get("build", {}).get("vs_solution") or config.get("vs_debug", {}).get("solution", "")
    build_output = config.get("build", {}).get("build_output") or config.get("paths", {}).get("build_output", "")
    if solution:
        if os.path.isabs(solution):
            return os.path.normpath(str(solution))
        if build_output:
            return os.path.normpath(str(Path(build_output) / solution))
        return os.path.normpath(str(solution))

    compile_solution = config.get("compile", {}).get("vs_sln", "")
    if compile_solution:
        if os.path.isabs(compile_solution) or not build_output:
            return os.path.normpath(str(compile_solution))
        return os.path.normpath(str(Path(build_output) / compile_solution))

    if build_output:
        return os.path.normpath(str(Path(build_output) / "dc_tools" / "selena" / "selena.sln"))
    return ""


def render_selena_config(config: dict) -> dict[str, Any]:
    """Return and synchronize the resolved Selena config payload."""
    payload = {
        "machine": dict(config.get("machine", {})),
        "build": dict(config.get("build", {})),
        "assets": dict(config.get("assets", {})),
        "environment": dict(config.get("environment", {})),
        "vs_debug": dict(config.get("vs_debug", {})),
    }
    assets = payload["assets"]
    assets_root = assets.get("root", "")
    source = assets.get("config_template", "")
    fixed = assets.get("fixed_config_path", "")

    if not source:
        raise ValueError("Missing assets.config_template. Set a project-maintained Selena paramconfig source in config.")
    if not fixed:
        raise ValueError("Missing assets.fixed_config_path. Configure where the fixed Selena paramconfig should be written.")

    source_path = Path(source)
    fixed_path = Path(fixed)
    if not source_path.is_absolute() and assets_root:
        source_path = Path(assets_root) / source_path
    if not fixed_path.is_absolute() and assets_root:
        fixed_path = Path(assets_root) / fixed_path

    source_path = Path(os.path.normpath(str(source_path)))
    fixed_path = Path(os.path.normpath(str(fixed_path)))
    if not source_path.exists():
        raise FileNotFoundError(
            f"Selena config source not found: {source_path}. "
            f"Ensure assets.config_template points to an existing file under assets.root ({assets_root})."
        )

    template_text = source_path.read_text(encoding="utf-8")
    sim = get_simulation_config(config)
    sim.setdefault("paramconfig_path", str(fixed_path))
    sim.setdefault("input_mf4", config.get("paths", {}).get("input_mf4") or config.get("input_mf4") or "")
    sim.setdefault("output_mf4", config.get("paths", {}).get("output_mf4") or config.get("output_mf4") or "")
    replacements = build_paramconfig_placeholders(config, sim)
    rendered_text = template_text
    for placeholder, value in replacements.items():
        if placeholder in rendered_text:
            rendered_text = rendered_text.replace(placeholder, value)

    normalized_lines = []
    for line in rendered_text.splitlines():
        stripped = line.strip()
        if stripped in {"adapterfile=", "matfilefilter=", "source=", "{{EXTRA_PARAMCONFIG_LINES}}"}:
            continue
        if stripped == "":
            normalized_lines.append(line)
            continue
        if "=" in stripped:
            key, value = stripped.split("=", 1)
            if key and value == "":
                continue
        normalized_lines.append(line)
    rendered_text = "\n".join(normalized_lines).strip() + "\n"

    fixed_path.parent.mkdir(parents=True, exist_ok=True)
    fixed_path.write_text(rendered_text, encoding="utf-8")

    assets["config_template"] = str(source_path)
    assets["fixed_config_path"] = str(fixed_path)
    payload["assets"] = assets
    return payload


def render_selena_environment_path(config: dict) -> str:
    """Assemble the Selena debug PATH."""
    env = config.get("environment", {})
    parts: list[str] = []

    for segment in env.get("path_prefix", []) or []:
        if segment and segment not in parts:
            parts.append(segment)

    matlab_root = env.get("matlab_root") or env.get("matlab_path", "")
    if matlab_root:
        matlab_segment = os.path.normpath(str(Path(matlab_root) / "bin" / "win64"))
        if matlab_segment not in parts:
            parts.append(matlab_segment)

    selena_env_path = env.get("selena_env_path", "")
    if selena_env_path:
        selena_segment = os.path.normpath(str(Path(selena_env_path) / "MSYS" / "mingw64" / "bin"))
        if selena_segment not in parts:
            parts.append(selena_segment)

    qt_path = env.get("qt_path", "")
    if qt_path:
        qt_segment = os.path.normpath(str(Path(qt_path) / "bin"))
        if qt_segment not in parts:
            parts.append(qt_segment)
        qt_lib_segment = os.path.normpath(str(Path(qt_path) / "lib"))
        if qt_lib_segment not in parts:
            parts.append(qt_lib_segment)

    boost_root = env.get("boost_root", "")
    if boost_root:
        boost_segment = os.path.normpath(str(Path(boost_root) / "lib64-msvc-14.0"))
        if boost_segment not in parts:
            parts.append(boost_segment)

    parts.extend(["$(Path)", "$(LocalDebuggerEnvironment)"])
    return ";".join(parts) + ";"


def resolve_selena_executable(config: dict, build_mode: Optional[str] = None) -> str:
    """Resolve selena.exe path from build_output plus configured relative pattern."""
    build_output = str(config.get("paths", {}).get("build_output", "") or "").strip()
    if not build_output:
        return ""

    selena = config.get("selena", {}) or {}
    build = config.get("build", {}) or {}
    selected_mode = (
        build_mode
        or build.get("build_mode")
        or selena.get("build_mode")
        or "RelWithDebInfo"
    )
    executable_name = str(selena.get("executable_name", "selena.exe") or "selena.exe")
    pattern = str(selena.get("exe_pattern", "dc_tools/selena/core/{build_mode}") or "dc_tools/selena/core/{build_mode}")
    rendered = pattern.format(
        build_mode=selected_mode,
        executable_name=executable_name,
        build_output=build_output,
        project_root=str(config.get("project_root", "") or ""),
    )
    relative_path = Path(rendered)
    if relative_path.suffix:
        exe_path = Path(build_output) / relative_path
    else:
        exe_path = Path(build_output) / relative_path / executable_name
    return os.path.normpath(str(exe_path))


def get_selena_command_args(config: dict) -> list[str]:
    """Build the standard Selena R2D2 command arguments."""
    args: list[str] = []
    build = config.get("build", {})
    build_config = build.get("build_config", "")
    if build_config:
        config_name = os.path.splitext(os.path.basename(build_config))[0] if os.path.isabs(build_config) else build_config
        args.extend(["-m", config_name])
    build_mode = build.get("build_mode", "")
    if build_mode:
        args.extend(["-bm", build_mode])
    args.extend(["-ghs_math", "-use_mat", "-notests"])
    vs_postfix = config.get("vs_postfix", "")
    if vs_postfix:
        args.extend(vs_postfix.split())
    return args


def get_results_base_dir() -> Path:
    """Get the shared results root directory (follows RSIM_HOME if set)."""
    return get_data_root() / "results"


def get_env_path(config: dict) -> str:
    """Assemble PATH environment variable from config."""
    return render_selena_environment_path(config)


def get_git_info(root: str) -> dict:
    """Get Git info for the project."""
    info = {"dirty": False}
    try:
        result = subprocess.run(
            ["git", "-C", root, "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            info["branch"] = result.stdout.strip()
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            info["commit"] = result.stdout.strip()
        result = subprocess.run(
            ["git", "-C", root, "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            info["dirty"] = bool(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return info


# ============================================================
# Results directory management (project-isolated)
# ============================================================

def get_results_dir(project: str, timestamp: Optional[str] = None) -> Path:
    """Get results directory for a project, creating if needed (follows RSIM_HOME)."""
    results = get_data_root() / "results" / project
    if timestamp:
        results = results / timestamp
    results.mkdir(parents=True, exist_ok=True)
    return results


def get_latest_result_dir(project: str) -> Optional[Path]:
    """Get the latest result directory for a project (follows RSIM_HOME)."""
    results = get_data_root() / "results" / project
    if not results.exists():
        return None
    dirs = sorted([d for d in results.iterdir() if d.is_dir()], reverse=True)
    return dirs[0] if dirs else None


# ============================================================
# User-facing config (local.yaml read/write) — 6 fields
# ============================================================

USER_CONFIG_FIELDS = [
    "source", "code_path", "env_build_script", "selena_build_script",
    "selena_branch", "runtime_path", "adapter_path", "data_path", "selena_exe", "backend",
]


def get_user_config(project: str) -> dict[str, Any]:
    """Return the 9 user-facing fields (flat shape) from the effective config.

    Reads the ``active_profile`` marker to pick between local-build / existing-
    selena. Falls back to the first non-default profile for legacy single-profile
    local.yaml files.
    """
    config = load_config(project)
    from core.profiles import list_profiles

    profiles = list_profiles(config)
    active_name = str(config.get("active_profile") or "")
    active = None
    if active_name:
        active = next((p for p in profiles if p.get("name") == active_name), None)
    if not active:
        # Legacy single-profile fallback: first non-default profile.
        active = next((p for p in profiles if p["name"] != "default"), profiles[0]) if profiles else {}
    selena = active.get("selena") or {}
    sim = config.get("simulation", {}) or {}
    build = config.get("build", {}) or {}
    repos = config.get("repos", {}) or {}
    assets = config.get("assets", {}) or {}

    datasets = sim.get("datasets", []) or []
    first_dataset = datasets[0] if datasets else {}
    data_path = str(first_dataset.get("input_mf4") or first_dataset.get("input_dir") or "")

    return {
        "source": str(selena.get("source") or "build"),
        "code_path": str(repos.get("inner_repo_root") or repos.get("outer_repo_root") or ""),
        "env_build_script": str(build.get("env_build_script") or ""),
        "selena_build_script": str(build.get("selena_build_script") or ""),
        "selena_branch": str(build.get("selena_branch") or selena.get("selena_branch") or ""),
        "runtime_path": str(sim.get("runtime_xml") or assets.get("runtime_xml") or ""),
        "adapter_path": str(sim.get("adapter_file") or assets.get("adapter_file") or ""),
        "data_path": data_path,
        "selena_exe": str(selena.get("exe") or ""),
        "backend": str(active.get("backend") or config.get("active_backend") or "local"),
        "active_profile": str(active.get("name") or "default"),
    }


def save_local_config(project: str, user_input: dict[str, Any]) -> Path:
    """Deep-merge user_input (flat shape) into projects/<project>/local.yaml.

    Creates local.yaml if absent. Writes a .bak backup first. The dual profiles
    (local-build / existing-selena) are merged by name so repeated saves update
    the matching profile instead of appending duplicates.
    """
    project_dir = get_projects_dir() / project
    local_path = local_yaml_path_for_project(project)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if local_path.exists():
        existing = _load_yaml_file(local_path) or {}
        bak = local_path.with_suffix(".yaml.bak")
        try:
            bak.write_text(local_path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass

    overlay = _flat_to_nested_user_config(user_input)
    merged = _deep_merge(existing, {k: v for k, v in overlay.items() if k != "profiles"})
    # Merge profiles by name (lists don't deep-merge).
    # Also merge any _extra_profiles injected by callers (e.g. wizard).
    all_new_profiles = list(overlay.get("profiles") or [])
    for ep in user_input.get("_extra_profiles") or []:
        if isinstance(ep, dict) and ep.get("name"):
            all_new_profiles.append(ep)
    if all_new_profiles:
        merged_profiles = list(existing.get("profiles") or [])
        for new_prof in all_new_profiles:
            idx = next((i for i, p in enumerate(merged_profiles) if isinstance(p, dict) and p.get("name") == new_prof.get("name")), None)
            if idx is not None:
                merged_profiles[idx] = _deep_merge(merged_profiles[idx], new_prof)
            else:
                merged_profiles.append(new_prof)
        merged["profiles"] = merged_profiles
    merged["active_profile"] = overlay.get("active_profile", "local-build")
    local_path.write_text(
        yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return local_path


def _flat_to_nested_user_config(user_input: dict[str, Any]) -> dict[str, Any]:
    """Map the flat frontend shape to a dual-profile local.yaml structure.

    Writes two profiles — ``local-build`` (source=build) and ``existing-selena``
    (source=path) — so one local.yaml holds both Selena-source options. The user's
    current ``source`` choice is recorded as ``active_profile`` so get_user_config
    can return the active one. Shared fields (repos/build/assets/datasets) live
    at the top level; source-specific fields (exe, selena_branch) live in profiles.
    """
    result: dict[str, Any] = {}
    source = str(user_input.get("source") or "build").lower()
    backend = str(user_input.get("backend") or "local")
    code_path = str(user_input.get("code_path") or "").strip()
    if code_path:
        result.setdefault("repos", {})
        result["repos"]["outer_repo_root"] = code_path
        result["repos"]["inner_repo_root"] = code_path

    build_overrides: dict[str, Any] = {}
    if user_input.get("env_build_script"):
        build_overrides["env_build_script"] = str(user_input["env_build_script"])
    if user_input.get("selena_build_script"):
        build_overrides["selena_build_script"] = str(user_input["selena_build_script"])
    if user_input.get("selena_branch"):
        build_overrides["selena_branch"] = str(user_input["selena_branch"])
    if build_overrides:
        result["build"] = build_overrides

    if user_input.get("runtime_path"):
        result.setdefault("assets", {})["runtime_xml"] = str(user_input["runtime_path"])
        result.setdefault("simulation", {})["runtime_xml"] = str(user_input["runtime_path"])

    if user_input.get("adapter_path"):
        result.setdefault("assets", {})["adapter_file"] = str(user_input["adapter_path"])
        result.setdefault("simulation", {})["adapter_file"] = str(user_input["adapter_path"])

    if user_input.get("data_path"):
        result.setdefault("simulation", {})
        existing_datasets = result["simulation"].get("datasets")
        if not isinstance(existing_datasets, list) or not existing_datasets:
            result["simulation"]["datasets"] = [{"name": "default", "input_dir": str(user_input["data_path"])}]
        else:
            result["simulation"]["datasets"] = list(existing_datasets)
            result["simulation"]["datasets"][0] = {**result["simulation"]["datasets"][0], "input_dir": str(user_input["data_path"])}

    # Dual profiles: both source options coexist; active_profile marks the current choice.
    local_build: dict[str, Any] = {
        "name": "local-build",
        "description": "本地编译 Selena",
        "backend": backend,
        "selena": {"source": "build"},
    }
    if user_input.get("selena_branch"):
        local_build["selena"]["selena_branch"] = str(user_input["selena_branch"])

    existing_selena: dict[str, Any] = {
        "name": "existing-selena",
        "description": "已有 Selena exe",
        "backend": backend,
        "selena": {"source": "path", "exe": str(user_input.get("selena_exe") or "")},
    }

    result["profiles"] = [local_build, existing_selena]
    result["active_profile"] = "local-build" if source == "build" else "existing-selena"
    return result


# ---------------------------------------------------------------------------
# Wizard: create project from browser form (PRD §1.5 / §1.7)
# ---------------------------------------------------------------------------

def validate_wizard_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate wizard form fields and preview auto-derived values.

    Supports two scenarios (PRD §1.5.1):
      - ``has_code`` (T1/T2): requires outer_repo_root; build script optional
      - ``no_code`` (T3): no repo needed; requires selena_exe instead

    Returns ``{ok: bool, errors: [...], warnings: [...], derived: {...}}``.
    Does NOT write anything to disk — pure validation + derivation preview.
    """
    errors: list[str] = []
    warnings: list[str] = []
    derived: dict[str, Any] = {}

    scenario = str(fields.get("scenario") or "has_code").strip()
    project_name = str(fields.get("project_name") or "").strip()
    if not project_name:
        errors.append("项目名称不能为空")
    elif not re.match(r"^[A-Za-z0-9_\-]+$", project_name):
        errors.append("项目名称只能包含字母、数字、下划线和连字符")

    if scenario == "no_code":
        # T3: no repo, must have selena exe path.
        selena_exe = str(fields.get("selena_exe") or "").strip()
        if not selena_exe:
            errors.append("T3 场景需要提供已有 Selena 可执行文件路径")
    else:
        # T1/T2: requires repo root.
        outer_repo = str(fields.get("outer_repo_root") or "").strip()
        if not outer_repo:
            errors.append("源码仓路径不能为空")

        script = str(fields.get("selena_build_script") or "").strip()
        if script:
            ctx = derive_project_context_from_selena_script(script)
            for key in ("build_config", "build_output", "binding", "project_root", "r2d2_script"):
                if ctx.get(key):
                    derived[key] = ctx[key]
            if not fields.get("build_config") and derived.get("build_config"):
                derived["build_config_auto"] = True
            if not fields.get("build_output") and derived.get("build_output"):
                derived["build_output_auto"] = True
        else:
            warnings.append("未填写 Selena 编译脚本，将跳过自动推导 build_config / build_output")

    # Check for duplicate project name.
    if project_name:
        existing = get_projects_dir() / project_name / "config.yaml"
        if existing.exists():
            errors.append(f"项目 '{project_name}' 已存在（{existing}）")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "derived": derived,
    }


def create_project_from_wizard(fields: dict[str, Any]) -> dict[str, Any]:
    """Create a complete project configuration from wizard form data.

    Dispatches to ``_create_t3_project()`` for ``scenario=no_code`` or
    ``_create_t1t2_project()`` for ``scenario=has_code`` (default).

    Returns ``{project_dir, config_yaml_path, local_yaml_path,
    effective_config, derived_fields}``.

    Raises ``ValueError`` on validation failure.
    """
    validation = validate_wizard_fields(fields)
    if not validation["ok"]:
        raise ValueError("; ".join(validation["errors"]))

    scenario = str(fields.get("scenario") or "has_code").strip()
    if scenario == "no_code":
        return _create_t3_project(fields, validation)
    return _create_t1t2_project(fields, validation)


def _create_t3_project(fields: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    """Create a T3 project: no compile, existing selena, cluster only.

    Creates a single 'existing-selena' profile (no local-build).
    """
    project_name = str(fields["project_name"]).strip()
    platform = str(fields.get("platform") or "gen5_selena").strip()
    selena_exe = str(fields.get("selena_exe") or "").strip()

    cfg: dict[str, Any] = {
        "project": {"name": project_name, "platform": platform},
    }

    # Assets (optional for T3).
    assets: dict[str, Any] = {}
    for fk, yk in (("runtime_xml", "runtime_xml"), ("adapter_file", "adapter_file"), ("matfilefilter", "matfilefilter")):
        val = str(fields.get(fk) or "").strip()
        if val:
            assets[yk] = val
    if assets:
        cfg["assets"] = assets

    # Datasets.
    raw_datasets = fields.get("datasets")
    if isinstance(raw_datasets, list) and raw_datasets:
        datasets = [{"name": str(ds["name"]), "input_dir": str(ds["input_dir"])}
                     for ds in raw_datasets if isinstance(ds, dict) and ds.get("name") and ds.get("input_dir")]
        if datasets:
            cfg.setdefault("simulation", {})["datasets"] = datasets

    # Cluster settings.
    cluster: dict[str, Any] = {}
    for fk, yk in (("cluster_workspace_root", "workspace_root"), ("cluster_software_path", "software_path"),
                    ("cluster_group", "group"), ("cluster_subgroup", "subgroup"), ("cluster_timeout_min", "timeout_min")):
        val = fields.get(fk)
        if val is not None and str(val).strip():
            cluster[yk] = val
    if cluster:
        cfg["cluster"] = cluster

    # Single profile: existing-selena + cluster.
    cfg["profiles"] = [{
        "name": "cloud-shared",
        "description": "Cluster: 使用已有 Selena + 共享数据",
        "backend": "cluster",
        "selena": {"source": "path", "exe": selena_exe},
        "data": {"copy": False},
        "cluster": {
            "group": str(fields.get("cluster_group") or "Radar"),
            "subgroup": str(fields.get("cluster_subgroup") or "PSS2"),
            "simulation_prio": 4,
            "timeout_min": int(fields.get("cluster_timeout_min") or 120),
        },
    }]

    # Write config.yaml.
    project_dir = get_projects_dir() / project_name
    project_dir.mkdir(parents=True, exist_ok=True)
    config_yaml_path = project_dir / "config.yaml"
    config_yaml_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")

    # Create local.yaml with source=path.
    user_input: dict[str, Any] = {"source": "path", "backend": "cluster", "selena_exe": selena_exe}
    local_path = save_local_config(project_name, user_input)

    try:
        effective = load_config(project_name)
    except Exception:
        effective = cfg

    return {
        "project_dir": str(project_dir),
        "config_yaml_path": str(config_yaml_path),
        "local_yaml_path": str(local_path),
        "effective_config": effective,
        "derived_fields": {},
    }


def _create_t1t2_project(fields: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    """Create a T1/T2 project: has code repo, can compile, dual profiles."""
    project_name = str(fields["project_name"]).strip()
    platform = str(fields.get("platform") or "gen5_selena").strip()
    outer_repo = str(fields["outer_repo_root"]).strip()
    inner_repo = str(fields.get("inner_repo_root") or outer_repo).strip()
    script = str(fields.get("selena_build_script") or "").strip()
    branch = str(fields.get("selena_branch") or "").strip()

    # Auto-derive from script.
    derived = validation["derived"]
    build_config = str(fields.get("build_config") or derived.get("build_config") or "").strip()
    build_output = str(fields.get("build_output") or derived.get("build_output") or "").strip()

    # Build the nested config.yaml structure.
    cfg: dict[str, Any] = {
        "project": {"name": project_name, "platform": platform},
        "repos": {"outer_repo_root": outer_repo, "inner_repo_root": inner_repo},
    }

    build_section: dict[str, Any] = {}
    if script:
        build_section["selena_build_script"] = script
    if branch:
        build_section["selena_branch"] = branch
    if build_config:
        build_section["build_config"] = build_config
    if build_output:
        build_section["build_output"] = build_output
    if build_section:
        cfg["build"] = build_section

    # Assets.
    assets: dict[str, Any] = {}
    for field_key, yaml_key in (
        ("runtime_xml", "runtime_xml"),
        ("adapter_file", "adapter_file"),
        ("matfilefilter", "matfilefilter"),
    ):
        val = str(fields.get(field_key) or "").strip()
        if val:
            assets[yaml_key] = val
    if assets:
        cfg["assets"] = assets

    # Datasets.
    raw_datasets = fields.get("datasets")
    if isinstance(raw_datasets, list) and raw_datasets:
        datasets = []
        for ds in raw_datasets:
            if isinstance(ds, dict) and ds.get("name") and ds.get("input_dir"):
                datasets.append({"name": str(ds["name"]), "input_dir": str(ds["input_dir"])})
        if datasets:
            cfg.setdefault("simulation", {})["datasets"] = datasets

    # Cluster settings.
    cluster: dict[str, Any] = {}
    for field_key, yaml_key in (
        ("cluster_workspace_root", "workspace_root"),
        ("cluster_software_path", "software_path"),
        ("cluster_group", "group"),
        ("cluster_subgroup", "subgroup"),
        ("cluster_timeout_min", "timeout_min"),
    ):
        val = fields.get(field_key)
        if val is not None and str(val).strip():
            cluster[yaml_key] = val
    if cluster:
        cfg["cluster"] = cluster

    # Generate default profiles.
    cfg["profiles"] = [
        {
            "name": "local-build",
            "description": "本地编译 Selena + 本地/共享数据原地引用",
            "backend": "local",
            "selena": {"source": "build"},
            "data": {"copy": False},
        },
        {
            "name": "cloud-build",
            "description": "Cluster: 打包本地编译 Selena + 引用共享数据",
            "backend": "cluster",
            "selena": {"source": "build"},
            "data": {"copy": False},
            "cluster": {
                "group": str(fields.get("cluster_group") or "Radar"),
                "subgroup": str(fields.get("cluster_subgroup") or "PSS2"),
                "simulation_prio": 4,
                "timeout_min": int(fields.get("cluster_timeout_min") or 120),
            },
        },
    ]

    # Write config.yaml.
    project_dir = get_projects_dir() / project_name
    project_dir.mkdir(parents=True, exist_ok=True)
    config_yaml_path = project_dir / "config.yaml"
    config_yaml_path.write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # Create initial local.yaml via save_local_config.
    # We inject the cloud-build profile explicitly so it survives the merge
    # with the auto-generated local-build / existing-selena profiles.
    user_input: dict[str, Any] = {
        "source": "build",
        "backend": "local",
        "code_path": outer_repo,
    }
    if script:
        user_input["selena_build_script"] = script
    if branch:
        user_input["selena_branch"] = branch
    # Pass extra profiles so save_local_config merges them by name.
    cloud_profile = cfg["profiles"][1]  # cloud-build
    user_input["_extra_profiles"] = [cloud_profile]
    local_path = save_local_config(project_name, user_input)

    # Load back through the standard pipeline to verify round-trip.
    try:
        effective = load_config(project_name)
    except Exception:
        effective = cfg  # Fallback: return what we wrote.

    return {
        "project_dir": str(project_dir),
        "config_yaml_path": str(config_yaml_path),
        "local_yaml_path": str(local_path),
        "effective_config": effective,
        "derived_fields": derived,
    }


# ---------------------------------------------------------------------------
# Full config export / import (one-file portable project config)
# ---------------------------------------------------------------------------

_EMPTY_PROJECT_TEMPLATE = """\
# radar-sim 项目配置 — 一个文件搞定一切
# 填写后点「保存配置」即可使用

project:
  name: "my_project"
  platform: "gen5_selena"

# 源码仓（T3 无代码用户可删除此段）
repos:
  outer_repo_root: ""       # 例: D:/bydod25fr/byd
  inner_repo_root: ""       # 默认同上

# 编译配置（T3 用户可删除此段）
build:
  selena_build_script: ""   # 例: D:/.../jenkins_selena_build.bat
  selena_branch: ""         # 例: develop_evo

# 仿真资产
assets:
  runtime_xml: ""           # Runtime XML 路径
  adapter_file: ""          # Adapter 文件路径（可选）
  matfilefilter: ""         # Matfilefilter 路径（可选）

# 数据集
simulation:
  datasets:
    - name: "default"
      input_dir: ""         # MF4 文件或目录路径

# Cluster 设置
cluster:
  workspace_root: "\\\\\\\\abtvdfs2.de.bosch.com\\\\ismdfs\\\\loc\\\\szh\\\\Isilon3\\\\Cluster"
  software_path: "\\\\\\\\szhradar01\\\\cluster_software"
  group: "Radar"
  subgroup: "PSS2"
  timeout_min: 120

# Profile: 选择编译方式和仿真后端
profiles:
  - name: "local-build"
    description: "本地编译 + 本地仿真"
    backend: "local"
    selena: { source: "build" }
  - name: "cloud-build"
    description: "本地编译 + Cluster 仿真"
    backend: "cluster"
    selena: { source: "build" }
    cluster: { group: "Radar", subgroup: "PSS2" }
  - name: "cloud-shared"
    description: "已有 Selena + Cluster 仿真 (T3)"
    backend: "cluster"
    selena: { source: "path", exe: "" }   # T3 用户填写 selena.exe 路径
    cluster: { group: "Radar", subgroup: "PSS2" }
"""


def get_empty_project_template() -> str:
    """Return a blank project YAML template for new projects."""
    return _EMPTY_PROJECT_TEMPLATE


def export_full_config(project: str) -> str:
    """Export the complete merged config for a project as a YAML string.

    Includes all layers (defaults + platform + recipe + config.yaml + local.yaml)
    plus signals and rules. The result is a self-contained, portable YAML that
    can be imported on any machine to recreate the project.
    """
    cfg = load_config(project)

    # Strip internal/meta keys that shouldn't be in a portable export.
    strip_keys = {"_meta", "_profile_selena_source", "_profile_selena_branch",
                  "_active_profile_name"}
    export_cfg = {k: v for k, v in cfg.items() if k not in strip_keys}

    # Attach signals and rules if they exist.
    try:
        sigs = load_signals(project)
        if sigs:
            export_cfg["signals"] = sigs
    except Exception:
        pass

    rules_path = get_projects_dir() / project / "rules.yaml"
    if rules_path.exists():
        rules = _load_yaml_file(rules_path)
        if rules:
            export_cfg["rules"] = rules

    return yaml.safe_dump(export_cfg, sort_keys=False, allow_unicode=True, default_flow_style=False)


def import_full_config(yaml_content: str) -> dict[str, Any]:
    """Import a complete project config from a YAML string.

    Creates or overwrites the project directory with config.yaml (and
    optionally signals.yaml / rules.yaml). Auto-generates local.yaml.

    Returns ``{ok, project, config_yaml_path, local_yaml_path}``.
    Raises ``ValueError`` on invalid input.
    """
    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML 解析失败: {exc}")

    if not isinstance(data, dict):
        raise ValueError("配置文件必须是 YAML 字典")

    project_section = data.get("project") or {}
    project_name = str(project_section.get("name") or "").strip()
    if not project_name:
        raise ValueError("缺少 project.name 字段")
    if not re.match(r"^[A-Za-z0-9_\-]+$", project_name):
        raise ValueError("project.name 只能包含字母、数字、下划线和连字符")

    # Create project directory.
    project_dir = get_projects_dir() / project_name
    project_dir.mkdir(parents=True, exist_ok=True)

    # Separate signals and rules from the main config.
    signals_data = data.pop("signals", None)
    rules_data = data.pop("rules", None)

    # Write config.yaml (everything except signals/rules).
    config_yaml_path = project_dir / "config.yaml"
    config_yaml_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )

    # Write signals.yaml if present.
    if signals_data:
        sig_path = project_dir / "signals.yaml"
        sig_path.write_text(
            yaml.safe_dump(signals_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    # Write rules.yaml if present.
    if rules_data:
        rules_path = project_dir / "rules.yaml"
        rules_path.write_text(
            yaml.safe_dump(rules_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    # Generate local.yaml from profiles.
    profiles = data.get("profiles") or []
    active_profile = "local-build"
    source = "build"
    selena_exe = ""
    code_path = ""
    build_script = ""
    branch = ""
    backend = "local"

    for p in profiles:
        if not isinstance(p, dict):
            continue
        pname = p.get("name", "")
        if pname == "local-build":
            active_profile = "local-build"
        selena = p.get("selena") or {}
        if selena.get("source") == "path":
            selena_exe = selena.get("exe", "")
        s = str(selena.get("source") or "build")
        b = str(p.get("backend") or "local")
        if pname == active_profile:
            source = s
            backend = b

    repos = data.get("repos") or {}
    code_path = str(repos.get("outer_repo_root") or "")
    build_section = data.get("build") or {}
    build_script = str(build_section.get("selena_build_script") or "")
    branch = str(build_section.get("selena_branch") or "")

    user_input: dict[str, Any] = {
        "source": source,
        "backend": backend,
        "code_path": code_path,
        "selena_build_script": build_script,
        "selena_branch": branch,
        "selena_exe": selena_exe,
    }
    # Inject non-local-build profiles so they survive the merge.
    extra_profiles = [p for p in profiles if isinstance(p, dict) and p.get("name") != "local-build"]
    if extra_profiles:
        user_input["_extra_profiles"] = extra_profiles

    local_path = save_local_config(project_name, user_input)

    return {
        "ok": True,
        "project": project_name,
        "config_yaml_path": str(config_yaml_path),
        "local_yaml_path": str(local_path),
    }
