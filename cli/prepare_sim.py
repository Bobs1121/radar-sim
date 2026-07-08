"""
rsim prepare-sim — Prepare simulation environment.

This command validates configuration, sets up simulation assets,
and ensures all dependencies are ready before running simulations.
"""
import os
import shutil
from pathlib import Path

from core.config import render_selena_config, render_selena_environment_path
from core.recipes import get_for_config
from core.simulation import apply_simulation_to_config, get_simulation_config


def register(subparsers):
    p = subparsers.add_parser("prepare-sim", help="Prepare simulation environment")
    p.add_argument("--dry-run", action="store_true", help="Show what would be done without actually doing it")
    p.add_argument("--force", action="store_true", help="Force re-preparation")


def run(args, config):
    """Execute prepare-sim command."""
    configured_project = config.get("project", {}).get("name")
    project = getattr(args, "project", None) or configured_project or "unknown"
    handler = get_for_config(config)
    sim = handler.prepare_simulation(config, get_simulation_config(config), stage="prepare")
    config_for_recipe = apply_simulation_to_config(config, sim)
    print(f"[INFO] Preparing simulation environment for project '{project}'")

    errors = _validate_configuration(config_for_recipe)
    if errors:
        print("[ERROR] Configuration validation failed:")
        for error in errors:
            print(f"  - {error}")
        return 1

    try:
        selena_cfg = render_selena_config(config_for_recipe)
    except (ValueError, FileNotFoundError) as exc:
        print(f"[ERROR] {exc}")
        return 1

    assets = selena_cfg.get("assets", {})
    fixed_config_path = assets.get("fixed_config_path", "")
    solution = config_for_recipe.get("build", {}).get("vs_solution", "")
    target_project = (
        config_for_recipe.get("project", {}).get("target_project")
        or config_for_recipe.get("build", {}).get("target_project")
        or config_for_recipe.get("vs_debug", {}).get("target_project", "")
    )
    if not target_project:
        target_project = "selena"
    debug_path = render_selena_environment_path(config_for_recipe)

    print()
    print("Visual Studio guidance:")
    print(f"  Solution: {solution}")
    print(f"  Target project: {target_project}")
    print(f"  Args: --paramconfig {fixed_config_path}")
    print(f"  Runtime XML: {sim.get('runtime_xml', '')}")
    print(f"  Source: {sim.get('source', '') or 'auto'}")
    print(f"  Mounting: {sim.get('mounting_position', '') or 'auto'}")
    print(f"  PATH: {debug_path}")
    return 0


def _validate_configuration(config: dict) -> list[str]:
    """Validate project configuration."""
    errors = []
    
    # Check project config exists
    project_config = config.get("project", {})
    if not project_config:
        errors.append("Project configuration missing")
        
    # Check platform is specified
    platform_name = project_config.get("platform", "")
    if not platform_name:
        errors.append("Platform not specified in project config")

    assets = config.get("assets", {})
    if not assets.get("root", ""):
        errors.append("Assets root not configured (assets.root)")
    if not assets.get("config_template", ""):
        errors.append("Selena config source not configured (assets.config_template)")
    if not assets.get("fixed_config_path", ""):
        errors.append("Fixed Selena config path not configured (assets.fixed_config_path)")

    if not config.get("build", {}).get("vs_solution", ""):
        errors.append("Visual Studio solution not configured (build.vs_solution)")

    return errors


def _setup_assets(config: dict, dry_run: bool, force: bool) -> list[str]:
    """Setup simulation assets."""
    errors = []
    
    # Get project info
    project = config.get("project", {})
    project_name = project.get("name", "unknown")
    
    # Get assets directory
    assets_dir = Path(config.get("assets_dir", ""))
    if not _path_exists(assets_dir):
        errors.append(f"Assets directory does not exist: {assets_dir}")
        return errors
        
    # Get project assets directory
    project_assets_dir = assets_dir / project_name
    if not _path_exists(project_assets_dir):
        errors.append(f"Project assets directory does not exist: {project_assets_dir}")
        return errors
    
    # Get target directories
    build_output = Path(config.get("paths", {}).get("build_output", ""))
    sim_input_dir = Path(config.get("paths", {}).get("sim_input_dir", ""))
    
    if not _path_exists(build_output):
        if not dry_run:
            _make_dirs(build_output)
        else:
            print(f"  Would create build output directory: {build_output}")
    
    if not _path_exists(sim_input_dir):
        if not dry_run:
            _make_dirs(sim_input_dir)
        else:
            print(f"  Would create simulation input directory: {sim_input_dir}")
    
    # Copy essential assets
    required_files = [
        "runtime.xml",
        "matfilter.mat.filter"
    ]
    
    for filename in required_files:
        src_path = project_assets_dir / filename
        if _path_exists(src_path):
            dst_path = build_output / filename
            if not dry_run:
                if force or not _path_exists(dst_path):
                    _copy_file(src_path, dst_path)
                    print(f"  Copied: {filename}")
                else:
                    print(f"  Skipped (already exists): {filename}")
            else:
                print(f"  Would copy: {filename}")
        else:
            # For now, don't treat missing assets as fatal errors
            print(f"  Warning: Required asset file not found: {filename}")
    
    return errors


def _check_dependencies(config: dict, dry_run: bool) -> list[str]:
    """Check simulation dependencies."""
    errors = []
    
    # Check environment variables
    environment = config.get("environment", {})
    required_env_vars = ["matlab_root", "qt_path"]
    for var in required_env_vars:
        if not environment.get(var, ""):
            errors.append(f"Required environment variable not set: {var}")
    
    # Check executables
    required_executables = ["python3", "matlab"]
    for exe in required_executables:
        if not _find_executable(exe):
            errors.append(f"Required executable not found: {exe}")
    
    # Check platform-specific requirements
    from platforms import get as get_platform
    
    project_config = config.get("project", {})
    platform_name = project_config.get("platform", "gen5_selena")
    try:
        platform = get_platform(platform_name, config)
        platform_errors = platform.check_environment()
        if platform_errors:
            errors.extend(platform_errors)
    except Exception as e:
        errors.append(f"Failed to initialize platform '{platform_name}': {e}")
        
    return errors


def _find_executable(name: str) -> bool:
    """Check if executable exists in PATH."""
    if os.name == 'nt':
        name = name + ".exe"
    
    for path in os.environ.get("PATH", "").split(os.pathsep):
        exe_path = os.path.join(path, name)
        if _is_file(exe_path) and _is_executable(exe_path):
            return True
    return False


def _path_exists(path: Path | str) -> bool:
    return os.path.exists(str(path))


def _make_dirs(path: Path | str) -> None:
    os.makedirs(path, exist_ok=True)


def _copy_file(src_path: Path | str, dst_path: Path | str) -> None:
    shutil.copy2(src_path, dst_path)


def _is_file(path: Path | str) -> bool:
    return os.path.isfile(path)


def _is_executable(path: Path | str) -> bool:
    return os.access(path, os.X_OK)
