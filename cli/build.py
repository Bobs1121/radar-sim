"""
rsim build — compile HEX and/or Selena.

HEX build can be interrupted (Ctrl+C) after initial copy phase.
Selena build produces selena.exe + VS solution.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from core.config import resolve_selena_executable
from core.recipes import get_for_config


def register(subparsers):
    p = subparsers.add_parser("build", help="Compile HEX and/or Selena")
    p.add_argument("build_type", choices=["hex", "selena", "all"],
                    help="Build type (default: selena)", nargs="?", default="selena")
    p.add_argument("--clean", action="store_true", help="Clean before build")
    p.add_argument("--mode", default="RelWithDebInfo", help="Build mode")
    p.add_argument("--no-progress", action="store_true", help="Disable progress display")


def run(args, config):
    project = args.project
    build_type = args.build_type
    handler = get_for_config(config)

    repo_issue = handler.prepare_repo_context(config, _prepare_repo_context)
    if repo_issue:
        print(f"[ERROR] {repo_issue}")
        return 1

    if build_type in ("hex", "all"):
        result = _build_hex(config, args.clean, no_progress=args.no_progress)
        if not result.success and build_type == "hex":
            return 1
        if result.interrupted:
            print("[INFO] HEX build interrupted — proceeding to Selena build.")

    if build_type in ("selena", "all"):
        result = _build_selena(config, args.clean, args.mode)
        if not result.success:
            return 1

    return 0


def _build_hex(config: dict, clean: bool, no_progress: bool = False) -> "BuildResult":
    """Build HEX firmware — with interrupt support."""
    from core.models import BuildResult

    script = config.get("hex_build_script", "")
    if not script or not os.path.exists(script):
        return BuildResult(success=False, build_type="hex",
                           errors=[f"HEX build script not found: {script}"])

    start = time.time()
    state_file = Path(".build_state")
    interrupted = False

    try:
        env = os.environ.copy()
        py3 = config.get("environment", {}).get("python3_path", "")
        if py3:
            env["PYTHON3"] = py3
        boost = config.get("boost_root", "") or config.get("environment", {}).get("boost_root", "")
        if boost:
            env["BOOST_ROOT"] = boost

        clean_arg = "-clean" if clean else "-no-clean"
        cmd = ["cmd", "/c", script, clean_arg]

        print(f"  Build script: {script}")
        print(f"  Press Ctrl+C to interrupt after copy phase...")
        print()

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
        )

        # Wait for copy phase to complete (first 60s)
        copy_done = False
        copy_start = time.time()
        progress_lines = []

        while True:
            try:
                output = proc.stdout.readline()
                if not output and proc.poll() is not None:
                    break
                if output:
                    progress_lines.append(output.rstrip())
                    if not no_progress:
                        _print_progress_line(output.rstrip(), spinner=not copy_done)
                    # Check if copy phase is done (60s elapsed or keywords)
                    if not copy_done and (time.time() - copy_start > 60 or
                                          any(kw in output.lower() for kw in ["copy", "done", "complete", "finished"])):
                        copy_done = True
                        state_file.write_text(
                            json.dumps({"status": "copy_done", "time": datetime.now().isoformat()}),
                            encoding="utf-8",
                        )
                        print()
                        print("[COPY] File copy phase complete — Ctrl+C is now safe.")
                        print()
            except KeyboardInterrupt:
                print()
                if copy_done:
                    print("[INFO] Interrupted after copy phase — HEX files may be partially built.")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    interrupted = True
                    return BuildResult(
                        success=False, build_type="hex", interrupted=True,
                        duration_sec=time.time() - start,
                    )
                else:
                    print("[INFO] Cancelled during copy phase — restarting needed.")
                    proc.kill()
                    return BuildResult(
                        success=False, build_type="hex",
                        duration_sec=time.time() - start,
                        errors=["Interrupted during copy phase"],
                    )

        retcode = proc.returncode
        duration = time.time() - start

        if retcode != 0:
            errors = _extract_errors(progress_lines)
            return BuildResult(
                success=False, build_type="hex", duration_sec=duration, errors=errors,
                warnings=_extract_warnings(progress_lines),
            )

        # Clean up state file
        if state_file.exists():
            state_file.unlink()

        return BuildResult(
            success=True, build_type="hex", duration_sec=duration,
            warnings=_extract_warnings(progress_lines),
        )

    except FileNotFoundError:
        return BuildResult(
            success=False, build_type="hex",
            duration_sec=time.time() - start,
            errors=[f"Build script not found: {script}"],
        )
    except subprocess.TimeoutExpired:
        return BuildResult(
            success=False, build_type="hex",
            duration_sec=time.time() - start,
            errors=["HEX build timed out after 1800s"],
        )


def _build_selena(config: dict, clean: bool, mode: str) -> "BuildResult":
    """Build Selena simulation environment."""
    from core.models import BuildResult

    script = config.get("build", {}).get("selena_build_script", "") or config.get("selena_build_script", "")
    r2d2 = config.get("paths", {}).get("r2d2_script", "")
    build_config = config.get("paths", {}).get("build_config", "")
    build_config_full = config.get("build", {}).get("build_config", build_config)
    python3 = config.get("environment", {}).get("python3_path", "python3")
    project_root = config.get("project_root", "")
    binding = config.get("binding", "")

    start = time.time()
    env = _build_env(config)
    cmd: list[str]
    cwd = None
    build_label = "R2D2"
    config_path = ""

    if script and os.path.exists(script):
        cmd, cwd = _build_selena_script_command(config, mode)
        build_label = "Selena build script"
    else:
        if not r2d2 or not os.path.exists(r2d2):
            return BuildResult(success=False, build_type="selena",
                               errors=[f"R2D2.py not found: {r2d2}"])

        if not build_config:
            return BuildResult(success=False, build_type="selena",
                               errors=["No build config specified (paths.build_config)"])

        config_path = _resolve_config_path(build_config_full, project_root)
        if not config_path or not os.path.exists(config_path):
            return BuildResult(success=False, build_type="selena",
                               errors=[f"Build config not found: {config_path}"])

        cmd = [
            python3, r2d2,
            "-m", config_path,
        ]

        if clean:
            cmd.extend(["-clean"])

        cmd.extend(["-ghs_math", "-use_mat", "-notests", "-bm", mode])

        vs_postfix = config.get("vs_postfix", "") or _detect_vs_postfix()
        if vs_postfix:
            cmd.extend(vs_postfix.split())

    try:
        from core.build_lock import WorkspaceBuildLock, build_workspace_from_config
        build_lock = WorkspaceBuildLock(build_workspace_from_config(config)).acquire()
    except Exception as exc:
        return BuildResult(
            success=False,
            build_type="selena",
            duration_sec=time.time() - start,
            errors=[str(exc)],
        )
    try:
        print(f"  Build entry: {build_label}")
        if script and os.path.exists(script):
            print(f"  Script: {script}")
        else:
            print(f"  R2D2: {r2d2}")
            print(f"  Config: {config_path}")
            print(f"  Python3: {python3}")
        print(f"  Mode: {mode}")
        if not script and (config.get("vs_postfix", "") or _detect_vs_postfix()):
            vs_postfix = config.get("vs_postfix", "") or _detect_vs_postfix()
            print(f"  VS postfix: {vs_postfix}")
        print()

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
            cwd=cwd,
        )

        output_lines = []
        for line in iter(proc.stdout.readline, ""):
            output_lines.append(line.rstrip())
            if line:
                _print_progress_line(line.rstrip())

        proc.stdout.close()
        retcode = proc.wait()
        duration = time.time() - start

        if retcode != 0:
            errors = _extract_errors(output_lines)
            return BuildResult(
                success=False, build_type="selena", duration_sec=duration,
                errors=errors, warnings=_extract_warnings(output_lines),
            )

        exe_path = resolve_selena_executable(config, build_mode=mode)

        return BuildResult(
            success=True, build_type="selena", duration_sec=duration,
            executable_path=exe_path,
            warnings=_extract_warnings(output_lines),
        )

    except FileNotFoundError:
        return BuildResult(
            success=False, build_type="selena",
            duration_sec=time.time() - start,
            errors=[f"Command not found: {script or python3}"],
        )
    finally:
        build_lock.release()


def _build_selena_script_command(config: dict, mode: str) -> tuple[list[str], str]:
    """Build the Selena script command from config-driven arguments."""
    handler = get_for_config(config)
    build = config.get("build", {})
    script = build.get("selena_build_script", "") or config.get("selena_build_script", "")
    args = handler.shape_selena_script_args(config, mode)
    cwd = build.get("script_workdir") or str(Path(script).parent)
    return ["cmd", "/c", script, *args], cwd


def _prepare_repo_context(config: dict) -> str:
    """Ensure repo context is ready before build. Delegates to core.repo."""
    from core.repo import prepare_repo_context

    message = prepare_repo_context(config)
    if not message:
        target_branch = (
            config.get("_profile_selena_branch")
            or config.get("build", {}).get("selena_branch", "")
            or config.get("repos", {}).get("inner_repo_branch", "")
        )
        if target_branch:
            print(f"[INFO] Inner repo verified on Selena branch: {target_branch}")
    return message


def _print_progress_line(line: str, spinner: bool = False):
    """Print build output line with optional spinner."""
    if spinner:
        sys.stdout.write(f"\r  [*] {line[:100]}")
        sys.stdout.flush()
    else:
        print(f"  {line[:120]}")


def _extract_errors(lines: list[str]) -> list[str]:
    from core.build_diagnostics import extract_actionable_build_errors

    errors = extract_actionable_build_errors(lines)
    return errors or ["Selena build failed; inspect the build log for details"]


def _extract_warnings(lines: list[str]) -> list[str]:
    warnings = []
    for line in lines:
        if "warning" in line.lower():
            warnings.append(line.strip())
    return warnings[:20]


def _build_env(config: dict) -> dict:
    """Assemble environment variables for build subprocess."""
    env = os.environ.copy()
    environment = config.get("environment", {})

    boost = config.get("boost_root", "") or environment.get("boost_root", "")
    if boost:
        env["BOOST_ROOT"] = boost

    py3 = environment.get("python3_path", "")
    selena_env = environment.get("selena_env_path", "")
    qt_path = environment.get("qt_path", "")
    matlab_root = environment.get("matlab_root", "")

    path_parts: list[str] = []
    if py3:
        path_parts.append(os.path.dirname(py3))
    if selena_env:
        path_parts.append(os.path.join(selena_env, "MSYS", "mingw64", "bin"))
    if qt_path:
        path_parts.append(os.path.join(qt_path, "bin"))
        path_parts.append(os.path.join(qt_path, "lib"))
    if matlab_root:
        path_parts.append(os.path.join(matlab_root, "bin", "win64"))
    if boost:
        path_parts.append(os.path.join(boost, "lib64-msvc-14.0"))

    existing_path = env.get("PATH", "")
    path_parts.append(existing_path)
    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped = []
    for p in path_parts:
        norm = os.path.normpath(p)
        if norm not in seen:
            seen.add(norm)
            deduped.append(p)
    env["PATH"] = os.pathsep.join(deduped)
    return env


def _resolve_config_path(build_config: str, project_root: str) -> str:
    """Resolve build_config to a full .config file path.

    If build_config is already a full path and exists, return it.
    Otherwise, look in the known Selena config directories used by supported repos.
    """
    if os.path.isabs(build_config) and os.path.exists(build_config):
        return build_config

    config_name = build_config
    if config_name.endswith(".config"):
        config_name = config_name[:-7]

    candidates = [
        os.path.join(project_root, "apl", "byd", "selena", "cmake_build_cfg", f"{config_name}.config"),
        os.path.join(project_root, "apl", "byd", "selena", "config", "cmake", f"{config_name}.config"),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    if os.path.isabs(build_config):
        return build_config

    return candidates[0]


def _detect_vs_postfix() -> str:
    """Auto-detect VS postfix from installed Visual Studio version."""
    if os.path.exists(r"C:\Program Files (x86)\Microsoft Visual Studio\2019"):
        return "-vs vs16"
    if os.path.exists(r"C:\Program Files (x86)\Microsoft Visual Studio\2022"):
        return "-vs vs17"
    if os.path.exists(r"C:\Program Files (x86)\Microsoft Visual Studio\2017"):
        return "-vs vs15"
    return ""
