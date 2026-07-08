"""rsim check — check environment."""

import os
from pathlib import Path

import platforms

from core.recipes import get_for_config
from core.simulation import get_simulation_config


def register(subparsers):
    p = subparsers.add_parser("check", help="Check environment")
    p.add_argument("--deps", action="store_true", help="Print dependency/install hints derived from config and build scripts")
    p.add_argument("--backend", choices=["local", "cluster"], default="", help="Check only the given backend (local/cluster)")
    p.add_argument("--profile", default="", help="Apply a simulation profile before checking")


def run(args, config):
    backend = _str_attr(args, "backend")
    profile = _str_attr(args, "profile")
    if backend or profile:
        return _run_backend_check(args, config, backend, profile)

    # Auto-pick backend scope when neither --backend nor --profile is given.
    # On a cluster-only access point (Mode A: no local profile, no local
    # toolchain paths), run cluster checks instead of the full local checks —
    # otherwise BOOST_ROOT / build scripts / VS produce false errors.
    if _is_cluster_only_config(config):
        return _run_backend_check(args, config, "cluster", "")

    issues = []
    handler = get_for_config(config)

    # 1. Platform environment checks
    platform_name = config.get("project", {}).get("platform", "gen5_selena")
    try:
        platform = platforms.get(platform_name, config)
        issues.extend(platform.check_environment())
    except Exception as exc:
        issues.append(f"Platform: Failed to check '{platform_name}': {exc}")

    # 2. Configuration validation
    issues.extend(_check_config(config))

    # 2b. Recipe-specific validation
    issues.extend(handler.validate(config))

    # 3. Build script availability
    issues.extend(_check_build_scripts(config))

    # 4. Environment variables
    issues.extend(_check_environment_vars(config))

    # 5. Repo context
    issues.extend(_check_repo_context(config))

    # 6. Simulation config
    issues.extend(_check_simulation_config(config))

    # 7. Build consistency
    issues.extend(_check_build_consistency(config))

    if getattr(args, "deps", False):
        _print_dependency_hints(config)

    if not issues:
        print("All environment checks passed.")
    else:
        print(f"Found {len(issues)} environment issue(s):")
        for issue in issues:
            print(f"  [!] {issue}")
        return 1
    return 0


def _run_backend_check(args, config, backend, profile):
    """Focused check for a single backend via core.environment."""
    from core.environment import check_for_backend

    target = backend or ("cluster" if profile and _profile_backend(config, profile) == "cluster" else "local")
    report = check_for_backend(config, target, profile=profile)
    print(f"Environment check (backend={report.backend}, profile={report.profile}):")
    for item in report.items:
        if item.ok and item.severity == "info":
            mark = "OK"
        elif item.severity == "warning":
            mark = "W "
        else:
            mark = "!!"
        print(f"  [{mark}] {item.name}: {item.detail}")
    if report.ok:
        n_warn = len(report.warnings)
        print(f"Backend check passed." + (f" ({n_warn} warning(s))" if n_warn else ""))
        return 0
    print(f"Backend check found {len(report.errors)} error(s), {len(report.warnings)} warning(s).")
    return 1


def _profile_backend(config, profile):
    from core.profiles import get_profile

    try:
        return get_profile(config, profile).get("backend", "local")
    except ValueError:
        return "local"


def _str_attr(args, name):
    """Return args.<name> as a string, treating None/Mock/empty as unset."""
    value = getattr(args, name, "")
    if not isinstance(value, str):
        return ""
    return value.strip()


def _is_cluster_only_config(config: dict) -> bool:
    """True when this config is a cluster-only access point (Mode A).

    No local-backend profile AND no local toolchain path configured → the user
    is not expected to have MATLAB/Qt/Boost/VS, so the full local check would
    produce false errors. Mirrors cli.doctor._infer_backend.
    """
    profiles = config.get("profiles", []) or []
    if any(p.get("backend") == "local" for p in profiles):
        return False
    env = config.get("environment", {}) or {}
    toolchain_keys = (
        "matlab_root", "qt_path", "boost_root", "BOOST_ROOT",
        "selena_env_path", "vs_version", "python3_path",
    )
    if any(str(env.get(k) or "").strip() for k in toolchain_keys):
        return False
    return True


