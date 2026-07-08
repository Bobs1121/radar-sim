"""
rsim run — Launch Selena simulation, produce output MF4.

The command normalizes project simulation config, optionally auto-detects
radar orientation from the input MF4, renders a per-run Selena paramconfig,
and launches selena.exe with that generated config.
"""

import json
import os
import queue
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from core.config import render_selena_config, render_selena_environment_path, resolve_selena_executable
from core.recipes import get_for_config
from core.simulation import (
    apply_simulation_to_config,
    build_effective_simulation,
    gen_output_path,
    get_simulation_config,
    resolve_dataset_files,
)


def register(subparsers):
    p = subparsers.add_parser("run", help="Run Selena simulation")
    p.add_argument("input_mf4", nargs="?", default=None, help="Input MF4 path (file or directory)")
    p.add_argument("--output-mf4", help="Output MF4 file path (default: <input>out.MF4)")
    p.add_argument("--dataset", help="Run all MF4 files in dataset directory")
    p.add_argument("--profile", default="", help="Simulation profile to apply (e.g. local-build)")
    p.add_argument("--select", action="store_true", help="Scan a directory/dataset and pick MF4 files interactively")
    p.add_argument("--limit", type=int, default=0, help="With --select, cap how many MF4 files to list (0=all)")
    p.add_argument("--required-signal", action="append", default=[], help="Signal name that should exist in each input MF4 (scan-time check)")
    p.add_argument("--timeout", type=int, default=3600, help="Simulation timeout in seconds (default: 3600)")
    p.add_argument("--max-duration", type=int, help="Per-file hard runtime limit in seconds")
    p.add_argument("--stall-timeout", type=int, help="Abort one file after this many seconds without log/output activity")
    p.add_argument("--no-retry", action="store_true", help="Disable end-of-batch retry for failed files")
    p.add_argument("--no-wait", action="store_true", help="Launch selena.exe but don't wait for exit")
    p.add_argument("--dry-run", action="store_true", help="Show what would be done without launching")
    p.add_argument("--extra-args", nargs="*", default=[], help="Extra args for selena.exe")
    p.add_argument("--extra-arg", action="append", default=[], help="Repeatable extra arg for selena.exe")


