"""Agent-local lease for a staged Runtime Bundle archive awaiting upload."""

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
from typing import Any, Mapping

from core.runtime_bundle import RuntimeBundleManifest, RuntimeFile, RuntimeSourceEvidence
from core.runtime_bundle_archive import RuntimeBundleArchive, verify_runtime_bundle_archive


class AgentRuntimeBundleLeaseError(ValueError):
    """Path-free lease validation or persistence failure."""


_LEASE_RE = re.compile(r"^runtime-bundle-lease:sha256:[0-9a-f]{64}$")


def default_agent_runtime_bundle_lease_db_path() -> Path:
    home = str(os.environ.get("RSIM_HOME") or "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".rsim"
    return base / "agent" / "runtime_bundle_leases.db"


@dataclass(frozen=True)
class AgentRuntimeBundleLease:
    lease_id: str
    build_stage_id: str
    build_attempt: int
    project: str
    workspace_binding_id: str
    manifest: RuntimeBundleManifest
    archive_checksum: str
    archive_size: int
    archive_path: Path
    file_identity: tuple[int, int, int, int]
    created_at: float
    expires_at: float
    status: str

    @property
    def public_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "build_evidence_ref": f"{self.build_stage_id}:{self.build_attempt}",
            "project": self.project,
            "workspace_binding_id": self.workspace_binding_id,
            "runtime_bundle": self.manifest.to_dict(),
            "archive": {
                "bundle_id": self.manifest.id,
                "checksum": self.archive_checksum,
                "size": self.archive_size,
                "file_count": len(self.manifest.files),
                "format": "radar-sim.runtime-bundle-archive/1",
            },
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
        }