def _check_config(config):
    """Validate configuration structure."""
    issues = []
    
    # Check required top-level keys
    required_keys = ["project", "paths"]
    for key in required_keys:
        if key not in config:
            issues.append(f"Config: Missing required section '{key}'")
    
    # Early return if missing required sections
    if len(issues) > 0:
        return issues
    
    # Check project name and platform
    project = config.get("project", {})
    if "name" not in project:
        issues.append("Config: Missing project name")
        
    if "platform" not in project:
        issues.append("Config: Missing platform in project section")
    
    # Check paths
    paths = config.get("paths", {})
    required_paths = ["project_root"]
    for path_key in required_paths:
        if path_key not in paths:
            issues.append(f"Config: Missing required path '{path_key}'")
            
    return issues


def _check_build_scripts(config):
    """Check build script availability."""
    issues = []

    script_candidates = [
        ("R2D2.py", config.get("paths", {}).get("r2d2_script", "")),
        ("jenkins_selena_build.bat", config.get("build", {}).get("selena_build_script", "") or config.get("selena_build_script", "")),
        ("testbuild_BaseC0S_SINGLE.bat", config.get("build", {}).get("hex_build_script", "") or config.get("hex_build_script", "")),
    ]

    project_root = config.get("paths", {}).get("project_root", "")
    if project_root and not Path(project_root).exists():
        return issues

    for label, script_path in script_candidates:
        candidate = script_path
        if not candidate and project_root:
            if label == "testbuild_BaseC0S_SINGLE.bat" and not config.get("binding"):
                continue
            fallback_map = {
                "R2D2.py": Path(project_root) / "R2D2.py",
                "jenkins_selena_build.bat": Path(project_root) / "jenkins_selena_build.bat",
                "testbuild_BaseC0S_SINGLE.bat": Path(project_root) / "testbuild_BaseC0S_SINGLE.bat",
            }
            candidate = str(fallback_map[label])
        if not candidate:
            continue
        if not Path(candidate).exists():
            if project_root:
                alt_paths = [
                    Path(project_root) / "scripts" / Path(candidate).name,
                    Path(project_root) / "build" / Path(candidate).name,
                    Path(project_root) / "tools" / Path(candidate).name,
                ]
                if any(path.exists() for path in alt_paths):
                    continue
            issues.append(f"Build: Script not found: {label} -> {script_path}")

    return issues


def _check_environment_vars(config):
    """Check required environment variables."""
    issues = []

    env_vars = config.get("environment", {})
    boost_root = (
        env_vars.get("boost_root")
        or env_vars.get("BOOST_ROOT")
        or config.get("boost_root", "")
        or os.environ.get("BOOST_ROOT", "")
    )
    if not boost_root:
        if "BOOST_ROOT" in env_vars and env_vars.get("BOOST_ROOT", "") == "":
            issues.append("Env: Environment variable 'BOOST_ROOT' is empty")
        else:
            issues.append("Env: Required environment variable 'BOOST_ROOT' not configured")

    python3_path = env_vars.get("python3_path") or config.get("python3_path", "")
    if python3_path and not _is_deferred_env_path(python3_path) and not Path(python3_path).exists():
        issues.append(f"Env: python3 path not found: {python3_path}")

    return issues


def _is_deferred_env_path(value: str) -> bool:
    """Return True for paths materialized by build-script init commands."""
    text = str(value or "")
    return "%" in text or "$(" in text


