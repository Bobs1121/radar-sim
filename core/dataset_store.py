"""Private multi-file upload store for MF4 datasets.

The public contract contains only opaque upload/file identifiers and logical
dataset references.  Physical staging and content paths stay inside this
module and are never serialized by the HTTP adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable, Mapping

from core.data import is_input_mf4
from core.user import normalize_user


_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SERVER_CHECKSUM = "server"
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SESSION_RE = re.compile(r"^dsup_[0-9a-f]{24}$")
_FILE_ID_RE = re.compile(r"^dsfile_[0-9a-f]{24}$")
_WINDOWS_ILLEGAL_RE = re.compile(r'[<>:"|?*]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class DatasetStoreError(ValueError):
    """Base error for dataset upload storage."""


class DatasetUploadPathError(DatasetStoreError):
    pass


class DatasetUploadSessionError(DatasetStoreError):
    pass


class DatasetUploadChecksumError(DatasetStoreError):
    pass


class DatasetUploadQuotaError(DatasetStoreError):
    pass


@dataclass(frozen=True)
class DatasetStoreQuota:
    max_files: int = 20_000
    max_file_size: int = 2 * 1024**4
    max_total_size: int = 20 * 1024**4
    max_owner_reserved_bytes: int = 20 * 1024**4
    max_owner_active_sessions: int = 10
    min_free_bytes: int = 1024**3
    chunk_size: int = 4 * 1024**2
    session_ttl_seconds: float = 24 * 3600.0

    def __post_init__(self) -> None:
        for name in (
            "max_files",
            "max_file_size",
            "max_total_size",
            "max_owner_reserved_bytes",
            "max_owner_active_sessions",
            "chunk_size",
        ):
            if int(getattr(self, name)) <= 0:
                raise DatasetUploadQuotaError(f"{name} must be greater than zero")
        if int(self.min_free_bytes) < 0 or float(self.session_ttl_seconds) <= 0:
            raise DatasetUploadQuotaError("dataset upload quota is invalid")


@dataclass(frozen=True)
class DatasetUploadFile:
    file_id: str
    relative_path: str
    expected_size: int
    expected_checksum: str
    received_bytes: int
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "relative_path": self.relative_path,
            "expected_size": self.expected_size,
            # ``server`` is an internal sentinel used by browser uploads.  Do
            # not make it part of the public protocol; after finalize this is
            # always the materialized sha256 value.
            "expected_checksum": "" if self.expected_checksum == _SERVER_CHECKSUM else self.expected_checksum,
            "received_bytes": self.received_bytes,
            "status": self.status,
        }


@dataclass(frozen=True)
class DatasetUploadSession:
    session_id: str
    owner: str
    project: str
    source_kind: str
    evidence_ref: str
    manifest_fingerprint: str
    total_size: int
    chunk_size: int
    status: str
    files: tuple[DatasetUploadFile, ...]
    created_at: float
    updated_at: float
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "project": self.project,
            "manifest_fingerprint": self.manifest_fingerprint,
            "total_size": self.total_size,
            "chunk_size": self.chunk_size,
            "status": self.status,
            "files": [item.to_dict() for item in self.files],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class FinalizedDatasetUpload:
    session_id: str
    owner: str
    project: str
    source_kind: str
    evidence_ref: str
    manifest_fingerprint: str
    storage_ref: str
    source_location: str
    files: tuple[DatasetUploadFile, ...]
    reused: bool


class DatasetStore:
    """SQLite-backed resumable store for one private multi-file dataset."""

    def __init__(
        self,
        root: str | Path,
        db_path: str | Path | None = None,
        *,
        quota: DatasetStoreQuota | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._db_path = str(Path(db_path) if db_path is not None else self._root / ".store" / "uploads.db")
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._quota = quota or DatasetStoreQuota()
        self._now_fn = now_fn
        self._lock = threading.RLock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS dataset_upload_sessions (
                    session_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    project TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL DEFAULT '',
                    manifest_fingerprint TEXT NOT NULL,
                    total_size INTEGER NOT NULL,
                    chunk_size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dataset_sessions_owner
                    ON dataset_upload_sessions(owner, status);
                CREATE TABLE IF NOT EXISTS dataset_upload_files (
                    file_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    expected_size INTEGER NOT NULL,
                    expected_checksum TEXT NOT NULL,
                    received_bytes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    UNIQUE(session_id, ordinal),
                    UNIQUE(session_id, relative_path),
                    FOREIGN KEY(session_id) REFERENCES dataset_upload_sessions(session_id)
                );
                CREATE TABLE IF NOT EXISTS dataset_upload_chunks (
                    file_id TEXT NOT NULL,
                    offset INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    checksum TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(file_id, offset),
                    FOREIGN KEY(file_id) REFERENCES dataset_upload_files(file_id)
                );
                CREATE TABLE IF NOT EXISTS dataset_upload_finalized (
                    owner TEXT NOT NULL,
                    project TEXT NOT NULL,
                    manifest_fingerprint TEXT NOT NULL,
                    storage_ref TEXT NOT NULL,
                    source_location TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(owner, project, manifest_fingerprint)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_dataset_finalized_storage_ref
                    ON dataset_upload_finalized(storage_ref);
                """
            )

    def create_session(
        self,
        *,
        owner: str,
        project: str,
        files: Iterable[Mapping[str, Any]],
        source_kind: str = "central_upload",
        evidence_ref: str = "",
    ) -> DatasetUploadSession:
        owner = normalize_user(owner)
        project = _validate_project(project)
        if source_kind not in {"central_upload", "agent_upload"}:
            raise DatasetUploadSessionError("dataset upload source kind is invalid")
        evidence_ref = _validate_evidence_ref(evidence_ref)
        if source_kind == "agent_upload" and not evidence_ref:
            raise DatasetUploadSessionError("agent dataset upload requires trusted stage evidence")
        manifest = _validate_manifest(
            files,
            self._quota,
            allow_server_checksum=source_kind == "central_upload",
        )
        fingerprint = _manifest_fingerprint(manifest)
        total_size = sum(item[1] for item in manifest)
        now = float(self._now_fn())
        session_id = "dsup_" + uuid.uuid4().hex[:24]
        self.cleanup_expired()
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(total_size),0) FROM dataset_upload_sessions "
                "WHERE owner=? AND status='active' AND expires_at>=?",
                (owner, now),
            ).fetchone()
            if int(active[0]) >= self._quota.max_owner_active_sessions:
                raise DatasetUploadQuotaError("owner active dataset upload limit reached")
            if int(active[1]) + total_size > self._quota.max_owner_reserved_bytes:
                raise DatasetUploadQuotaError("owner reserved dataset upload bytes exceeded")
            globally_reserved = int(
                conn.execute(
                    "SELECT COALESCE(SUM(total_size),0) FROM dataset_upload_sessions "
                    "WHERE status='active' AND expires_at>=?",
                    (now,),
                ).fetchone()[0]
            )
            free = shutil.disk_usage(self._root).free
            if free - globally_reserved - total_size < self._quota.min_free_bytes:
                raise DatasetUploadQuotaError("dataset store has insufficient free space")
            conn.execute(
                "INSERT INTO dataset_upload_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id, owner, project, source_kind, evidence_ref, fingerprint,
                    total_size, self._quota.chunk_size, "active", now, now,
                    now + self._quota.session_ttl_seconds,
                ),
            )
            for ordinal, (relative, size, checksum) in enumerate(manifest):
                conn.execute(
                    "INSERT INTO dataset_upload_files(file_id,session_id,ordinal,relative_path,expected_size,expected_checksum) "
                    "VALUES (?,?,?,?,?,?)",
                    ("dsfile_" + uuid.uuid4().hex[:24], session_id, ordinal, relative, size, checksum),
                )
            conn.commit()
        return self.get_session(session_id, owner=owner)

    def get_session(self, session_id: str, *, owner: str) -> DatasetUploadSession:
        if not _SESSION_RE.fullmatch(str(session_id or "")):
            raise DatasetUploadSessionError("dataset upload session is unavailable")
        owner = normalize_user(owner)
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM dataset_upload_sessions WHERE session_id=? AND owner=?",
                (session_id, owner),
            ).fetchone()
            if row is None:
                raise DatasetUploadSessionError("dataset upload session is unavailable")
            files = conn.execute(
                "SELECT * FROM dataset_upload_files WHERE session_id=? ORDER BY ordinal",
                (session_id,),
            ).fetchall()
        if row["status"] == "active" and float(row["expires_at"]) < float(self._now_fn()):
            raise DatasetUploadSessionError("dataset upload session has expired")
        return _session_from_rows(row, files)

    def append_file(
        self,
        session_id: str,
        file_id: str,
        *,
        owner: str,
        offset: int,
        data: bytes,
    ) -> DatasetUploadSession:
        data = bytes(data)
        offset = int(offset)
        if not _FILE_ID_RE.fullmatch(str(file_id or "")) or offset < 0:
            raise DatasetUploadSessionError("dataset upload file or offset is invalid")
        if not data or len(data) > self._quota.chunk_size:
            raise DatasetUploadSessionError("dataset upload chunk size is invalid")
        session = self.get_session(session_id, owner=owner)
        if session.status != "active":
            raise DatasetUploadSessionError("dataset upload session is not active")
        selected = next((item for item in session.files if item.file_id == file_id), None)
        if selected is None:
            raise DatasetUploadSessionError("dataset upload file is unavailable")
        if offset + len(data) > selected.expected_size:
            raise DatasetUploadSessionError("dataset upload chunk exceeds expected file size")
        chunk_checksum = "sha256:" + hashlib.sha256(data).hexdigest()
        target = self._staging_file(session_id, selected.relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        now = float(self._now_fn())
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT size,checksum FROM dataset_upload_chunks WHERE file_id=? AND offset=?",
                (file_id, offset),
            ).fetchone()
            if existing is not None:
                if int(existing["size"]) != len(data) or existing["checksum"] != chunk_checksum:
                    raise DatasetUploadSessionError("dataset upload retry chunk does not match")
                conn.commit()
                return self.get_session(session_id, owner=owner)
            current = conn.execute(
                "SELECT received_bytes FROM dataset_upload_files WHERE file_id=? AND session_id=?",
                (file_id, session_id),
            ).fetchone()
            if current is None or int(current[0]) != offset:
                expected = int(current[0]) if current is not None else 0
                raise DatasetUploadSessionError(f"Upload-Offset must equal {expected}")
            with target.open("r+b" if target.exists() else "wb") as handle:
                handle.seek(offset)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            conn.execute(
                "INSERT INTO dataset_upload_chunks VALUES (?,?,?,?,?)",
                (file_id, offset, len(data), chunk_checksum, now),
            )
            received = offset + len(data)
            status = "uploaded" if received == selected.expected_size else "active"
            conn.execute(
                "UPDATE dataset_upload_files SET received_bytes=?,status=? WHERE file_id=?",
                (received, status, file_id),
            )
            conn.execute(
                "UPDATE dataset_upload_sessions SET updated_at=? WHERE session_id=?",
                (now, session_id),
            )
            conn.commit()
        return self.get_session(session_id, owner=owner)

    def finalize(self, session_id: str, *, owner: str) -> FinalizedDatasetUpload:
        session = self.get_session(session_id, owner=owner)
        if session.status == "finalized":
            result = self._finalized(session, reused=True)
            self._verify_content_root(Path(result.source_location), session)
            return result
        if session.status != "active":
            raise DatasetUploadSessionError("dataset upload session cannot be finalized")
        session = self._materialize_server_checksums(session)
        target = self._content_path(session.owner, session.project, session.manifest_fingerprint)
        staging = self._staging_path(session.session_id)
        if staging.exists():
            self._verify_content_root(staging, session)
            self._write_private_manifest(staging, session)
        elif target.exists():
            # Recovery path for a crash after atomic rename but before DB commit.
            self._verify_content_root(target, session)
        else:
            raise DatasetUploadChecksumError("dataset upload staging content is unavailable")
        now = float(self._now_fn())
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM dataset_upload_finalized WHERE owner=? AND project=? AND manifest_fingerprint=?",
                (session.owner, session.project, session.manifest_fingerprint),
            ).fetchone()
            reused = existing is not None
            if existing is None:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    # Recover an interrupted finalize only after full revalidation.
                    self._verify_content_root(target, session)
                    reused = True
                else:
                    os.replace(str(staging), str(target))
                storage_ref = _storage_ref(session.owner, session.project, session.manifest_fingerprint)
                conn.execute(
                    "INSERT INTO dataset_upload_finalized VALUES (?,?,?,?,?,?)",
                    (session.owner, session.project, session.manifest_fingerprint, storage_ref, str(target), now),
                )
            else:
                storage_ref = str(existing["storage_ref"])
                self._verify_content_root(Path(str(existing["source_location"])), session)
                if staging.exists():
                    shutil.rmtree(staging)
            conn.execute(
                "UPDATE dataset_upload_sessions SET status='finalized',updated_at=? WHERE session_id=?",
                (now, session.session_id),
            )
            conn.execute(
                "UPDATE dataset_upload_files SET status='finalized' WHERE session_id=?",
                (session.session_id,),
            )
            conn.commit()
        return self._finalized(self.get_session(session_id, owner=owner), reused=reused)

    def _materialize_server_checksums(self, session: DatasetUploadSession) -> DatasetUploadSession:
        """Hash checksum-less browser files without loading them into memory.

        The manifest fingerprint is provisional while a browser upload is
        active.  Before any content-addressed path is chosen, replace every
        internal sentinel with the real digest and atomically update the
        session fingerprint.
        """
        pending = [item for item in session.files if item.expected_checksum == _SERVER_CHECKSUM]
        if not pending:
            return session
        staging = self._staging_path(session.session_id)
        checksums: dict[str, str] = {}
        for item in pending:
            path = _safe_child(staging, *PurePosixPath(item.relative_path).parts)
            if item.received_bytes != item.expected_size or not path.is_file():
                raise DatasetUploadChecksumError(f"dataset file is incomplete: {item.relative_path}")
            if path.stat().st_size != item.expected_size:
                raise DatasetUploadChecksumError(f"dataset file size mismatch: {item.relative_path}")
            checksums[item.file_id] = _sha256_file(path)

        final_manifest = tuple(
            (
                item.relative_path,
                item.expected_size,
                checksums.get(item.file_id, item.expected_checksum),
            )
            for item in session.files
        )
        fingerprint = _manifest_fingerprint(final_manifest)
        now = float(self._now_fn())
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM dataset_upload_sessions WHERE session_id=? AND owner=?",
                (session.session_id, session.owner),
            ).fetchone()
            if row is None or row["status"] != "active":
                raise DatasetUploadSessionError("dataset upload session is not active")
            for file_id, checksum in checksums.items():
                conn.execute(
                    "UPDATE dataset_upload_files SET expected_checksum=? WHERE file_id=? AND session_id=?",
                    (checksum, file_id, session.session_id),
                )
            conn.execute(
                "UPDATE dataset_upload_sessions SET manifest_fingerprint=?,updated_at=? WHERE session_id=?",
                (fingerprint, now, session.session_id),
            )
            conn.commit()
        return self.get_session(session.session_id, owner=session.owner)

    def cleanup_expired(self) -> int:
        """Expire abandoned sessions and remove their private staging bytes."""
        now = float(self._now_fn())
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT session_id FROM dataset_upload_sessions WHERE status='active' AND expires_at<?",
                (now,),
            ).fetchall()
            for row in rows:
                session_id = str(row["session_id"])
                file_ids = [
                    str(item[0])
                    for item in conn.execute(
                        "SELECT file_id FROM dataset_upload_files WHERE session_id=?", (session_id,)
                    ).fetchall()
                ]
                if file_ids:
                    placeholders = ",".join("?" for _ in file_ids)
                    conn.execute(f"DELETE FROM dataset_upload_chunks WHERE file_id IN ({placeholders})", file_ids)
                conn.execute(
                    "UPDATE dataset_upload_sessions SET status='expired',updated_at=? WHERE session_id=?",
                    (now, session_id),
                )
                conn.execute(
                    "UPDATE dataset_upload_files SET status='expired' WHERE session_id=?",
                    (session_id,),
                )
            conn.commit()
        for row in rows:
            staging = self._staging_path(str(row["session_id"]))
            if staging.exists():
                shutil.rmtree(staging)
        return len(rows)

    def _finalized(self, session: DatasetUploadSession, *, reused: bool) -> FinalizedDatasetUpload:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM dataset_upload_finalized WHERE owner=? AND project=? AND manifest_fingerprint=?",
                (session.owner, session.project, session.manifest_fingerprint),
            ).fetchone()
        if row is None:
            raise DatasetUploadSessionError("finalized dataset is unavailable")
        return FinalizedDatasetUpload(
            session_id=session.session_id,
            owner=session.owner,
            project=session.project,
            source_kind=session.source_kind,
            evidence_ref=session.evidence_ref,
            manifest_fingerprint=session.manifest_fingerprint,
            storage_ref=str(row["storage_ref"]),
            source_location=str(row["source_location"]),
            files=session.files,
            reused=bool(reused),
        )

    def _staging_path(self, session_id: str) -> Path:
        if not _SESSION_RE.fullmatch(session_id):
            raise DatasetUploadPathError("dataset staging session is invalid")
        return _safe_child(self._root, ".store", "staging", session_id)

    def _staging_file(self, session_id: str, relative_path: str) -> Path:
        return _safe_child(self._staging_path(session_id), *PurePosixPath(relative_path).parts)

    def _content_path(self, owner: str, project: str, fingerprint: str) -> Path:
        owner_key = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:24]
        digest = fingerprint.removeprefix("sha256:")
        return _safe_child(self._root, "content", owner_key, project, digest)

    @staticmethod
    def _verify_content_root(root: Path, session: DatasetUploadSession) -> None:
        for item in session.files:
            path = _safe_child(root, *PurePosixPath(item.relative_path).parts)
            if item.received_bytes != item.expected_size or not path.is_file():
                raise DatasetUploadChecksumError(f"dataset file is incomplete: {item.relative_path}")
            if path.stat().st_size != item.expected_size or _sha256_file(path) != item.expected_checksum:
                raise DatasetUploadChecksumError(f"dataset file checksum mismatch: {item.relative_path}")

    @staticmethod
    def _write_private_manifest(root: Path, session: DatasetUploadSession) -> None:
        path = _safe_child(root, ".dataset-manifest.json")
        payload = {
            "manifest_fingerprint": session.manifest_fingerprint,
            "files": [
                {
                    "relative_path": item.relative_path,
                    "size": item.expected_size,
                    "checksum": item.expected_checksum,
                }
                for item in session.files
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temp = path.with_suffix(".tmp")
        temp.write_bytes(encoded)
        os.replace(str(temp), str(path))


def _validate_manifest(
    files: Iterable[Mapping[str, Any]],
    quota: DatasetStoreQuota,
    *,
    allow_server_checksum: bool = False,
) -> tuple[tuple[str, int, str], ...]:
    items = list(files or [])
    if not items or len(items) > quota.max_files:
        raise DatasetUploadQuotaError("dataset upload file count is invalid")
    result: list[tuple[str, int, str]] = []
    seen: set[str] = set()
    total = 0
    for raw in items:
        relative = _validate_relative_path(str(raw.get("relative_path") or ""))
        collision_key = unicodedata.normalize("NFC", relative).casefold()
        if collision_key in seen:
            raise DatasetUploadPathError("dataset upload file paths collide case-insensitively")
        seen.add(collision_key)
        size = int(raw.get("size") or 0)
        checksum = str(raw.get("checksum") or "").strip().lower()
        if not checksum and allow_server_checksum:
            checksum = _SERVER_CHECKSUM
        if size <= 0 or size > quota.max_file_size:
            raise DatasetUploadQuotaError("dataset upload file size is invalid")
        if checksum != _SERVER_CHECKSUM and not _CHECKSUM_RE.fullmatch(checksum):
            raise DatasetUploadChecksumError("dataset upload file checksum is invalid")
        total += size
        if total > quota.max_total_size:
            raise DatasetUploadQuotaError("dataset upload total size exceeds quota")
        result.append((relative, size, checksum))
    result.sort(key=lambda item: (item[0].casefold(), item[0]))
    return tuple(result)


def _validate_relative_path(value: str) -> str:
    if not value or value != value.strip() or "\\" in value or unicodedata.normalize("NFC", value) != value:
        raise DatasetUploadPathError("dataset upload path must be normalized POSIX relative path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise DatasetUploadPathError("dataset upload path must be relative")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise DatasetUploadPathError("dataset upload path contains traversal or empty segment")
    if len(value) > 1024:
        raise DatasetUploadPathError("dataset upload path is too long")
    for part in posix.parts:
        stem = part.split(".", 1)[0].upper()
        if (
            len(part) > 255
            or stem in _WINDOWS_RESERVED
            or _WINDOWS_ILLEGAL_RE.search(part)
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in part)
            or part.endswith((".", " "))
        ):
            raise DatasetUploadPathError("dataset upload path contains an unsafe segment")
    normalized = posix.as_posix()
    if not is_input_mf4(Path(normalized)):
        raise DatasetUploadPathError("dataset upload accepts input MF4 files only")
    return normalized


def _validate_project(project: str) -> str:
    project = str(project or "").strip()
    if not _PROJECT_RE.fullmatch(project):
        raise DatasetUploadSessionError("dataset upload project is invalid")
    return project


def _validate_evidence_ref(value: str) -> str:
    value = str(value or "").strip()
    if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", value):
        raise DatasetUploadSessionError("dataset upload evidence reference is invalid")
    return value


def _manifest_fingerprint(files: Iterable[tuple[str, int, str]]) -> str:
    raw = json.dumps(list(files), ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _storage_ref(owner: str, project: str, fingerprint: str) -> str:
    opaque = hashlib.sha256("\0".join((owner, project, fingerprint)).encode("utf-8")).hexdigest()
    return f"shared://datasets/{project}/{opaque}"


def _safe_child(root: Path, *parts: str) -> Path:
    root = root.resolve()
    target = root.joinpath(*parts).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise DatasetUploadPathError("dataset store path escapes its root") from exc
    return target


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def default_dataset_root() -> Path:
    configured = os.environ.get("RSIM_DATASET_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    rsim_home = os.environ.get("RSIM_HOME", "").strip()
    if rsim_home:
        return Path(rsim_home).expanduser().resolve() / "datasets"
    return Path.home() / ".rsim" / "datasets"


def default_dataset_catalog_db_path() -> Path:
    path = default_dataset_root() / ".store" / "catalog.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _session_from_rows(row: sqlite3.Row, files: Iterable[sqlite3.Row]) -> DatasetUploadSession:
    return DatasetUploadSession(
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        project=str(row["project"]),
        source_kind=str(row["source_kind"]),
        evidence_ref=str(row["evidence_ref"]),
        manifest_fingerprint=str(row["manifest_fingerprint"]),
        total_size=int(row["total_size"]),
        chunk_size=int(row["chunk_size"]),
        status=str(row["status"]),
        files=tuple(
            DatasetUploadFile(
                file_id=str(item["file_id"]),
                relative_path=str(item["relative_path"]),
                expected_size=int(item["expected_size"]),
                expected_checksum=str(item["expected_checksum"]),
                received_bytes=int(item["received_bytes"]),
                status=str(item["status"]),
            )
            for item in files
        ),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        expires_at=float(row["expires_at"]),
    )


__all__ = [
    "DatasetStore", "DatasetStoreError", "DatasetStoreQuota", "DatasetUploadChecksumError",
    "DatasetUploadFile", "DatasetUploadPathError", "DatasetUploadQuotaError",
    "DatasetUploadSession", "DatasetUploadSessionError", "FinalizedDatasetUpload",
    "default_dataset_catalog_db_path", "default_dataset_root",
]
