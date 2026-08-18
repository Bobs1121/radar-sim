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
from core.agent_store_paths import default_agent_binding_db_path
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
    # ``source_path`` is canonicalized for authorization/discovery. Keep the
    # user's original spelling separately because Windows DFS/UNC
    # ``Path.resolve()`` may replace an accessible alias with a backend host
    # name that a Selena child process cannot open.
    source_path_text: str = ""

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
                    source_path_text TEXT NOT NULL DEFAULT '',
                    files_json TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    dataset_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(agent_data_leases)").fetchall()
            }
            if "source_path_text" not in columns:
                conn.execute(
                    "ALTER TABLE agent_data_leases ADD COLUMN source_path_text TEXT NOT NULL DEFAULT ''"
                )
            conn.commit()

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
            runtime_signals = [
                str(item).strip()
                for item in payload.get("runtime_data_player_signals") or []
                if str(item).strip()
            ]
            if runtime_signals:
                # Do this before checksumming or uploading the MF4 set.  A
                # bounded byte scan cannot prove a channel is absent, whereas
                # the Runtime.xml DataPlayer contract must be exact.
                from core.preflight import assert_runtime_data_signal_contract

                assert_runtime_data_signal_contract(source, runtime_signals)
            files = discover_dataset_files(
                source,
                payload.get("required_signals") or (),
                checksum=bool(checksum),
                cancel_requested=cancel_requested,
            )
        except DatasetDiscoveryCancelled:
            raise
        except Exception as exc:
            # Preserve the stable, path-free Runtime/MF4 diagnostic for the
            # task centre. Other discovery failures remain intentionally
            # generic because their original exceptions can contain a local
            # Windows path.
            from core.preflight import RuntimeDataSignalContractError

            if isinstance(exc, RuntimeDataSignalContractError):
                raise
            raise AgentDataLeaseError("authorized data discovery failed") from exc
        lease_id = "data-lease:sha256:" + uuid.uuid4().hex
        now = float(self._now_fn())
        existing_after_insert: AgentDataLease | None = None
        with self._lock, self._connect() as conn:
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO agent_data_leases(
                    lease_id,project,binding_id,source_path,source_path_text,files_json,evidence_ref,status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,'ready',?,?)
                """,
                (
                    lease_id, project, binding_id, str(source), str(data_path),
                    json.dumps([asdict(item) for item in files], sort_keys=True),
                    evidence_ref, now, now,
                ),
            )
            if inserted.rowcount == 0:
                # Two Agent workers can discover the same evidence concurrently
                # (for example after a reconnect).  The evidence key is the
                # idempotency boundary; return the first immutable lease instead
                # of leaking SQLite's UNIQUE constraint as a task failure.
                row = conn.execute(
                    "SELECT * FROM agent_data_leases WHERE evidence_ref=?",
                    (evidence_ref,),
                ).fetchone()
                if row is None:
                    raise AgentDataLeaseError("prepare_data lease could not be persisted")
                existing_after_insert = _row_to_lease(row)
                try:
                    same_source = existing_after_insert.source_path.resolve(strict=False) == source.resolve(strict=False)
                except OSError:
                    same_source = str(existing_after_insert.source_path) == str(source)
                if (
                    existing_after_insert.project != project
                    or existing_after_insert.binding_id != binding_id
                    or not same_source
                ):
                    raise AgentDataLeaseError("prepare_data lease evidence conflicts with existing lease")
            conn.commit()
        if existing_after_insert is not None:
            self._revalidate(existing_after_insert)
            return existing_after_insert
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
        source_path_text=(
            str(row["source_path_text"] or "")
            if "source_path_text" in row.keys()
            else ""
        ),
    )


__all__ = ["AgentDataLease", "AgentDataLeaseError", "AgentDataLeaseStore"]
