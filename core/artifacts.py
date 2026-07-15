"""SQLite-backed Selena artifact catalog.

The catalog stores logical/platform references only. It never opens or
validates user-local executable paths; build/register boundaries must provide
checksums and storage references explicitly.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_STORAGE_REF_RE = re.compile(r"^(artifact|cluster|shared|legacy)://[^\s]+$", re.IGNORECASE)
_VISIBILITIES = {"private", "shared"}
_ACCESSIBILITIES = {"local", "cluster", "shared"}
_HEALTH_VALUES = {"ready", "degraded", "failed", "unknown"}
_REGISTER_LOCK = threading.Lock()


class ArtifactError(ValueError):
    """Base stable error for artifact catalog operations."""


class ArtifactValidationError(ArtifactError):
    """Raised when artifact input violates the stable catalog contract."""


class ArtifactNotFoundError(ArtifactError):
    """Raised when an artifact id cannot be found."""


class ArtifactAccessError(ArtifactError):
    """Raised when an artifact exists but is not visible or target-compatible."""


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(_thaw_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str) -> dict[str, Any]:
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise ArtifactValidationError("Artifact manifest JSON must be an object")
    return parsed


def _clean_text(value: Any, field_name: str, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ArtifactValidationError(f"{field_name} must not be empty")
    return text


def _clean_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise ArtifactValidationError("boolean artifact fields must be true or false")


def _clean_time(value: Any, field_name: str) -> float:
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(f"{field_name} must be numeric") from exc
    if parsed < 0:
        raise ArtifactValidationError(f"{field_name} must be non-negative")
    if not math.isfinite(parsed):
        raise ArtifactValidationError(f"{field_name} must be finite")
    return parsed


def _validate_checksum(value: str) -> str:
    checksum = str(value or "").strip().lower()
    if not _CHECKSUM_RE.fullmatch(checksum):
        raise ArtifactValidationError("binary_checksum must match sha256:<64 lowercase hex>")
    return checksum


def _validate_storage_ref(value: Any) -> str:
    storage_ref = _clean_text(value, "storage_ref")
    if not _STORAGE_REF_RE.fullmatch(storage_ref):
        raise ArtifactValidationError(
            "storage_ref must be a logical artifact://, cluster://, shared://, or legacy:// reference"
        )
    return storage_ref


def _target_compatible(accessibility: str, target: str) -> bool:
    target = str(target or "").strip().lower()
    if not target or target == "auto":
        return accessibility in _ACCESSIBILITIES
    if target == "local":
        return accessibility == "local"
    if target == "cluster":
        return accessibility in {"cluster", "shared"}
    raise ArtifactValidationError(f"Unsupported target accessibility: {target}")


@dataclass(frozen=True)
class SelenaArtifact:
    id: str
    project: str
    owner: str
    visibility: str
    branch: str
    commit: str
    source_kind: str
    dirty: bool
    dirty_fingerprint: str
    source_changed_during_build: bool
    build_mode: str
    toolchain_fingerprint: str
    binary_checksum: str
    interface_manifest: Mapping[str, Any]
    signal_manifest: Mapping[str, Any]
    storage_ref: str
    accessibility: str
    health: str
    created_by: str
    created_at: float
    retain_until: float

    def __post_init__(self) -> None:
        visibility = _clean_text(self.visibility, "visibility").lower()
        dirty = _clean_bool(self.dirty)
        changed = _clean_bool(self.source_changed_during_build)
        if visibility not in _VISIBILITIES:
            raise ArtifactValidationError(f"Unsupported visibility: {visibility}")
        if dirty or changed:
            visibility = "private"

        accessibility = _clean_text(self.accessibility, "accessibility").lower()
        if accessibility not in _ACCESSIBILITIES:
            raise ArtifactValidationError(f"Unsupported accessibility: {accessibility}")
        health = _clean_text(self.health, "health").lower()
        if health not in _HEALTH_VALUES:
            raise ArtifactValidationError(f"Unsupported health: {health}")

        normalized = {
            "id": _clean_text(self.id, "id", required=False),
            "project": _clean_text(self.project, "project"),
            "owner": _clean_text(self.owner, "owner"),
            "visibility": visibility,
            "branch": _clean_text(self.branch, "branch", required=False),
            "commit": _clean_text(self.commit, "commit", required=False),
            "source_kind": _clean_text(self.source_kind, "source_kind"),
            "dirty": dirty,
            "dirty_fingerprint": _clean_text(self.dirty_fingerprint, "dirty_fingerprint", required=False),
            "source_changed_during_build": changed,
            "build_mode": _clean_text(self.build_mode, "build_mode"),
            "toolchain_fingerprint": _clean_text(self.toolchain_fingerprint, "toolchain_fingerprint", required=False),
            "binary_checksum": _validate_checksum(self.binary_checksum),
            "interface_manifest": _freeze_json(self.interface_manifest or {}),
            "signal_manifest": _freeze_json(self.signal_manifest or {}),
            "storage_ref": _validate_storage_ref(self.storage_ref),
            "accessibility": accessibility,
            "health": health,
            "created_by": _clean_text(self.created_by, "created_by"),
            "created_at": _clean_time(self.created_at, "created_at"),
            "retain_until": _clean_time(self.retain_until, "retain_until"),
        }
        for manifest_name in ("interface_manifest", "signal_manifest"):
            try:
                _json_dumps(normalized[manifest_name])
            except (TypeError, ValueError) as exc:
                raise ArtifactValidationError(f"{manifest_name} must contain JSON-compatible values") from exc
        for key, value in normalized.items():
            object.__setattr__(self, key, value)

    def to_dict(self) -> dict[str, Any]:
        return {field.name: _thaw_json(getattr(self, field.name)) for field in fields(self)}


class ArtifactCatalog:
    """Small SQLite catalog with one connection per operation."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        conn = self._conn()
        try:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS selena_artifacts (
                    id TEXT PRIMARY KEY,
                    identity_key TEXT NOT NULL UNIQUE,
                    project TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    visibility TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    dirty INTEGER NOT NULL,
                    dirty_fingerprint TEXT NOT NULL,
                    source_changed_during_build INTEGER NOT NULL,
                    build_mode TEXT NOT NULL,
                    toolchain_fingerprint TEXT NOT NULL,
                    binary_checksum TEXT NOT NULL,
                    interface_manifest_json TEXT NOT NULL,
                    signal_manifest_json TEXT NOT NULL,
                    storage_ref TEXT NOT NULL,
                    accessibility TEXT NOT NULL,
                    health TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    retain_until REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_selena_artifacts_project_health
                    ON selena_artifacts(project, health, build_mode, created_at);
                CREATE INDEX IF NOT EXISTS idx_selena_artifacts_checksum
                    ON selena_artifacts(project, binary_checksum);
                CREATE INDEX IF NOT EXISTS idx_selena_artifacts_owner
                    ON selena_artifacts(owner, visibility);
                CREATE INDEX IF NOT EXISTS idx_selena_artifacts_storage_ref
                    ON selena_artifacts(storage_ref);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def register(self, artifact: SelenaArtifact | Mapping[str, Any]) -> SelenaArtifact:
        candidate = self._coerce_artifact(artifact)
        identity_key = self._identity_key(candidate)
        if not candidate.id:
            candidate = self._replace(candidate, id=self._generated_id(candidate, identity_key))

        with _REGISTER_LOCK:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing_ref = self._select_by_storage_ref(conn, candidate.storage_ref)
                if existing_ref is not None:
                    existing_artifact = self._row_to_artifact(existing_ref)
                    if self._same_storage_identity(existing_artifact, candidate):
                        conn.commit()
                        return existing_artifact
                    raise ArtifactValidationError(
                        f"storage_ref already exists with different identity: {candidate.storage_ref}"
                    )
                existing = self._select_by_identity(conn, identity_key)
                if existing is not None:
                    conn.commit()
                    return self._row_to_artifact(existing)
                existing_id = self._select_by_id(conn, candidate.id)
                if existing_id is not None:
                    if (
                        str(existing_id["identity_key"]) == identity_key
                        and str(existing_id["binary_checksum"]) == candidate.binary_checksum
                    ):
                        conn.commit()
                        return self._row_to_artifact(existing_id)
                    raise ArtifactValidationError(f"artifact id already exists with different identity: {candidate.id}")
                conn.execute(
                    """
                    INSERT INTO selena_artifacts (
                        id, identity_key, project, owner, visibility, branch, commit_sha,
                        source_kind, dirty, dirty_fingerprint, source_changed_during_build,
                        build_mode, toolchain_fingerprint, binary_checksum,
                        interface_manifest_json, signal_manifest_json, storage_ref,
                        accessibility, health, created_by, created_at, retain_until
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._artifact_values(candidate, identity_key),
                )
                conn.commit()
                return candidate
            except sqlite3.IntegrityError:
                if conn.in_transaction:
                    conn.rollback()
                existing = self._fetch_by_identity(identity_key)
                if existing is not None:
                    return existing
                existing_id = self._fetch_by_id(candidate.id)
                if existing_id is not None:
                    raise ArtifactValidationError(
                        f"artifact id already exists with different identity: {candidate.id}"
                    )
                raise ArtifactValidationError("artifact catalog registration conflict")
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def get(self, artifact_id: str, *, owner: str = "") -> SelenaArtifact:
        """Return a shared artifact or a private artifact owned by ``owner``."""
        artifact = self._get_unchecked(artifact_id)
        owner = str(owner or "").strip()
        if artifact.visibility != "shared" and artifact.owner != owner:
            raise ArtifactAccessError(f"Artifact is private to a different owner: {artifact_id}")
        return artifact

    def get_privileged(self, artifact_id: str) -> SelenaArtifact:
        """Explicit platform-only lookup that bypasses owner visibility filtering."""
        return self._get_unchecked(artifact_id)

    def get_by_storage_ref(self, storage_ref: str, *, owner: str = "") -> SelenaArtifact:
        """Return a shared artifact or an owner-visible private artifact by path."""
        artifact = self._get_by_storage_ref_unchecked(storage_ref)
        owner = str(owner or "").strip()
        if artifact.visibility != "shared" and artifact.owner != owner:
            raise ArtifactAccessError(f"Artifact is private to a different owner: {storage_ref}")
        return artifact

    def get_by_storage_ref_privileged(self, storage_ref: str) -> SelenaArtifact:
        """Platform-only path lookup that bypasses visibility filtering."""
        return self._get_by_storage_ref_unchecked(storage_ref)

    def _get_unchecked(self, artifact_id: str) -> SelenaArtifact:
        artifact_id = _clean_text(artifact_id, "artifact_id")
        conn = self._conn()
        try:
            row = self._select_by_id(conn, artifact_id)
            if row is None:
                raise ArtifactNotFoundError(f"Artifact not found: {artifact_id}")
            return self._row_to_artifact(row)
        finally:
            conn.close()

    def list(
        self,
        *,
        project: str | None = None,
        owner: str | None = None,
        include_private: bool = True,
    ) -> tuple[SelenaArtifact, ...]:
        clauses: list[str] = []
        params: list[Any] = []
        if project:
            clauses.append("project = ?")
            params.append(str(project).strip())
        if owner and include_private:
            clauses.append("(visibility = 'shared' OR owner = ?)")
            params.append(str(owner).strip())
        else:
            clauses.append("visibility = 'shared'")
        sql = "SELECT * FROM selena_artifacts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY project, created_at DESC, id"
        conn = self._conn()
        try:
            return tuple(self._row_to_artifact(row) for row in conn.execute(sql, params).fetchall())
        finally:
            conn.close()

    def snapshot(self, *, project: str | None = None, owner: str | None = None) -> tuple[dict[str, Any], ...]:
        return tuple(artifact.to_dict() for artifact in self.list(project=project, owner=owner, include_private=True))

    def recommend(
        self,
        *,
        project: str,
        owner: str,
        build_mode: str = "",
        target_accessibility: str = "auto",
        now: float | None = None,
    ) -> tuple[SelenaArtifact, ...]:
        project = _clean_text(project, "project")
        owner = _clean_text(owner, "owner")
        build_mode = str(build_mode or "").strip()
        now_value = _clean_time(time.time() if now is None else now, "now")
        clauses = [
            "project = ?",
            "health = 'ready'",
            "dirty = 0",
            "source_changed_during_build = 0",
            "(retain_until = 0 OR retain_until >= ?)",
            "(visibility = 'shared' OR owner = ?)",
        ]
        params: list[Any] = [project, now_value, owner]
        if build_mode:
            clauses.append("build_mode = ?")
            params.append(build_mode)
        sql = (
            "SELECT * FROM selena_artifacts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, binary_checksum, id"
        )
        conn = self._conn()
        try:
            rows = conn.execute(sql, params).fetchall()
            artifacts = [self._row_to_artifact(row) for row in rows]
        finally:
            conn.close()
        return tuple(
            artifact
            for artifact in artifacts
            if _target_compatible(artifact.accessibility, target_accessibility)
        )

    def verify_access(
        self,
        artifact_id: str,
        *,
        owner: str,
        target_accessibility: str = "auto",
        now: float | None = None,
    ) -> SelenaArtifact:
        artifact = self._get_unchecked(artifact_id)
        owner = _clean_text(owner, "owner")
        if artifact.visibility != "shared" and artifact.owner != owner:
            raise ArtifactAccessError(f"Artifact is private to a different owner: {artifact_id}")
        if artifact.health != "ready":
            raise ArtifactAccessError(f"Artifact is not ready: {artifact_id}")
        now_value = _clean_time(time.time() if now is None else now, "now")
        if artifact.retain_until and artifact.retain_until < now_value:
            raise ArtifactAccessError(f"Artifact retention has expired: {artifact_id}")
        if not _target_compatible(artifact.accessibility, target_accessibility):
            raise ArtifactAccessError(f"Artifact is not accessible for target: {target_accessibility}")
        return artifact

    def verify_storage_access(
        self,
        storage_ref: str,
        *,
        owner: str,
        target_accessibility: str = "auto",
        now: float | None = None,
    ) -> SelenaArtifact:
        """Verify visibility, health, retention and target access by logical path."""
        artifact = self._get_by_storage_ref_unchecked(storage_ref)
        owner = _clean_text(owner, "owner")
        if artifact.visibility != "shared" and artifact.owner != owner:
            raise ArtifactAccessError(f"Artifact is private to a different owner: {storage_ref}")
        if artifact.health != "ready":
            raise ArtifactAccessError(f"Artifact is not ready: {storage_ref}")
        now_value = _clean_time(time.time() if now is None else now, "now")
        if artifact.retain_until and artifact.retain_until < now_value:
            raise ArtifactAccessError(f"Artifact retention has expired: {storage_ref}")
        if not _target_compatible(artifact.accessibility, target_accessibility):
            raise ArtifactAccessError(f"Artifact is not accessible for target: {target_accessibility}")
        return artifact

    @staticmethod
    def _coerce_artifact(value: SelenaArtifact | Mapping[str, Any]) -> SelenaArtifact:
        if isinstance(value, SelenaArtifact):
            return value
        if not isinstance(value, Mapping):
            raise ArtifactValidationError("artifact must be a SelenaArtifact or mapping")
        return SelenaArtifact(**dict(value))

    @staticmethod
    def _replace(artifact: SelenaArtifact, **patch: Any) -> SelenaArtifact:
        data = artifact.to_dict()
        data.update(patch)
        return SelenaArtifact(**data)

    @staticmethod
    def _identity_key(artifact: SelenaArtifact) -> str:
        return f"storage-ref:{artifact.storage_ref}"

    @staticmethod
    def _same_storage_identity(existing: SelenaArtifact, candidate: SelenaArtifact) -> bool:
        return (
            existing.storage_ref == candidate.storage_ref
            and existing.project == candidate.project
            and existing.binary_checksum == candidate.binary_checksum
            and existing.accessibility == candidate.accessibility
            and existing.visibility == candidate.visibility
            and (existing.visibility == "shared" or existing.owner == candidate.owner)
        )

    @staticmethod
    def _generated_id(artifact: SelenaArtifact, identity_key: str) -> str:
        suffix = uuid.uuid5(uuid.NAMESPACE_URL, identity_key).hex[:16]
        return f"selena:{artifact.project}:{suffix}"

    @staticmethod
    def _artifact_values(artifact: SelenaArtifact, identity_key: str) -> tuple[Any, ...]:
        return (
            artifact.id,
            identity_key,
            artifact.project,
            artifact.owner,
            artifact.visibility,
            artifact.branch,
            artifact.commit,
            artifact.source_kind,
            1 if artifact.dirty else 0,
            artifact.dirty_fingerprint,
            1 if artifact.source_changed_during_build else 0,
            artifact.build_mode,
            artifact.toolchain_fingerprint,
            artifact.binary_checksum,
            _json_dumps(artifact.interface_manifest),
            _json_dumps(artifact.signal_manifest),
            artifact.storage_ref,
            artifact.accessibility,
            artifact.health,
            artifact.created_by,
            artifact.created_at,
            artifact.retain_until,
        )

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> SelenaArtifact:
        return SelenaArtifact(
            id=row["id"],
            project=row["project"],
            owner=row["owner"],
            visibility=row["visibility"],
            branch=row["branch"],
            commit=row["commit_sha"],
            source_kind=row["source_kind"],
            dirty=bool(row["dirty"]),
            dirty_fingerprint=row["dirty_fingerprint"],
            source_changed_during_build=bool(row["source_changed_during_build"]),
            build_mode=row["build_mode"],
            toolchain_fingerprint=row["toolchain_fingerprint"],
            binary_checksum=row["binary_checksum"],
            interface_manifest=_json_loads(row["interface_manifest_json"]),
            signal_manifest=_json_loads(row["signal_manifest_json"]),
            storage_ref=row["storage_ref"],
            accessibility=row["accessibility"],
            health=row["health"],
            created_by=row["created_by"],
            created_at=float(row["created_at"]),
            retain_until=float(row["retain_until"]),
        )

    @staticmethod
    def _select_by_identity(conn: sqlite3.Connection, identity_key: str) -> Optional[sqlite3.Row]:
        return conn.execute("SELECT * FROM selena_artifacts WHERE identity_key = ?", (identity_key,)).fetchone()

    @staticmethod
    def _select_by_id(conn: sqlite3.Connection, artifact_id: str) -> Optional[sqlite3.Row]:
        return conn.execute("SELECT * FROM selena_artifacts WHERE id = ?", (artifact_id,)).fetchone()

    @staticmethod
    def _select_by_storage_ref(conn: sqlite3.Connection, storage_ref: str) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM selena_artifacts WHERE storage_ref = ? ORDER BY created_at, id",
            (storage_ref,),
        ).fetchone()

    def _fetch_by_identity(self, identity_key: str) -> SelenaArtifact | None:
        conn = self._conn()
        try:
            row = self._select_by_identity(conn, identity_key)
            return self._row_to_artifact(row) if row is not None else None
        finally:
            conn.close()

    def _get_by_storage_ref_unchecked(self, storage_ref: str) -> SelenaArtifact:
        storage_ref = _validate_storage_ref(storage_ref)
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM selena_artifacts WHERE storage_ref = ? ORDER BY created_at, id",
                (storage_ref,),
            ).fetchall()
            if not rows:
                raise ArtifactNotFoundError(f"Artifact not found for storage_ref: {storage_ref}")
            if len(rows) > 1:
                raise ArtifactValidationError(f"Ambiguous legacy storage_ref: {storage_ref}")
            return self._row_to_artifact(rows[0])
        finally:
            conn.close()

    def _fetch_by_id(self, artifact_id: str) -> SelenaArtifact | None:
        conn = self._conn()
        try:
            row = self._select_by_id(conn, artifact_id)
            return self._row_to_artifact(row) if row is not None else None
        finally:
            conn.close()


__all__ = [
    "ArtifactAccessError",
    "ArtifactCatalog",
    "ArtifactError",
    "ArtifactNotFoundError",
    "ArtifactValidationError",
    "SelenaArtifact",
]