def run(args, config):
    project = getattr(args, "project", None) or config.get("project", {}).get("name", "unknown")
    extra_args = list(getattr(args, "extra_args", []) or [])
    extra_args.extend(list(getattr(args, "extra_arg", []) or []))
    args.extra_args = extra_args
    profile_name = _str_attr(args, "profile")
    if profile_name:
        from core.profiles import apply_profile
        config = apply_profile(config, profile_name)
    handler = get_for_config(config)
    sim = handler.prepare_simulation(config, get_simulation_config(config), stage="base")
    sim = _apply_cli_runtime_overrides(sim, args)
    config_for_recipe = apply_simulation_to_config(config, sim)

    # Resolve input MF4(s) — supports single file, dataset, directory scan + select
    input_files = _resolve_input_files(args, config_for_recipe, sim)
    if input_files is None:
        return 1

    # Validate selena.exe exists (profile-aware)
    selena_exe = _find_selena_exe(config_for_recipe)
    if not selena_exe or not os.path.exists(selena_exe):
        print(f"[ERROR] selena.exe not found: {selena_exe}")
        print()
        print("  Run 'rsim build selena' first to compile the simulation,")
        print("  or use --profile <name> pointing at an existing selena.exe.")
        _print_env_hints(config_for_recipe)
        return 1

    # Build environment PATH
    env = _build_env(config_for_recipe)

    # Working directory (selena.exe dir for plugin discovery)
    cwd = os.path.dirname(selena_exe) or "."

    file_status = {path: {"status": "pending", "attempts": 0, "last_return_code": None} for path in input_files}
    failed_attempts = 0

    dataset_cfg = {}
    if args.dataset:
        dataset_cfg, _ = resolve_dataset_files(sim, args.dataset)
    work_items = [{"input_mf4": path, "attempt": 0} for path in input_files]
    retry_queue = []

    while work_items:
        item = work_items.pop(0)
        input_mf4 = item["input_mf4"]
        attempt = item["attempt"]
        requested_output = args.output_mf4 if len(input_files) == 1 else None
        effective_sim = build_effective_simulation(
            config_for_recipe,
            input_mf4,
            output_mf4=requested_output,
            dataset=dataset_cfg,
        )
        effective_sim = handler.prepare_simulation(config_for_recipe, effective_sim, stage="run")
        ret = _run_single(
            project=project,
            config=config_for_recipe,
            sim=effective_sim,
            selena_exe=selena_exe,
            input_mf4=input_mf4,
            output_mf4=effective_sim["output_mf4"],
            timeout=args.timeout,
            no_wait=args.no_wait,
            dry_run=args.dry_run,
            extra_args=args.extra_args,
            env=env,
            cwd=cwd,
            file_index=len(input_files) - len(work_items),
            file_total=len(input_files),
        )
        file_status[input_mf4]["attempts"] += 1
        file_status[input_mf4]["last_return_code"] = ret
        if ret == 0:
            file_status[input_mf4]["status"] = "success"
        else:
            failed_attempts += 1
            can_retry = (
                not args.dry_run
                and effective_sim.get("continue_on_failure", True)
                and effective_sim.get("retry_failed_at_end", True)
                and attempt < int(effective_sim.get("max_retries_per_file", 1))
            )
            if can_retry:
                retry_queue.append({"input_mf4": input_mf4, "attempt": attempt + 1})
                file_status[input_mf4]["status"] = "queued_retry"
                print(f"[INFO] Queued retry for failed input at end of batch: {input_mf4}")
            else:
                file_status[input_mf4]["status"] = "failed"
                if not effective_sim.get("continue_on_failure", True):
                    print("[ERROR] Stopping batch because continue_on_failure is disabled.")
                    work_items = []
                    retry_queue = []
        if len(input_files) > 1 and (work_items or retry_queue):
            print()
            print("=" * 60)
            print()

        if not work_items and retry_queue:
            print("[INFO] Starting retry pass for failed inputs...")
            work_items = retry_queue
            retry_queue = []

    succeeded = [path for path, item in file_status.items() if item["status"] == "success"]
    failed = [path for path, item in file_status.items() if item["status"] != "success"]
    _save_batch_summary(project, input_files, file_status, failed_attempts)

    print(
        f"[SUMMARY] {len(succeeded)} file(s) succeeded, {len(failed)} file(s) failed "
        f"out of {len(input_files)} ({failed_attempts} failed attempt(s))"
    )
    if failed:
        print("[SUMMARY] Failed files were recorded for later inspection/retry:")
        for path in failed[:10]:
            print(f"  - {path} (attempts={file_status[path]['attempts']})")
        if len(failed) > 10:
            print(f"  ... and {len(failed) - 10} more")
    return 0 if not failed and failed_attempts == 0 else 1


def _apply_cli_runtime_overrides(sim, args):
    """Apply runtime safety overrides without forcing users to edit config."""
    result = dict(sim)
    max_duration = getattr(args, "max_duration", None)
    stall_timeout = getattr(args, "stall_timeout", None)
    if max_duration is not None:
        result["max_duration_per_file_sec"] = max_duration
    if stall_timeout is not None:
        result["stall_timeout_sec"] = stall_timeout
    if getattr(args, "no_retry", False):
        result["retry_failed_at_end"] = False
    return result