def _print_dependency_hints(config):
    """Print a compact dependency matrix for onboarding / delivery checks."""
    env = config.get("environment", {})
    build = config.get("build", {})
    sim = get_simulation_config(config)
    rows = [
        ("Python", env.get("python3_path") or "python 3.9+ on PATH", "Run rsim / R2D2 helpers"),
        ("Visual Studio", f"VS{env.get('vs_version', '2019')}", "Build and debug Selena"),
        ("MATLAB", env.get("matlab_root", ""), "Selena MATLAB transport/runtime"),
        ("Qt", env.get("qt_path", ""), "Selena runtime DLLs"),
        ("Boost", env.get("boost_root", "") or config.get("boost_root", ""), "Build/runtime dependency"),
        ("Selena env", env.get("selena_env_path", ""), "MSYS/mingw runtime for Selena"),
        ("Selena build script", build.get("selena_build_script", ""), "Project compile entrypoint"),
        ("Runtime XML", sim.get("runtime_xml", ""), "Simulation runnable/connection config"),
        ("MAT filter", sim.get("matfilefilter", ""), "MAT output filtering"),
        ("Adapter", sim.get("adapter_file", ""), "Project-specific Selena adapter"),
    ]

    print("Dependency hints:")
    for name, location, purpose in rows:
        if location:
            status = "deferred" if _is_deferred_env_path(str(location)) else ("ok" if Path(str(location)).exists() else "missing")
        else:
            status = "unset"
        print(f"  - {name}: {location or '(unset)'} [{status}] - {purpose}")

    script_hints = build.get("script_dependency_hints", []) or []
    if script_hints:
        print("Build-script install/init hints:")
        for hint in script_hints:
            print(f"  - {hint}")


def _check_build_consistency(config):
    """Check build consistency."""
    issues = []
    
    paths = config.get("paths", {})
    build_output = paths.get("build_output", "")
    
    if not build_output:
        return issues
        
    build_output_path = Path(build_output)
    if not build_output_path.exists():
        # This is not necessarily an error - build dir might be created during build
        return issues
    
    # Check for git submodules
    project_root = paths.get("project_root", "")
    if project_root:
        try:
            import subprocess
            result = subprocess.run(
                ["git", "-C", project_root, "submodule", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if line.startswith("-"):
                        name = line.split()[1] if len(line.split()) > 1 else "?"
                        issues.append(f"Git: Submodule '{name}' not initialized")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
            
    return issues


def _check_repo_context(config):
    """Check outer/inner repo context and branch readiness. Delegates to core.repo."""
    from core.repo import check_repo_context

    items = check_repo_context(config, allow_switch=False)
    # Translate to issue strings for the legacy check pipeline; only surface
    # warnings/errors (info items are just confirmation).
    issues = []
    for item in items:
        if item.severity == "info" and item.ok:
            continue
        prefix = "Repo" if item.category == "repo" else (item.category.capitalize() or "Repo")
        issues.append(f"{prefix}: {item.name}: {item.detail}")
    return issues


def _check_simulation_config(config):
    """Check runtime/data settings required for simulation."""
    issues = []
    sim = get_simulation_config(config)

    runtime_xml = sim.get("runtime_xml", "")
    if runtime_xml and not Path(runtime_xml).exists():
        issues.append(f"Sim: runtime_xml not found: {runtime_xml}")

    matfilefilter = sim.get("matfilefilter", "")
    if matfilefilter and not Path(matfilefilter).exists():
        issues.append(f"Sim: matfilefilter not found: {matfilefilter}")

    adapter_file = sim.get("adapter_file", "")
    if adapter_file and not Path(adapter_file).exists():
        issues.append(f"Sim: adapter_file not found: {adapter_file}")

    datasets = sim.get("datasets", []) or []
    for dataset in datasets:
        name = dataset.get("name", "<unnamed>")
        input_dir = dataset.get("input_dir", "")
        input_mf4 = dataset.get("input_mf4", "")
        if input_dir and not Path(input_dir).exists():
            issues.append(f"Sim: dataset '{name}' input_dir not found: {input_dir}")
        if input_mf4 and not Path(input_mf4).exists():
            issues.append(f"Sim: dataset '{name}' input_mf4 not found: {input_mf4}")
        dataset_runtime = dataset.get("runtime_xml", "")
        if dataset_runtime and not Path(dataset_runtime).exists():
            issues.append(f"Sim: dataset '{name}' runtime_xml not found: {dataset_runtime}")

    return issues
