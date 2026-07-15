"""Owner-scoped reusable Adapter and MatFilter assets.

Runtime XML is intentionally excluded: it belongs to an immutable Selena
Runtime Bundle.  These records cover only the two mandatory, independently
selected simulation configuration files in the public user YAML.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.user import normalize_user


class ConfigAssetError(ValueError):
    """Stable validation, ownership, or storage failure."""


_KINDS = {"adapter", "mat_filter"}
_ID_RE = re.compile(r"^config-asset:sha256:[0-9a-f]{64}$")
_URI_RE = re.compile(r"^config-asset://sha256/([0-9a-f]{64})$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ConfigAssetRecord:
    id: str
    kind: str
    filename: str
    checksum: str
    size: int
    owner: str
    created_at: float

    @property
    def uri(self) -> str:
        return "config-asset://sha256/" + self.id.rsplit(":", 1)[-1]

    @property
    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "uri": self.uri,
            "kind": self.kind,
            "filename": self.filename,
            "checksum": self.checksum,
            "size": self.size,
            "created_at": self.created_at,
        }


class ConfigAssetStore:
    def __init__(
        self,
        root: str | Path,
        db_path: str | Path,
        *,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS config_assets (
                    owner TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    location TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(owner, asset_id, kind)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def put(self, *, owner: str, kind: str, filename: str, content: bytes) -> ConfigAssetRecord:
        owner = normalize_user(owner)
        kind = str(kind or "").strip().lower()
        filename = str(filename or "").strip()
        if kind not in _KINDS:
            raise ConfigAssetError("configuration asset kind is invalid")
        if not _SAFE_NAME_RE.fullmatch(filename) or filename.upper().split(".", 1)[0] in {
            "CON", "PRN", "AUX", "NUL", "COM1", "LPT1"
        }:
            raise ConfigAssetError("configuration asset filename is invalid")
        if not isinstance(content, bytes) or not content or len(content) > _MAX_BYTES:
            raise ConfigAssetError("configuration asset size is invalid")
        if b"\x00" in content:
            raise ConfigAssetError("configuration asset must be a text file")
        digest = hashlib.sha256(content).hexdigest()
        asset_id = "config-asset:sha256:" + digest
        checksum = "sha256:" + digest
        now = float(self._now_fn())
        owner_key = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:24]
        directory = self.root / "content" / owner_key / digest
        target = directory / filename
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM config_assets WHERE owner=? AND asset_id=? AND kind=?",
                (owner, asset_id, kind),
            ).fetchone()
            if existing is not None:
                record = self._record(existing)
                self._verify(Path(str(existing["location"])), record)
                return record
            directory.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(prefix="asset-", suffix=".tmp", dir=str(directory))
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                if target.exists():
                    if target.read_bytes() != content:
                        raise ConfigAssetError("configuration asset storage collision")
                    temporary.unlink(missing_ok=True)
                else:
                    os.replace(temporary, target)
                conn.execute(
                    "INSERT INTO config_assets VALUES (?,?,?,?,?,?,?,?)",
                    (owner, asset_id, kind, filename, checksum, len(content), str(target), now),
                )
                conn.commit()
            finally:
                temporary.unlink(missing_ok=True)
        return ConfigAssetRecord(asset_id, kind, filename, checksum, len(content), owner, now)

    def get(self, value: str, *, owner: str, kind: str = "") -> ConfigAssetRecord:
        owner = normalize_user(owner)
        asset_id = config_asset_id(value)
        kind = str(kind or "").strip().lower()
        with self._lock, self._connect() as conn:
            if kind:
                row = conn.execute(
                    "SELECT * FROM config_assets WHERE owner=? AND asset_id=? AND kind=?",
                    (owner, asset_id, kind),
                ).fetchone()
            else:
                rows = conn.execute(
                    "SELECT * FROM config_assets WHERE owner=? AND asset_id=?",
                    (owner, asset_id),
                ).fetchall()
                if len(rows) != 1:
                    raise ConfigAssetError("configuration asset kind is required")
                row = rows[0]
        if row is None:
            raise ConfigAssetError("configuration asset is unavailable")
        record = self._record(row)
        self._verify(Path(str(row["location"])), record)
        return record

    def resolve_location(self, value: str, *, owner: str, kind: str) -> Path:
        asset_id = config_asset_id(value)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM config_assets WHERE owner=? AND asset_id=? AND kind=?",
                (normalize_user(owner), asset_id, str(kind or "").strip().lower()),
            ).fetchone()
        if row is None:
            raise ConfigAssetError("configuration asset is unavailable")
        record = self._record(row)
        location = Path(str(row["location"]))
        self._verify(location, record)
        return location

    def list(self, *, owner: str, kind: str = "") -> list[ConfigAssetRecord]:
        owner = normalize_user(owner)
        kind = str(kind or "").strip().lower()
        with self._lock, self._connect() as conn:
            if kind:
                if kind not in _KINDS:
                    raise ConfigAssetError("configuration asset kind is invalid")
                rows = conn.execute(
                    "SELECT * FROM config_assets WHERE owner=? AND kind=? ORDER BY created_at DESC",
                    (owner, kind),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM config_assets WHERE owner=? ORDER BY created_at DESC",
                    (owner,),
                ).fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _record(row: sqlite3.Row) -> ConfigAssetRecord:
        return ConfigAssetRecord(
            id=str(row["asset_id"]), kind=str(row["kind"]), filename=str(row["filename"]),
            checksum=str(row["checksum"]), size=int(row["size"]), owner=str(row["owner"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _verify(path: Path, record: ConfigAssetRecord) -> None:
        if not path.is_file() or path.is_symlink() or path.stat().st_size != record.size:
            raise ConfigAssetError("configuration asset content is unavailable")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if "sha256:" + digest != record.checksum:
            raise ConfigAssetError("configuration asset content changed")


def config_asset_id(value: str) -> str:
    text = str(value or "").strip().lower()
    match = _URI_RE.fullmatch(text)
    if match:
        return "config-asset:sha256:" + match.group(1)
    if _ID_RE.fullmatch(text):
        return text
    raise ConfigAssetError("configuration asset reference is invalid")


def is_config_asset_ref(value: str) -> bool:
    try:
        config_asset_id(value)
        return True
    except ConfigAssetError:
        return False


__all__ = [
    "ConfigAssetError", "ConfigAssetRecord", "ConfigAssetStore", "config_asset_id",
    "is_config_asset_ref",
]