def _resolve_input_files(args, config, sim):
    """Return list of input MF4 paths.

    Supports:
      - single file:        rsim run <file.mf4>
      - dataset (all):      rsim run --dataset <name>
      - directory scan:     rsim run <dir>  (with optional --select)
      - dataset scan+pick:  rsim run --dataset <name> --select
    Data access is validated via core.data; UNC data is referenced in place
    unless the profile sets data.copy=true.
    """
    from core.data import (
        check_data_access,
        iter_mf4_inputs,
        looks_local_windows_path,
        resolve_data_for_local,
        scan_data_file,
    )
    from core.simulation import _results_runtime_dir

    dataset_name = getattr(args, "dataset", "") or ""
    input_arg = getattr(args, "input_mf4", None)
    select_mode = bool(getattr(args, "select", False))
    limit = int(getattr(args, "limit", 0) or 0)
    required_signals = list(getattr(args, "required_signal", []) or [])
    profile_data = _profile_data_block(config)

    # Determine the source path (file/dir) and dataset config.
    source_path = ""
    dataset_cfg = {}
    if dataset_name:
        dataset_cfg, files = resolve_dataset_files(sim, dataset_name)
        if dataset_cfg and files and not select_mode:
            # Dataset resolved to explicit files; validate access and return.
            return _validate_and_stage(files, config, profile_data, args)
        if not dataset_cfg:
            available = [d.get("name", "?") for d in sim.get("datasets", [])]
            print(f"[ERROR] Dataset '{dataset_name}' not found. Available: {available or 'none'}")
            return None
        source_path = dataset_cfg.get("input_mf4") or dataset_cfg.get("input_dir") or ""
        if not source_path:
            print(f"[ERROR] Dataset '{dataset_name}' has no input_dir/input_mf4.")
            return None
    elif input_arg:
        source_path = input_arg
    else:
        print("[ERROR] Input MF4 path required.")
        print()
        print("  Usage:")
        print("    rsim run <input.mf4>                       # single file")
        print("    rsim run <dir> --select                    # scan a directory and pick")
        print("    rsim run --dataset CBNA_23-4-26            # all MF4 in dataset")
        print("    rsim run --dataset CBNA_23-4-26 --select   # scan dataset and pick")
        return None

    if not os.path.exists(source_path):
        print(f"[ERROR] Input path not found: {source_path}")
        return None

    # Single file, no select: validate + stage.
    if os.path.isfile(source_path) and not select_mode:
        return _validate_and_stage([source_path], config, profile_data, args)

    # Directory or --select: scan candidates and optionally pick.
    candidates = list(iter_mf4_inputs(Path(source_path), limit=0 if not limit else limit))
    if not candidates:
        print(f"[ERROR] No input MF4 files found under: {source_path}")
        return None

    if not select_mode:
        # Directory given without --select: run all candidates.
        chosen = [str(p) for p in candidates]
        print(f"[INFO] Found {len(chosen)} MF4 file(s) under {source_path}")
        return _validate_and_stage(chosen, config, profile_data, args)

    # --select: list with signal scan, let user pick.
    chosen = _interactive_select(candidates, required_signals, limit)
    if not chosen:
        print("[INFO] No files selected.")
        return None
    return _validate_and_stage(chosen, config, profile_data, args)


def _interactive_select(candidates, required_signals, limit):
    """Print candidates with signal status and read a compact selection."""
    from core.data import scan_data_file

    print(f"[INFO] {len(candidates)} candidate MF4 file(s):")
    max_bytes = 8 * 1024 * 1024
    for idx, path in enumerate(candidates, 1):
        size_mb = path.stat().st_size / (1024 * 1024) if path.exists() else 0
        status = ""
        if required_signals:
            scanned = scan_data_file(path, required_signals, max_bytes=max_bytes)
            status = f" [{scanned.signal_status}]"
        print(f"  {idx:>3}. {path} ({size_mb:.1f} MB){status}")
    print()
    print("  Enter file numbers (e.g. 1,3 or 1-3), or 'all', or blank to cancel:")
    try:
        raw = input("  > ").strip()
    except EOFError:
        return []
    if not raw:
        return []
    if raw.lower() == "all":
        return [str(p) for p in candidates]
    indices = _parse_selection(raw, len(candidates))
    return [str(candidates[i - 1]) for i in indices if 1 <= i <= len(candidates)]


