"""Adapter bridging the web console's BuildTaskRegistry-shaped contract to the
control-plane (ControlService) job/task model.

The web frontend (web/app.js) polls `/api/build/status` and `/api/sim/status`
expecting the dict shape produced by ``TaskRegistry.tail()`` (11 fields incl.
``status`` in {queued,running,success,failed,cancelled}). The control plane
uses a different status vocabulary (``succeeded`` vs ``success``) and a
log_id cursor. This module translates one to the other so the frontend needs
zero changes when ``rsim web`` routes build/sim/tcc through the control plane.
"""

from __future__ import annotations

from typing import Any, Optional

from core.control_service import ControlService

# Control-plane statuses that map onto BuildTask's vocabulary.
_STATUS_MAP = {
    "queued": "queued",
    "running": "running",
    "succeeded": "success",
    "failed": "failed",
    "cancelled": "cancelled",
    # job-level pre-cancel marker (tasks still running) — surface as running.
    "cancel_requested": "running",
}


def _service() -> ControlService:
    """Return the control service for the current user (embedded web: single user).

    Uses the per-user DB path so jobs/logs are isolated between users even when
    sharing a machine via RSIM_HOME. ``set_service`` overrides for tests.
    """
    global _SERVICE
    if _SERVICE is None:
        from core.user import control_db_path_for_user
        _SERVICE = ControlService(control_db_path_for_user())
    return _SERVICE


_SERVICE: Optional[ControlService] = None
_REMOTE = None  # RemoteControlClient | None


def set_service(service: Optional[ControlService]) -> None:
    """Inject a control service (used by tests and ``rsim web`` embedded mode)."""
    global _SERVICE, _REMOTE
    _SERVICE = service
    _REMOTE = None


def set_remote_client(client) -> None:
    """Inject a remote control client (used by ``rsim web --server-url`` mode).

    When set, all operations forward to the remote server over HTTP with the
    caller's user identity. Mutually exclusive with ``set_service``.
    """
    global _SERVICE, _REMOTE
    _REMOTE = client
    _SERVICE = None


def _map_status(control_status: str) -> str:
    return _STATUS_MAP.get(control_status, control_status)


def start_build_via_control(project: str, *, mode: str = "RelWithDebInfo", clean: bool = False) -> str:
    """Create a build job; return job_id (the frontend treats it as task_id)."""
    payload = {"project": project, "mode": mode, "clean": bool(clean)}
    if _REMOTE:
        return _REMOTE.create_job("local.build_selena", payload=payload)["job_id"]
    job = _service().create_job("local.build_selena", payload=payload)
    return job["job_id"]


def start_sim_via_control(project: str, *, backend: str, data_path: str, dry_run: bool = False) -> str:
    """Create a simulation job; return job_id."""
    payload = {
        "project": project,
        "input_mf4": data_path,
        "input_path": data_path,
        "backend": backend,
        "dry_run": bool(dry_run),
    }
    if _REMOTE:
        return _REMOTE.create_job("local.run_sim", payload=payload)["job_id"]
    job = _service().create_job("local.run_sim", payload=payload)
    return job["job_id"]


def start_tcc_via_control(project: str, action: str, toolcollection: str = "") -> str:
    """Create a TCC job; return job_id.

    ``action`` is one of bootstrap_itc2 / install_toolcollection / auto_repair_all
    (the BuildTask action names). Maps to task_type ``tcc.<action>``.
    """
    task_type = f"tcc.{action}"
    payload = {"project": project}
    if toolcollection:
        payload["toolcollection"] = toolcollection
    if _REMOTE:
        return _REMOTE.create_job(task_type, payload=payload)["job_id"]
    job = _service().create_job(task_type, payload=payload)
    return job["job_id"]


