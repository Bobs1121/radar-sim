"""SQLite-backed task persistence for build/sim/tcc tasks.

Stores task metadata + incremental log lines so the web frontend can recover
state after a page refresh: a task started before the refresh is still visible
(its status/logs queried from SQLite) even though the in-memory TaskRegistry
lost the frontend's task_id.

Two tables:
  tasks      — one row per task (metadata, status)
  task_logs  — one row per log line (task_id, seq, line), indexed for tail
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from core.config import get_data_root


def _db_path() -> Path:
    d = get_data_root() / "results"
    d.mkdir(parents=True, exist_ok=True)
    return d / "_tasks.db"


class TaskStore:
    """Thread-safe SQLite persistence for tasks + log lines."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = str(db_path or _db_path())
        self._lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        # check_same_thread=False: TaskRegistry threads share the store.
        return sqlite3.connect(self._db_path, check_same_thread=False)

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        project TEXT,
                        kind TEXT,
                        status TEXT,
                        started_at REAL,
                        finished_at REAL,
                        returncode INTEGER,
                        exe_path TEXT,
                        current_file TEXT,
                        files_done INTEGER,
                        files_total INTEGER,
                        errors TEXT,
                        log_seq INTEGER DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS task_logs (
                        task_id TEXT,
                        seq INTEGER,
                        line TEXT,
                        PRIMARY KEY (task_id, seq)
                    );
                    CREATE INDEX IF NOT EXISTS idx_task_logs_seq ON task_logs(task_id, seq);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def save_task(self, task: Any, new_lines: Optional[list[str]] = None) -> None:
        """Upsert task metadata + append new log lines.

        ``task`` is a BuildTask-like object (duck-typed: task_id/project/kind/...).
        ``new_lines`` are log lines appended since the last save_task call.
        """
        with self._lock:
            conn = self._conn()
            try:
                # Append new lines FIRST (using the pre-update log_seq as start offset).
                if new_lines:
                    start_seq = self._stored_log_count(conn, task.task_id)
                    rows = [(task.task_id, start_seq + i, line) for i, line in enumerate(new_lines)]
                    conn.executemany("INSERT OR IGNORE INTO task_logs (task_id, seq, line) VALUES (?,?,?)", rows)
                # Then upsert metadata (log_seq = total lines now stored).
                conn.execute(
                    """INSERT INTO tasks (task_id, project, kind, status, started_at, finished_at,
                       returncode, exe_path, current_file, files_done, files_total, errors, log_seq)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(task_id) DO UPDATE SET
                         project=excluded.project, kind=excluded.kind, status=excluded.status,
                         started_at=excluded.started_at, finished_at=excluded.finished_at,
                         returncode=excluded.returncode, exe_path=excluded.exe_path,
                         current_file=excluded.current_file, files_done=excluded.files_done,
                         files_total=excluded.files_total, errors=excluded.errors,
                         log_seq=excluded.log_seq""",
                    (task.task_id, task.project, task.kind, task.status, task.started_at,
                     task.finished_at, task.returncode, task.exe_path, task.current_file,
                     task.files_done, task.files_total, json.dumps(task.errors, ensure_ascii=False),
                     len(task.stdout_lines)),
                )
                conn.commit()
            finally:
                conn.close()

    def _stored_log_count(self, conn: sqlite3.Connection, task_id: str) -> int:
        row = conn.execute("SELECT log_seq FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def load_task(self, task_id: str) -> Optional[dict]:
        """Return a task dict (no full logs) or None if not found."""
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    """SELECT task_id, project, kind, status, started_at, finished_at,
                       returncode, exe_path, current_file, files_done, files_total, errors, log_seq
                       FROM tasks WHERE task_id=?""", (task_id,)
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        return {
            "task_id": row[0], "project": row[1], "kind": row[2], "status": row[3],
            "started_at": row[4], "finished_at": row[5], "returncode": row[6],
            "exe_path": row[7], "current_file": row[8], "files_done": row[9],
            "files_total": row[10],
            "errors": json.loads(row[11]) if row[11] else [],
            "total_lines": int(row[12] or 0),
        }

    def tail_logs(self, task_id: str, since: int = 0) -> list[str]:
        """Return log lines with seq >= since, in order."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT line FROM task_logs WHERE task_id=? AND seq>=? ORDER BY seq",
                    (task_id, since),
                ).fetchall()
            finally:
                conn.close()
        return [r[0] for r in rows]

    def list_tasks(self, limit: int = 20) -> list[dict]:
        """Return recent tasks (newest first), no logs."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    """SELECT task_id, project, kind, status, started_at, finished_at,
                       returncode, exe_path, current_file, files_done, files_total, log_seq
                       FROM tasks ORDER BY started_at DESC LIMIT ?""", (limit,)
                ).fetchall()
            finally:
                conn.close()
        return [{
            "task_id": r[0], "project": r[1], "kind": r[2], "status": r[3],
            "started_at": r[4], "finished_at": r[5], "returncode": r[6],
            "exe_path": r[7], "current_file": r[8], "files_done": r[9],
            "files_total": r[10], "total_lines": int(r[11] or 0),
        } for r in rows]


# Module-level singleton.
_STORE: Optional[TaskStore] = None


def get_store() -> TaskStore:
    global _STORE
    if _STORE is None:
        _STORE = TaskStore()
    return _STORE
