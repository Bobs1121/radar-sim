"""Windows-Agent authorization for readable MF4 data roots.

Data bindings are deliberately independent of Selena/workspace recognition.
Older connector versions keyed a binding by ``project``; that value is kept
as a compatibility projection, but new registrations are keyed by the
authenticated owner, device and a normalized root path.  This lets a Selena
recognizer change its internal project token without invalidating an already
authorized data root.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable

from core.agent_store_paths import default_agent_binding_db_path
from core.path_normalization import path_token, normalize_path_text


class AgentDataBindingError(ValueError):
    pass


_ID_RE = re.compile(r"^data-root:sha256:[0-9a-f]{24}$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _identity_token(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 256 or any(ord(char) < 0x20 for char in text):
        raise AgentDataBindingError(f"data binding {label} is invalid")
    return text


def _parse_binding_args(
    args: tuple[object, ...],
    *,
    project: str = "",
    root_path: str | os.PathLike[str] = "",
    owner: str = "",
    device: str = "",
    device_id: str = "",
) -> tuple[str, str, str, str, str]:
    """Accept both legacy ``(project, root)`` and v2 ``(owner, device, root)``.

    Keyword arguments are preferred by new code.  The two positional forms
    are retained because third-party connectors and older tests imported
    these helpers directly.
    """

    if len(args) == 3:
        if any((project, root_path, owner, device, device_id)):
            raise AgentDataBindingError("data binding arguments are ambiguous")
        owner, device, root_path = (str(args[0] or ""), str(args[1] or ""), str(args[2] or ""))
    elif len(args) == 2:
        if any((root_path, owner, device, device_id)):
            raise AgentDataBindingError("data binding arguments are ambiguous")
        project, root_path = str(args[0] or ""), str(args[1] or "")
    elif len(args) == 1:
        if root_path:
            # ``make_data_binding_id(root, owner=..., device=...)`` is not a
            # supported spelling; forcing explicit keywords avoids ambiguity.
            raise AgentDataBindingError("data binding arguments are ambiguous")
        root_path = str(args[0] or "")
    elif len(args) > 3:
        raise AgentDataBindingError("data binding arguments are invalid")

    if device and device_id and str(device) != str(device_id):
        raise AgentDataBindingError("data binding device is ambiguous")
    device = str(device or device_id or "").strip()
    project = str(project or "").strip()
    owner = str(owner or "").strip()
    root_path = str(root_path or "").strip()
    # A modern binding must carry both owner and device.  A missing modern
    # identity falls back to the old project-scoped token for compatibility.
    if owner or device:
        if not owner or not device:
            raise AgentDataBindingError("data binding owner and device are required")
        return "", owner, device, root_path, "modern"
    return project, "", "", root_path, "legacy"


def _make_binding_id(project: str, owner: str, device: str, root_path: str) -> str:
    normalized = _normalized_path_token(root_path)
    if not normalized:
        raise AgentDataBindingError("data binding root is required")
    if owner and device:
        identity = "v2\0" + owner + "\0" + device + "\0" + normalized
    else:
        if not _PROJECT_RE.fullmatch(project):
            raise AgentDataBindingError("data binding project is invalid")
        identity = "v1\0" + project + "\0" + normalized
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"data-root:sha256:{digest[:24]}"


def make_data_binding_id(*args: object, **kwargs: object) -> str:
    """Create an opaque binding id.

    Legacy: ``make_data_binding_id(project, root_path)``.
    Current: ``make_data_binding_id(owner, device_id, root_path)`` or the
    keyword form ``owner=..., device_id=..., root_path=...``.
    """

    project, owner, device, root, _kind = _parse_binding_args(
        tuple(args),
        project=str(kwargs.pop("project", "") or ""),
        root_path=str(kwargs.pop("root_path", "") or ""),
        owner=str(kwargs.pop("owner", "") or ""),
        device=str(kwargs.pop("device", "") or ""),
        device_id=str(kwargs.pop("device_id", "") or ""),
    )
    if kwargs:
        raise TypeError(f"unexpected data binding arguments: {', '.join(sorted(kwargs))}")
    if owner:
        owner = _identity_token(owner, label="owner")
        device = _identity_token(device, label="device")
    return _make_binding_id(project, owner, device, root)


def candidate_data_binding_ids(*args: object, **kwargs: object) -> tuple[str, ...]:
    """Return exact/ancestor ids central can compare to path-free adverts."""

    project, owner, device, data_path, kind = _parse_binding_args(
        tuple(args),
        project=str(kwargs.pop("project", "") or ""),
        root_path=str(kwargs.pop("data_path", kwargs.pop("root_path", "")) or ""),
        owner=str(kwargs.pop("owner", "") or ""),
        device=str(kwargs.pop("device", "") or ""),
        device_id=str(kwargs.pop("device_id", "") or ""),
    )
    if kwargs:
        raise TypeError(f"unexpected data binding arguments: {', '.join(sorted(kwargs))}")
    if owner:
        owner = _identity_token(owner, label="owner")
        device = _identity_token(device, label="device")
    text = normalize_path_text(data_path)
    path = PureWindowsPath(text)
    if not path.is_absolute() or not path.drive:
        return ()
    values = [path, *path.parents]
    result: list[str] = []
    for value in values:
        try:
            binding_id = _make_binding_id(project, owner, device, str(value))
        except AgentDataBindingError:
            continue
        if binding_id not in result:
            result.append(binding_id)
    return tuple(result)


@dataclass(frozen=True)
class DataRootBinding:
    binding_id: str
    project: str
    root_path: Path
    created_at: float
    updated_at: float
    owner: str = ""
    device_id: str = ""

    @property
    def device(self) -> str:
        """Alias used by newer connector payloads."""
        return self.device_id

    @property
    def public_dict(self) -> dict:
        value = {"id": self.binding_id, "healthy": True}
        if self.owner or self.device_id:
            value.update({"owner": self.owner, "device_id": self.device_id})
        else:
            # Keep the old path-free projection readable by old central
            # services.  New data never depends on this field.
            value["project"] = self.project
        return value


class AgentDataBindingStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_agent_binding_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS data_root_bindings (
                    binding_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL DEFAULT '',
                    root_path TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(project, root_path)
                )
                """
            )
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(data_root_bindings)").fetchall()
            }
            if "owner" not in columns:
                conn.execute("ALTER TABLE data_root_bindings ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
            if "device_id" not in columns:
                conn.execute("ALTER TABLE data_root_bindings ADD COLUMN device_id TEXT NOT NULL DEFAULT ''")
            conn.commit()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _storage_project(project: str, owner: str, device: str) -> str:
        # The old UNIQUE(project, root_path) constraint remains on migrated
        # stores.  A private owner/device marker prevents two modern devices
        # from colliding while the public projection omits this implementation
        # detail entirely.
        if owner and device:
            digest = hashlib.sha256((owner + "\0" + device).encode("utf-8")).hexdigest()[:32]
            return "@" + digest
        return project

    @staticmethod
    def _row_binding(row: sqlite3.Row) -> DataRootBinding:
        owner = str(row["owner"] or "") if "owner" in row.keys() else ""
        device = str(row["device_id"] or "") if "device_id" in row.keys() else ""
        # Legacy project is intentionally hidden from the modern projection.
        project = "" if owner or device else str(row["project"] or "")
        root = Path(str(row["root_path"])).resolve(strict=True)
        if not root.is_dir() or not os.access(root, os.R_OK):
            raise AgentDataBindingError("data binding is unhealthy")
        return DataRootBinding(
            binding_id=str(row["binding_id"]),
            project=project,
            root_path=root,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            owner=owner,
            device_id=device,
        )

    def register(
        self,
        project: str = "",
        root_path: str | Path = "",
        *,
        owner: str = "",
        device: str = "",
        device_id: str = "",
    ) -> DataRootBinding:
        # ``project`` remains positional for older CLI/connector callers.
        project, owner, device, raw_root, kind = _parse_binding_args(
            (),
            project=project,
            root_path=str(root_path),
            owner=owner,
            device=device,
            device_id=device_id,
        )
        if kind == "legacy":
            if not _PROJECT_RE.fullmatch(project):
                raise AgentDataBindingError("data binding project is invalid")
        else:
            owner = _identity_token(owner, label="owner")
            device = _identity_token(device, label="device")
        root = Path(raw_root).expanduser().resolve(strict=True)
        if not root.is_dir() or not os.access(root, os.R_OK):
            raise AgentDataBindingError("data binding root is not a readable directory")
        binding_id = _make_binding_id(project, owner, device, str(root))
        now = float(self._now_fn())
        if not math.isfinite(now) or now < 0:
            raise AgentDataBindingError("system clock is invalid")
        storage_project = self._storage_project(project, owner, device)
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT binding_id FROM data_root_bindings WHERE owner=? AND device_id=? AND root_path=?",
                (owner, device, str(root)),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO data_root_bindings(binding_id,project,root_path,created_at,updated_at,owner,device_id)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (binding_id, storage_project, str(root), now, now, owner, device),
                )
            else:
                binding_id = str(existing["binding_id"])
                conn.execute(
                    "UPDATE data_root_bindings SET updated_at=? WHERE binding_id=?",
                    (now, binding_id),
                )
            conn.commit()
        return self.get(binding_id, project=project if kind == "legacy" else "", owner=owner, device_id=device)

    def get(
        self,
        binding_id: str,
        *,
        project: str = "",
        owner: str = "",
        device: str = "",
        device_id: str = "",
    ) -> DataRootBinding:
        if not _ID_RE.fullmatch(str(binding_id or "")):
            raise AgentDataBindingError("data binding id is invalid")
        if device and device_id and device != device_id:
            raise AgentDataBindingError("data binding device is ambiguous")
        device = str(device or device_id or "").strip()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM data_root_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
        if row is None:
            raise AgentDataBindingError("data binding is unavailable")
        row_owner = str(row["owner"] or "")
        row_device = str(row["device_id"] or "")
        row_project = str(row["project"] or "")
        # Modern bindings are authorized by owner/device.  A project supplied
        # by an old central payload must not invalidate them; legacy rows still
        # enforce the historical project check.
        if row_owner or row_device:
            if owner and row_owner != str(owner).strip():
                raise AgentDataBindingError("data binding owner mismatch")
            if device and row_device != device:
                raise AgentDataBindingError("data binding device mismatch")
        elif project and row_project != str(project).strip():
            raise AgentDataBindingError("data binding is unavailable")
        return self._row_binding(row)

    def list(
        self,
        *,
        project: str = "",
        owner: str = "",
        device: str = "",
        device_id: str = "",
    ) -> list[DataRootBinding]:
        if device and device_id and device != device_id:
            raise AgentDataBindingError("data binding device is ambiguous")
        device = str(device or device_id or "").strip()
        clauses: list[str] = []
        values: list[str] = []
        if owner:
            clauses.append("owner=?")
            values.append(str(owner).strip())
        if device:
            clauses.append("device_id=?")
            values.append(device)
        # ``project`` only filters legacy rows.  Modern bindings remain valid
        # when Selena's project recognition changes.
        if project:
            if owner:
                clauses.append("((owner='' AND device_id='' AND project=?) OR owner=?)")
                values.extend([str(project).strip(), str(owner).strip()])
            else:
                clauses.append("owner='' AND device_id='' AND project=?")
                values.append(str(project).strip())
        query = "SELECT * FROM data_root_bindings"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC"
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, values).fetchall()
        result: list[DataRootBinding] = []
        for row in rows:
            try:
                result.append(self._row_binding(row))
            except (AgentDataBindingError, OSError):
                continue
        return result

    def authorize_path(
        self,
        *,
        project: str = "",
        binding_id: str,
        data_path: str,
        owner: str = "",
        device: str = "",
        device_id: str = "",
    ) -> Path:
        binding = self.get(
            binding_id,
            project=project,
            owner=owner,
            device=device,
            device_id=device_id,
        )
        try:
            target = Path(data_path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise AgentDataBindingError("authorized data path is unavailable") from exc
        if not _is_contained(binding.root_path, target):
            raise AgentDataBindingError("data path is outside the authorized root")
        if not os.access(target, os.R_OK):
            raise AgentDataBindingError("authorized data path is unreadable")
        return target

    def delete(self, binding_id: str) -> None:
        if not _ID_RE.fullmatch(str(binding_id or "")):
            raise AgentDataBindingError("data binding id is invalid")
        with self._lock, self._connect() as conn:
            deleted = conn.execute("DELETE FROM data_root_bindings WHERE binding_id=?", (binding_id,))
            if deleted.rowcount != 1:
                raise AgentDataBindingError("data binding is unavailable")
            conn.commit()


def _normalized_path_token(value: str) -> str:
    return path_token(value)


def _is_contained(root: Path, target: Path) -> bool:
    root_text = os.path.normcase(os.path.normpath(str(root.resolve())))
    target_text = os.path.normcase(os.path.normpath(str(target.resolve())))
    try:
        return os.path.commonpath((root_text, target_text)) == root_text
    except ValueError:
        return False


__all__ = [
    "AgentDataBindingError",
    "AgentDataBindingStore",
    "DataRootBinding",
    "candidate_data_binding_ids",
    "make_data_binding_id",
]
