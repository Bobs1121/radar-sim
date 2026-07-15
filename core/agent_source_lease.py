"""Persistent Windows Agent lease for an isolated Selena branch worktree."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.agent_bindings import AgentBindingStore
from core.repo import (
    DetachedWorktreeHandle,
    RepoSourceError,
    cleanup_detached_worktree,
    inspect_workspace,
    prepare_detached_worktree,
)


class AgentSourceLeaseError(ValueError):
    """Path-free isolated source lease error."""


_LEASE_RE = re.compile(r"^source-lease:sha256:[0-9a-f]{64}$")


def default_agent_source_lease_db_path() -> Path:
    home = str(os.environ.get("RSIM_HOME") or "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".rsim"
    return base / "agent" / "source_leases.db"


@dataclass(frozen=True)
class AgentSourceLease:
    lease_id: str
    prepare_stage_id: str
    prepare_attempt: int
    project: str
    workspace_binding_id: str
    requested_ref: str
    commit: str
    repo_path: Path
    worktree_path: Path
    controlled_root: Path
    created_at: float
    expires_at: float
    status: str

    @property
    def public_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "source_evidence_ref": f"{self.prepare_stage_id}:{self.prepare_attempt}",
            "project": self.project,
            "workspace_binding_id": self.workspace_binding_id,
            "source_kind": "branch_worktree",
            "branch": self.requested_ref,
            "commit": self.commit,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
        }

    @property
    def handle(self) -> DetachedWorktreeHandle:
        return DetachedWorktreeHandle(
            repo=str(self.repo_path), path=str(self.worktree_path), root=str(self.controlled_root),
            commit=self.commit, ref=self.requested_ref, job_id="recovered", stage_id=self.prepare_stage_id,
        )


class AgentSourceLeaseStore:
    def __init__(self, db_path: str | Path | None = None, *, now_fn=time.time) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_agent_source_lease_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_leases (
                    lease_id TEXT PRIMARY KEY,
                    prepare_stage_id TEXT NOT NULL,
                    prepare_attempt INTEGER NOT NULL,
                    project TEXT NOT NULL,
                    workspace_binding_id TEXT NOT NULL,
                    requested_ref TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    worktree_path TEXT NOT NULL,
                    controlled_root TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    UNIQUE(prepare_stage_id,prepare_attempt)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def create(
        self,
        *,
        project: str,
        workspace_binding_id: str,
        requested_ref: str,
        prepare_stage_id: str,
        prepare_attempt: int,
        job_id: str,
        binding_store: AgentBindingStore | None = None,
        ttl_seconds: float = 86400.0,
    ) -> AgentSourceLease:
        stage_id = str(prepare_stage_id or "").strip()
        attempt = int(prepare_attempt or 0)
        if not stage_id or attempt < 1:
            raise AgentSourceLeaseError("source attempt reference is invalid")
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM source_leases WHERE prepare_stage_id=? AND prepare_attempt=?",
                (stage_id, attempt),
            ).fetchone()
        if existing is not None:
            lease = self._row(existing)
            self._validate(lease)
            return lease
        bindings = binding_store or AgentBindingStore()
        try:
            binding = bindings.get(workspace_binding_id, project=project)
            handle = prepare_detached_worktree(
                binding.workspace_root, requested_ref, str(job_id or "job"), stage_id
            )
        except Exception as exc:
            raise AgentSourceLeaseError("isolated Selena source preparation failed") from exc
        now = float(self._now_fn())
        expires = now + float(ttl_seconds)
        if not math.isfinite(now) or not math.isfinite(expires) or now < 0 or expires <= now:
            try:
                cleanup_detached_worktree(handle)
            finally:
                raise AgentSourceLeaseError("source lease timestamps are invalid")
        lease_id = "source-lease:sha256:" + hashlib.sha256(
            "\0".join((stage_id, str(attempt), handle.commit, uuid.uuid4().hex)).encode("utf-8")
        ).hexdigest()
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO source_leases(
                        lease_id,prepare_stage_id,prepare_attempt,project,workspace_binding_id,
                        requested_ref,commit_sha,repo_path,worktree_path,controlled_root,
                        created_at,expires_at,status
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'ready')
                    """,
                    (
                        lease_id, stage_id, attempt, project, workspace_binding_id, requested_ref,
                        handle.commit, handle.repo, handle.path, handle.root, now, expires,
                    ),
                )
                conn.commit()
        except Exception:
            cleanup_detached_worktree(handle)
            raise
        return self.get(lease_id)

    def get(self, lease_id: str, *, source_evidence_ref: str = "") -> AgentSourceLease:
        if not _LEASE_RE.fullmatch(str(lease_id or "")):
            raise AgentSourceLeaseError("source lease id is invalid")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM source_leases WHERE lease_id=?", (lease_id,)).fetchone()
        if row is None:
            raise AgentSourceLeaseError("source lease is unavailable")
        lease = self._row(row)
        if source_evidence_ref and source_evidence_ref != f"{lease.prepare_stage_id}:{lease.prepare_attempt}":
            raise AgentSourceLeaseError("source lease evidence mismatch")
        if lease.status != "ready":
            raise AgentSourceLeaseError("source lease is not ready")
        if lease.expires_at <= float(self._now_fn()):
            raise AgentSourceLeaseError("source lease has expired")
        self._validate(lease)
        return lease

    def release(self, lease_id: str) -> None:
        if not _LEASE_RE.fullmatch(str(lease_id or "")):
            raise AgentSourceLeaseError("source lease id is invalid")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM source_leases WHERE lease_id=?", (lease_id,)).fetchone()
        if row is None:
            raise AgentSourceLeaseError("source lease is unavailable")
        lease = self._row(row)
        if lease.status != "ready":
            raise AgentSourceLeaseError("source lease is not ready")
        try:
            cleanup_detached_worktree(lease.handle)
            status = "released"
        except Exception as exc:
            with self._lock, self._connect() as conn:
                conn.execute("UPDATE source_leases SET status='cleanup_pending' WHERE lease_id=?", (lease_id,))
                conn.commit()
            raise AgentSourceLeaseError("isolated source cleanup is pending") from exc
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE source_leases SET status=? WHERE lease_id=?", (status, lease_id))
            conn.commit()

    @staticmethod
    def _validate(lease: AgentSourceLease) -> None:
        try:
            fingerprint = inspect_workspace(lease.worktree_path)
        except (RepoSourceError, OSError) as exc:
            raise AgentSourceLeaseError("isolated source lease is unhealthy") from exc
        if fingerprint.commit != lease.commit or fingerprint.dirty:
            raise AgentSourceLeaseError("isolated source lease content changed")

    @staticmethod
    def _row(row: sqlite3.Row) -> AgentSourceLease:
        return AgentSourceLease(
            lease_id=str(row["lease_id"]), prepare_stage_id=str(row["prepare_stage_id"]),
            prepare_attempt=int(row["prepare_attempt"]), project=str(row["project"]),
            workspace_binding_id=str(row["workspace_binding_id"]), requested_ref=str(row["requested_ref"]),
            commit=str(row["commit_sha"]), repo_path=Path(str(row["repo_path"])),
            worktree_path=Path(str(row["worktree_path"])), controlled_root=Path(str(row["controlled_root"])),
            created_at=float(row["created_at"]), expires_at=float(row["expires_at"]), status=str(row["status"]),
        )


__all__ = ["AgentSourceLease", "AgentSourceLeaseError", "AgentSourceLeaseStore"]