def _tail_from_job_and_logs(job_id: str, job: dict, logs: dict, since: int) -> dict[str, Any]:
    """Build the 11-field tail dict from a job dict + logs dict (shared by local/remote)."""
    tasks = job.get("tasks") or []
    if not tasks:
        return {"found": False}
    task = tasks[0]

    entries = logs.get("entries") or []
    lines = [str(entry["message"]) for entry in entries]
    next_since = logs.get("next_since") or since or 0

    started = float(task.get("started_at") or task.get("created_at") or 0.0)
    completed = float(task.get("completed_at") or 0.0)
    duration = round((completed or _now()) - started, 1) if started else 0.0

    result = task.get("result") or {}
    return {
        "found": True,
        "task_id": job_id,
        "status": _map_status(str(task.get("status") or "")),
        "returncode": task.get("returncode"),
        "lines": lines,
        "total_lines": next_since,
        "errors": _extract_errors(result, task),
        "exe_path": str(result.get("exe_path") or ""),
        "current_file": str(result.get("current_file") or ""),
        "files_done": int(result.get("files_done") or 0),
        "files_total": int(result.get("files_total") or 0),
        "duration_sec": duration,
    }


def tail_via_control(job_id: str, since: int = 0) -> dict[str, Any]:
    """Return a BuildTask-shaped tail dict for a control-plane job.

    ``since`` is a log_id cursor (the frontend advances it with ``total_lines``).
    """
    if _REMOTE:
        from core.remote_control import RemoteControlError
        try:
            job = _REMOTE.get_job(job_id)
            logs = _REMOTE.get_logs(job_id, since=since, limit=500)
        except RemoteControlError as exc:
            if exc.status == 404:
                return {"found": False}
            raise
        return _tail_from_job_and_logs(job_id, job, logs, since)

    service = _service()
    try:
        job = service.get_job(job_id)
    except KeyError:
        return {"found": False}
    logs = service.get_logs(job_id=job_id, since=int(since or 0), limit=500)
    return _tail_from_job_and_logs(job_id, job, logs, since)


def cancel_via_control(job_id: str) -> bool:
    """Cancel a control-plane job; True if the cancel was accepted."""
    if _REMOTE:
        from core.remote_control import RemoteControlError
        try:
            job = _REMOTE.cancel_job(job_id)
        except RemoteControlError as exc:
            if exc.status == 404:
                return False
            raise
        return bool(job.get("cancel_requested"))
    try:
        job = _service().cancel_job(job_id)
    except KeyError:
        return False
    return bool(job.get("cancel_requested"))


def list_jobs_via_control(limit: int = 20) -> list[dict[str, Any]]:
    """List recent control-plane jobs in BuildTask.list_tasks shape."""
    if _REMOTE:
        rows = _REMOTE.list_jobs(limit=limit)
    else:
        service = _service()
        rows = service.list_jobs(limit=limit) if hasattr(service, "list_jobs") else []
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({
            "task_id": row.get("job_id") or row.get("task_id"),
            "project": (row.get("payload") or {}).get("project", ""),
            "kind": row.get("job_type", ""),
            "status": _map_status(str(row.get("status") or "")),
            "started_at": float(row.get("created_at") or 0.0),
            "finished_at": float(row.get("completed_at") or 0.0),
            "returncode": row.get("returncode"),
            "exe_path": "",
            "current_file": "",
            "files_done": 0,
            "files_total": 0,
            "total_lines": 0,
        })
    return out


def list_agents_via_control() -> list[dict[str, Any]]:
    """List registered agents for the web observability panel.

    Works in both embedded (local ``ControlService``) and remote
    (``RemoteControlClient``) modes. Returns the agent shape as-is from the
    control plane (agent_id/name/status/hostname/capabilities/last_heartbeat/
    current_task_id).
    """
    if _REMOTE:
        return _REMOTE.list_agents()
    service = _service()
    return service.list_agents() if hasattr(service, "list_agents") else []


def _extract_errors(result: dict, task: dict) -> list[str]:
    err = result.get("error")
    if err:
        return [str(err)]
    # A failed task with no explicit error string — synthesize one.
    if str(task.get("status") or "") == "failed":
        return [f"task failed (returncode={task.get('returncode')})"]
    return []


def _now() -> float:
    import time
    return time.time()
