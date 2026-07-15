"""Windows Agent-local authorization store for readable MF4 data roots."""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable

from core.agent_bindings import default_agent_binding_db_path


class AgentDataBindingError(ValueError):
    pass


_ID_RE = re.compile(r"^data-root:sha256:[0-9a-f]{24}$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def make_data_binding_id(project: str, root_path: str) -> str:
    project = str(project or "").strip()
    if not _PROJECT_RE.fullmatch(project):
        raise AgentDataBindingError("data binding project is invalid")
    normalized = _normalized_path_token(root_path)
    if not normalized:
        raise AgentDataBindingError("data binding root is required")
    digest = hashlib.sha256("\0".join((project, normalized)).encode("utf-8")).hexdigest()
    return f"data-root:sha256:{digest[:24]}"


def candidate_data_binding_ids(project: str, data_path: str) -> tuple[str, ...]:
    """Return exact/ancestor IDs central can compare to path-free adverts."""
    text = str(data_path or "").strip()
    path = PureWindowsPath(text)
    if not path.is_absolute() or not path.drive:
        return ()
    values = [path, *path.parents]
    result: list[str] = []
    for value in values:
        try:
            binding_id = make_data_binding_id(project, str(value))
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

    @property
    def public_dict(self) -> dict:
        return {"id": self.binding_id, "project": self.project, "healthy": True}


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
                    project TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(project, root_path)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def register(self, *, project: str, root_path: str | Path) -> DataRootBinding:
        root = Path(root_path).expanduser().resolve(strict=True)
        if not root.is_dir() or not os.access(root, os.R_OK):
            raise AgentDataBindingError("data binding root is not a readable directory")
        binding_id = make_data_binding_id(project, str(root))
        now = float(self._now_fn())
        if not math.isfinite(now) or now < 0:
            raise AgentDataBindingError("system clock is invalid")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO data_root_bindings(binding_id,project,root_path,created_at,updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(binding_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (binding_id, project, str(root), now, now),
            )
            conn.commit()
        return self.get(binding_id, project=project)

    def get(self, binding_id: str, *, project: str = "") -> DataRootBinding:
        if not _ID_RE.fullmatch(str(binding_id or "")):
            raise AgentDataBindingError("data binding id is invalid")
        with self._lock, self._connect() as conn:
            if project:
                row = conn.execute(
                    "SELECT * FROM data_root_bindings WHERE binding_id=? AND project=?",
                    (binding_id, project),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM data_root_bindings WHERE binding_id=?", (binding_id,)
                ).fetchone()
        if row is None:
            raise AgentDataBindingError("data binding is unavailable")
        root = Path(str(row["root_path"])).resolve(strict=True)
        if not root.is_dir() or not os.access(root, os.R_OK):
            raise AgentDataBindingError("data binding is unhealthy")
        return DataRootBinding(
            binding_id=str(row["binding_id"]),
            project=str(row["project"]),
            root_path=root,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def list(self, *, project: str = "") -> list[DataRootBinding]:
        with self._lock, self._connect() as conn:
            if project:
                rows = conn.execute(
                    "SELECT binding_id FROM data_root_bindings WHERE project=? ORDER BY updated_at DESC",
                    (project,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT binding_id FROM data_root_bindings ORDER BY updated_at DESC"
                ).fetchall()
        result = []
        for row in rows:
            try:
                result.append(self.get(str(row["binding_id"]), project=project))
            except (AgentDataBindingError, OSError):
                continue
        return result

    def authorize_path(self, *, project: str, binding_id: str, data_path: str) -> Path:
        binding = self.get(binding_id, project=project)
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
    text = str(value or "").strip().replace("\\", "/")
    text = re.sub(r"/+", "/", text).rstrip("/")
    return os.path.normcase(text).casefold()


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
