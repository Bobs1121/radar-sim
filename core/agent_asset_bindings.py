"""Windows Agent-local authorization for Runtime XML, Adapter and MatFilter files."""

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


class AgentAssetBindingError(ValueError):
    """Path-free configuration asset authorization error."""


_ID_RE = re.compile(r"^asset-root:sha256:[0-9a-f]{24}$")
_ROLE_SUFFIXES = {
    "runtime_xml": {".xml"},
    "adapter": set(),
    "mat_filter": set(),
}


def make_asset_binding_id(root_path: str) -> str:
    normalized = _normalized_path_token(root_path)
    if not normalized:
        raise AgentAssetBindingError("asset binding root is required")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"asset-root:sha256:{digest[:24]}"


def candidate_asset_binding_ids(asset_path: str) -> tuple[str, ...]:
    """Return exact parent/ancestor root IDs without exposing any path."""
    path = PureWindowsPath(str(asset_path or "").strip())
    if not path.is_absolute() or not path.drive:
        return ()
    result: list[str] = []
    for value in [path.parent, *path.parent.parents]:
        try:
            binding_id = make_asset_binding_id(str(value))
        except AgentAssetBindingError:
            continue
        if binding_id not in result:
            result.append(binding_id)
    return tuple(result)


@dataclass(frozen=True)
class AssetRootBinding:
    binding_id: str
    root_path: Path
    created_at: float
    updated_at: float

    @property
    def public_dict(self) -> dict:
        return {"id": self.binding_id, "healthy": True}


class AgentAssetBindingStore:
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
                CREATE TABLE IF NOT EXISTS asset_root_bindings (
                    binding_id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def register(self, root_path: str | Path) -> AssetRootBinding:
        try:
            root = Path(root_path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise AgentAssetBindingError("asset binding root is unavailable") from exc
        if not root.is_dir() or root.is_symlink() or not os.access(root, os.R_OK):
            raise AgentAssetBindingError("asset binding root is not a readable directory")
        binding_id = make_asset_binding_id(str(root))
        now = float(self._now_fn())
        if not math.isfinite(now) or now < 0:
            raise AgentAssetBindingError("system clock is invalid")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO asset_root_bindings(binding_id,root_path,created_at,updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT(binding_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (binding_id, str(root), now, now),
            )
            conn.commit()
        return self.get(binding_id)

    def get(self, binding_id: str) -> AssetRootBinding:
        if not _ID_RE.fullmatch(str(binding_id or "")):
            raise AgentAssetBindingError("asset binding id is invalid")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM asset_root_bindings WHERE binding_id=?", (binding_id,)
            ).fetchone()
        if row is None:
            raise AgentAssetBindingError("asset binding is unavailable")
        try:
            root = Path(str(row["root_path"])).resolve(strict=True)
        except OSError as exc:
            raise AgentAssetBindingError("asset binding is unhealthy") from exc
        if not root.is_dir() or root.is_symlink() or not os.access(root, os.R_OK):
            raise AgentAssetBindingError("asset binding is unhealthy")
        return AssetRootBinding(
            binding_id=str(row["binding_id"]),
            root_path=root,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def list(self) -> list[AssetRootBinding]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT binding_id FROM asset_root_bindings ORDER BY updated_at DESC"
            ).fetchall()
        result = []
        for row in rows:
            try:
                result.append(self.get(str(row["binding_id"])))
            except (AgentAssetBindingError, OSError):
                continue
        return result

    def authorize_path(self, *, binding_id: str, asset_path: str, role: str) -> Path:
        if role not in _ROLE_SUFFIXES:
            raise AgentAssetBindingError("asset role is invalid")
        binding = self.get(binding_id)
        try:
            target = Path(asset_path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise AgentAssetBindingError("authorized asset is unavailable") from exc
        if not target.is_file() or target.is_symlink() or not os.access(target, os.R_OK):
            raise AgentAssetBindingError("authorized asset is not a readable file")
        if not _is_contained(binding.root_path, target):
            raise AgentAssetBindingError("asset is outside the authorized root")
        suffixes = _ROLE_SUFFIXES[role]
        if suffixes and target.suffix.casefold() not in suffixes:
            raise AgentAssetBindingError("authorized asset has an invalid file type")
        return target

    def authorize_any(self, *, asset_path: str, role: str) -> tuple[AssetRootBinding, Path]:
        advertised = {binding.binding_id: binding for binding in self.list()}
        for binding_id in candidate_asset_binding_ids(asset_path):
            binding = advertised.get(binding_id)
            if binding is None:
                continue
            return binding, self.authorize_path(binding_id=binding_id, asset_path=asset_path, role=role)
        raise AgentAssetBindingError("asset path is not authorized on this Agent")

    def delete(self, binding_id: str) -> None:
        if not _ID_RE.fullmatch(str(binding_id or "")):
            raise AgentAssetBindingError("asset binding id is invalid")
        with self._lock, self._connect() as conn:
            deleted = conn.execute("DELETE FROM asset_root_bindings WHERE binding_id=?", (binding_id,))
            if deleted.rowcount != 1:
                raise AgentAssetBindingError("asset binding is unavailable")
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
    "AgentAssetBindingError",
    "AgentAssetBindingStore",
    "AssetRootBinding",
    "candidate_asset_binding_ids",
    "make_asset_binding_id",
]
