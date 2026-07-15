"""Central filesystem artifact store for Selena binaries.

The store uses an administrator-configured filesystem root, but exposes only
normalized logical refs such as ``shared://selena/<project>/<user-path>/selena.exe``.

Security rules enforced by this module:
- No traversal (..), absolute, UNC, drive, or device paths as publish destinations.
- Symlink/reparse escape blocked via realpath containment.
- Hardlink tricks rejected where applicable (Windows).
- Upload sessions are isolated by UUID, resumed via SQLite, and finalized only
  after exact size + SHA-256 match.
- Same-checksum idempotent finalize reuses; different-checksum collision at the
  same logical path is rejected with a stable error.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.user import normalize_user

# Logical ref prefix used for all artifact storage references.
_ARTIFACT_REF_PREFIX = "shared://selena/"

# Segment validation: printable ASCII, no slashes, no control chars, no reserved names.
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.~!$&'()*+,;=:@-]+$")

# Chunk size default (4 MiB).
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024

# Internal reserved namespace segments that users cannot address.
_RESERVED_INTERNAL = {".store", "temp", "metadata", "sessions", "chunks", "artifact_finalized"}


class ArtifactStoreError(ValueError):
    """Base error for artifact store operations."""


class ArtifactPathError(ArtifactStoreError):
    """Raised when a logical or physical path violates safety rules."""


class ArtifactSessionError(ArtifactStoreError):
    """Raised when an upload session is invalid, expired, or mismatched."""


class ArtifactConflictError(ArtifactStoreError):
    """Raised when a different checksum already exists at the same logical path."""


class ArtifactChecksumError(ArtifactStoreError):
    """Raised when size or SHA-256 does not match on finalize."""


def _artifact_root() -> Path:
    """Return the configured artifact root.

    Priority:
    1. RSIM_ARTIFACT_ROOT environment variable.
    2. RSIM_HOME/artifacts for dev/test.
    3. User home ~/.rsim/artifacts as last fallback.
    """
    env_root = os.environ.get("RSIM_ARTIFACT_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    home = os.environ.get("RSIM_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve() / "artifacts"
    return Path.home() / ".rsim" / "artifacts"


def default_artifact_catalog_db_path() -> Path:
    """Return the central catalog DB path without exposing it in API results."""
    path = _artifact_root().resolve() / ".store" / "catalog.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _validate_segment(segment: str) -> str:
    """Return a cleaned path segment or raise ArtifactPathError."""
    segment = str(segment or "").strip()
    if not segment:
        raise ArtifactPathError("path segment must not be empty")
    if segment in {"..", "."}:
        raise ArtifactPathError(f"path segment must not be '{segment}'")
    if ".." in segment:
        raise ArtifactPathError("path segment must not contain '..'")
    # Windows reserved names (case-insensitive)
    if segment.endswith("."):
        raise ArtifactPathError("path segment must not end with a dot")
    if segment.split(".", 1)[0].upper() in _RESERVED_NAMES:
        raise ArtifactPathError(f"path segment uses reserved name: {segment}")
    if not _SEGMENT_RE.fullmatch(segment):
        raise ArtifactPathError(f"path segment contains illegal characters: {segment}")
    return segment


def _validate_logical_path(logical_path: str) -> str:
    """Validate a user-chosen relative logical path.

    The path must be relative, use forward slashes, contain no traversal,
    and must not start with a slash, backslash, drive letter, or UNC prefix.
    """
    path = str(logical_path or "").strip()
    if not path:
        raise ArtifactPathError("logical path must not be empty")
    if path.startswith("/") or path.startswith("\\"):
        raise ArtifactPathError("logical path must not be absolute")
    if len(path) >= 2 and path[1] == ":":
        raise ArtifactPathError("logical path must not contain a drive letter")
    if path.startswith(".."):
        raise ArtifactPathError("logical path must not start with '..'")
    # Normalize to POSIX separators and validate each segment.
    normalized = path.replace("\\", "/")
    segments = [seg for seg in normalized.split("/") if seg]
    for seg in segments:
        if seg.lower() in _RESERVED_INTERNAL:
            raise ArtifactPathError(f"logical path uses reserved internal namespace: {seg}")
    cleaned = "/".join(_validate_segment(seg) for seg in segments)
    return cleaned


def _normalize_publish_path(logical_path: str, object_filename: str = "selena.exe") -> str:
    """Normalize a publish path to exactly one configured final object name.

    If the input already ends with 'selena.exe' (case-insensitive), preserve it.
    Otherwise append '/selena.exe'.
    """
    path = str(logical_path or "").strip()
    if path.replace("\\", "/").rsplit("/", 1)[-1].lower() == object_filename.lower():
        return path
    if path and not path.endswith("/"):
        path += "/"
    return path + object_filename


def _comparison_path(path: Path) -> str:
    """Return a canonical path string used only for containment comparison.

    On Windows, ``Path.resolve()`` may return either ``C:\\...`` or the
    equivalent extended-length form ``\\\\?\\C:\\...`` depending on whether
    the target exists at the instant it is resolved.  Concurrent finalizers
    can therefore observe two spellings for the same path.  Strip only that
    representation prefix, then apply the platform's case normalization.
    """
    text = str(path.resolve(strict=False))
    if os.name == "nt":
        lowered = text.lower()
        if lowered.startswith("\\\\?\\unc\\"):
            text = "\\\\" + text[8:]
        elif lowered.startswith("\\\\?\\"):
            text = text[4:]
    return os.path.normcase(os.path.normpath(text))


def _is_contained(root: Path, target: Path) -> bool:
    root_text = _comparison_path(root)
    target_text = _comparison_path(target)
    try:
        return os.path.commonpath((root_text, target_text)) == root_text
    except ValueError:
        # Different Windows drives or otherwise incomparable paths.
        return False


def _validate_containment(root: Path, target: Path) -> None:
    """Ensure resolved target is under root, rejecting symlink/reparse escapes."""
    resolved_root = root.resolve(strict=False)
    if not _is_contained(resolved_root, target):
        raise ArtifactPathError("logical path escapes artifact root")
    # Detect symlink / reparse point escape on Windows and Unix.
    # Walk the pre-resolution path and flag any symlink or reparse point
    # that resolves outside the root.
    for part in target.parents:
        if part == root or part == resolved_root:
            break
        if part.is_symlink():
            link_target = part.readlink()
            resolved_link = (part.parent / link_target).resolve(strict=False)
            if not _is_contained(resolved_root, resolved_link):
                raise ArtifactPathError("symlink escape detected in artifact path")
        if os.name == "nt":
            try:
                if part.exists() and part.lstat().st_reparse_tag != 0:
                    # Reparse point (junction/mount point): verify target stays under root.
                    # resolve() follows reparse points; if the resolved path escapes,
                    # the initial relative_to check above would have caught it.
                    # But for defense-in-depth, we also check the reparse target directly.
                    resolved_reparse = part.resolve(strict=False)
                    if not _is_contained(resolved_root, resolved_reparse):
                        raise ArtifactPathError("reparse point escape detected in artifact path")
            except AttributeError:
                pass


def _logical_to_physical(root: Path, project: str, logical_path: str) -> Path:
    """Map a validated logical path to a physical path under root/content/<project>.

    Containment is verified via resolved paths. Symlink/reparse escape is
    rejected.
    """
    # Physical layout: root/content/<project>/<normalized-publish-path>
    content_dir = root / "content" / project
    target = content_dir / logical_path
    _validate_containment(root, target)
    target = target.resolve(strict=False)
    # Detect hardlink tricks: if the final path exists and has more than one
    # link, reject it to prevent cross-directory hardlink attacks.
    if target.exists():
        try:
            if target.lstat().st_nlink > 1:
                raise ArtifactPathError("hardlink detected at artifact path")
        except (OSError, AttributeError):
            pass
    return target


def _make_storage_ref(project: str, logical_path: str, prefix: str = _ARTIFACT_REF_PREFIX) -> str:
    """Return a stable logical storage reference."""
    return f"{prefix}{project}/{logical_path}"


def _parse_storage_ref(ref: str, prefix: str = _ARTIFACT_REF_PREFIX) -> tuple[str, str]:
    """Parse a storage ref into (project, logical_path).

    Raises ArtifactPathError if the ref does not match the expected prefix.
    """
    if not ref.startswith(prefix):
        raise ArtifactPathError(f"storage_ref must start with {prefix}")
    rest = ref[len(prefix):]
    parts = rest.split("/", 1)
    if len(parts) != 2:
        raise ArtifactPathError("storage_ref must contain project and logical path")
    return parts[0], parts[1]


def _validate_checksum(value: str) -> str:
    """Validate expected_checksum format: sha256:<64 lowercase hex>."""
    checksum = str(value or "").strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum):
        raise ArtifactSessionError("expected_checksum must match sha256:<64 lowercase hex>")
    return checksum


def _validate_size(value: int) -> int:
    """Validate expected_size is > 0."""
    size = int(value)
    if size <= 0:
        raise ArtifactSessionError("expected_size must be greater than 0")
    return size


def _validate_evidence_ref(value: str) -> str:
    ref = str(value or "").strip()
    if ref and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", ref):
        raise ArtifactSessionError("evidence_ref must be a logical identifier")
    return ref


@dataclass(frozen=True)
class UploadSession:
    """Immutable snapshot of an upload session."""

    session_id: str
    owner: str
    project: str
    logical_path: str
    storage_ref: str
    evidence_ref: str
    expected_size: int
    expected_checksum: str
    chunk_size: int
    received_bytes: int
    status: str
    created_at: float
    updated_at: float
    expires_at: float


class ArtifactStore:
    """Central filesystem artifact store with SQLite session persistence."""

    def __init__(
        self,
        root: Path | None = None,
        db_path: Path | str | None = None,
        *,
        now_fn: Callable[[], float] = time.time,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        object_filename: str = "selena.exe",
        storage_ref_prefix: str = _ARTIFACT_REF_PREFIX,
    ) -> None:
        self._root = (root or _artifact_root()).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn
        self._chunk_size = int(chunk_size)
        self._object_filename = _validate_segment(str(object_filename or ""))
        self._storage_ref_prefix = str(storage_ref_prefix or "").strip()
        if not re.fullmatch(r"shared://[a-z0-9][a-z0-9-]*/", self._storage_ref_prefix):
            raise ArtifactPathError("storage reference prefix is invalid")
        if db_path is None:
            self._db_path = str(self._root / ".store" / "sessions.db")
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        else:
            self._db_path = str(db_path)
        self._lock = threading.RLock()
        self._init_schema()

    def _now(self) -> float:
        return float(self._now_fn())

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS artifact_upload_sessions (
                        session_id TEXT PRIMARY KEY,
                        owner TEXT NOT NULL,
                        project TEXT NOT NULL,
                        logical_path TEXT NOT NULL,
                        storage_ref TEXT NOT NULL,
                        evidence_ref TEXT NOT NULL DEFAULT '',
                        expected_size INTEGER NOT NULL,
                        expected_checksum TEXT NOT NULL,
                        chunk_size INTEGER NOT NULL,
                        received_bytes INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_sessions_owner
                        ON artifact_upload_sessions(owner, status);
                    CREATE INDEX IF NOT EXISTS idx_sessions_project
                        ON artifact_upload_sessions(project, logical_path);

                    CREATE TABLE IF NOT EXISTS artifact_chunks (
                        chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        offset INTEGER NOT NULL,
                        size INTEGER NOT NULL,
                        checksum TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        UNIQUE(session_id, offset),
                        FOREIGN KEY(session_id) REFERENCES artifact_upload_sessions(session_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_chunks_session
                        ON artifact_chunks(session_id, offset);

                    CREATE TABLE IF NOT EXISTS artifact_finalized (
                        artifact_id TEXT PRIMARY KEY,
                        project TEXT NOT NULL,
                        logical_path TEXT NOT NULL,
                        storage_ref TEXT NOT NULL UNIQUE,
                        checksum TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        owner TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        UNIQUE(project, logical_path)
                    );
                    CREATE INDEX IF NOT EXISTS idx_finalized_storage_ref
                        ON artifact_finalized(storage_ref);
                    """
                )
                columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(artifact_upload_sessions)").fetchall()
                }
                if "evidence_ref" not in columns:
                    conn.execute(
                        "ALTER TABLE artifact_upload_sessions ADD COLUMN evidence_ref TEXT NOT NULL DEFAULT ''"
                    )
                conn.commit()
            finally:
                conn.close()

    def create_upload_session(
        self,
        owner: str,
        project: str,
        logical_path: str,
        expected_size: int,
        expected_checksum: str,
        *,
        evidence_ref: str = "",
        expires_after_seconds: float = 3600.0,
    ) -> UploadSession:
        """Create a new upload session.

        Validates the logical path, ensures it does not escape the root, and
        persists the session in SQLite so it survives process restarts.
        """
        owner = normalize_user(owner)
        project = _validate_segment(project)
        logical_path = _validate_logical_path(logical_path)
        logical_path = _normalize_publish_path(logical_path, self._object_filename)
        expected_size = _validate_size(expected_size)
        expected_checksum = _validate_checksum(expected_checksum)
        evidence_ref = _validate_evidence_ref(evidence_ref)
        # Validate containment at session creation time.
        _logical_to_physical(self._root, project, logical_path)
        storage_ref = _make_storage_ref(project, logical_path, self._storage_ref_prefix)
        now = self._now()
        expires = now + float(expires_after_seconds)
        session_id = f"upload_{uuid.uuid4().hex[:16]}"
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO artifact_upload_sessions (
                        session_id, owner, project, logical_path, storage_ref, evidence_ref,
                        expected_size, expected_checksum, chunk_size,
                        received_bytes, status, created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        owner,
                        project,
                        logical_path,
                        storage_ref,
                        evidence_ref,
                        expected_size,
                        expected_checksum,
                        self._chunk_size,
                        0,
                        "active",
                        now,
                        now,
                        expires,
                    ),
                )
                conn.commit()
                return self._get_session_locked(conn, session_id)
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def get_session(self, session_id: str, *, owner: str = "") -> UploadSession:
        """Return an upload session, including its terminal status."""
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ArtifactSessionError("session_id is required")
        with self._lock:
            conn = self._conn()
            try:
                session = self._get_session_locked(conn, session_id)
            finally:
                conn.close()
        if owner and session.owner != normalize_user(owner):
            raise ArtifactSessionError("session owner mismatch")
        if session.status == "active" and session.expires_at < self._now():
            raise ArtifactSessionError("session has expired")
        return session

    def append_chunk(
        self,
        session_id: str,
        offset: int,
        data: bytes,
        *,
        owner: str = "",
    ) -> UploadSession:
        """Append a chunk at an exact offset.

        The chunk is written to a temporary file under the store root, and its
        metadata is persisted in SQLite. Overwriting an existing offset is
        allowed for safe retry/restart ONLY if size/checksum match.
        Contiguous offset semantics: a new chunk offset must equal current
        received_bytes. received_bytes means contiguous committed prefix.
        """
        offset = int(offset)
        data = bytes(data)
        if offset < 0:
            raise ArtifactSessionError("offset must be non-negative")
        if not data:
            raise ArtifactSessionError("chunk data must not be empty")
        session = self.get_session(session_id, owner=owner)
        if session.status != "active":
            raise ArtifactSessionError(f"session is not active: {session.status}")
        if offset + len(data) > session.expected_size:
            raise ArtifactSessionError("chunk exceeds expected total size")
        now = self._now()
        chunk_checksum = "sha256:" + hashlib.sha256(data).hexdigest()
        temp_path = self._temp_path(session_id)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                # Check existing chunk at this offset.
                existing = conn.execute(
                    "SELECT size, checksum FROM artifact_chunks WHERE session_id=? AND offset=?",
                    (session_id, offset),
                ).fetchone()
                if existing is not None:
                    # Idempotent only if exact match.
                    if int(existing["size"]) != len(data) or str(existing["checksum"]).strip().lower() != chunk_checksum:
                        raise ArtifactSessionError(
                            "chunk at this offset already exists with different size/checksum"
                        )
                    # Exact retry: nothing to do.
                    conn.commit()
                    return self._get_session_locked(conn, session_id)
                # Validate contiguous semantics.
                current_received = conn.execute(
                    "SELECT received_bytes FROM artifact_upload_sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
                if offset != current_received:
                    raise ArtifactSessionError(
                        f"chunk offset {offset} does not match contiguous received_bytes {current_received}"
                    )
                # Write chunk to temp file at exact offset.
                with open(temp_path, "r+b" if temp_path.exists() else "wb") as f:
                    f.seek(offset)
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                # Insert chunk metadata.
                conn.execute(
                    """
                    INSERT INTO artifact_chunks (session_id, offset, size, checksum, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (session_id, offset, len(data), chunk_checksum, now),
                )
                new_received = offset + len(data)
                conn.execute(
                    "UPDATE artifact_upload_sessions SET received_bytes=?, updated_at=? WHERE session_id=?",
                    (new_received, now, session_id),
                )
                conn.commit()
                return self._get_session_locked(conn, session_id)
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def finalize_upload(
        self,
        session_id: str,
        *,
        owner: str = "",
        declared_size: int | None = None,
        declared_checksum: str | None = None,
    ) -> dict[str, Any]:
        """Finalize an upload after exact size and SHA-256 match.

        Returns a dict with ``storage_ref``, ``checksum``, ``size``, and
        ``artifact_id`` (or existing artifact info on idempotent reuse).

        Rules:
        - The concatenated temp file must exactly match expected_size.
        - SHA-256 must match expected_checksum.
        - received_bytes must equal expected_size.
        - If the same logical path already has the same checksum, reuse.
        - If the same logical path has a different checksum, raise
          ArtifactConflictError.
        - The final file is moved atomically (via os.replace) to the target
          physical path, then registered in the catalog.
        - Never overwrite an untracked existing target. If target exists and DB
          row is absent, hash it: same checksum may recover idempotently;
          different checksum is a stable conflict.
        """
        session = self.get_session(session_id, owner=owner)
        if session.status == "finalized":
            existing = self.lookup_by_storage_ref(session.storage_ref)
            return {
                "session_id": session_id,
                "status": "finalized",
                "artifact_id": existing["artifact_id"],
                "storage_ref": session.storage_ref,
                "checksum": existing["checksum"],
                "size": existing["size"],
                "reused": True,
            }
        if session.status != "active":
            raise ArtifactSessionError(f"session cannot be finalized: {session.status}")
        temp_path = self._temp_path(session_id)
        now = self._now()
        # Verify contiguous received bytes equals expected size.
        if session.received_bytes != session.expected_size:
            raise ArtifactChecksumError(
                f"size mismatch: expected {session.expected_size}, received {session.received_bytes}"
            )
        # Verify actual file size.
        actual_size = temp_path.stat().st_size if temp_path.exists() else 0
        if actual_size != session.expected_size:
            raise ArtifactChecksumError(
                f"size mismatch: expected {session.expected_size}, got {actual_size}"
            )
        # Verify checksum.
        hasher = hashlib.sha256()
        if temp_path.exists():
            with open(temp_path, "rb") as f:
                while True:
                    block = f.read(65536)
                    if not block:
                        break
                    hasher.update(block)
        actual_checksum = "sha256:" + hasher.hexdigest()
        if actual_checksum != session.expected_checksum:
            raise ArtifactChecksumError(
                f"checksum mismatch: expected {session.expected_checksum}, got {actual_checksum}"
            )
        # Optional caller declarations (for extra safety).
        if declared_size is not None and int(declared_size) != actual_size:
            raise ArtifactChecksumError("declared size does not match actual size")
        if declared_checksum is not None and str(declared_checksum).strip().lower() != actual_checksum:
            raise ArtifactChecksumError("declared checksum does not match actual checksum")

        target_physical = _logical_to_physical(self._root, session.project, session.logical_path)
        target_physical.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                # Check for existing finalized artifact at the same logical path.
                existing = conn.execute(
                    "SELECT artifact_id, checksum FROM artifact_finalized WHERE logical_path=? AND project=?",
                    (session.logical_path, session.project),
                ).fetchone()
                if existing is not None:
                    existing_checksum = str(existing["checksum"] or "").strip().lower()
                    if existing_checksum == actual_checksum:
                        # Idempotent reuse.
                        conn.execute(
                            "UPDATE artifact_upload_sessions SET status='finalized', updated_at=? WHERE session_id=?",
                            (now, session_id),
                        )
                        conn.commit()
                        return {
                            "session_id": session_id,
                            "status": "finalized",
                            "artifact_id": str(existing["artifact_id"]),
                            "storage_ref": session.storage_ref,
                            "checksum": actual_checksum,
                            "size": actual_size,
                            "reused": True,
                        }
                    raise ArtifactConflictError(
                        "different checksum already exists at this project/path"
                    )

                # Handle untracked existing target.
                if target_physical.exists():
                    existing_hash = hashlib.sha256()
                    with open(target_physical, "rb") as f:
                        while True:
                            block = f.read(65536)
                            if not block:
                                break
                            existing_hash.update(block)
                    existing_file_checksum = "sha256:" + existing_hash.hexdigest()
                    if existing_file_checksum == actual_checksum:
                        # Same checksum: recover idempotently by registering the existing file.
                        artifact_id = f"art_{uuid.uuid4().hex[:16]}"
                        conn.execute(
                            """
                            INSERT INTO artifact_finalized (
                                artifact_id, project, logical_path, storage_ref,
                                checksum, size, owner, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                artifact_id,
                                session.project,
                                session.logical_path,
                                session.storage_ref,
                                actual_checksum,
                                actual_size,
                                session.owner,
                                now,
                            ),
                        )
                        conn.execute(
                            "UPDATE artifact_upload_sessions SET status='finalized', updated_at=? WHERE session_id=?",
                            (now, session_id),
                        )
                        conn.commit()
                        return {
                            "session_id": session_id,
                            "status": "finalized",
                            "artifact_id": artifact_id,
                            "storage_ref": session.storage_ref,
                            "checksum": actual_checksum,
                            "size": actual_size,
                            "reused": True,
                        }
                    raise ArtifactConflictError(
                        "different checksum already exists at this project/path"
                    )

                # Atomic move: fsync parent directory for recovery-friendliness.
                if temp_path.exists():
                    os.replace(str(temp_path), str(target_physical))
                    try:
                        parent_fd = os.open(str(target_physical.parent), os.O_RDONLY | os.O_DIRECTORY)
                        os.fsync(parent_fd)
                        os.close(parent_fd)
                    except (OSError, AttributeError):
                        pass
                # Record finalized artifact.
                artifact_id = f"art_{uuid.uuid4().hex[:16]}"
                conn.execute(
                    """
                    INSERT INTO artifact_finalized (
                        artifact_id, project, logical_path, storage_ref,
                        checksum, size, owner, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        session.project,
                        session.logical_path,
                        session.storage_ref,
                        actual_checksum,
                        actual_size,
                        session.owner,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE artifact_upload_sessions SET status='finalized', updated_at=? WHERE session_id=?",
                    (now, session_id),
                )
                conn.commit()
                return {
                    "session_id": session_id,
                    "status": "finalized",
                    "artifact_id": artifact_id,
                    "storage_ref": session.storage_ref,
                    "checksum": actual_checksum,
                    "size": actual_size,
                    "reused": False,
                }
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def lookup_by_storage_ref(self, storage_ref: str, *, owner: str = "") -> dict[str, Any]:
        """Return finalized artifact metadata for a storage_ref.

        All finalized artifacts are visible to any caller (shared visibility).
        """
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM artifact_finalized WHERE storage_ref=?",
                    (str(storage_ref),),
                ).fetchone()
                if row is None:
                    raise ArtifactStoreError("artifact not found for ref")
                return {
                    "artifact_id": row["artifact_id"],
                    "project": row["project"],
                    "logical_path": row["logical_path"],
                    "storage_ref": row["storage_ref"],
                    "checksum": row["checksum"],
                    "size": row["size"],
                    "owner": row["owner"],
                    "created_at": row["created_at"],
                }
            finally:
                conn.close()

    def resolve_location(self, storage_ref: str, *, verify_checksum: bool = True) -> Path:
        """Resolve a finalized logical ref to a private, contained file path.

        Trusted executors use this method; public response builders must keep
        using :meth:`lookup_by_storage_ref`, which intentionally has no path.
        Size and checksum are revalidated so replaced content cannot be staged
        into a Cluster run under an existing ArtifactRef.
        """
        metadata = self.lookup_by_storage_ref(storage_ref)
        ref_project, logical_path = _parse_storage_ref(storage_ref, self._storage_ref_prefix)
        if ref_project != str(metadata.get("project") or ""):
            raise ArtifactStoreError("artifact storage reference project mismatch")
        target = _logical_to_physical(self._root, ref_project, logical_path)
        if not target.is_file():
            raise ArtifactStoreError("artifact content is unavailable")
        if int(target.stat().st_size) != int(metadata.get("size") or -1):
            raise ArtifactStoreError("artifact content size mismatch")
        if verify_checksum:
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            actual = "sha256:" + digest.hexdigest()
            if actual != str(metadata.get("checksum") or "").lower():
                raise ArtifactStoreError("artifact content checksum mismatch")
        return target

    def list_finalized(
        self,
        *,
        project: str | None = None,
        owner: str | None = None,
    ) -> list[dict[str, Any]]:
        """List finalized artifacts with optional filtering."""
        clauses: list[str] = []
        params: list[Any] = []
        if project:
            clauses.append("project = ?")
            params.append(str(project).strip())
        if owner:
            clauses.append("owner = ?")
            params.append(normalize_user(owner))
        sql = "SELECT * FROM artifact_finalized"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(sql, params).fetchall()
                return [
                    {
                        "artifact_id": row["artifact_id"],
                        "project": row["project"],
                        "logical_path": row["logical_path"],
                        "storage_ref": row["storage_ref"],
                        "checksum": row["checksum"],
                        "size": row["size"],
                        "owner": row["owner"],
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ]
            finally:
                conn.close()

    def delete_session(self, session_id: str, *, owner: str = "") -> None:
        """Delete an upload session and its temp file."""
        session = self.get_session(session_id, owner=owner)
        temp_path = self._temp_path(session_id)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "DELETE FROM artifact_chunks WHERE session_id=?", (session_id,)
                )
                conn.execute(
                    "DELETE FROM artifact_upload_sessions WHERE session_id=?", (session_id,)
                )
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()
        if temp_path.exists():
            temp_path.unlink()

    def _temp_path(self, session_id: str) -> Path:
        """Return the temporary file path for a session."""
        # Store temp files under .store/temp/<session_id> to keep them isolated.
        return self._root / ".store" / "temp" / f"{session_id}.tmp"

    def _get_session_locked(self, conn: sqlite3.Connection, session_id: str) -> UploadSession:
        row = conn.execute(
            "SELECT * FROM artifact_upload_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise ArtifactSessionError("session not found")
        return UploadSession(
            session_id=row["session_id"],
            owner=row["owner"],
            project=row["project"],
            logical_path=row["logical_path"],
            storage_ref=row["storage_ref"],
            evidence_ref=row["evidence_ref"],
            expected_size=row["expected_size"],
            expected_checksum=row["expected_checksum"],
            chunk_size=row["chunk_size"],
            received_bytes=row["received_bytes"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )


__all__ = [
    "ArtifactChecksumError",
    "ArtifactConflictError",
    "ArtifactPathError",
    "ArtifactSessionError",
    "ArtifactStore",
    "ArtifactStoreError",
    "UploadSession",
    "DEFAULT_CHUNK_SIZE",
    "default_artifact_catalog_db_path",
]