def _parse_selection(raw, total):
    indices: list[int] = []
    for token in raw.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            parts = token.split("-")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                lo, hi = int(parts[0]), int(parts[1])
                indices.extend(range(min(lo, hi), max(lo, hi) + 1))
        elif token.isdigit():
            indices.append(int(token))
    return indices


def _validate_and_stage(paths, config, profile_data, args):
    """Validate access for each path; stage locally per the unified run policy.

    Uses core.policy.policy_from_config to decide copy_data per the 8-combination
    matrix. Local backend + UNC data → download to local runtime dir; local data
    → in place. Cluster backend is not staged here (prepare_cluster_job handles it).
    """
    from core.data import resolve_data_for_local
    from core.policy import policy_from_config
    from core.simulation import _results_runtime_dir

    runtime_data_dir = _results_runtime_dir(config) / "data"
    user_copy = (profile_data or {}).get("copy") if profile_data else None

    resolved: list[str] = []
    for path in paths:
        policy = policy_from_config(config, path)
        # User-explicit copy overrides policy; otherwise use policy.copy_data.
        copy = bool(user_copy) if user_copy is not None else policy.copy_data
        result = resolve_data_for_local(
            {}, input_path=path, profile_data={"copy": copy}, runtime_data_dir=runtime_data_dir,
        )
        for warning in result.warnings:
            print(f"  [!] {warning}")
        if not result.access.ok:
            print(f"[ERROR] Input data not accessible: {path} ({result.access.detail})")
            return None
        if result.copied:
            print(f"  [INFO] Staged input locally: {path} → {result.resolved_path}")
        resolved.append(result.resolved_path)
    return resolved


def _profile_data_block(config):
    """Return the active profile's data block (copy/required_signals)."""
    cluster = config.get("cluster") or {}
    return {
        "copy": bool(cluster.get("copy_data", False)),
        "required_signals": list(cluster.get("required_input_signals") or []),
    }


def _str_attr(args, name):
    value = getattr(args, name, "")
    if not isinstance(value, str):
        return ""
    return value.strip()


def _print_env_hints(config):
    """Print a short environment-check summary when run cannot start."""
    try:
        from core.environment import check_local_environment
        items = check_local_environment(config)
        print()
        print("  Environment check:")
        for item in items:
            mark = "OK" if item.ok else "!!"
            print(f"    [{mark}] {item.name}: {item.detail}")
    except Exception:
        pass


def _gen_output_path(input_mf4):
    """Generate output path: same dir, <stem>out.MF4."""
    return gen_output_path(input_mf4)


def _find_selena_exe(config):
    """Find selena.exe: prefer profile/cluster selena_exe, then build output."""
    cluster_exe = str((config.get("cluster") or {}).get("selena_exe") or "")
    if cluster_exe and os.path.exists(cluster_exe):
        return cluster_exe
    exe = resolve_selena_executable(config)
    if exe and os.path.exists(exe):
        return exe
    # Report whichever path is configured so the error message is useful.
    return cluster_exe or exe or ""


def _build_env(config):
    """Build environment dict with required PATH entries."""
    env = os.environ.copy()
    env_path = render_selena_environment_path(config)
    env_path = env_path.replace("$(Path)", env.get("PATH", "")).replace("$(LocalDebuggerEnvironment)", "")
    env_segments = [s for s in env_path.split(";") if s]
    existing = env.get("PATH", "").split(";")
    seen = set()
    merged = []
    for seg in [*env_segments, *existing]:
        norm = os.path.normpath(seg)
        if norm and norm not in seen:
            seen.add(norm)
            merged.append(seg)
    env["PATH"] = ";".join(merged)
    boost_root = config.get("environment", {}).get("boost_root", "") or config.get("boost_root", "")
    if boost_root:
        env["BOOST_ROOT"] = boost_root
    return env


