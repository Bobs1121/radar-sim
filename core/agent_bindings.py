"""Local-only SQLite binding store for agent workspace registrations.

No network, no catalog, no scheduler.  Each binding maps a canonical
(project, workspace_path) → stable binding_id shared with the central
source-resolution runtime.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.agent_artifact_staging import AgentArtifactStagingError, AuthorizedRoots


class AgentBindingError(ValueError):
    """Stable binding-store error with path-free public messages."""


# ---------------------------------------------------------------------------
# Pure binding-id algorithm (shared with source_resolution_runtime)
# ---------------------------------------------------------------------------

def make_workspace_binding_id(project: str, workspace_path: str) -> str:
    """Return a stable logical workspace id without exposing absolute paths.

    The algorithm is intentionally identical to the legacy
    ``logical_workspace_binding_id`` so central and local stores agree on
    the same identifier for the same canonical (project, path) pair.
    """
    workspace_path = str(workspace_path or "").strip()
    if not workspace_path:
        return ""
    _validate_project_token(str(project or ""))
    normalized_path = re.sub(r"/+", "/", workspace_path.replace("\\", "/")).rstrip("/").casefold()
    payload = "\0".join([str(project or "").strip(), normalized_path])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"workspace:sha256:{digest[:24]}"


def make_workspace_path_id(workspace_path: str) -> str:
    """Project-independent opaque path id for project-free dispatch matching."""
    normalized_path = re.sub(r"/+", "/", str(workspace_path or "").strip().replace("\\", "/")).rstrip("/").casefold()
    if not normalized_path:
        return ""
    return "workspace-path:sha256:" + hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Immutable binding record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkspaceBinding:
    """Immutable local binding record.

    *binding_id* is the logical token (workspace:sha256:24hex).
    *project* is the strict logical project token.
    *workspace_root* and *output_roots* are resolved ``Path`` objects.
    *created_at* / *updated_at* are finite non-negative floats.
    """

    binding_id: str
    project: str
    workspace_root: Path
    output_roots: tuple[Path, ...]
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        _validate_binding_id(self.binding_id)
        _validate_project_token(self.project)
        object.__setattr__(self, "workspace_root", Path(self.workspace_root))
        object.__setattr__(self, "output_roots", tuple(Path(p) for p in self.output_roots))
        for ts_name, ts_value in (("created_at", self.created_at), ("updated_at", self.updated_at)):
            if not isinstance(ts_value, (int, float)) or math.isnan(ts_value) or math.isinf(ts_value) or ts_value < 0:
                raise AgentBindingError(f"{ts_name} must be a finite non-negative number")

    @property
    def public_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict containing **no paths**.

        Includes only logical identifiers, counts, booleans, and timestamps.
        """
        return {
            "id": self.binding_id,
            "path_id": make_workspace_path_id(str(self.workspace_root)),
            "project": self.project,
            "output_root_count": len(self.output_roots),
            "configured": True,
            "healthy": len(self.output_roots) > 0,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

_BINDING_ID_RE = re.compile(r"^workspace:sha256:[0-9a-f]{24}$")

def _validate_binding_id(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or not _BINDING_ID_RE.fullmatch(value):
        raise AgentBindingError("binding_id is invalid")


def _validate_project_token(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AgentBindingError("project must not be empty")
    if value != value.strip():
        raise AgentBindingError("project must not contain leading or trailing whitespace")
    if value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise AgentBindingError("project must be a logical token, not a path")


# ---------------------------------------------------------------------------
# Default db path
# ---------------------------------------------------------------------------

def default_agent_binding_db_path() -> Path:
    """Return the default local SQLite path for agent bindings.

    Preference:
    1. ``RSIM_HOME/agent/bindings.db`` if ``RSIM_HOME`` is set.
    2. ``~/.rsim/agent/bindings.db`` otherwise.

    Never uses the repository CWD.
    """
    rsim_home = os.environ.get("RSIM_HOME", "").strip()
    if rsim_home:
        base = Path(rsim_home).expanduser() / "agent"
    else:
        base = Path.home() / ".rsim" / "agent"
    return base / "bindings.db"


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------

class AgentBindingStore:
    """Thread-safe local SQLite store for workspace bindings.

    Uses WAL mode, a busy timeout, and a threading lock so multiple threads
    in the same process can share one store safely.  No network access.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_agent_binding_db_path()
        self._lock = threading.Lock()
        self._ensure_parent()
        self._init_schema()

    # -- internal helpers ----------------------------------------------------

    def _ensure_parent(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AgentBindingError("binding database directory is not writable") from exc

    def _connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            return conn
        except (sqlite3.Error, OSError) as exc:
            if conn is not None:
                conn.close()
            raise AgentBindingError("binding database is unavailable") from exc

    def _init_schema(self) -> None:
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS workspace_bindings (
                        binding_id TEXT PRIMARY KEY,
                        project TEXT NOT NULL,
                        workspace_root TEXT NOT NULL,
                        output_roots TEXT NOT NULL CHECK(json_valid(output_roots)),
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_workspace_bindings_project
                    ON workspace_bindings(project)
                    """
                )
        except AgentBindingError:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise AgentBindingError("binding database initialization failed") from exc

    @staticmethod
    def _now() -> float:
        now = time.time()
        if not math.isfinite(now) or now < 0:
            raise AgentBindingError("system clock is invalid")
        return now

    @staticmethod
    def _encode_roots(output_roots: tuple[Path, ...]) -> str:
        payload = [str(p) for p in output_roots]
        try:
            raw = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise AgentBindingError("output_roots serialization failed") from exc
        # Strict round-trip validation.
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise AgentBindingError("output_roots JSON shape is invalid")
        return raw

    @staticmethod
    def _decode_roots(raw: str) -> tuple[Path, ...]:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise AgentBindingError("stored output_roots JSON is malformed") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise AgentBindingError("stored output_roots JSON shape is invalid")
        return tuple(Path(p) for p in parsed)

    @staticmethod
    def _row_to_binding(row: sqlite3.Row) -> WorkspaceBinding:
        return WorkspaceBinding(
            binding_id=row["binding_id"],
            project=row["project"],
            workspace_root=Path(row["workspace_root"]),
            output_roots=AgentBindingStore._decode_roots(row["output_roots"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -- public API ----------------------------------------------------------

    def register(
        self,
        project: str,
        workspace_root: str | Path,
        output_roots: tuple[str | Path, ...],
    ) -> WorkspaceBinding:
        """Register or update a binding for *project* + *workspace_root*.

        Steps:
        1. Validate via :class:`AuthorizedRoots` (resolves paths, checks
           containment, symlinks, drive roots, etc.).
        2. Compute a canonical binding_id via :func:`make_workspace_binding_id`.
        3. Upsert with ``BEGIN IMMEDIATE`` so concurrent registrations for the
           same canonical pair are serialized and idempotent.
        4. Retain ``created_at`` when updating output roots only.

        Raises :class:`AgentBindingError` on validation or DB failure.
        """
        _validate_project_token(project)

        # Validate filesystem authorization (resolves paths, checks containment).
        try:
            authorized = AuthorizedRoots(workspace_root=workspace_root, output_roots=output_roots)
        except AgentArtifactStagingError as exc:
            raise AgentBindingError(str(exc)) from exc

        canonical_workspace = str(authorized.workspace_root)
        canonical_outputs = authorized.output_roots
        binding_id = make_workspace_binding_id(project, canonical_workspace)
        if not binding_id:
            raise AgentBindingError("workspace_path is required")

        now = self._now()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
            raise AgentBindingError("system clock is invalid")

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT project, workspace_root, created_at FROM workspace_bindings WHERE binding_id = ?",
                    (binding_id,),
                ).fetchone()
                if row is None:
                    created_at = now
                else:
                    if row["project"] != project or Path(row["workspace_root"]) != Path(canonical_workspace):
                        raise AgentBindingError("workspace binding id collision")
                    created_at = row["created_at"]
                conn.execute(
                    """
                    INSERT INTO workspace_bindings(binding_id, project, workspace_root, output_roots, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(binding_id) DO UPDATE SET
                        project=excluded.project,
                        workspace_root=excluded.workspace_root,
                        output_roots=excluded.output_roots,
                        created_at=excluded.created_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        binding_id,
                        project,
                        canonical_workspace,
                        self._encode_roots(canonical_outputs),
                        created_at,
                        now,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

        return WorkspaceBinding(
            binding_id=binding_id,
            project=project,
            workspace_root=Path(canonical_workspace),
            output_roots=canonical_outputs,
            created_at=created_at,
            updated_at=now,
        )

    def get(self, binding_id: str, project: str | None = None) -> WorkspaceBinding:
        """Fetch a binding by *binding_id*.

        If *project* is given, the binding must match it or
        :class:`AgentBindingError` is raised.

        Re-validates stored paths on every read so bindings whose directories
        have been removed are reported as unhealthy without leaking paths.
        """
        _validate_binding_id(binding_id)
        if project is not None:
            _validate_project_token(project)

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workspace_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()

        if row is None:
            raise AgentBindingError("binding not found")

        binding = self._row_to_binding(row)
        if project is not None and binding.project != project:
            raise AgentBindingError("binding project mismatch")

        # Re-validate paths on every read.
        self._revalidate_or_raise(binding)
        return binding

    def list(self, project: str | None = None) -> tuple[WorkspaceBinding, ...]:
        """Return all bindings, optionally filtered by *project*.

        Re-validates stored paths on every read.
        """
        if project is not None:
            _validate_project_token(project)

        with self._lock, self._connect() as conn:
            if project is not None:
                rows = conn.execute(
                    "SELECT * FROM workspace_bindings WHERE project = ? ORDER BY updated_at DESC",
                    (project,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM workspace_bindings ORDER BY updated_at DESC"
                ).fetchall()

        results: list[WorkspaceBinding] = []
        for row in rows:
            binding = self._row_to_binding(row)
            try:
                self._revalidate_or_raise(binding)
                results.append(binding)
            except AgentBindingError:
                # Skip unhealthy bindings in list views.
                continue
        return tuple(results)

    def delete(self, binding_id: str) -> None:
        """Delete a binding by *binding_id*.

        Raises :class:`AgentBindingError` if the binding does not exist.
        """
        _validate_binding_id(binding_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    "DELETE FROM workspace_bindings WHERE binding_id = ?",
                    (binding_id,),
                )
                if cursor.rowcount == 0:
                    raise AgentBindingError("binding not found")
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def resolve_authorized_roots(self, binding_id: str, project: str) -> AuthorizedRoots:
        """Return an :class:`AuthorizedRoots` for the given binding.

        Re-validates paths and raises :class:`AgentBindingError` if the binding
        is missing, mismatched, or unhealthy.
        """
        binding = self.get(binding_id, project=project)
        try:
            return AuthorizedRoots(
                workspace_root=binding.workspace_root,
                output_roots=binding.output_roots,
            )
        except AgentArtifactStagingError as exc:
            raise AgentBindingError(str(exc)) from exc

    # -- internal ------------------------------------------------------------

    def _revalidate_or_raise(self, binding: WorkspaceBinding) -> None:
        """Re-validate that stored paths still form a healthy authorization.

        Raises :class:`AgentBindingError` with a path-free message if any
        directory has disappeared or is no longer valid.
        """
        try:
            AuthorizedRoots(
                workspace_root=binding.workspace_root,
                output_roots=binding.output_roots,
            )
        except AgentArtifactStagingError as exc:
            raise AgentBindingError("binding is unhealthy") from exc


__all__ = [
    "AgentBindingError",
    "WorkspaceBinding",
    "AgentBindingStore",
    "make_workspace_binding_id",
    "default_agent_binding_db_path",
]
