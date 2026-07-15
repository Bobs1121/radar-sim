"""Agent-local lease for a built Selena file awaiting central upload.

The central scheduler sees only ``lease_id``. The absolute artifact path and
file identity remain in the Windows Agent SQLite store and are revalidated
immediately before upload.
"""

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

from core.agent_artifact_staging import validate_and_hash_artifact
from core.agent_build_stage import PreparedSelenaBuild


class AgentArtifactLeaseError(ValueError):
    """Path-free lease validation or persistence failure."""


_LEASE_RE = re.compile(r"^artifact-lease:sha256:[0-9a-f]{64}$")


def default_agent_artifact_lease_db_path() -> Path:
    home = os.environ.get("RSIM_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".rsim"
    return base / "agent" / "artifact_leases.db"


@dataclass(frozen=True)
class AgentArtifactLease:
    lease_id: str
    build_stage_id: str
    build_attempt: int
    project: str
    workspace_binding_id: str
    checksum: str
    size: int
    logical_path: str
    created_at: float
    expires_at: float
    status: str
    artifact_path: Path
    file_identity: tuple[int, int, int, int]

    @property
    def public_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "build_evidence_ref": f"{self.build_stage_id}:{self.build_attempt}",
            "project": self.project,
            "workspace_binding_id": self.workspace_binding_id,
            "checksum": self.checksum,
            "size": self.size,
            "logical_path": self.logical_path,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
        }


