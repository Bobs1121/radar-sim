"""Durable terminal-result outbox for the Windows Agent.

The simulation process and the Linux control plane are separate failure
domains.  A local run may finish successfully while the callback connection is
down.  This small SQLite outbox records the path-free terminal payload before
the HTTP request, so an Agent restart can deliver the result without launching
Selena again.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.agent_store_paths import default_agent_binding_db_path


class AgentResultOutboxError(ValueError):
    """The local result outbox cannot safely persist or read a callback."""


@dataclass(frozen=True)
class PendingAgentResult:
    task_id: str
    attempt: int
    agent_id: str
    status: str
    returncode: int
    result: dict[str, Any]
    attempts: int
    last_error: str
    created_at: float
    updated_at: float


class AgentResultOutbox:
    """SQLite-backed at-least-once result delivery queue."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        configured = str(os.environ.get("RSIM_AGENT_RESULT_OUTBOX_DB") or "").strip()
        default = (
            Path(configured).expanduser()
            if configured
            else default_agent_binding_db_path().with_name("result-outbox.db")
        )
        self.db_path = Path(db_path) if db_path is not None else default
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_result_outbox (
                    task_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    returncode INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(task_id, attempt)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_result_outbox_updated "
                "ON agent_result_outbox(updated_at, task_id, attempt)"
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def put(
        self,
        task_id: str,
        *,
        attempt: int = 0,
        agent_id: str,
        status: str,
        returncode: int,
        result: dict[str, Any],
    ) -> PendingAgentResult:
        task_id = str(task_id or "").strip()
        agent_id = str(agent_id or "").strip()
        status = str(status or "").strip()
        attempt = int(attempt or 0)
        if not task_id or not agent_id or not status or attempt <= 0:
            raise AgentResultOutboxError("result outbox identity is invalid")
        if not isinstance(result, dict):
            raise AgentResultOutboxError("result outbox payload is invalid")
        try:
            encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise AgentResultOutboxError("result outbox payload is not JSON serializable") from exc
        now = float(self._now_fn())
        if now < 0:
            raise AgentResultOutboxError("result outbox clock is invalid")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_result_outbox(
                    task_id,attempt,agent_id,status,returncode,result_json,
                    attempts,last_error,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,0,'',?,?)
                """,
                (task_id, attempt, agent_id, status, int(returncode), encoded, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM agent_result_outbox WHERE task_id=? AND attempt=?",
                (task_id, attempt),
            ).fetchone()
        if row is None:
            raise AgentResultOutboxError("result outbox entry was not persisted")
        if (
            str(row["agent_id"]) != agent_id
            or str(row["status"]) != status
            or int(row["returncode"]) != int(returncode)
            or str(row["result_json"]) != encoded
        ):
            raise AgentResultOutboxError(
                "result outbox identity already contains a different terminal payload"
            )
        return _row_to_pending(row)

    def pending(self, *, limit: int = 32) -> list[PendingAgentResult]:
        size = max(1, min(int(limit), 256))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_result_outbox "
                "ORDER BY updated_at, task_id, attempt LIMIT ?",
                (size,),
            ).fetchall()
        return [_row_to_pending(row) for row in rows]

    def mark_failure(self, task_id: str, attempt: int, error: str) -> None:
        now = float(self._now_fn())
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE agent_result_outbox
                SET attempts=attempts+1,last_error=?,updated_at=?
                WHERE task_id=? AND attempt=?
                """,
                (str(error or "result callback failed")[:1000], now, str(task_id), int(attempt)),
            )
            conn.commit()

    def remove(self, task_id: str, attempt: int = 0) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM agent_result_outbox WHERE task_id=? AND attempt=?",
                (str(task_id), int(attempt)),
            )
            conn.commit()


def _row_to_pending(row: sqlite3.Row) -> PendingAgentResult:
    try:
        result = json.loads(str(row["result_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentResultOutboxError("result outbox payload is corrupt") from exc
    if not isinstance(result, dict):
        raise AgentResultOutboxError("result outbox payload is not an object")
    return PendingAgentResult(
        task_id=str(row["task_id"]),
        attempt=int(row["attempt"] or 0),
        agent_id=str(row["agent_id"]),
        status=str(row["status"]),
        returncode=int(row["returncode"]),
        result=dict(result),
        attempts=int(row["attempts"] or 0),
        last_error=str(row["last_error"] or ""),
        created_at=float(row["created_at"] or 0),
        updated_at=float(row["updated_at"] or 0),
    )


__all__ = ["AgentResultOutbox", "AgentResultOutboxError", "PendingAgentResult"]