class AgentRuntimeBundleLeaseStore:
    def __init__(self, db_path: str | Path | None = None, *, now_fn=time.time) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_agent_runtime_bundle_lease_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_bundle_leases (
                    lease_id TEXT PRIMARY KEY,
                    build_stage_id TEXT NOT NULL,
                    build_attempt INTEGER NOT NULL,
                    project TEXT NOT NULL,
                    workspace_binding_id TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    archive_checksum TEXT NOT NULL,
                    archive_size INTEGER NOT NULL,
                    archive_path TEXT NOT NULL,
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
        *,
        project: str,
        workspace_binding_id: str,
        build_stage_id: str,
        build_attempt: int,
        manifest: RuntimeBundleManifest,
        archive: RuntimeBundleArchive,
        ttl_seconds: float = 86400.0,
    ) -> AgentRuntimeBundleLease:
        stage_id = str(build_stage_id or "").strip()
        attempt = int(build_attempt or 0)
        if not stage_id or attempt < 1:
            raise AgentRuntimeBundleLeaseError("build attempt reference is invalid")
        if not isinstance(manifest, RuntimeBundleManifest) or not isinstance(archive, RuntimeBundleArchive):
            raise AgentRuntimeBundleLeaseError("runtime bundle evidence is invalid")
        if manifest.id != archive.bundle_id:
            raise AgentRuntimeBundleLeaseError("runtime bundle archive identity mismatch")
        verify_runtime_bundle_archive(archive)
        identity = _file_identity(archive.path)
        now = float(self._now_fn())
        expires = now + float(ttl_seconds)
        if not math.isfinite(now) or not math.isfinite(expires) or now < 0 or expires <= now:
            raise AgentRuntimeBundleLeaseError("runtime bundle lease timestamps are invalid")
        lease_id = "runtime-bundle-lease:sha256:" + hashlib.sha256(
            "\0".join((stage_id, str(attempt), manifest.id, archive.checksum, uuid.uuid4().hex)).encode("utf-8")
        ).hexdigest()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM runtime_bundle_leases WHERE build_stage_id=? AND build_attempt=?",
                (stage_id, attempt),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                lease = self._row(existing)
                self._validate_file(lease)
                return lease
            conn.execute(
                """
                INSERT INTO runtime_bundle_leases(
                    lease_id,build_stage_id,build_attempt,project,workspace_binding_id,
                    manifest_json,archive_checksum,archive_size,archive_path,file_identity_json,
                    created_at,expires_at,status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'ready')
                """,
                (
                    lease_id, stage_id, attempt, str(project), str(workspace_binding_id),
                    json.dumps(_private_manifest_dict(manifest), sort_keys=True),
                    archive.checksum, archive.size, str(archive.path), json.dumps(list(identity)),
                    now, expires,
                ),
            )
            conn.commit()
        return self.get(lease_id)

    def create_from_catalog_archive(
        self,
        *,
        project: str,
        cache_stage_id: str,
        cache_attempt: int,
        manifest: RuntimeBundleManifest,
        archive_path: str | Path,
        archive_checksum: str,
        archive_size: int,
        ttl_seconds: float = 86400.0,
    ) -> AgentRuntimeBundleLease:
        """Create a node-local lease from an authenticated catalog download.

        The archive is already stored under the Agent-controlled cache root.
        This method rechecks its immutable central evidence before persisting
        only the private path.  Public control-plane evidence remains logical.
        """
        path = Path(archive_path)
        archive = RuntimeBundleArchive(
            bundle_id=manifest.id,
            path=path,
            checksum=str(archive_checksum or "").strip().lower(),
            size=int(archive_size or 0),
            file_count=len(manifest.files),
        )
        try:
            verify_runtime_bundle_archive(archive)
        except Exception as exc:
            raise AgentRuntimeBundleLeaseError("downloaded Runtime Bundle archive changed") from exc
        return self.create(
            project=str(project or "").strip(),
            workspace_binding_id="catalog:" + manifest.id,
            build_stage_id=str(cache_stage_id or "").strip(),
            build_attempt=int(cache_attempt or 0),
            manifest=manifest,
            archive=archive,
            ttl_seconds=ttl_seconds,
        )

    def get(self, lease_id: str, *, build_evidence_ref: str = "") -> AgentRuntimeBundleLease:
        if not _LEASE_RE.fullmatch(str(lease_id or "")):
            raise AgentRuntimeBundleLeaseError("runtime bundle lease id is invalid")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM runtime_bundle_leases WHERE lease_id=?", (lease_id,)).fetchone()
        if row is None:
            raise AgentRuntimeBundleLeaseError("runtime bundle lease is unavailable")
        lease = self._row(row)
        if build_evidence_ref and build_evidence_ref != f"{lease.build_stage_id}:{lease.build_attempt}":
            raise AgentRuntimeBundleLeaseError("runtime bundle build evidence mismatch")
        if lease.status not in {"ready", "uploaded"}:
            raise AgentRuntimeBundleLeaseError("runtime bundle lease is not ready")
        if lease.expires_at <= float(self._now_fn()):
            raise AgentRuntimeBundleLeaseError("runtime bundle lease has expired")
        self._validate_file(lease)
        return lease

    def mark_uploaded(self, lease_id: str, storage_ref: str) -> AgentRuntimeBundleLease:
        if not str(storage_ref or "").startswith("shared://selena-bundles/"):
            raise AgentRuntimeBundleLeaseError("runtime bundle storage reference is invalid")
        with self._lock, self._connect() as conn:
            updated = conn.execute(
                "UPDATE runtime_bundle_leases SET status='uploaded',storage_ref=? WHERE lease_id=?",
                (storage_ref, lease_id),
            )
            if updated.rowcount != 1:
                raise AgentRuntimeBundleLeaseError("runtime bundle lease is unavailable")
            conn.commit()
        return self.get(lease_id)

    def _validate_file(self, lease: AgentRuntimeBundleLease) -> None:
        if _file_identity(lease.archive_path) != lease.file_identity:
            raise AgentRuntimeBundleLeaseError("runtime bundle archive changed")
        archive = RuntimeBundleArchive(
            bundle_id=lease.manifest.id,
            path=lease.archive_path,
            checksum=lease.archive_checksum,
            size=lease.archive_size,
            file_count=len(lease.manifest.files),
        )
        try:
            verify_runtime_bundle_archive(archive)
        except Exception as exc:
            raise AgentRuntimeBundleLeaseError("runtime bundle archive changed") from exc

    @staticmethod
    def _row(row: sqlite3.Row) -> AgentRuntimeBundleLease:
        return AgentRuntimeBundleLease(
            lease_id=str(row["lease_id"]),
            build_stage_id=str(row["build_stage_id"]),
            build_attempt=int(row["build_attempt"]),
            project=str(row["project"]),
            workspace_binding_id=str(row["workspace_binding_id"]),
            manifest=_manifest_from_dict(json.loads(row["manifest_json"])),
            archive_checksum=str(row["archive_checksum"]),
            archive_size=int(row["archive_size"]),
            archive_path=Path(str(row["archive_path"])),
            file_identity=tuple(int(value) for value in json.loads(row["file_identity_json"])),
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
            status=str(row["status"]),
        )


def _private_manifest_dict(manifest: RuntimeBundleManifest) -> dict[str, Any]:
    value = manifest.to_dict()
    value["source"] = manifest.source.identity_dict()
    return value


def _manifest_from_dict(value: Mapping[str, Any]) -> RuntimeBundleManifest:
    source = RuntimeSourceEvidence(**dict(value.get("source") or {}))
    files = tuple(RuntimeFile(**dict(item)) for item in value.get("files") or [])
    return RuntimeBundleManifest(
        id=str(value.get("id") or ""), files=files, source=source, created_at=float(value.get("created_at") or 0)
    )


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise AgentRuntimeBundleLeaseError("runtime bundle archive is unavailable") from exc
    if not path.is_file() or path.is_symlink():
        raise AgentRuntimeBundleLeaseError("runtime bundle archive is invalid")
    return (
        int(getattr(stat, "st_dev", 0)), int(getattr(stat, "st_ino", 0)), int(stat.st_size),
        int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
    )


__all__ = [
    "AgentRuntimeBundleLease",
    "AgentRuntimeBundleLeaseError",
    "AgentRuntimeBundleLeaseStore",
    "default_agent_runtime_bundle_lease_db_path",
]
