"""DatasetRef contract and catalog for v1 data resolution.

Users provide one ``data.path``. Existing ``core.data.iter_mf4_inputs`` owns
recursive MF4 discovery; this module records the discovered dataset behind a
logical reference so later Cluster Stages do not depend on a user machine path.
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
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable

from core.data import iter_mf4_inputs, scan_data_file
from core.shared_namespace import SharedNamespaceError, SharedNamespaceRegistry
from core.user import normalize_user


class DatasetError(ValueError):
    """Stable dataset contract or catalog error."""


class DatasetDiscoveryCancelled(DatasetError):
    """Raised when a caller cooperatively cancels discovery or hashing."""


_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^dataset:sha256:[0-9a-f]{64}$")
_DATASET_URI_RE = re.compile(r"^dataset://sha256/([0-9a-f]{64})$")


@dataclass(frozen=True)
class DatasetFileRef:
    relative_path: str
    size: int
    checksum: str = ""
    signal_status: str = "not-scanned"
    mtime_ns: int = 0

    def __post_init__(self) -> None:
        relative = str(self.relative_path or "").strip().replace("\\", "/")
        posix = PurePosixPath(relative)
        windows = PureWindowsPath(relative)
        if (
            not relative
            or posix.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise DatasetError("dataset file path must be relative")
        size = int(self.size)
        if size < 0:
            raise DatasetError("dataset file size is invalid")
        checksum = str(self.checksum or "").strip().lower()
        if checksum and not _CHECKSUM_RE.fullmatch(checksum):
            raise DatasetError("dataset file checksum is invalid")
        status = str(self.signal_status or "not-scanned").strip()
        if status not in {"present", "missing", "missing-in-prefix", "not-scanned"}:
            raise DatasetError("dataset signal status is invalid")
        mtime_ns = int(self.mtime_ns or 0)
        if mtime_ns < 0:
            raise DatasetError("dataset file mtime is invalid")
        object.__setattr__(self, "relative_path", posix.as_posix())
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "checksum", checksum)
        object.__setattr__(self, "signal_status", status)
        object.__setattr__(self, "mtime_ns", mtime_ns)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DatasetRef:
    id: str
    project: str
    owner: str
    source_kind: str
    accessibility: str
    storage_ref: str
    files: tuple[DatasetFileRef, ...]
    created_at: float
    source_fingerprint: str

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(str(self.id or "")):
            raise DatasetError("dataset id is invalid")
        project = str(self.project or "").strip()
        owner = normalize_user(self.owner)
        if not project or not owner:
            raise DatasetError("dataset project and owner are required")
        if self.source_kind not in {"shared_path", "central_upload", "agent_upload"}:
            raise DatasetError("dataset source kind is invalid")
        if self.accessibility not in {"cluster", "shared"}:
            raise DatasetError("dataset accessibility is invalid")
        storage_ref = str(self.storage_ref or "").strip()
        if not (
            storage_ref.startswith("shared://datasets/")
            or storage_ref.startswith("shared-path:sha256:")
        ):
            raise DatasetError("dataset storage reference is invalid")
        files = tuple(self.files or ())
        if not files or any(not isinstance(item, DatasetFileRef) for item in files):
            raise DatasetError("dataset must contain at least one MF4 file")
        if len({item.relative_path.casefold() for item in files}) != len(files):
            raise DatasetError("dataset file paths must be case-insensitively unique")
        created = float(self.created_at)
        if created < 0 or not math.isfinite(created):
            raise DatasetError("dataset timestamp is invalid")
        fingerprint = str(self.source_fingerprint or "").lower()
        if not _CHECKSUM_RE.fullmatch(fingerprint):
            raise DatasetError("dataset source fingerprint is invalid")
        if self.source_kind != "shared_path" and any(not item.checksum for item in files):
            raise DatasetError("uploaded dataset files require checksums")
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "storage_ref", storage_ref)
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "source_fingerprint", fingerprint)

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "owner": self.owner,
            "source_kind": self.source_kind,
            "accessibility": self.accessibility,
            "storage_ref": self.storage_ref,
            "file_count": len(self.files),
            "total_size": self.total_size,
            "files": [item.to_dict() for item in self.files],
            "created_at": self.created_at,
            "source_fingerprint": self.source_fingerprint,
        }


@dataclass(frozen=True)
class DataResolution:
    status: str
    code: str
    route: str
    action: str = ""
    dataset: DatasetRef | None = None

    def __post_init__(self) -> None:
        if self.status not in {"resolved", "requires_agent", "needs_input"}:
            raise DatasetError("data resolution status is invalid")
        if self.route not in {"shared", "central", "agent", "unknown"}:
            raise DatasetError("data resolution route is invalid")
        if self.status == "resolved" and not isinstance(self.dataset, DatasetRef):
            raise DatasetError("resolved data requires DatasetRef")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "route": self.route,
            "action": self.action,
            "dataset": self.dataset.to_dict() if self.dataset else None,
        }


def classify_data_path(path: str) -> str:
    """Classify syntax only; filesystem probing belongs to the selected node."""
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return "unknown"
    if text.startswith("shared://") or text.startswith("//"):
        return "shared"
    if re.match(r"^[A-Za-z]:/", text):
        return "agent"
    if text.startswith("/"):
        return "central"
    return "unknown"


def dataset_uri(dataset: DatasetRef) -> str:
    return "dataset://sha256/" + dataset.id.removeprefix("dataset:sha256:")


def dataset_id_from_uri(value: str) -> str:
    match = _DATASET_URI_RE.fullmatch(str(value or "").strip().lower())
    if match is None:
        raise DatasetError("dataset reference is invalid")
    return "dataset:sha256:" + match.group(1)


def resolve_data_reference(
    catalog: "DatasetCatalog",
    registry: SharedNamespaceRegistry,
    *,
    owner: str,
    project: str,
    data_path: str,
    required_signals: Iterable[str] = (),
) -> DataResolution:
    text = str(data_path or "").strip()
    if _DATASET_URI_RE.fullmatch(text.lower()):
        try:
            dataset = catalog.get(dataset_id_from_uri(text), owner=owner, project=project)
            return DataResolution("resolved", "uploaded_dataset_resolved", "central", dataset=dataset)
        except DatasetError:
            return DataResolution(
                "needs_input",
                "uploaded_dataset_unavailable",
                "central",
                action="Upload the dataset again or select an available dataset reference.",
            )
    route = classify_data_path(text)
    if route == "shared" and (text.startswith("//") or text.startswith("\\\\")):
        return resolve_shared_data(
            catalog,
            registry,
            owner=owner,
            project=project,
            source_path=text,
            required_signals=required_signals,
        )
    if route == "agent":
        return DataResolution(
            "requires_agent",
            "agent_data_upload_required",
            "agent",
            action="Use an authorized Windows Agent to discover and upload this local data path.",
        )
    return DataResolution(
        "needs_input",
        "data_path_not_resolvable",
        route,
        action="Choose an authorized shared path or upload the local MF4 folder.",
    )


def discover_dataset_files(
    source: Path,
    required_signals: Iterable[str] = (),
    *,
    limit: int = 0,
    max_read_mb: int = 8,
    checksum: bool = False,
    cancel_requested: Callable[[], bool] | None = None,
) -> tuple[DatasetFileRef, ...]:
    """Recursively discover MF4s at any nesting level using ``core.data``."""
    source = Path(source)
    root = source if source.is_dir() else source.parent
    results: list[DatasetFileRef] = []
    signals = [str(item) for item in required_signals if str(item).strip()]
    cancelled = cancel_requested or (lambda: False)
    # Inventory and fingerprint must always cover the complete dataset.
    # SimulationSpec.data.limit is applied later when selecting run inputs.
    for path in iter_mf4_inputs(source, limit=0):
        if cancelled():
            raise DatasetDiscoveryCancelled("dataset discovery cancelled")
        scan = scan_data_file(path, signals, max_bytes=max(0, int(max_read_mb)) * 1024 * 1024)
        if scan.signal_status in {"missing", "error"}:
            raise DatasetError("dataset file failed required-signal validation")
        relative = path.name if source.is_file() else path.relative_to(root).as_posix()
        results.append(
            DatasetFileRef(
                relative_path=relative,
                size=int(scan.size),
                checksum=_sha256_file(path, cancel_requested=cancelled) if checksum else "",
                signal_status=scan.signal_status,
                mtime_ns=int(path.stat().st_mtime_ns),
            )
        )
    if cancelled():
        raise DatasetDiscoveryCancelled("dataset discovery cancelled")
    if not results:
        raise DatasetError("no input MF4 files were found")
    return tuple(results)


def dataset_fingerprint(files: Iterable[DatasetFileRef]) -> str:
    payload = [item.to_dict() for item in files]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DatasetCatalog:
    """SQLite catalog that keeps physical source locations server-side only."""

    def __init__(self, db_path: str | Path, *, now_fn: Callable[[], float] = time.time) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    dataset_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    accessibility TEXT NOT NULL,
                    storage_ref TEXT NOT NULL,
                    source_location TEXT NOT NULL,
                    probe_location TEXT NOT NULL DEFAULT '',
                    files_json TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(project, owner, source_fingerprint, source_location)
                )
                """
            )
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(datasets)").fetchall()}
            if "probe_location" not in columns:
                conn.execute("ALTER TABLE datasets ADD COLUMN probe_location TEXT NOT NULL DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def register_shared(
        self,
        *,
        project: str,
        owner: str,
        source_path: str,
        files: tuple[DatasetFileRef, ...],
        probe_path: str = "",
    ) -> DatasetRef:
        fingerprint = dataset_fingerprint(files)
        normalized = os.path.normcase(os.path.normpath(str(source_path or "")))
        digest = hashlib.sha256("\0".join([project, normalize_user(owner), normalized, fingerprint]).encode()).hexdigest()
        dataset_id = f"dataset:sha256:{digest}"
        storage_ref = "shared-path:sha256:" + hashlib.sha256(normalized.encode()).hexdigest()
        created = float(self._now_fn())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO datasets(
                    dataset_id, project, owner, source_kind, accessibility,
                    storage_ref, source_location, probe_location, files_json, source_fingerprint, created_at
                ) VALUES (?, ?, ?, 'shared_path', 'cluster', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset_id) DO NOTHING
                """,
                (
                    dataset_id,
                    project,
                    normalize_user(owner),
                    storage_ref,
                    str(source_path),
                    str(probe_path or ""),
                    json.dumps([item.to_dict() for item in files], sort_keys=True),
                    fingerprint,
                    created,
                ),
            )
            conn.commit()
        return self.get(dataset_id, owner=owner)

    def register_uploaded(
        self,
        *,
        project: str,
        owner: str,
        source_kind: str,
        source_path: str,
        storage_ref: str,
        files: tuple[DatasetFileRef, ...],
    ) -> DatasetRef:
        if source_kind not in {"central_upload", "agent_upload"}:
            raise DatasetError("uploaded dataset source kind is invalid")
        if not storage_ref.startswith("shared://datasets/"):
            raise DatasetError("uploaded dataset storage reference is invalid")
        fingerprint = dataset_fingerprint(files)
        owner = normalize_user(owner)
        digest = hashlib.sha256("\0".join((project, owner, storage_ref, fingerprint)).encode()).hexdigest()
        dataset_id = f"dataset:sha256:{digest}"
        created = float(self._now_fn())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO datasets(
                    dataset_id, project, owner, source_kind, accessibility,
                    storage_ref, source_location, probe_location, files_json, source_fingerprint, created_at
                ) VALUES (?, ?, ?, ?, 'cluster', ?, ?, '', ?, ?, ?)
                ON CONFLICT(dataset_id) DO NOTHING
                """,
                (
                    dataset_id, project, owner, source_kind, storage_ref, str(source_path),
                    json.dumps([item.to_dict() for item in files], sort_keys=True), fingerprint, created,
                ),
            )
            conn.commit()
        return self.get(dataset_id, owner=owner, project=project)

    def get(self, dataset_id: str, *, owner: str, project: str = "") -> DatasetRef:
        if not _ID_RE.fullmatch(str(dataset_id or "")):
            raise DatasetError("dataset id is invalid")
        with self._lock, self._connect() as conn:
            if project:
                row = conn.execute(
                    "SELECT * FROM datasets WHERE dataset_id=? AND owner=? AND project=?",
                    (dataset_id, normalize_user(owner), str(project)),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM datasets WHERE dataset_id=? AND owner=?",
                    (dataset_id, normalize_user(owner)),
                ).fetchone()
        if row is None:
            raise DatasetError("dataset is unavailable")
        files = tuple(DatasetFileRef(**item) for item in json.loads(row["files_json"]))
        return DatasetRef(
            id=row["dataset_id"],
            project=row["project"],
            owner=row["owner"],
            source_kind=row["source_kind"],
            accessibility=row["accessibility"],
            storage_ref=row["storage_ref"],
            files=files,
            created_at=row["created_at"],
            source_fingerprint=row["source_fingerprint"],
        )

    def resolve_location(self, dataset_id: str, *, owner: str) -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT source_location FROM datasets WHERE dataset_id=? AND owner=?",
                (dataset_id, normalize_user(owner)),
            ).fetchone()
        if row is None:
            raise DatasetError("dataset is unavailable")
        return str(row["source_location"])

    def resolve_probe_location(self, dataset_id: str, *, owner: str) -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT probe_location FROM datasets WHERE dataset_id=? AND owner=?",
                (dataset_id, normalize_user(owner)),
            ).fetchone()
        if row is None:
            raise DatasetError("dataset is unavailable")
        return str(row["probe_location"] or "")


