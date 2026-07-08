"""rsim server - minimal control-plane shell and HTTP server."""

from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
from pathlib import Path

from core.control_http import make_control_handler
from core.control_service import ControlService, default_control_db_path

# This command runs on the control server (possibly Linux) without project config.
NO_CONFIG = True


def register(subparsers):
    parser = subparsers.add_parser("server", help="Run or inspect the minimal control server")
    server_sub = parser.add_subparsers(dest="server_command", help="Server commands")

    serve = server_sub.add_parser("serve", help="Start the stdlib HTTP JSON control server")
    serve.add_argument("--host", default="127.0.0.1", help="Bind host")
    serve.add_argument("--port", type=int, default=8877, help="Bind port")
    serve.add_argument("--db-path", default="", help="SQLite database path")
    serve.add_argument(
        "--allowed-task-types",
        default="",
        help="Comma-separated task_type whitelist (e.g. 'cluster.run'). Empty (default) "
        "accepts all task types — use this for Mode B (full local+cluster) servers. "
        "Set to 'cluster.run' for Mode A (Linux cluster-only service) so the server "
        "rejects local.check / local.build_selena / local.run_sim jobs with HTTP 400.",
    )

    create = server_sub.add_parser("create-job", help="Create a control job in the local control DB")
    create.add_argument("job_type", help="Job type, e.g. local.check or local.run_sim")
    create.add_argument("--db-path", default="", help="SQLite database path")
    create.add_argument("--project", default="", help="Project name to put in the job payload")
    create.add_argument("--config-path", default="", help="Config path to map to rsim --config")
    create.add_argument("--payload-json", default="", help="Extra JSON payload to merge into the task payload")
    create.add_argument("--metadata-json", default="", help="Optional JSON metadata for the job")
    create.add_argument("--input-mf4", default="", help="Input MF4 path for local.run_sim or cluster.run")
    create.add_argument("--input-path", default="", help="Generic input path alias for local.run_sim or cluster.run")
    create.add_argument("--dataset", default="", help="Dataset name for local.run_sim or cluster.run")
    create.add_argument("--profile", default="", help="Simulation or cluster profile")
    create.add_argument("--backend", default="", help="Backend override for local.check")
    create.add_argument("--output-mf4", default="", help="Explicit output MF4 path for local.run_sim")
    create.add_argument("--mode", default="", help="Build mode for local.build_selena")
    create.add_argument("--run-id", default="", help="Stable run id for cluster.run")
    create.add_argument("--timeout", type=int, default=0, help="Timeout in seconds for local.run_sim")
    create.add_argument("--limit", type=int, default=0, help="Selection limit for local.run_sim or cluster.run")
    create.add_argument("--max-duration", type=int, default=0, help="Per-file hard runtime limit for local.run_sim")
    create.add_argument("--stall-timeout", type=int, default=0, help="Per-file inactivity timeout for local.run_sim")
    create.add_argument("--max-minutes", type=int, default=0, help="Wait timeout in minutes for cluster.run")
    create.add_argument("--clean", action="store_true", help="Pass --clean to local.build_selena")
    create.add_argument("--deps", action="store_true", help="Pass --deps to local.check")
    create.add_argument("--dry-run", action="store_true", help="Pass --dry-run to local.run_sim")
    create.add_argument("--execute", action="store_true", help="Pass --execute to cluster.run")
    create.add_argument("--copy-data", action="store_true", help="Pass --copy-data to cluster.run")
    create.add_argument("--copy-selena", action="store_true", help="Pass --copy-selena to cluster.run")
    create.add_argument("--no-progress", action="store_true", help="Pass --no-progress to local.build_selena")
    create.add_argument("--select", action="store_true", help="Pass --select to local.run_sim or cluster.run")
    create.add_argument("--no-retry", action="store_true", help="Pass --no-retry to local.run_sim")
    create.add_argument("--no-wait", action="store_true", help="Pass --no-wait to cluster.run")
    create.add_argument("--no-fetch", action="store_true", help="Pass --no-fetch to cluster.run")
    create.add_argument("--required-signal", action="append", default=[], help="Repeatable signal filter for cluster.run")
    create.add_argument("--extra-arg", action="append", default=[], help="Repeatable extra arg for local.run_sim")

    get_job = server_sub.add_parser("get-job", help="Show a control job from the local control DB")
    get_job.add_argument("job_id", help="Job id")
    get_job.add_argument("--db-path", default="", help="SQLite database path")

    get_logs = server_sub.add_parser("get-logs", help="Show control task logs from the local control DB")
    get_logs.add_argument("job_id", help="Job id")
    get_logs.add_argument("--task-id", default="", help="Optional task id")
    get_logs.add_argument("--since", type=int, default=0, help="Cursor from previous read")
    get_logs.add_argument("--limit", type=int, default=200, help="Max log lines")
    get_logs.add_argument("--db-path", default="", help="SQLite database path")

    cancel = server_sub.add_parser("cancel", help="Cancel a control job in the local control DB")
    cancel.add_argument("job_id", help="Job id")
    cancel.add_argument("--db-path", default="", help="SQLite database path")

    reclaim = server_sub.add_parser(
        "reclaim",
        help="Requeue running tasks whose agent has gone silent (dead-agent recovery)",
    )
    reclaim.add_argument(
        "--stale-after", type=float, default=300.0,
        help="Seconds since last agent heartbeat before a task is considered stale (default 300)",
    )
    reclaim.add_argument(
        "--max-attempts", type=int, default=3,
        help="Fail tasks that have already been reclaimed this many times (default 3, 0=unlimited)",
    )
    reclaim.add_argument("--db-path", default="", help="SQLite database path")

    list_agents = server_sub.add_parser(
        "list-agents",
        help="List registered agents (id, status, last heartbeat, current task)",
    )
    list_agents.add_argument("--db-path", default="", help="SQLite database path")


