"""rsim cluster - package and submit Cluster V2.0 batch jobs."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

from core.cluster import (
    check_cluster_environment,
    detect_python2_candidates,
    fetch_cluster_job,
    get_cluster_web_status,
    inspect_cluster_job,
    list_cluster_jobs,
    list_cluster_profiles,
    package_to_dict,
    prepare_cluster_job,
    scan_cluster_data,
    submit_cluster_job,
)


def register(subparsers):
    p = subparsers.add_parser("cluster", help="Prepare and submit server Cluster batch simulation jobs")
    cluster_sub = p.add_subparsers(dest="cluster_command", help="Cluster commands")

    check = cluster_sub.add_parser("check", help="Check Cluster paths and local submit prerequisites")
    check.add_argument("--profile", default="", help="Cluster runtime profile")
    check.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    python = cluster_sub.add_parser("python", help="Detect Python2 runtimes usable by client.py")
    python.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    profiles = cluster_sub.add_parser("profiles", help="List Cluster runtime profiles")
    profiles.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    data = cluster_sub.add_parser("data", help="List/scan candidate MF4 inputs for Cluster submission")
    data.add_argument("input_path", nargs="?", default="", help="Input MF4 or dataset directory")
    data.add_argument("--dataset", default="", help="Use a configured simulation.datasets entry")
    data.add_argument("--profile", default="", help="Cluster runtime profile")
    data.add_argument("--required-signal", action="append", default=[], help="Signal name that should exist in input MF4")
    data.add_argument("--limit", type=int, default=20, help="Max MF4 files to inspect")
    data.add_argument("--max-read-mb", type=int, default=8, help="Max MB to scan per file for signal names")
    data.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    prepare = cluster_sub.add_parser("prepare", help="Create a Cluster job package without submitting it")
    prepare.add_argument("input_path", nargs="?", default="", help="Input MF4, zip, or dataset directory")
    prepare.add_argument("--dataset", default="", help="Use a configured simulation.datasets entry")
    prepare.add_argument("--profile", default="", help="Cluster runtime profile")
    prepare.add_argument("--run-id", default="", help="Stable run id / folder name")
    prepare.add_argument("--copy-data", action="store_true", help="Copy input data into the job folder")
    prepare.add_argument("--copy-selena", action="store_true", help="Copy the local Selena runtime into the job folder")
    prepare.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    submit = cluster_sub.add_parser("submit", help="Submit a prepared Config.cfg through client.py")
    submit.add_argument("config_path", help="Path to generated Config.cfg")
    submit.add_argument("--execute", action="store_true", help="Actually call client.py; default is dry-run")
    submit.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    list_cmd = cluster_sub.add_parser("list", help="List prepared Cluster job packages")
    list_cmd.add_argument("--limit", type=int, default=20, help="Max jobs to show")
    list_cmd.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    status = cluster_sub.add_parser("status", help="Inspect a prepared/submitted Cluster job folder")
    status.add_argument("job_dir", help="Cluster job directory")
    status.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    web_status = cluster_sub.add_parser("web-status", help="Read official Cluster web status for a job id or job dir")
    web_status.add_argument("job", help="Cluster job id or job directory")
    web_status.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    wait = cluster_sub.add_parser("wait", help="Poll official Cluster status and shared output until completion or timeout")
    wait.add_argument("job", help="Cluster job id or job directory")
    wait.add_argument("--job-dir", default="", help="Optional prepared job directory to inspect shared output")
    wait.add_argument("--interval", type=int, default=60, help="Seconds between polls")
    wait.add_argument("--max-minutes", type=int, default=0, help="Stop after this many minutes; 0 uses Cluster timeout when available")
    wait.add_argument("--once", action="store_true", help="Poll once and exit")
    wait.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    fetch = cluster_sub.add_parser("fetch", help="Copy Cluster output files into a local results directory")
    fetch.add_argument("job_dir", help="Cluster job directory")
    fetch.add_argument("--dest", default="", help="Destination directory")
    fetch.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    fetch.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    run_cmd = cluster_sub.add_parser("run", help="One-shot prepare → submit → wait → fetch")
    run_cmd.add_argument("input_path", nargs="?", default="", help="Input MF4, zip, or dataset directory")
    run_cmd.add_argument("--dataset", default="", help="Use a configured simulation.datasets entry")
    run_cmd.add_argument("--profile", default="", help="Cluster runtime profile")
    run_cmd.add_argument("--run-id", default="", help="Stable run id / folder name")
    run_cmd.add_argument("--select", action="store_true", help="Scan a directory/dataset and pick MF4 files interactively")
    run_cmd.add_argument("--limit", type=int, default=0, help="With --select, cap how many MF4 files to list (0=all)")
    run_cmd.add_argument("--required-signal", action="append", default=[], help="Signal name that should exist in each input MF4")
    run_cmd.add_argument("--copy-data", action="store_true", help="Copy input data into the job folder")
    run_cmd.add_argument("--copy-selena", action="store_true", help="Copy the local Selena runtime into the job folder")
    run_cmd.add_argument("--execute", action="store_true", help="Actually submit; default is dry-run (prepare only)")
    run_cmd.add_argument("--no-wait", action="store_true", help="Submit and return without waiting for completion")
    run_cmd.add_argument("--no-fetch", action="store_true", help="Do not fetch outputs after completion")
    run_cmd.add_argument("--max-minutes", type=int, default=0, help="Wait timeout in minutes; 0 uses Cluster timeout")
    run_cmd.add_argument("--json", action="store_true", help="Print machine-readable JSON")


def run(args, config):
    command = getattr(args, "cluster_command", "") or ""
    if command == "check":
        return _run_check(args, config)
    if command == "python":
        return _run_python(args, config)
    if command == "profiles":
        return _run_profiles(args, config)
    if command == "data":
        return _run_data(args, config)
    if command == "prepare":
        return _run_prepare(args, config)
    if command == "submit":
        return _run_submit(args, config)
    if command == "list":
        return _run_list(args, config)
    if command == "status":
        return _run_status(args, config)
    if command == "web-status":
        return _run_web_status(args, config)
    if command == "wait":
        return _run_wait(args, config)
    if command == "fetch":
        return _run_fetch(args, config)
    if command == "run":
        return _run_one_shot(args, config)
    print("Missing cluster command. Use: rsim cluster check|python|profiles|data|prepare|submit|list|status|web-status|wait|fetch|run")
    return 1


def _run_check(args, config):
    items = check_cluster_environment(config, profile=getattr(args, "profile", "") or "")
    if getattr(args, "json", False):
        print(json.dumps([item.__dict__ for item in items], indent=2))
        return 0 if all(item.ok for item in items) else 1

    print("Cluster environment check:")
    for item in items:
        mark = "OK" if item.ok else "!!"
        print(f"  [{mark}] {item.name}: {item.detail}")
    if all(item.ok for item in items):
        print("Cluster check passed.")
        return 0
    print("Cluster check found issues. Fix the failed required path before real submit.")
    return 1


def _run_python(args, config):
    configured = str((config.get("cluster") or {}).get("python_path") or "")
    candidates = detect_python2_candidates(configured)
    if getattr(args, "json", False):
        print(json.dumps([item.__dict__ for item in candidates], indent=2))
        return 0 if any(item.ok for item in candidates) else 1
    print("Python2 candidates for client.py:")
    for item in candidates:
        mark = "OK" if item.ok else "!!"
        print(f"  [{mark}] {item.path}: {item.detail}")
    return 0 if any(item.ok for item in candidates) else 1


def _run_profiles(args, config):
    profiles = list_cluster_profiles(config)
    if getattr(args, "json", False):
        print(json.dumps(profiles, indent=2))
        return 0
    print("Cluster profiles:")
    for item in profiles:
        print(f"  {item.get('name', '(unnamed)')}: {item.get('description', '')}")
        if item.get("selena_exe"):
            print(f"    Selena:  {item['selena_exe']}")
        if item.get("runtime_xml"):
            print(f"    Runtime: {item['runtime_xml']}")
        if item.get("required_input_signals"):
            print(f"    Signals: {', '.join(item['required_input_signals'])}")
    return 0


def _run_data(args, config):
    result = scan_cluster_data(
        config,
        input_path=getattr(args, "input_path", "") or "",
        dataset=getattr(args, "dataset", "") or "",
        profile=getattr(args, "profile", "") or "",
        required_signals=list(getattr(args, "required_signal", []) or []) or None,
        limit=int(getattr(args, "limit", 20) or 20),
        max_read_mb=int(getattr(args, "max_read_mb", 8) or 0),
    )
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return 0
    print("Cluster data candidates:")
    print(f"  Source:       {result.get('source') or '(unset)'}")
    print(f"  Profile:      {result.get('profile') or 'default'}")
    print(f"  Required:     {', '.join(result.get('required_signals') or []) or '(none)'}")
    print(f"  Max read/file:{result.get('max_read_mb')} MB")
    for warning in result.get("warnings", []):
        print(f"  [!] {warning}")
    for item in result.get("files", []):
        mb = float(item.get("size", 0)) / 1024 / 1024
        print(f"  [{item.get('signal_status')}] {item.get('path')} ({mb:.1f} MB)")
        if item.get("matched_signals"):
            print(f"      matched: {', '.join(item['matched_signals'])}")
        if item.get("missing_signals"):
            print(f"      missing: {', '.join(item['missing_signals'])}")
        if item.get("detail"):
            print(f"      {item['detail']}")
    return 0


def _run_prepare(args, config):
    package = prepare_cluster_job(
        config,
        input_path=getattr(args, "input_path", "") or "",
        dataset=getattr(args, "dataset", "") or "",
        run_id=getattr(args, "run_id", "") or "",
        profile=getattr(args, "profile", "") or "",
        copy_data=bool(getattr(args, "copy_data", False)) or None,
        copy_selena=bool(getattr(args, "copy_selena", False)) or None,
    )
    data = package_to_dict(package)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return 0

    print("Cluster job package prepared:")
    print(f"  Run id:       {package.run_id}")
    print(f"  Profile:      {package.profile}")
    print(f"  Job dir:      {package.job_dir}")
    print(f"  Config.cfg:   {package.config_path}")
    print(f"  Simulation:   {package.simulation_script}")
    print(f"  Manifest:     {package.manifest_path}")
    print(f"  Data path:    {package.datafile_path or '(unset)'}")
    print(f"  Output hint:  {package.output_hint}")
    print()
    print("Submit command:")
    print("  " + _quote_command(package.submit_command))
    if package.warnings:
        print()
        print("Warnings:")
        for warning in package.warnings:
            print(f"  [!] {warning}")
    return 0


def _run_submit(args, config):
    config_path = os.path.normpath(str(args.config_path))
    if not Path(config_path).exists():
        print(f"Config.cfg not found: {config_path}")
        return 1

    dry_run = not bool(getattr(args, "execute", False))
    result = submit_cluster_job(config_path, config, dry_run=dry_run)
    if dry_run:
        if getattr(args, "json", False):
            print(json.dumps(result.__dict__, indent=2))
        else:
            print(f"Dry-run submit mode: {result.mode}")
            print("Submit command:")
            print("  " + _quote_command(result.command))
            print("Use --execute to actually call client.py.")
        return 0

    payload = {
        "mode": result.mode,
        "dry_run": result.dry_run,
        "command": result.command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(result.stdout or "")
        if result.stderr:
            print(result.stderr)
        print(f"submit mode: {result.mode}")
        print(f"submit exit code: {result.returncode}")
    return result.returncode


def _run_list(args, config):
    jobs = list_cluster_jobs(config, limit=int(getattr(args, "limit", 20) or 20))
    if getattr(args, "json", False):
        print(json.dumps(jobs, indent=2))
        return 0
    if not jobs:
        print("No Cluster job packages found.")
        return 0
    print("Cluster job packages:")
    for job in jobs:
        status = job.get("status", {})
        print(f"  {job.get('run_id', '(unknown)')}  {status.get('state', 'unknown')}  {job.get('job_dir', '')}")
    return 0


def _run_status(args, config):
    status = inspect_cluster_job(os.path.normpath(str(args.job_dir)))
    if getattr(args, "json", False):
        print(json.dumps(status, indent=2))
        return 0 if status.get("exists") else 1
    print(f"Cluster job status: {status['state']}")
    print(f"  Job dir:      {status['job_dir']}")
    print(f"  Output dirs:  {', '.join(status['output_dirs']) or '(none)'}")
    print(f"  Files:        {status['file_count']}")
    print(f"  Total size:   {status['total_bytes']} bytes")
    print(f"  Tasks OK/NOK: {status.get('success_count', 0)}/{status.get('fail_count', 0)}")
    if status.get("truncated"):
        print("  Note:         output scan truncated")
    if status.get("output_mf4"):
        print("  MF4 outputs:")
        for item in status["output_mf4"]:
            print(f"    {item['relative_path']} ({item['size']} bytes)")
    if status.get("result_files"):
        print("  Result files:")
        for item in status["result_files"]:
            print(f"    {item['relative_path']}")
    if status.get("error_summary"):
        print("  Error summary:")
        for line in status["error_summary"]:
            print(f"    {line}")
    return 0 if status.get("exists") else 1


def _run_web_status(args, config):
    status = get_cluster_web_status(config, str(args.job))
    if getattr(args, "json", False):
        print(json.dumps(status, indent=2))
        return 0 if status.get("found") else 1
    if not status.get("found"):
        print(f"Official Cluster status not found: {status.get('error')}")
        return 1
    print(f"Official Cluster job {status.get('job_id')}: {status.get('state', 'unknown')}")
    for task in status.get("tasks", []):
        print(f"  task {task.get('task_id', '?')} {task.get('simulation_state', '')} worker={task.get('worker_host', '')} error={task.get('error_message', '')}")
    return 0


def _run_wait(args, config):
    job = str(args.job)
    job_dir = _resolve_wait_job_dir(job, str(getattr(args, "job_dir", "") or ""))
    interval = max(5, int(getattr(args, "interval", 60) or 60))
    max_minutes = max(0, int(getattr(args, "max_minutes", 0) or 0))
    started = time.monotonic()
    last = {}
    exit_code = 1

    while True:
        web = get_cluster_web_status(config, job)
        shared = inspect_cluster_job(job_dir) if job_dir else {}
        last = {
            "web": web,
            "shared": shared,
            "diagnosis": _diagnose_wait_state(web, shared, max_minutes=max_minutes),
        }
        if getattr(args, "json", False) and getattr(args, "once", False):
            print(json.dumps(last, indent=2))
        elif not getattr(args, "json", False):
            _print_wait_snapshot(last)

        diagnosis = last["diagnosis"]
        if diagnosis["done"]:
            exit_code = 0 if diagnosis["outcome"] == "success" else 2
            break
        if getattr(args, "once", False):
            exit_code = 0 if web.get("found") else 1
            break
        if max_minutes and time.monotonic() - started >= max_minutes * 60:
            exit_code = 2
            break
        time.sleep(interval)

    if getattr(args, "json", False) and not getattr(args, "once", False):
        print(json.dumps(last, indent=2))
    return exit_code


def _resolve_wait_job_dir(job, configured_job_dir):
    candidate = str(configured_job_dir or "")
    if not candidate and not str(job).isdigit():
        candidate = str(job)
    return os.path.normpath(candidate) if candidate else ""


def _diagnose_wait_state(web, shared, *, max_minutes):
    states = [str(task.get("simulation_state") or "") for task in web.get("tasks", [])]
    errors = [str(task.get("error_message") or "") for task in web.get("tasks", []) if task.get("error_message")]
    finished_times = [str(task.get("time_finished") or "") for task in web.get("tasks", [])]
    latest_runtime = max((_task_runtime_minutes(task) for task in web.get("tasks", [])), default=0.0)
    shared_state = str(shared.get("state") or "")
    success_count = int(shared.get("success_count") or 0)
    fail_count = int(shared.get("fail_count") or 0)
    output_count = len(shared.get("output_mf4") or [])
    done = False
    outcome = "running"

    if shared_state == "finished-success" or success_count > 0 or output_count > 0:
        done = True
        outcome = "success"
    elif shared_state == "finished-failed" or fail_count > 0 or errors:
        done = True
        outcome = "failed"
    elif states and all(state == "finished" for state in states):
        done = True
        outcome = "finished-no-output"

    stale_after = _max_task_timeout(web)
    if max_minutes:
        stale_after = max_minutes if not stale_after else min(stale_after, max_minutes)
    stale = bool(stale_after and latest_runtime >= stale_after and not done)

    return {
        "done": done,
        "outcome": outcome,
        "states": sorted(set(states)),
        "errors": errors,
        "finished_times": finished_times,
        "shared_state": shared_state,
        "success_count": success_count,
        "fail_count": fail_count,
        "output_count": output_count,
        "runtime_minutes": round(latest_runtime, 1),
        "stale_after_minutes": stale_after,
        "stale": stale,
    }


def _print_wait_snapshot(snapshot):
    web = snapshot["web"]
    diagnosis = snapshot["diagnosis"]
    print(f"Official Cluster job {web.get('job_id') or web.get('query')}: {web.get('state', 'unknown')}")
    print(
        "  shared={shared} ok/nok={ok}/{nok} outputs={outputs} runtime={runtime}min stale={stale}".format(
            shared=diagnosis["shared_state"] or "(none)",
            ok=diagnosis["success_count"],
            nok=diagnosis["fail_count"],
            outputs=diagnosis["output_count"],
            runtime=diagnosis["runtime_minutes"],
            stale=diagnosis["stale"],
        )
    )
    for task in web.get("tasks", []):
        print(
            "  task {task} {state} worker={worker} started={started} finished={finished} error={error}".format(
                task=task.get("task_id", "?"),
                state=task.get("simulation_state", ""),
                worker=task.get("worker_host", ""),
                started=task.get("time_simulation_is_running", ""),
                finished=task.get("time_finished", ""),
                error=task.get("error_message", ""),
            )
        )


def _task_runtime_minutes(task):
    raw = str(task.get("time_simulation_is_running") or "")
    started = _parse_cluster_time(raw)
    if not started:
        return 0.0
    return max(0.0, (datetime.now() - started).total_seconds() / 60)


def _max_task_timeout(web):
    values = []
    for task in web.get("tasks", []):
        try:
            values.append(int(str(task.get("timeout") or "").strip()))
        except ValueError:
            continue
    return max(values) if values else 0


def _parse_cluster_time(value):
    if not value or value == "0000-00-00 00:00:00":
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _run_fetch(args, config):
    job_dir = os.path.normpath(str(args.job_dir))
    dest = getattr(args, "dest", "") or _default_fetch_dest(config, job_dir)
    result = fetch_cluster_job(job_dir, dest, overwrite=bool(getattr(args, "overwrite", False)))
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return 0
    copied = result.get("copied", [])
    print(f"Fetched Cluster outputs to: {result['destination']}")
    print(f"  Files considered: {len(copied)}")
    print(f"  Copied: {sum(1 for item in copied if not item.get('skipped'))}")
    print(f"  Skipped: {sum(1 for item in copied if item.get('skipped'))}")
    return 0


def _default_fetch_dest(config, job_dir):
    project = config.get("_meta", {}).get("project") or config.get("project", {}).get("name") or "default"
    run_id = Path(job_dir).name
    return str(Path("results") / str(project) / "cluster" / run_id)


def _quote_command(cmd):
    return " ".join(f'"{part}"' if " " in str(part) else str(part) for part in cmd)


def _run_one_shot(args, config):
    """Prepare → submit → wait → fetch in one command.

    Honors --select to scan+pick inputs first. Without --execute this stops
    after prepare (dry-run), matching the safe default of submit/prepare.
    """
    profile = getattr(args, "profile", "") or ""
    input_path = getattr(args, "input_path", "") or ""
    dataset = getattr(args, "dataset", "") or ""

    if getattr(args, "select", False):
        input_path = _select_cluster_input(config, input_path, dataset, args)
        if not input_path:
            return 1
        # After selection, treat as an explicit input path (not a dataset).
        dataset = ""

    package = prepare_cluster_job(
        config,
        input_path=input_path,
        dataset=dataset,
        run_id=getattr(args, "run_id", "") or "",
        profile=profile,
        copy_data=bool(getattr(args, "copy_data", False)) or None,
        copy_selena=bool(getattr(args, "copy_selena", False)) or None,
    )
    print("Cluster job package prepared:")
    print(f"  Run id:     {package.run_id}")
    print(f"  Profile:    {package.profile}")
    print(f"  Job dir:    {package.job_dir}")
    print(f"  Data path:  {package.datafile_path or '(unset)'}")
    print(f"  Selena:     {package.submit_command and 'staged' if getattr(args, 'copy_selena', False) else '(profile)'}")
    if package.warnings:
        print("  Warnings:")
        for warning in package.warnings:
            print(f"    [!] {warning}")

    if not getattr(args, "execute", False):
        print()
        print("[DRY-RUN] Prepared only. Use --execute to submit through the manager.")
        if getattr(args, "json", False):
            print(json.dumps(package_to_dict(package), indent=2))
        return 0

    result = submit_cluster_job(package.config_path, config, dry_run=False)
    print()
    print(f"Submit: mode={result.mode} returncode={result.returncode}")
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    if result.returncode != 0:
        return result.returncode

    if getattr(args, "no_wait", False):
        print("[INFO] Submitted (--no-wait). Use 'rsim cluster wait <job>' to track.")
        return 0

    print()
    print("[INFO] Waiting for job completion...")
    wait_args = SimpleNamespace(
        job=package.run_id,
        job_dir=package.job_dir,
        interval=60,
        max_minutes=int(getattr(args, "max_minutes", 0) or 0),
        once=False,
        json=bool(getattr(args, "json", False)),
    )
    wait_code = _run_wait(wait_args, config)
    # wait returns 0 on success, 2 on failure/timeout; fetch regardless if outputs exist.

    if getattr(args, "no_fetch", False):
        return wait_code

    print()
    print("[INFO] Fetching outputs...")
    fetch_args = SimpleNamespace(
        job_dir=package.job_dir,
        dest="",
        overwrite=False,
        json=bool(getattr(args, "json", False)),
    )
    _run_fetch(fetch_args, config)
    return wait_code


def _select_cluster_input(config, input_path, dataset, args):
    """Scan a directory/dataset and let the user pick one MF4 for cluster submission."""
    from core.data import iter_mf4_inputs, scan_data_file
    from core.simulation import get_simulation_config

    sim = get_simulation_config(config)
    source = input_path
    if not source and dataset:
        for item in sim.get("datasets", []) or []:
            if item.get("name") == dataset:
                source = item.get("input_mf4") or item.get("input_dir") or ""
                break
    if not source:
        print("[ERROR] --select requires an input path or --dataset.")
        return ""
    if not os.path.exists(source):
        print(f"[ERROR] Input path not found: {source}")
        return ""

    required = list(getattr(args, "required_signal", []) or [])
    limit = int(getattr(args, "limit", 0) or 0) or 0
    candidates = list(iter_mf4_inputs(Path(source), limit=limit or 0))
    if not candidates:
        print(f"[ERROR] No input MF4 files found under: {source}")
        return ""

    print(f"[INFO] {len(candidates)} candidate MF4 file(s):")
    for idx, path in enumerate(candidates, 1):
        size_mb = path.stat().st_size / (1024 * 1024) if path.exists() else 0
        status = ""
        if required:
            scanned = scan_data_file(path, required, max_bytes=8 * 1024 * 1024)
            status = f" [{scanned.signal_status}]"
        print(f"  {idx:>3}. {path} ({size_mb:.1f} MB){status}")
    print()
    print("  Enter one file number to submit (e.g. 1), or blank to cancel:")
    try:
        raw = input("  > ").strip()
    except EOFError:
        return ""
    if not raw or not raw.isdigit() or not (1 <= int(raw) <= len(candidates)):
        print("[INFO] No valid selection.")
        return ""
    return str(candidates[int(raw) - 1])