def resolve_shared_data(
    catalog: DatasetCatalog,
    registry: SharedNamespaceRegistry,
    *,
    owner: str,
    project: str,
    source_path: str,
    required_signals: Iterable[str] = (),
) -> DataResolution:
    try:
        mapping = registry.resolve(source_path)
        files = discover_dataset_files(Path(mapping.central_probe_path), required_signals, checksum=False)
        dataset = catalog.register_shared(
            project=project,
            owner=owner,
            source_path=mapping.worker_path,
            probe_path=mapping.central_probe_path,
            files=files,
        )
        return DataResolution("resolved", "shared_dataset_resolved", "shared", dataset=dataset)
    except (DatasetError, SharedNamespaceError) as exc:
        return DataResolution(
            "needs_input",
            "shared_dataset_unavailable",
            "shared",
            action=str(exc),
        )


def _sha256_file(
    path: Path,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> str:
    digest = hashlib.sha256()
    cancelled = cancel_requested or (lambda: False)
    with path.open("rb") as handle:
        while True:
            if cancelled():
                raise DatasetDiscoveryCancelled("dataset checksum cancelled")
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


__all__ = [
    "DataResolution",
    "DatasetCatalog",
    "DatasetDiscoveryCancelled",
    "DatasetError",
    "DatasetFileRef",
    "DatasetRef",
    "classify_data_path",
    "dataset_fingerprint",
    "dataset_id_from_uri",
    "dataset_uri",
    "discover_dataset_files",
    "resolve_shared_data",
    "resolve_data_reference",
]