def run(args, config):
    command = getattr(args, "server_command", "") or ""
    if command == "serve":
        return _run_serve(args)
    if command == "create-job":
        return _run_create_job(args)
    if command == "get-job":
        return _print_json(_service_from_args(args).get_job(args.job_id))
    if command == "get-logs":
        return _print_json(
            _service_from_args(args).get_logs(
                job_id=args.job_id,
                task_id=getattr(args, "task_id", "") or "",
                since=int(getattr(args, "since", 0) or 0),
                limit=int(getattr(args, "limit", 200) or 200),
            )
        )
    if command == "cancel":
        return _print_json(_service_from_args(args).cancel_job(args.job_id))
    if command == "reclaim":
        max_attempts = int(getattr(args, "max_attempts", 3) or 0)
        reclaimed = _service_from_args(args).reclaim_stale_tasks(
            stale_after_seconds=float(getattr(args, "stale_after", 300.0) or 300.0),
            max_attempts=(max_attempts if max_attempts > 0 else None),
        )
        return _print_json({"reclaimed": reclaimed, "count": len(reclaimed)})
    if command == "list-agents":
        agents = _service_from_args(args).list_agents()
        return _print_json({"agents": agents})
    print("Missing server command. Use: rsim server serve|create-job|get-job|get-logs|cancel|reclaim|list-agents")
    return 1


def _run_serve(args) -> int:
    host = getattr(args, "host", "127.0.0.1")
    port = int(getattr(args, "port", 8877))

    allowed_raw = getattr(args, "allowed_task_types", "") or ""
    allowed = {part.strip() for part in allowed_raw.split(",") if part.strip()} or None

    explicit_db = getattr(args, "db_path", "") or ""
    if explicit_db:
        # Single explicit DB → single-user mode (backward compatible).
        service = ControlService(Path(explicit_db))
        handler = make_control_handler(service, allowed_task_types=allowed)
        db_desc = str(explicit_db)
    else:
        # Multi-user: route to a per-user DB via the X-Rsim-User header.
        handler = make_control_handler(_per_user_service_factory(), allowed_task_types=allowed)
        db_desc = "per-user (RSIM_HOME/results/_control_<user>.db)"

    server = ThreadingHTTPServer((host, port), handler)
    print(f"Radar Sim control server: http://{host}:{port}/")
    print(f"Control DB: {db_desc}")
    if allowed:
        print(f"Allowed task types: {', '.join(sorted(allowed))}")
    else:
        print("Allowed task types: all (Mode B — full local+cluster)")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


