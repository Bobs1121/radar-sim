"""Minimal persistent control-plane service for agents, jobs, and task logs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled"}
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}


def _data_root() -> Path:
    """Return the data root (follows RSIM_HOME; stdlib-only, avoids core.config)."""
    import os
    home = os.environ.get("RSIM_HOME", "").strip()
    if home:
        return Path(home).expanduser()
    # Fallback when RSIM_HOME is unset. In a normal repo checkout, __file__ is
    # core/control_service.py → parent.parent is the repo root. But inside a
    # zipapp (.pyz), __file__ points *into* the archive
    # (e.g. /tmp/rsim_server.pyz/core/control_service.py) and parent.parent is
    # the .pyz itself — creating ./results under it fails with NotADirectory.
    # So for zipapp deployment, fall back to a user home dir instead.
    here = Path(__file__).resolve()
    # Detect zipapp: __file__ is /path/to/archive.pyz/core/control_service.py,
    # so one of the *parent* path components is the .pyz file itself (suffix
    # .pyz). Neither here.suffix (.py) nor ".pyz" in here.parts matches a
    # filename like "rsim_server.pyz", so check each parent's suffix.
    if any(p.suffix == ".pyz" for p in here.parents):
        return Path.home() / ".rsim"
    return here.parent.parent


def default_control_db_path() -> Path:
    """Return the default SQLite database path for the control service."""
    results_dir = _data_root() / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir / "_control.db"


class ControlService:
    """Thread-safe SQLite service backing the minimal job/agent control plane."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._db_path = str(db_path or default_control_db_path())
        self._lock = threading.RLock()
        self._now_fn = now_fn or time.time
        self._init_schema()

    def _now(self) -> float:
        return float(self._now_fn())

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Wait up to 5s on SQLite lock contention (e.g. WAL checkpoint) before
        # raising SQLITE_BUSY. All access is serialized under self._lock so this
        # rarely fires, but it turns a rare transient error into a non-issue.
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(
                    """
                    PRAGMA journal_mode=WAL;

                    CREATE TABLE IF NOT EXISTS agents (
                        agent_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        hostname TEXT NOT NULL,
                        capabilities_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        registered_at REAL NOT NULL,
                        last_heartbeat REAL NOT NULL,
                        current_task_id TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        job_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        completed_at REAL NOT NULL DEFAULT 0,
                        cancel_requested INTEGER NOT NULL DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL,
                        task_type TEXT NOT NULL,
                        order_index INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        assigned_agent_id TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        claimed_at REAL NOT NULL DEFAULT 0,
                        started_at REAL NOT NULL DEFAULT 0,
                        completed_at REAL NOT NULL DEFAULT 0,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        returncode INTEGER,
                        FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_tasks_status_created
                        ON tasks(status, created_at, order_index);
                    CREATE INDEX IF NOT EXISTS idx_tasks_job
                        ON tasks(job_id, order_index);

                    CREATE TABLE IF NOT EXISTS task_logs (
                        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        stream TEXT NOT NULL,
                        message TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_task_logs_task
                        ON task_logs(task_id, log_id);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def register_agent(
        self,
        name: str,
        *,
        agent_id: str = "",
        platform: str = "",
        hostname: str = "",
        capabilities: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        now = self._now()
        capabilities = list(capabilities or [])
        metadata = dict(metadata or {})
        agent_id = str(agent_id or f"agent_{uuid.uuid4().hex[:12]}")
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO agents (
                        agent_id, name, platform, hostname, capabilities_json,
                        metadata_json, status, registered_at, last_heartbeat, current_task_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                        name=excluded.name,
                        platform=excluded.platform,
                        hostname=excluded.hostname,
                        capabilities_json=excluded.capabilities_json,
                        metadata_json=excluded.metadata_json,
                        status=excluded.status,
                        last_heartbeat=excluded.last_heartbeat,
                        current_task_id=excluded.current_task_id
                    """,
                    (
                        agent_id,
                        name or agent_id,
                        platform,
                        hostname,
                        self._dumps(capabilities),
                        self._dumps(metadata),
                        "idle",
                        now,
                        now,
                        "",
                    ),
                )
                conn.commit()
                return self._get_agent_locked(conn, agent_id)
            finally:
                conn.close()

    def heartbeat(
        self,
        agent_id: str,
        *,
        status: str = "",
        current_task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        now = self._now()
        with self._lock:
            conn = self._conn()
            try:
                agent = self._get_agent_locked(conn, agent_id)
                next_status = status or agent["status"]
                next_task_id = current_task_id if current_task_id is not None else agent["current_task_id"]
                next_metadata = dict(agent["metadata"])
                if metadata:
                    next_metadata.update(metadata)
                conn.execute(
                    """
                    UPDATE agents
                    SET status=?, current_task_id=?, metadata_json=?, last_heartbeat=?
                    WHERE agent_id=?
                    """,
                    (next_status, next_task_id, self._dumps(next_metadata), now, agent_id),
                )
                conn.commit()
                task_id = next_task_id or agent["current_task_id"]
                cancel_requested = False
                task = None
                if task_id:
                    task = self._get_task_locked(conn, task_id)
                    cancel_requested = bool(task["cancel_requested"])
                return {
                    "agent": self._get_agent_locked(conn, agent_id),
                    "cancel_requested": cancel_requested,
                    "task": task,
                }
            finally:
                conn.close()

    def create_job(
        self,
        job_type: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        tasks: Optional[list[dict[str, Any]]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not job_type:
            raise ValueError("job_type is required")
        payload = dict(payload or {})
        metadata = dict(metadata or {})
        task_specs = [dict(item) for item in (tasks or [])]
        if not task_specs:
            task_specs = [{"task_type": job_type, "payload": payload}]
        now = self._now()
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id, job_type, status, payload_json, metadata_json,
                        result_json, created_at, updated_at, completed_at, cancel_requested
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        job_type,
                        "queued",
                        self._dumps(payload),
                        self._dumps(metadata),
                        self._dumps({}),
                        now,
                        now,
                        0.0,
                        0,
                    ),
                )
                for index, spec in enumerate(task_specs):
                    task_type = str(spec.get("task_type") or job_type)
                    task_payload = dict(spec.get("payload") or payload)
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            task_id, job_id, task_type, order_index, status,
                            payload_json, result_json, assigned_agent_id,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"task_{uuid.uuid4().hex[:12]}",
                            job_id,
                            task_type,
                            index,
                            "queued",
                            self._dumps(task_payload),
                            self._dumps({}),
                            "",
                            now,
                            now,
                        ),
                    )
                conn.commit()
                return self._get_job_locked(conn, job_id)
            finally:
                conn.close()

    def claim_next_task(self, agent_id: str) -> Optional[dict[str, Any]]:
        now = self._now()
        with self._lock:
            conn = self._conn()
            try:
                agent = self._get_agent_locked(conn, agent_id)
                current_task_id = str(agent.get("current_task_id") or "")
                if current_task_id:
                    current_task = self._get_task_locked(conn, current_task_id)
                    if current_task["status"] not in TERMINAL_TASK_STATUSES:
                        return current_task

                capabilities = list(agent.get("capabilities") or [])
                rows = conn.execute(
                    """
                    SELECT task_id, job_id, task_type, order_index
                    FROM tasks
                    WHERE status='queued'
                    ORDER BY created_at ASC, order_index ASC, task_id ASC
                    """
                ).fetchall()
                for row in rows:
                    if not self._capability_matches(row["task_type"], capabilities):
                        continue
                    if not self._task_is_ready_to_claim_locked(conn, row):
                        continue
                    updated = conn.execute(
                        """
                        UPDATE tasks
                        SET status='running', assigned_agent_id=?, claimed_at=?, started_at=?,
                            updated_at=?, attempt_count=attempt_count+1
                        WHERE task_id=? AND status='queued'
                        """,
                        (agent_id, now, now, now, row["task_id"]),
                    )
                    if updated.rowcount != 1:
                        continue
                    conn.execute(
                        """
                        UPDATE agents
                        SET status='busy', current_task_id=?, last_heartbeat=?
                        WHERE agent_id=?
                        """,
                        (row["task_id"], now, agent_id),
                    )
                    self._refresh_job_status_locked(conn, self._task_job_id_locked(conn, row["task_id"]), now)
                    conn.commit()
                    return self._get_task_locked(conn, row["task_id"])

                conn.execute(
                    "UPDATE agents SET status='idle', current_task_id='', last_heartbeat=? WHERE agent_id=?",
                    (now, agent_id),
                )
                conn.commit()
                return None
            finally:
                conn.close()

    def reclaim_stale_tasks(
        self,
        *,
        stale_after_seconds: float = 300.0,
        max_attempts: Optional[int] = 3,
    ) -> list[dict[str, Any]]:
        """Requeue running tasks whose agent has gone silent (dead-agent recovery).

        A task stuck in ``running`` whose assigned agent's ``last_heartbeat`` is
        older than ``stale_after_seconds`` is reset to ``queued`` so another
        agent can claim it. Tasks whose ``attempt_count`` already exceeds
        ``max_attempts`` are instead marked ``failed`` (the agent keeps crashing
        on them — don't loop forever). Returns the list of reclaimed tasks.

        This is the only recovery path for tasks orphaned by a crashed agent;
        without it they stay ``running`` forever. Idempotent: safe to call
        periodically (e.g. from the server's serve loop or an admin CLI).
        """
        now = self._now()
        cutoff = now - float(stale_after_seconds)
        with self._lock:
            conn = self._conn()
            try:
                # Running tasks whose agent hasn't heartbeat since the cutoff.
                rows = conn.execute(
                    """
                    SELECT t.task_id, t.job_id, t.assigned_agent_id, t.attempt_count
                    FROM tasks t
                    LEFT JOIN agents a ON a.agent_id = t.assigned_agent_id
                    WHERE t.status='running'
                      AND (a.agent_id IS NULL OR a.last_heartbeat < ?)
                    """,
                    (cutoff,),
                ).fetchall()
                reclaimed: list[dict[str, Any]] = []
                for row in rows:
                    attempts = int(row["attempt_count"] or 0)
                    if max_attempts is not None and attempts >= max_attempts:
                        final = "failed"
                        conn.execute(
                            """
                            UPDATE tasks
                            SET status=?, result_json=?, updated_at=?, completed_at=?,
                                returncode=?
                            WHERE task_id=?
                            """,
                            (
                                final,
                                self._dumps({
                                    "error": f"task exceeded max_attempts ({max_attempts}) "
                                             f"after agent went silent"
                                }),
                                now, now, -1, row["task_id"],
                            ),
                        )
                    else:
                        final = "queued"
                        conn.execute(
                            """
                            UPDATE tasks
                            SET status='queued', assigned_agent_id='', claimed_at=0,
                                started_at=0, updated_at=?
                            WHERE task_id=?
                            """,
                            (now, row["task_id"]),
                        )
                    # Free the dead agent's current_task_id if it still points here.
                    if row["assigned_agent_id"]:
                        conn.execute(
                            """
                            UPDATE agents
                            SET status='idle', current_task_id=''
                            WHERE agent_id=? AND current_task_id=?
                            """,
                            (row["assigned_agent_id"], row["task_id"]),
                        )
                    self._refresh_job_status_locked(conn, row["job_id"], now)
                    reclaimed.append({
                        "task_id": row["task_id"],
                        "job_id": row["job_id"],
                        "agent_id": row["assigned_agent_id"],
                        "new_status": final,
                        "attempt_count": attempts,
                    })
                conn.commit()
                return reclaimed
            finally:
                conn.close()

    def append_logs(
        self,
        task_id: str,
        lines: list[str] | tuple[str, ...] | str,
        *,
        stream: str = "stdout",
    ) -> dict[str, Any]:
        if isinstance(lines, str):
            lines = [lines]
        entries = [str(line) for line in lines if str(line)]
        now = self._now()
        with self._lock:
            conn = self._conn()
            try:
                task = self._get_task_locked(conn, task_id)
                if entries:
                    conn.executemany(
                        """
                        INSERT INTO task_logs (task_id, stream, message, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(task_id, stream, message, now) for message in entries],
                    )
                    conn.execute("UPDATE tasks SET updated_at=? WHERE task_id=?", (now, task_id))
                    self._touch_job_locked(conn, task["job_id"], now)
                    conn.commit()
                return {"task_id": task_id, "appended": len(entries)}
            finally:
                conn.close()

    def submit_task_result(
        self,
        task_id: str,
        *,
        agent_id: str = "",
        status: str = "",
        returncode: Optional[int] = None,
        result: Optional[dict[str, Any]] = None,
        error: str = "",
    ) -> dict[str, Any]:
        now = self._now()
        result = dict(result or {})
        if error:
            result.setdefault("error", error)
        with self._lock:
            conn = self._conn()
            try:
                task = self._get_task_locked(conn, task_id)
                if task["status"] in TERMINAL_TASK_STATUSES:
                    raise ValueError(f"task already completed: {task_id}")
                assigned_agent_id = str(task["assigned_agent_id"] or "")
                if agent_id and assigned_agent_id and agent_id != assigned_agent_id:
                    raise ValueError(f"task {task_id} is assigned to {assigned_agent_id}")
                effective_agent_id = agent_id or assigned_agent_id
                final_status = self._resolve_task_result_status(task, status=status, returncode=returncode)

                conn.execute(
                    """
                    UPDATE tasks
                    SET status=?, result_json=?, updated_at=?, completed_at=?, returncode=?
                    WHERE task_id=?
                    """,
                    (
                        final_status,
                        self._dumps(result),
                        now,
                        now,
                        returncode,
                        task_id,
                    ),
                )
                if final_status in {"failed", "cancelled"}:
                    self._cancel_remaining_tasks_locked(conn, task["job_id"], now, exclude_task_id=task_id)
                if effective_agent_id:
                    conn.execute(
                        """
                        UPDATE agents
                        SET status='idle', current_task_id='', last_heartbeat=?
                        WHERE agent_id=?
                        """,
                        (now, effective_agent_id),
                    )
                self._refresh_job_status_locked(conn, task["job_id"], now)
                conn.commit()
                return self._get_job_locked(conn, task["job_id"])
            finally:
                conn.close()

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        now = self._now()
        with self._lock:
            conn = self._conn()
            try:
                job = self._get_job_locked(conn, job_id)
                if job["status"] in TERMINAL_JOB_STATUSES:
                    return job
                conn.execute(
                    "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE job_id=?",
                    (now, job_id),
                )
                tasks = conn.execute(
                    """
                    SELECT task_id, status
                    FROM tasks
                    WHERE job_id=? AND status NOT IN ('succeeded', 'failed', 'cancelled')
                    """,
                    (job_id,),
                ).fetchall()
                for task in tasks:
                    if task["status"] == "queued":
                        conn.execute(
                            """
                            UPDATE tasks
                            SET status='cancelled', cancel_requested=1, updated_at=?, completed_at=?
                            WHERE task_id=?
                            """,
                            (now, now, task["task_id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE tasks SET cancel_requested=1, updated_at=? WHERE task_id=?",
                            (now, task["task_id"]),
                        )
                self._refresh_job_status_locked(conn, job_id, now)
                conn.commit()
                return self._get_job_locked(conn, job_id)
            finally:
                conn.close()

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._conn()
            try:
                return self._get_job_locked(conn, job_id)
            finally:
                conn.close()

    def get_logs(
        self,
        *,
        job_id: str = "",
        task_id: str = "",
        since: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        if not job_id and not task_id:
            raise ValueError("job_id or task_id is required")
        with self._lock:
            conn = self._conn()
            try:
                if job_id:
                    self._get_job_locked(conn, job_id)
                if task_id:
                    self._get_task_locked(conn, task_id)
                params: list[Any] = [int(since or 0)]
                query = [
                    """
                    SELECT task_logs.log_id, task_logs.task_id, task_logs.stream,
                           task_logs.message, task_logs.created_at
                    FROM task_logs
                    """
                ]
                if job_id:
                    query.append("JOIN tasks ON tasks.task_id = task_logs.task_id")
                where = ["task_logs.log_id > ?"]
                if task_id:
                    where.append("task_logs.task_id = ?")
                    params.append(task_id)
                if job_id:
                    where.append("tasks.job_id = ?")
                    params.append(job_id)
                query.append("WHERE " + " AND ".join(where))
                query.append("ORDER BY task_logs.log_id ASC LIMIT ?")
                params.append(int(limit or 200))
                rows = conn.execute("\n".join(query), params).fetchall()
                entries = [
                    {
                        "log_id": row["log_id"],
                        "task_id": row["task_id"],
                        "stream": row["stream"],
                        "message": row["message"],
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ]
                next_since = since
                if entries:
                    next_since = entries[-1]["log_id"]
                return {"entries": entries, "next_since": next_since}
            finally:
                conn.close()

    def list_agents(self) -> list[dict[str, Any]]:
        """Return all registered agents, newest registration first.

        Used by the observability endpoint (``GET /api/agents``) and
        ``rsim server list-agents`` so operators can see which agents are
        connected, their last heartbeat, and what they're currently running.
        """
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    """
                    SELECT agent_id FROM agents
                    ORDER BY registered_at DESC, agent_id DESC
                    """,
                ).fetchall()
                return [self._get_agent_locked(conn, row["agent_id"]) for row in rows]
            finally:
                conn.close()

    def list_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent jobs newest-first with payload/status (no tasks/logs)."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    """
                    SELECT job_id, job_type, status, payload_json, metadata_json,
                           created_at, updated_at, completed_at, cancel_requested
                    FROM jobs
                    ORDER BY created_at DESC, job_id DESC
                    LIMIT ?
                    """,
                    (int(limit or 20),),
                ).fetchall()
                out: list[dict[str, Any]] = []
                for row in rows:
                    out.append({
                        "job_id": row["job_id"],
                        "job_type": row["job_type"],
                        "status": row["status"],
                        "payload": self._loads(row["payload_json"]),
                        "metadata": self._loads(row["metadata_json"]),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "completed_at": row["completed_at"],
                        "cancel_requested": bool(row["cancel_requested"]),
                    })
                return out
            finally:
                conn.close()

    def _get_agent_locked(self, conn: sqlite3.Connection, agent_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown agent: {agent_id}")
        return {
            "agent_id": row["agent_id"],
            "name": row["name"],
            "platform": row["platform"],
            "hostname": row["hostname"],
            "capabilities": self._loads(row["capabilities_json"]),
            "metadata": self._loads(row["metadata_json"]),
            "status": row["status"],
            "registered_at": row["registered_at"],
            "last_heartbeat": row["last_heartbeat"],
            "current_task_id": row["current_task_id"],
        }

    def _get_job_locked(self, conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown job: {job_id}")
        task_rows = conn.execute(
            "SELECT * FROM tasks WHERE job_id=? ORDER BY order_index ASC, task_id ASC",
            (job_id,),
        ).fetchall()
        return {
            "job_id": row["job_id"],
            "job_type": row["job_type"],
            "status": row["status"],
            "payload": self._loads(row["payload_json"]),
            "metadata": self._loads(row["metadata_json"]),
            "result": self._loads(row["result_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "cancel_requested": bool(row["cancel_requested"]),
            "tasks": [self._task_row_to_dict(task_row) for task_row in task_rows],
        }

    def _get_task_locked(self, conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown task: {task_id}")
        return self._task_row_to_dict(row)

    def _task_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "job_id": row["job_id"],
            "task_type": row["task_type"],
            "order_index": row["order_index"],
            "status": row["status"],
            "payload": self._loads(row["payload_json"]),
            "result": self._loads(row["result_json"]),
            "assigned_agent_id": row["assigned_agent_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "claimed_at": row["claimed_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "cancel_requested": bool(row["cancel_requested"]),
            "attempt_count": row["attempt_count"],
            "returncode": row["returncode"],
        }

    def _touch_job_locked(self, conn: sqlite3.Connection, job_id: str, now: float) -> None:
        conn.execute("UPDATE jobs SET updated_at=? WHERE job_id=?", (now, job_id))

    def _task_job_id_locked(self, conn: sqlite3.Connection, task_id: str) -> str:
        row = conn.execute("SELECT job_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown task: {task_id}")
        return str(row["job_id"])

    def _refresh_job_status_locked(self, conn: sqlite3.Connection, job_id: str, now: float) -> None:
        job_row = conn.execute(
            "SELECT cancel_requested FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not job_row:
            raise KeyError(f"unknown job: {job_id}")
        tasks = conn.execute(
            "SELECT status, cancel_requested, completed_at, result_json, returncode FROM tasks WHERE job_id=?",
            (job_id,),
        ).fetchall()
        statuses = [str(task["status"]) for task in tasks]
        job_cancel_requested = bool(job_row["cancel_requested"])
        max_completed_at = max([float(task["completed_at"] or 0.0) for task in tasks] or [0.0])
        if any(status == "running" for status in statuses):
            next_status = "cancel_requested" if job_cancel_requested or any(bool(task["cancel_requested"]) for task in tasks) else "running"
            completed_at = 0.0
        elif any(status == "queued" for status in statuses):
            next_status = "cancel_requested" if job_cancel_requested else "queued"
            completed_at = 0.0
        elif statuses and all(status == "succeeded" for status in statuses):
            next_status = "succeeded"
            completed_at = max_completed_at or now
        elif statuses and all(status == "cancelled" for status in statuses):
            next_status = "cancelled"
            completed_at = max_completed_at or now
        else:
            next_status = "failed"
            completed_at = max_completed_at or now

        result: dict[str, Any] = {}
        if tasks and next_status in TERMINAL_TASK_STATUSES:
            result = {
                "task_results": [
                    {
                        "status": task["status"],
                        "returncode": task["returncode"],
                        "result": self._loads(task["result_json"]),
                    }
                    for task in tasks
                ]
            }
        conn.execute(
            """
            UPDATE jobs
            SET status=?, updated_at=?, completed_at=?, result_json=?
            WHERE job_id=?
            """,
            (next_status, now, completed_at, self._dumps(result), job_id),
        )

    def _task_is_ready_to_claim_locked(self, conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
        blockers = conn.execute(
            """
            SELECT status
            FROM tasks
            WHERE job_id=? AND order_index<?
            ORDER BY order_index ASC, task_id ASC
            """,
            (row["job_id"], row["order_index"]),
        ).fetchall()
        return all(str(blocker["status"]) == "succeeded" for blocker in blockers)

    def _resolve_task_result_status(
        self,
        task: dict[str, Any],
        *,
        status: str = "",
        returncode: Optional[int] = None,
    ) -> str:
        normalized_status = str(status or "")
        if normalized_status and normalized_status not in TERMINAL_TASK_STATUSES:
            raise ValueError(f"unsupported task status: {normalized_status}")
        if task["cancel_requested"]:
            if normalized_status in ("", "cancelled"):
                return "cancelled"
            if normalized_status == "succeeded":
                return "succeeded"
            if normalized_status == "failed" or returncode != 0:
                return "cancelled"
        if normalized_status:
            return normalized_status
        if returncode == 0:
            return "succeeded"
        return "failed"

    def _cancel_remaining_tasks_locked(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        now: float,
        *,
        exclude_task_id: str,
    ) -> None:
        rows = conn.execute(
            """
            SELECT task_id, status
            FROM tasks
            WHERE job_id=? AND task_id<>? AND status NOT IN ('succeeded', 'failed', 'cancelled')
            """,
            (job_id, exclude_task_id),
        ).fetchall()
        for row in rows:
            if row["status"] == "queued":
                conn.execute(
                    """
                    UPDATE tasks
                    SET status='cancelled', cancel_requested=1, updated_at=?, completed_at=?
                    WHERE task_id=?
                    """,
                    (now, now, row["task_id"]),
                )
                continue
            conn.execute(
                "UPDATE tasks SET cancel_requested=1, updated_at=? WHERE task_id=?",
                (now, row["task_id"]),
            )

    @staticmethod
    def _capability_matches(task_type: str, capabilities: list[str]) -> bool:
        if not capabilities or "*" in capabilities:
            return True
        for capability in capabilities:
            if capability == task_type:
                return True
            if capability.endswith(".*") and task_type.startswith(capability[:-1]):
                return True
        return False

    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _loads(value: str) -> Any:
        return json.loads(value) if value else {}