def _build_command(sim, selena_exe, paramconfig_path, extra_args):
    """Build selena.exe command from generated paramconfig."""
    cmd = [selena_exe, "--paramconfig", paramconfig_path]
    cli_extra_args = list(sim.get("extra_args", []))
    cli_extra_args.extend(extra_args)
    if sim.get("tolerant") and "--tolerant" not in cli_extra_args:
        cli_extra_args.append("--tolerant")
    cmd.extend(cli_extra_args)
    return cmd


def _format_size_mb(path: str) -> str:
    if not path or not os.path.exists(path):
        return "0.0 MB"
    try:
        return f"{os.path.getsize(path) / (1024 * 1024):.1f} MB"
    except OSError:
        return "0.0 MB"


def _render_runtime_bar(elapsed: float, limit_sec: int, width: int = 24) -> str:
    if limit_sec <= 0:
        return "[" + ("#" * (width // 2)).ljust(width, "-") + "]"
    ratio = max(0.0, min(1.0, elapsed / limit_sec))
    filled = int(width * ratio)
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def _get_effective_runtime_limit(timeout: int, sim: dict) -> int:
    """Return the effective per-file hard limit in seconds."""
    configured = int(sim.get("max_duration_per_file_sec", 0) or 0)
    if configured <= 0:
        return timeout
    return min(timeout, configured) if timeout > 0 else configured


def _run_single(project, config, sim, selena_exe, input_mf4, output_mf4,
                timeout, no_wait, dry_run, extra_args, env, cwd, file_index=1, file_total=1):
    """Run selena.exe for a single MF4. Return 0 on success."""
    input_mf4 = os.path.normpath(input_mf4)
    output_mf4 = os.path.normpath(output_mf4)
    config_for_run = apply_simulation_to_config(config, sim)
    try:
        rendered = render_selena_config(config_for_run)
    except (ValueError, FileNotFoundError) as exc:
        print(f"[ERROR] Failed to render Selena paramconfig: {exc}")
        return 1
    paramconfig_path = rendered.get("assets", {}).get("fixed_config_path", "")
    cmd = _build_command(sim, selena_exe, paramconfig_path, extra_args)

    print(f"[INFO] Launching Selena simulation for project '{project}'")
    print()
    print(f"  Executable:   {selena_exe}")
    print(f"  Input MF4:    {input_mf4}")
    print(f"  Output MF4:   {output_mf4}")
    print(f"  Paramconfig:  {paramconfig_path}")
    print(f"  Runtime XML:  {sim.get('runtime_xml', '')}")
    print(f"  Source:       {sim.get('source', '') or '(unset)'}")
    print(f"  Position:     {sim.get('mounting_position', '') or '(unset)'}")
    detection = sim.get("radar_detection")
    if detection:
        print(f"  Radar detect: {detection.get('position')} via {detection.get('method')} (conf={detection.get('confidence')})")
    print(f"  Timeout:      {timeout}s")
    print(f"  File limit:   {_get_effective_runtime_limit(timeout, sim)}s")
    print(f"  Batch item:   {file_index}/{file_total}")
    print(f"  Working dir:  {cwd}")
    print()

    if dry_run:
        print(f"[DRY-RUN] {' '.join(cmd)}")
        return 0

    if no_wait:
        print(f"[INFO] Launching selena.exe (no-wait mode)...")
        subprocess.Popen(cmd, cwd=cwd, env=env)
        print(f"[INFO] Process launched. Output MF4 will be written to: {output_mf4}")
        return 0

    print("[RUNNING] Simulation started... (press Ctrl+C to abort)")
    print()

    start = time.time()
    runtime_limit = _get_effective_runtime_limit(timeout, sim)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            env=env,
            bufsize=1,
        )
        line_queue: queue.Queue[str | None] = queue.Queue()

        def _reader():
            try:
                for raw_line in iter(proc.stdout.readline, ""):
                    line_queue.put(raw_line)
            finally:
                line_queue.put(None)

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()

        output_lines = []
        last_activity = time.time()
        stall_timeout = int(sim.get("stall_timeout_sec", 180))
        poll_interval = max(1, int(sim.get("poll_interval_sec", 1)))
        last_heartbeat = 0.0
        heartbeat_interval = int(sim.get("heartbeat_interval_sec", 15) or 15)
        last_size_sample = (time.time(), os.path.getsize(output_mf4) if os.path.exists(output_mf4) else 0)
        last_rate_mb_min = 0.0
        while True:
            try:
                elapsed = time.time() - start
                if elapsed > runtime_limit:
                    print()
                    print(f"[ERROR] Simulation exceeded per-file runtime limit after {runtime_limit}s")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    _save_run_record(
                        project=project,
                        input_mf4=input_mf4,
                        output_mf4=output_mf4,
                        status="runtime_limit",
                        duration_sec=time.time() - start,
                        config=config,
                        return_code=1,
                    )
                    return 1

                saw_activity = False
                while True:
                    try:
                        line = line_queue.get_nowait()
                    except queue.Empty:
                        break
                    if line is None:
                        if proc.poll() is not None:
                            break
                        continue
                    stripped = line.rstrip()
                    output_lines.append(stripped)
                    print(f"  {stripped[:120]}")
                    saw_activity = True

                if saw_activity or _has_runtime_progress(sim, output_mf4):
                    last_activity = time.time()

                if proc.poll() is not None and line_queue.empty():
                    break

                if heartbeat_interval > 0 and (time.time() - last_heartbeat) >= heartbeat_interval:
                    current_size = _format_size_mb(output_mf4)
                    now = time.time()
                    current_bytes = os.path.getsize(output_mf4) if os.path.exists(output_mf4) else 0
                    delta_sec = max(0.001, now - last_size_sample[0])
                    delta_mb = (current_bytes - last_size_sample[1]) / (1024 * 1024)
                    last_rate_mb_min = max(0.0, delta_mb * 60 / delta_sec)
                    last_size_sample = (now, current_bytes)
                    idle_for = int(time.time() - last_activity)
                    bar = _render_runtime_bar(elapsed, runtime_limit)
                    print(
                        f"  [PROGRESS] item {file_index}/{file_total} {bar} "
                        f"{int(elapsed)}s/{runtime_limit}s | output {current_size} "
                        f"| +{last_rate_mb_min:.1f} MB/min | idle {idle_for}s"
                    )
                    last_heartbeat = time.time()

                if stall_timeout > 0 and (time.time() - last_activity) > stall_timeout:
                    print()
                    print(f"[ERROR] Simulation stalled for more than {stall_timeout}s, aborting current file")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    _save_run_record(
                        project=project,
                        input_mf4=input_mf4,
                        output_mf4=output_mf4,
                        status="stalled",
                        duration_sec=time.time() - start,
                        config=config,
                        return_code=1,
                    )
                    return 1
                time.sleep(poll_interval)

            except KeyboardInterrupt:
                print()
                print("[INFO] Aborting simulation...")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                _save_run_record(
                    project=project,
                    input_mf4=input_mf4,
                    output_mf4=output_mf4,
                    status="aborted",
                    duration_sec=time.time() - start,
                    config=config,
                )
                return 130

        retcode = proc.returncode
        duration = time.time() - start
        errors = _extract_errors(output_lines)

        if errors:
            print()
            print(f"[WARN] {len(errors)} error(s) in output (first 5):")
            for e in errors[:5]:
                print(f"  - {e}")

        if os.path.exists(output_mf4) and retcode == 0:
            size_mb = os.path.getsize(output_mf4) / (1024 * 1024)
            print()
            print(f"[SUCCESS] Simulation completed ({duration:.1f}s)")
            print(f"  Output MF4: {output_mf4} ({size_mb:.1f} MB)")
            print()
            print(f"  Next: rsim analyze {output_mf4}")
        else:
            print()
            print(f"[ERROR] Simulation failed (exit={retcode})")
            if os.path.exists(output_mf4):
                size_mb = os.path.getsize(output_mf4) / (1024 * 1024)
                print(f"  Output MF4: {output_mf4} ({size_mb:.1f} MB) — may be incomplete")
            else:
                print(f"  Output MF4 not found: {output_mf4}")

        _save_run_record(
            project=project,
            input_mf4=input_mf4,
            output_mf4=output_mf4,
            status=_status_from_result(retcode, errors),
            return_code=retcode,
            duration_sec=duration,
            config=config,
        )

        return retcode

    except FileNotFoundError:
        print(f"[ERROR] selena.exe not executable: {selena_exe}")
        return 1


def _has_runtime_progress(sim, output_mf4):
    """Detect runtime progress via log/output file timestamp changes."""
    now = time.time()
    progress_files = [sim.get("log_file", ""), output_mf4]
    for path in progress_files:
        if path and os.path.exists(path):
            try:
                if now - os.path.getmtime(path) <= 2:
                    return True
            except OSError:
                pass
    return False


def _extract_errors(lines):
    errors = []
    for line in lines:
        lowered = line.lower()
        if "signals not found" in lowered or "connection errors" in lowered:
            continue
        if "float exceptions" in lowered:
            continue
        if any(kw in lowered for kw in ["error", "failed", "exception", "fatal"]):
            errors.append(line.strip())
    return errors[:50]


def _status_from_result(retcode, errors):
    if retcode == 0 and not errors:
        return "success"
    if retcode == 0 and errors:
        return "completed_with_warnings"
    return "failed"


def _save_run_record(project, input_mf4, output_mf4, status, duration_sec, config, return_code=None):
    """Save simulation run metadata."""
    try:
        from core.config import get_results_dir
        results_dir = str(get_results_dir(project))
        run_record = {
            "timestamp": datetime.now().isoformat(),
            "project": project,
            "status": status,
            "return_code": return_code,
            "duration_sec": round(duration_sec, 1),
            "input_mf4": input_mf4,
            "output_mf4": output_mf4,
        }
        run_file = os.path.join(results_dir, ".run_history.json")
        history = []
        if os.path.exists(run_file):
            with open(run_file, encoding="utf-8") as f:
                history = json.load(f)
        history.append(run_record)
        if len(history) > 100:
            history = history[-100:]
        with open(run_file, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _save_batch_summary(project, input_files, file_status, failed_attempts):
    """Persist the last batch result and a compact failed-file queue."""
    try:
        from core.config import get_results_dir
        results_dir = str(get_results_dir(project))
        os.makedirs(results_dir, exist_ok=True)
        summary = {
            "timestamp": datetime.now().isoformat(),
            "project": project,
            "total_files": len(input_files),
            "failed_attempts": failed_attempts,
            "files": [
                {
                    "input_mf4": path,
                    "status": item.get("status"),
                    "attempts": item.get("attempts", 0),
                    "last_return_code": item.get("last_return_code"),
                }
                for path, item in file_status.items()
            ],
        }
        summary_path = os.path.join(results_dir, ".last_run_summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        failed = [item for item in summary["files"] if item["status"] != "success"]
        failed_path = os.path.join(results_dir, ".failed_runs.json")
        if failed:
            history = []
            if os.path.exists(failed_path):
                with open(failed_path, encoding="utf-8") as f:
                    history = json.load(f)
            history.append(
                {
                    "timestamp": summary["timestamp"],
                    "project": project,
                    "failed_files": failed,
                }
            )
            if len(history) > 50:
                history = history[-50:]
            with open(failed_path, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