def _per_user_service_factory():
    """Return a ``(user) -> ControlService`` that caches one service per user."""
    from core.user import control_db_path_for_user
    cache: dict[str, ControlService] = {}
    lock = __import__("threading").Lock()

    def factory(user: str) -> ControlService:
        with lock:
            if user not in cache:
                cache[user] = ControlService(control_db_path_for_user(user))
            return cache[user]

    return factory


def _run_create_job(args) -> int:
    payload = _parse_json_flag(getattr(args, "payload_json", "") or "", "payload")
    metadata = _parse_json_flag(getattr(args, "metadata_json", "") or "", "metadata")
    # Start from the explicit JSON payload so values like "project" survive even
    # when the matching CLI flag is at its empty default. CLI flags then layer on
    # top, but only when actually set (non-empty) — otherwise the create-job
    # subcommand's own --project default ("") would clobber a project passed via
    # --payload-json or via the global rsim --project.
    task_payload = dict(payload)
    cli_overrides = {
        "project": getattr(args, "project", "") or "",
        "config_path": getattr(args, "config_path", "") or "",
        "profile": getattr(args, "profile", "") or "",
        "backend": getattr(args, "backend", "") or "",
        "input_mf4": getattr(args, "input_mf4", "") or "",
        "input_path": getattr(args, "input_path", "") or "",
        "dataset": getattr(args, "dataset", "") or "",
        "output_mf4": getattr(args, "output_mf4", "") or "",
        "mode": getattr(args, "mode", "") or "",
        "run_id": getattr(args, "run_id", "") or "",
        "timeout": int(getattr(args, "timeout", 0) or 0),
        "limit": int(getattr(args, "limit", 0) or 0),
        "max_duration": int(getattr(args, "max_duration", 0) or 0),
        "stall_timeout": int(getattr(args, "stall_timeout", 0) or 0),
        "max_minutes": int(getattr(args, "max_minutes", 0) or 0),
        "clean": bool(getattr(args, "clean", False)),
        "deps": bool(getattr(args, "deps", False)),
        "dry_run": bool(getattr(args, "dry_run", False)),
        "execute": bool(getattr(args, "execute", False)),
        "copy_data": bool(getattr(args, "copy_data", False)),
        "copy_selena": bool(getattr(args, "copy_selena", False)),
        "no_progress": bool(getattr(args, "no_progress", False)),
        "select": bool(getattr(args, "select", False)),
        "no_retry": bool(getattr(args, "no_retry", False)),
        "no_wait": bool(getattr(args, "no_wait", False)),
        "no_fetch": bool(getattr(args, "no_fetch", False)),
        "required_signals": list(getattr(args, "required_signal", []) or []),
        "extra_args": list(getattr(args, "extra_arg", []) or []),
    }
    for key, value in cli_overrides.items():
        # Only let a CLI flag override the JSON payload when it was actually
        # provided (non-empty / non-default). This keeps --payload-json the
        # authoritative source for fields the CLI doesn't expose (e.g. project
        # when invoked through the global rsim --project).
        if value not in ("", [], 0, False):
            task_payload[key] = value
    task_payload = {key: value for key, value in task_payload.items() if value not in ("", [], 0, False)}
    return _print_json(_service_from_args(args).create_job(args.job_type, payload=task_payload, metadata=metadata))


def _service_from_args(args) -> ControlService:
    return ControlService(_db_path_from_args(args))


def _db_path_from_args(args) -> Path:
    db_path = getattr(args, "db_path", "") or ""
    if db_path:
        return Path(db_path)
    # Default: per-user DB (isolates jobs/logs between users on a shared server).
    from core.user import control_db_path_for_user
    return control_db_path_for_user()


def _parse_json_flag(raw: str, label: str) -> dict:
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} JSON must be an object")
    return value


def _ensure_utf8_stdout() -> None:
    """Force stdout to UTF-8 so JSON with non-ASCII (e.g. Chinese check output
    in task logs) doesn't crash on cp936/charmap Windows terminals.

    Mirrors the agent's UTF-8 fix (cli/agent.py) for the server CLI's print
    path. Safe no-op on POSIX where stdout is already UTF-8. Python 3.7+.
    """
    import sys
    stream = getattr(sys.stdout, "reconfigure", None)
    if stream is not None:
        try:
            stream(encoding="utf-8", errors="replace")
        except (TypeError, ValueError, OSError):
            pass


def _print_json(payload) -> int:
    _ensure_utf8_stdout()
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0