class AgentArtifactLeaseStore:
    def __init__(self, db_path: str | Path | None = None, *, now_fn=time.time) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_agent_artifact_lease_db_path()
        self._now_fn = now_fn
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifact_leases (
                    lease_id TEXT PRIMARY KEY,
                    build_stage_id TEXT NOT NULL,
                    build_attempt INTEGER NOT NULL,
                    project TEXT NOT NULL,
                    workspace_binding_id TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    logical_path TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    file_identity_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    storage_ref TEXT NOT NULL DEFAULT '',
                    UNIQUE(build_stage_id, build_attempt)
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
        prepared: PreparedSelenaBuild,
        build_result: dict[str, Any],
        *,
        build_stage_id: str,
        build_attempt: int,
        ttl_seconds: float = 86400.0,
    ) -> AgentArtifactLease:
        if not isinstance(prepared, PreparedSelenaBuild):
            raise AgentArtifactLeaseError("prepared build is required")
        stage_id = str(build_stage_id or "").strip()
        attempt = int(build_attempt or 0)
        if not stage_id or attempt < 1:
            raise AgentArtifactLeaseError("build attempt reference is invalid")
        try:
            evidence = validate_and_hash_artifact(prepared.artifact_path, prepared.authorized)
        except Exception as exc:
            raise AgentArtifactLeaseError("built artifact is unavailable for lease") from exc
        public_artifact = dict(build_result.get("artifact") or {})
        if (
            public_artifact.get("checksum") != evidence.checksum
            or int(public_artifact.get("size") or 0) != evidence.size
            or public_artifact.get("logical_path") != evidence.logical_path
        ):
            raise AgentArtifactLeaseError("built artifact evidence changed before lease")
        identity = _file_identity(prepared.artifact_path)
        now = float(self._now_fn())
        expires = now + float(ttl_seconds)
        if not math.isfinite(now) or not math.isfinite(expires) or now < 0 or expires <= now:
            raise AgentArtifactLeaseError("artifact lease timestamps are invalid")
        digest = hashlib.sha256(
            "\0".join([stage_id, str(attempt), evidence.checksum, uuid.uuid4().hex]).encode("utf-8")
        ).hexdigest()
        lease_id = f"artifact-lease:sha256:{digest}"
        with self._lock, self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT * FROM artifact_leases WHERE build_stage_id=? AND build_attempt=?",
                    (stage_id, attempt),
                ).fetchone()
                if existing is not None:
                    conn.rollback()
                    lease = self._row(existing)
                    self._validate_lease_file(lease, prepared)
                    return lease
                conn.execute(
                    """
                    INSERT INTO artifact_leases(
                        lease_id, build_stage_id, build_attempt, project,
                        workspace_binding_id, checksum, size, logical_path,
                        artifact_path, file_identity_json, created_at, expires_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready')
                    """,
                    (
                        lease_id,
                        stage_id,
                        attempt,
                        prepared.project,
                        prepared.binding_id,
                        evidence.checksum,
                        evidence.size,
                        evidence.logical_path,
                        str(prepared.artifact_path),
                        json.dumps(list(identity)),
                        now,
                        expires,
                    ),
                )
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
        return self.get(lease_id, prepared=prepared)

    def get(
        self,
        lease_id: str,
        *,
        prepared: PreparedSelenaBuild | None = None,
        build_evidence_ref: str = "",
    ) -> AgentArtifactLease:
        if not _LEASE_RE.fullmatch(str(lease_id or "")):
            raise AgentArtifactLeaseError("artifact lease id is invalid")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM artifact_leases WHERE lease_id=?", (lease_id,)).fetchone()
        if row is None:
            raise AgentArtifactLeaseError("artifact lease is unavailable")
        lease = self._row(row)
        if build_evidence_ref and build_evidence_ref != f"{lease.build_stage_id}:{lease.build_attempt}":
            raise AgentArtifactLeaseError("artifact lease build evidence mismatch")
        if lease.status not in {"ready", "uploaded"}:
            raise AgentArtifactLeaseError("artifact lease is not ready")
        if lease.expires_at <= float(self._now_fn()):
            raise AgentArtifactLeaseError("artifact lease has expired")
        self._validate_lease_file(lease, prepared)
        return lease

    def mark_uploaded(self, lease_id: str, storage_ref: str) -> AgentArtifactLease:
        storage_ref = str(storage_ref or "").strip()
        if not storage_ref.startswith("shared://selena/"):
            raise AgentArtifactLeaseError("uploaded artifact storage reference is invalid")
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE artifact_leases SET status='uploaded', storage_ref=? WHERE lease_id=?",
                (storage_ref, lease_id),
            )
            if conn.total_changes != 1:
                raise AgentArtifactLeaseError("artifact lease is unavailable")
            conn.commit()
        return self.get(lease_id)

    def _validate_lease_file(
        self,
        lease: AgentArtifactLease,
        prepared: PreparedSelenaBuild | None,
    ) -> None:
        if prepared is not None:
            if prepared.project != lease.project or prepared.binding_id != lease.workspace_binding_id:
                raise AgentArtifactLeaseError("artifact lease build binding mismatch")
            if prepared.artifact_path.resolve(strict=False) != lease.artifact_path.resolve(strict=False):
                raise AgentArtifactLeaseError("artifact lease file mismatch")
        if _file_identity(lease.artifact_path) != lease.file_identity:
            raise AgentArtifactLeaseError("artifact lease file changed")
        digest = hashlib.sha256()
        try:
            with lease.artifact_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise AgentArtifactLeaseError("artifact lease file is unavailable") from exc
        if "sha256:" + digest.hexdigest() != lease.checksum:
            raise AgentArtifactLeaseError("artifact lease file checksum changed")

    @staticmethod
    def _row(row: sqlite3.Row) -> AgentArtifactLease:
        identity_raw = json.loads(row["file_identity_json"])
        return AgentArtifactLease(
            lease_id=row["lease_id"],
            build_stage_id=row["build_stage_id"],
            build_attempt=int(row["build_attempt"]),
            project=row["project"],
            workspace_binding_id=row["workspace_binding_id"],
            checksum=row["checksum"],
            size=int(row["size"]),
            logical_path=row["logical_path"],
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
            status=row["status"],
            artifact_path=Path(row["artifact_path"]),
            file_identity=tuple(int(item) for item in identity_raw),
        )


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise AgentArtifactLeaseError("artifact lease file is unavailable") from exc
    if not path.is_file() or path.is_symlink():
        raise AgentArtifactLeaseError("artifact lease file is invalid")
    return (
        int(getattr(stat, "st_dev", 0)),
        int(getattr(stat, "st_ino", 0)),
        int(stat.st_size),
        int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
    )


__all__ = [
    "AgentArtifactLease",
    "AgentArtifactLeaseError",
    "AgentArtifactLeaseStore",
    "default_agent_artifact_lease_db_path",
]
