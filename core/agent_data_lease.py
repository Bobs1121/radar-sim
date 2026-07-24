"""Agent-local immutable lease for authorized MF4 discovery and upload."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from core.agent_data_bindings import AgentDataBindingStore
from core.agent_bindings import default_agent_binding_db_path
from core.datasets import DatasetDiscoveryCancelled, DatasetFileRef, discover_dataset_files


class AgentDataLeaseError(ValueError):
    pass


_LEASE_RE = re.compile(r"^data-lease:sha256:[0-9a-f]{32}$")


@dataclass(frozen=True)
class AgentDataLease:
    lease_id: str
    project: str
    binding_id: str
    source_path: Path
    files: tuple[DatasetFileRef, ...]
    evidence_ref: str
    status: str
    dataset_id: str
    created_at: float
    updated_at: float

    @property
    def public_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "project": self.project,
            "file_count": len(self.files),
            "total_size": sum(item.size for item in self.files),
            "evidence_ref": self.evidence_ref,
            "status": self.status,
        }


class AgentDataLeaseStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        default = default_agent_binding_db_path().with_name("data-leases.db")
        self.db_path = Path(db_path) if db_path is not None else default
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_data_leases (
                    lease_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    binding_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    files_json TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    dataset_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def create(
        self,
        payload: dict[str, Any],
        bindings: AgentDataBindingStore,
        *,
        stage_id: str,
        attempt: int,
        checksum: bool = True,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> AgentDataLease:
        project = str(payload.get("project") or "").strip()
        binding_id = str(payload.get("data_binding_id") or "").strip()
        data_path = str(payload.get("data_path") or "").strip()
        if not project or not binding_id or not data_path or not stage_id or int(attempt) <= 0:
            raise AgentDataLeaseError("prepare_data lease input is invalid")
        evidence_ref = f"{stage_id}:{int(attempt)}"
        try:
            existing = self.get_by_evidence(evidence_ref)
            if existing.project != project or existing.binding_id != binding_id:
                raise AgentDataLeaseError("prepare_data lease evidence conflicts with existing lease")
            return existing
        except AgentDataLeaseError as exc:
            if "unavailable" not in str(exc):
                raise
        try:
            source = bindings.authorize_path(project=project, binding_id=binding_id, data_path=data_path)
            files = discover_dataset_files(
                source,
                payload.get("required_signals") or (),
                checksum=bool(checksum),
                cancel_requested=cancel_requested,
            )
        except DatasetDiscoveryCancelled:
            raise
        except Exception as exc:
            raise AgentDataLeaseError("authorized data discovery failed") from exc
        lease_id = "data-lease:sha256:" + uuid.uuid4().hex
        now = float(self._now_fn())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_data_leases(
                    lease_id,project,binding_id,source_path,files_json,evidence_ref,status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,'ready',?,?)
                """,
                (
                    lease_id, project, binding_id, str(source),
                    json.dumps([asdict(item) for item in files], sort_keys=True),
                    evidence_ref, now, now,
                ),
            )
            conn.commit()
        return self.get(lease_id, evidence_ref=evidence_ref)

    def get(self, lease_id: str, *, evidence_ref: str = "") -> AgentDataLease:
        if not _LEASE_RE.fullmatch(str(lease_id or "")):
            raise AgentDataLeaseError("data lease is unavailable")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM agent_data_leases WHERE lease_id=?", (lease_id,)).fetchone()
        if row is None or (evidence_ref and row["evidence_ref"] != evidence_ref):
            raise AgentDataLeaseError("data lease is unavailable")
        lease = _row_to_lease(row)
        self._revalidate(lease)
        return lease

    def get_by_evidence(self, evidence_ref: str) -> AgentDataLease:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_data_leases WHERE evidence_ref=?", (str(evidence_ref or ""),)
            ).fetchone()
        if row is None:
            raise AgentDataLeaseError("data lease is unavailable")
        lease = _row_to_lease(row)
        self._revalidate(lease)
        return lease

    def mark_uploaded(self, lease_id: str, dataset_id: str) -> AgentDataLease:
        if not str(dataset_id or "").startswith("dataset:sha256:"):
            raise AgentDataLeaseError("uploaded dataset id is invalid")
        now = float(self._now_fn())
        with self._lock, self._connect() as conn:
            updated = conn.execute(
                "UPDATE agent_data_leases SET status='uploaded',dataset_id=?,updated_at=? WHERE lease_id=?",
                (dataset_id, now, lease_id),
            )
            if updated.rowcount != 1:
                raise AgentDataLeaseError("data lease is unavailable")
            conn.commit()
        return self.get(lease_id)

    @staticmethod
    def _revalidate(lease: AgentDataLease) -> None:
        source = lease.source_path
        root = source if source.is_dir() else source.parent
        for item in lease.files:
            path = source if source.is_file() else root.joinpath(*Path(item.relative_path).parts)
            try:
                stat = path.stat()
            except OSError as exc:
                raise AgentDataLeaseError("leased data file is unavailable") from exc
            if stat.st_size != item.size or stat.st_mtime_ns != item.mtime_ns:
                raise AgentDataLeaseError("leased data file changed after discovery")


def _row_to_lease(row: sqlite3.Row) -> AgentDataLease:
    return AgentDataLease(
        lease_id=str(row["lease_id"]),
        project=str(row["project"]),
        binding_id=str(row["binding_id"]),
        source_path=Path(str(row["source_path"])),
        files=tuple(DatasetFileRef(**item) for item in json.loads(row["files_json"])),
        evidence_ref=str(row["evidence_ref"]),
        status=str(row["status"]),
        dataset_id=str(row["dataset_id"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


__all__ = ["AgentDataLease", "AgentDataLeaseError", "AgentDataLeaseStore"]
