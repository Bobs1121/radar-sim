"""Central shared catalog for immutable Selena Runtime Bundles."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.runtime_bundle import RuntimeBundleManifest, RuntimeFile, RuntimeSourceEvidence
from core.user import normalize_user


class RuntimeBundleCatalogError(ValueError):
    """Stable runtime-bundle catalog validation or conflict error."""


@dataclass(frozen=True)
class RuntimeBundleRecord:
    manifest: RuntimeBundleManifest
    internal_project: str
    storage_ref: str
    archive_checksum: str
    archive_size: int
    owner: str
    created_by: str

    @property
    def public_dict(self) -> dict[str, Any]:
        return {
            **self.manifest.to_dict(),
            "storage_ref": self.storage_ref,
            "archive_checksum": self.archive_checksum,
            "archive_size": self.archive_size,
            "visibility": "shared",
            "owner": self.owner,
            "created_by": self.created_by,
        }


class RuntimeBundleCatalog:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_bundles (
                    bundle_id TEXT PRIMARY KEY,
                    internal_project TEXT NOT NULL DEFAULT '',
                    manifest_json TEXT NOT NULL,
                    storage_ref TEXT NOT NULL UNIQUE,
                    archive_checksum TEXT NOT NULL,
                    archive_size INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    created_by TEXT NOT NULL
                )
                """
            )
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(runtime_bundles)").fetchall()}
            if "internal_project" not in columns:
                conn.execute("ALTER TABLE runtime_bundles ADD COLUMN internal_project TEXT NOT NULL DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def register(self, record: RuntimeBundleRecord) -> RuntimeBundleRecord:
        if not isinstance(record, RuntimeBundleRecord):
            raise RuntimeBundleCatalogError("runtime bundle record is required")
        normalize_user(record.owner)
        if not str(record.internal_project or "").strip():
            raise RuntimeBundleCatalogError("runtime bundle internal adapter project is required")
        if not record.storage_ref.startswith("shared://selena-bundles/"):
            raise RuntimeBundleCatalogError("runtime bundle storage reference is invalid")
        if not record.archive_checksum.startswith("sha256:") or record.archive_size <= 0:
            raise RuntimeBundleCatalogError("runtime bundle archive evidence is invalid")
        if not str(record.created_by or "").strip():
            raise RuntimeBundleCatalogError("runtime bundle builder is required")
        private_manifest = record.manifest.to_dict()
        private_manifest["source"] = record.manifest.source.identity_dict()
        manifest_json = json.dumps(private_manifest, sort_keys=True)
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM runtime_bundles WHERE bundle_id=? OR storage_ref=?",
                (record.manifest.id, record.storage_ref),
            ).fetchone()
            if existing is not None:
                current = self._row(existing)
                if current != record:
                    raise RuntimeBundleCatalogError("runtime bundle identity already has different metadata")
                return current
            conn.execute(
                """
                INSERT INTO runtime_bundles(
                    bundle_id,internal_project,manifest_json,storage_ref,archive_checksum,archive_size,owner,created_by
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    record.manifest.id, record.internal_project, manifest_json, record.storage_ref, record.archive_checksum,
                    record.archive_size, record.owner, record.created_by,
                ),
            )
            conn.commit()
        return record

    def get(self, bundle_id: str) -> RuntimeBundleRecord:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM runtime_bundles WHERE bundle_id=?", (str(bundle_id),)).fetchone()
        if row is None:
            raise RuntimeBundleCatalogError("runtime bundle is unavailable")
        return self._row(row)

    def get_by_storage_ref(self, storage_ref: str) -> RuntimeBundleRecord:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM runtime_bundles WHERE storage_ref=?", (str(storage_ref),)).fetchone()
        if row is None:
            raise RuntimeBundleCatalogError("runtime bundle is unavailable")
        return self._row(row)

    def list(self) -> list[RuntimeBundleRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM runtime_bundles ORDER BY rowid DESC").fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> RuntimeBundleRecord:
        value = json.loads(row["manifest_json"])
        manifest = RuntimeBundleManifest(
            id=str(value.get("id") or ""),
            files=tuple(RuntimeFile(**dict(item)) for item in value.get("files") or []),
            source=RuntimeSourceEvidence(**dict(value.get("source") or {})),
            created_at=float(value.get("created_at") or 0),
        )
        return RuntimeBundleRecord(
            manifest=manifest,
            internal_project=str(row["internal_project"]),
            storage_ref=str(row["storage_ref"]),
            archive_checksum=str(row["archive_checksum"]),
            archive_size=int(row["archive_size"]),
            owner=str(row["owner"]),
            created_by=str(row["created_by"]),
        )


__all__ = [
    "RuntimeBundleCatalog",
    "RuntimeBundleCatalogError",
    "RuntimeBundleRecord",
]
