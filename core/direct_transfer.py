"""Small, project-independent direct-transfer kernel.

Only a trusted deployment may issue a :class:`TransferPlan`.  The client
receives the deployment's ``client_target_root`` and copies bytes directly to
that data-plane root.  The control plane deals in plans, progress and
manifests only; this module never performs HTTP, YAML or project discovery.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable, Mapping, Optional, Tuple, Union


TRANSFER_MODES = frozenset({"shared_copy", "source_to_local", "gateway_upload"})
SOURCE_ROLES = frozenset(
    {"dataset", "runtime_bundle", "runtime_xml", "mat_filter", "adapter"}
)
TRANSFER_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "failed", "cancelled", "skipped_shared", "skipped_local"}
)
_TRANSFER_PROGRESS_MIN_INTERVAL_SEC = 1.0
_TRANSFER_PROGRESS_MIN_FRACTION = 0.05
_TRANSFER_PROGRESS_MIN_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_RE = re.compile(r"^[A-Za-z0-9:_-]{8,256}$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {"COM%d" % i for i in range(1, 10)}
    | {"LPT%d" % i for i in range(1, 10)}
)


class DirectTransferError(ValueError):
    """Stable error for a transfer contract or data-plane violation."""

    def __init__(self, message: str, *, code: str = "direct_transfer_error") -> None:
        super().__init__(message)
        self.code = code


class TransferCancelled(DirectTransferError):
    """The caller cancelled a copy operation."""


class SourceChangedError(DirectTransferError):
    """The source no longer matches its captured identity."""


class GatewayUnavailableError(DirectTransferError):
    """The optional gateway adapter is not part of the P0 kernel."""


class _TransferProgressReporter:
    """Separate per-chunk local notifications from throttled HTTP updates.

    ``execute_transfer`` deliberately reports every copied chunk so callers
    can render a smooth local progress indicator.  Sending each event to the
    control plane is unnecessarily expensive for large files, however.  The
    first update is sent immediately; later updates are sent when at least
    one of the elapsed-time, percentage, or byte thresholds is met.  The
    ``finish`` method always publishes the post-verification terminal state.

    The optional thresholds/clock are injectable for focused tests; SDK and
    Agent production callers use the conservative defaults above.
    """

    def __init__(
        self,
        publish: Callable[[Any], Any],
        local_callback: Callable[[Any], None] | None = None,
        *,
        min_interval_sec: float = _TRANSFER_PROGRESS_MIN_INTERVAL_SEC,
        min_fraction: float = _TRANSFER_PROGRESS_MIN_FRACTION,
        min_bytes: int = _TRANSFER_PROGRESS_MIN_BYTES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._publish = publish
        self._local_callback = local_callback
        self._min_interval_sec = max(0.0, float(min_interval_sec))
        self._min_fraction = max(0.0, float(min_fraction))
        self._min_bytes = max(0, int(min_bytes))
        self._clock = clock or time.monotonic
        self._last_published: Any | None = None
        self._last_published_at: float | None = None
        self._latest: Any | None = None

    @staticmethod
    def _snapshot(progress: Any) -> tuple[str, int, int, str, str, str]:
        """Return progress fields excluding volatile timestamps."""

        return (
            str(progress.transfer_id),
            int(progress.bytes_transferred),
            int(progress.bytes_total),
            str(progress.current_file or ""),
            str(progress.status),
            str(progress.owner_scope or ""),
        )

    def _should_publish(self, progress: Any, now: float) -> bool:
        previous = self._last_published
        if previous is None:
            return True
        if self._min_interval_sec <= 0.0 or self._last_published_at is None:
            elapsed = float("inf")
        else:
            elapsed = max(0.0, float(now) - self._last_published_at)
        bytes_delta = int(progress.bytes_transferred) - int(previous.bytes_transferred)
        if bytes_delta >= self._min_bytes:
            return True
        if elapsed >= self._min_interval_sec:
            return True
        total = int(progress.bytes_total)
        if total > 0 and (bytes_delta / total) >= self._min_fraction:
            return True
        return False

    def _publish_now(self, progress: Any, now: float) -> None:
        self._publish(progress)
        self._last_published = progress
        self._last_published_at = float(now)

    def emit(self, progress: Any) -> None:
        """Handle one kernel event and notify local callbacks every time."""

        now = float(self._clock())
        self._latest = progress
        if self._should_publish(progress, now):
            # Preserve the adapter's historical ordering: a control-plane
            # failure aborts the transfer before its local callback is called.
            self._publish_now(progress, now)
        if self._local_callback is not None:
            self._local_callback(progress)

    def finish(self, progress: Any) -> None:
        """Publish a verified terminal snapshot and notify local listeners once."""

        now = float(self._clock())
        previous = self._latest
        self._latest = progress
        # ``execute_transfer`` normally emits a chunk event at exactly
        # ``bytes_total``.  Avoid a duplicate local callback in that common
        # case, while synthesising one for empty/resumed files.  Publish first
        # so a callback exception cannot prevent terminal control-plane state.
        self._publish_now(progress, now)
        if (
            self._local_callback is not None
            and (previous is None or self._snapshot(previous) != self._snapshot(progress))
        ):
            self._local_callback(progress)


def _digest_token(value: str, length: int = 32) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _normalize_relative_path(value: str) -> str:
    """Normalise a relative path while rejecting traversal/device names."""

    text = str(value or "").strip().replace("\\", "/")
    if not text or "\x00" in text:
        raise DirectTransferError("relative path is empty or contains NUL")
    if text.startswith("/"):
        raise DirectTransferError("absolute paths are not allowed")
    windows = PureWindowsPath(text)
    if windows.is_absolute() or windows.drive or PurePosixPath(text).is_absolute():
        raise DirectTransferError("absolute or drive-qualified paths are not allowed")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise DirectTransferError("relative path contains traversal or empty segments")
    for part in parts:
        if ":" in part or part.endswith((" ", ".")):
            raise DirectTransferError("relative path contains a Windows-unsafe segment")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise DirectTransferError("relative path contains a Windows device name")
    return "/".join(parts)


def build_isolated_relative_root(owner_scope: str, job_id: str, transfer_id: str) -> str:
    """Return an opaque owner/job/transfer prefix used in both namespaces."""

    for name, value in (("owner_scope", owner_scope), ("job_id", job_id), ("transfer_id", transfer_id)):
        if not str(value or "").strip():
            raise DirectTransferError("%s is required" % name)
    return "_rsim_transfer/o_%s/j_%s/t_%s" % (
        _digest_token(str(owner_scope), 24),
        _digest_token(str(job_id), 24),
        _digest_token(str(transfer_id), 24),
    )


@dataclass(frozen=True)
class TransferSource:
    """A source file and optional identity captured before copying."""

    relative_path: str
    size: Optional[int] = None
    sha256: str = ""
    mtime_ns: Optional[int] = None

    def __post_init__(self) -> None:
        relative = _normalize_relative_path(self.relative_path)
        size = None if self.size is None else int(self.size)
        mtime = None if self.mtime_ns is None else int(self.mtime_ns)
        digest = str(self.sha256 or "").strip().lower()
        if size is not None and size < 0:
            raise DirectTransferError("TransferSource.size is invalid")
        if mtime is not None and mtime < 0:
            raise DirectTransferError("TransferSource.mtime_ns is invalid")
        if digest and not _SHA256_RE.fullmatch(digest):
            raise DirectTransferError("TransferSource.sha256 is invalid")
        object.__setattr__(self, "relative_path", relative)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "mtime_ns", mtime)


@dataclass(frozen=True)
class TransferPlanItem:
    """One metadata-only item in a plan (the service aliases this class)."""

    source_role: str
    relative_path: str
    size: int
    checksum: str = ""
    mtime_ns: Optional[int] = None

    def __post_init__(self) -> None:
        role = str(self.source_role or "").strip()
        if role not in SOURCE_ROLES:
            raise DirectTransferError("TransferPlanItem.source_role is invalid")
        relative = _normalize_relative_path(self.relative_path)
        size = int(self.size)
        checksum = str(self.checksum or "").strip().lower()
        mtime = None if self.mtime_ns is None else int(self.mtime_ns)
        if size < 0:
            raise DirectTransferError("TransferPlanItem.size is invalid")
        if checksum and not _SHA256_RE.fullmatch(checksum):
            raise DirectTransferError("TransferPlanItem.checksum is invalid")
        if mtime is not None and mtime < 0:
            raise DirectTransferError("TransferPlanItem.mtime_ns is invalid")
        object.__setattr__(self, "source_role", role)
        object.__setattr__(self, "relative_path", relative)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "checksum", checksum)
        object.__setattr__(self, "mtime_ns", mtime)

    @property
    def sha256(self) -> str:
        return self.checksum

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_role": self.source_role,
            "relative_path": self.relative_path,
            "size": self.size,
            "checksum": self.checksum,
            "mtime_ns": self.mtime_ns,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TransferPlanItem":
        unknown = set(value) - {"source_role", "relative_path", "size", "checksum", "sha256", "mtime_ns"}
        if unknown:
            raise DirectTransferError("TransferPlanItem contains unsupported fields")
        return cls(
            source_role=str(value.get("source_role") or ""),
            relative_path=str(value.get("relative_path") or ""),
            size=int(value.get("size") or 0),
            checksum=str(value.get("checksum") or value.get("sha256") or ""),
            mtime_ns=None if value.get("mtime_ns") is None else int(value.get("mtime_ns")),
        )


@dataclass(frozen=True, init=False)
class TransferPlan:
    """Signed plan shared by the service and the direct-transfer client.

    ``client_target_root`` is the only physical root in the plan.  The
    server-side probe/mount root is deployment state and is intentionally not
    represented here.  ``target_root`` remains a read/write constructor and
    property alias for old callers, but new serialised plans use the explicit
    ``client_target_root`` name.
    """

    transfer_id: str
    owner_scope: str
    job_id: str
    stage_id: str
    mode: str
    source_role: str
    client_target_root: str
    relative_root: str
    resume: bool
    expires_at: float
    owner: str
    items: Tuple[TransferPlanItem, ...]
    created_at: float
    status: str
    source_fingerprints: dict[str, str]

    def __init__(
        self,
        transfer_id: str,
        owner_scope: str,
        job_id: str,
        stage_id: str,
        mode: str,
        source_role: str,
        client_target_root: str = "",
        relative_root: str = "",
        resume: bool = True,
        expires_at: float = 0.0,
        *,
        target_root: str = "",
        server_probe_root: Optional[Union[str, os.PathLike[str]]] = None,
        owner: str = "",
        items: Iterable[TransferPlanItem] = (),
        created_at: float = 0.0,
        status: str = "pending",
        source_fingerprints: Optional[Mapping[str, Any]] = None,
    ) -> None:
        # target_root is compatibility input only; callers may not disagree
        # with the explicit client_target_root.
        if server_probe_root is not None and str(server_probe_root or "").strip():
            raise DirectTransferError("server_probe_root is deployment-only")
        client = str(client_target_root or "").strip()
        legacy = str(target_root or "").strip()
        if client and legacy and client != legacy:
            raise DirectTransferError("conflicting client target roots")
        client = client or legacy
        values = {
            "transfer_id": str(transfer_id or "").strip(),
            "owner_scope": str(owner_scope or "").strip(),
            "job_id": str(job_id or "").strip(),
            "stage_id": str(stage_id or "").strip(),
            "mode": str(mode or "").strip(),
            "source_role": str(source_role or "").strip(),
            "client_target_root": client,
            "relative_root": _normalize_relative_path(relative_root),
            "resume": bool(resume),
            "expires_at": float(expires_at),
            "owner": str(owner or "").strip(),
            "items": tuple(items or ()),
            "created_at": float(created_at),
            "status": str(status or "pending"),
            "source_fingerprints": _metadata_dict(source_fingerprints),
        }
        if not _OPAQUE_RE.fullmatch(values["transfer_id"]):
            raise DirectTransferError("TransferPlan.transfer_id is invalid")
        if not _OPAQUE_RE.fullmatch(values["owner_scope"]):
            raise DirectTransferError("TransferPlan.owner_scope is invalid")
        if not values["job_id"] or len(values["job_id"]) > 512 or "\x00" in values["job_id"]:
            raise DirectTransferError("TransferPlan.job_id is invalid")
        if not values["stage_id"] or len(values["stage_id"]) > 512 or "\x00" in values["stage_id"]:
            raise DirectTransferError("TransferPlan.stage_id is invalid")
        if values["mode"] not in TRANSFER_MODES:
            raise DirectTransferError("TransferPlan.mode is invalid")
        if values["source_role"] not in SOURCE_ROLES:
            raise DirectTransferError("TransferPlan.source_role is invalid")
        if not values["client_target_root"]:
            raise DirectTransferError("TransferPlan.client_target_root is required")
        expected = build_isolated_relative_root(
            values["owner_scope"], values["job_id"], values["transfer_id"]
        )
        if values["relative_root"] != expected:
            raise DirectTransferError("TransferPlan.relative_root is not owner/job/transfer isolated")
        if values["expires_at"] <= 0:
            raise DirectTransferError("TransferPlan.expires_at is invalid")
        if values["status"] not in TRANSFER_STATUSES:
            raise DirectTransferError("TransferPlan.status is invalid")
        for item in values["items"]:
            if not isinstance(item, TransferPlanItem):
                raise DirectTransferError("TransferPlan.items contains an invalid item")
            if item.source_role != values["source_role"]:
                raise DirectTransferError("TransferPlan item source_role mismatch")
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @property
    def target_root(self) -> str:
        """Compatibility alias; new code should use ``client_target_root``."""

        return self.client_target_root

    def as_kernel_plan(self) -> "TransferPlan":
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id,
            "owner": self.owner,
            "owner_scope": self.owner_scope,
            "job_id": self.job_id,
            "stage_id": self.stage_id,
            "mode": self.mode,
            "source_role": self.source_role,
            "client_target_root": self.client_target_root,
            "relative_root": self.relative_root,
            "items": [item.to_dict() for item in self.items],
            "resume": self.resume,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "status": self.status,
            "source_fingerprints": dict(self.source_fingerprints),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TransferPlan":
        unknown = set(value) - {
            "transfer_id", "owner", "owner_scope", "job_id", "stage_id", "mode", "source_role",
            "client_target_root", "target_root", "relative_root", "items", "resume",
            "expires_at", "expiry", "created_at", "status", "source_fingerprints", "server_probe_root",
        }
        if unknown:
            raise DirectTransferError("TransferPlan contains unsupported fields")
        if value.get("server_probe_root"):
            raise DirectTransferError("server_probe_root is deployment-only")
        raw_items = value.get("items") or ()
        items = tuple(item if isinstance(item, TransferPlanItem) else TransferPlanItem.from_dict(item) for item in raw_items)
        role = str(value.get("source_role") or (items[0].source_role if items else ""))
        return cls(
            transfer_id=str(value.get("transfer_id") or ""),
            owner_scope=str(value.get("owner_scope") or ""),
            job_id=str(value.get("job_id") or ""),
            stage_id=str(value.get("stage_id") or ""),
            mode=str(value.get("mode") or ""),
            source_role=role,
            client_target_root=str(value.get("client_target_root") or ""),
            target_root=str(value.get("target_root") or ""),
            relative_root=str(value.get("relative_root") or ""),
            items=items,
            resume=bool(value.get("resume", True)),
            expires_at=float(value.get("expires_at") or value.get("expiry") or 0.0),
            owner=str(value.get("owner") or ""),
            created_at=float(value.get("created_at") or 0.0),
            status=str(value.get("status") or "pending"),
            source_fingerprints=value.get("source_fingerprints") or {},
        )


def _metadata_dict(value: Optional[Mapping[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in dict(value or {}).items():
        if not isinstance(key, str) or not key.strip():
            raise DirectTransferError("metadata keys must be non-empty strings")
        if not isinstance(item, (str, int, float, bool)):
            raise DirectTransferError("transfer service accepts metadata only", code="file_body_rejected")
        result[key] = str(item)
    return result


@dataclass(frozen=True, init=False)
class ManifestEntry:
    """Path-free metadata for one published file.

    ``checksum``, ``target_logical_ref`` and ``result`` are accepted as
    service-facing aliases, avoiding a second Manifest implementation.
    """

    relative_path: str
    size: int
    sha256: str
    storage_ref: str
    mtime_ns: int
    status: str
    started_at: float
    completed_at: float

    def __init__(
        self,
        relative_path: str,
        size: int,
        sha256: str = "",
        storage_ref: str = "",
        mtime_ns: int = 0,
        status: str = "completed",
        started_at: float = 0.0,
        completed_at: float = 0.0,
        *,
        checksum: str = "",
        target_logical_ref: str = "",
        result: str = "",
    ) -> None:
        # The pre-convergence service class used positional
        # ``(..., mtime_ns, started_at, completed_at, result)``.  Accept that
        # shape while keeping the kernel's explicit ``status`` ordering.
        if not isinstance(status, str) and isinstance(completed_at, str):
            legacy_started, legacy_completed, legacy_result = status, started_at, completed_at
            status = "skipped" if legacy_result == "skipped" else "completed"
            started_at, completed_at = float(legacy_started), float(legacy_completed)
        digest = str(sha256 or checksum or "").strip().lower()
        ref = str(storage_ref or target_logical_ref or "").strip()
        normalized_status = status
        if result:
            normalized_status = "skipped" if result == "skipped" else "completed"
        normalized_status = "completed" if normalized_status == "ok" else str(normalized_status or "")
        values = {
            "relative_path": _normalize_relative_path(relative_path),
            "size": int(size),
            "sha256": digest,
            "storage_ref": ref,
            "mtime_ns": int(mtime_ns),
            "status": normalized_status,
            "started_at": float(started_at),
            "completed_at": float(completed_at),
        }
        if values["size"] < 0 or values["mtime_ns"] < 0:
            raise DirectTransferError("ManifestEntry size or mtime is invalid")
        if not _SHA256_RE.fullmatch(values["sha256"]):
            raise DirectTransferError("ManifestEntry.sha256 is invalid")
        _parse_storage_ref(values["storage_ref"])
        if values["status"] not in {"completed", "skipped"}:
            raise DirectTransferError("ManifestEntry.status is invalid")
        if values["started_at"] < 0 or values["completed_at"] < values["started_at"]:
            raise DirectTransferError("ManifestEntry timestamps are invalid")
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @property
    def checksum(self) -> str:
        return self.sha256

    @property
    def target_logical_ref(self) -> str:
        return self.storage_ref

    @property
    def result(self) -> str:
        return "skipped" if self.status == "skipped" else "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "size": self.size,
            "sha256": self.sha256,
            "checksum": self.sha256,
            "storage_ref": self.storage_ref,
            "target_logical_ref": self.storage_ref,
            "mtime_ns": self.mtime_ns,
            "status": self.status,
            "result": self.result,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ManifestEntry":
        unknown = set(value) - {
            "relative_path", "size", "sha256", "checksum", "storage_ref",
            "target_logical_ref", "mtime_ns", "status", "result", "started_at", "completed_at",
        }
        if unknown:
            raise DirectTransferError("ManifestEntry contains unsupported fields")
        return cls(
            relative_path=str(value.get("relative_path") or ""),
            size=int(value.get("size") or 0),
            sha256=str(value.get("sha256") or value.get("checksum") or ""),
            storage_ref=str(value.get("storage_ref") or value.get("target_logical_ref") or ""),
            mtime_ns=int(value.get("mtime_ns") or 0),
            status=str(value.get("status") or "completed"),
            result=str(value.get("result") or ""),
            started_at=float(value.get("started_at") or 0.0),
            completed_at=float(value.get("completed_at") or 0.0),
        )


# Service-facing spelling retained as an alias, not a second data model.
TransferManifestEntry = ManifestEntry


@dataclass(frozen=True, init=False)
class TransferManifest:
    """Completed metadata returned by a client; no file body is accepted."""

    transfer_id: str
    entries: Tuple[ManifestEntry, ...]
    started_at: float
    completed_at: float
    owner_scope: str
    job_id: str
    owner: str
    total_bytes: int
    status: str

    def __init__(
        self,
        transfer_id: str,
        entries: Iterable[ManifestEntry],
        started_at: float = 0.0,
        completed_at: float = 0.0,
        *,
        owner_scope: str = "",
        job_id: str = "",
        owner: str = "",
        total_bytes: Optional[int] = None,
        status: str = "completed",
    ) -> None:
        values = {
            "transfer_id": str(transfer_id or "").strip(),
            "entries": tuple(entries or ()),
            "started_at": float(started_at),
            "completed_at": float(completed_at),
            "owner_scope": str(owner_scope or "").strip(),
            "job_id": str(job_id or "").strip(),
            "owner": str(owner or "").strip(),
            "status": str(status or "completed"),
        }
        if not _OPAQUE_RE.fullmatch(values["transfer_id"]):
            raise DirectTransferError("TransferManifest.transfer_id is invalid")
        if not values["entries"]:
            raise DirectTransferError("TransferManifest.entries must not be empty")
        if values["status"] != "completed":
            raise DirectTransferError("TransferManifest.status is invalid")
        if values["started_at"] < 0 or values["completed_at"] < values["started_at"]:
            raise DirectTransferError("TransferManifest timestamps are invalid")
        if any(not isinstance(entry, ManifestEntry) for entry in values["entries"]):
            raise DirectTransferError("TransferManifest contains an invalid entry")
        total = sum(entry.size for entry in values["entries"])
        if total_bytes is not None and int(total_bytes) != total:
            raise DirectTransferError("TransferManifest total_bytes does not match entries")
        values["total_bytes"] = total
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id,
            "owner": self.owner,
            "owner_scope": self.owner_scope,
            "job_id": self.job_id,
            "entries": [entry.to_dict() for entry in self.entries],
            "total_bytes": self.total_bytes,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TransferManifest":
        unknown = set(value) - {
            "transfer_id", "owner", "owner_scope", "job_id", "entries", "total_bytes",
            "started_at", "completed_at", "status",
        }
        if unknown:
            raise DirectTransferError("TransferManifest contains unsupported fields")
        return cls(
            transfer_id=str(value.get("transfer_id") or ""),
            owner=str(value.get("owner") or ""),
            owner_scope=str(value.get("owner_scope") or ""),
            job_id=str(value.get("job_id") or ""),
            entries=tuple(ManifestEntry.from_dict(item) for item in (value.get("entries") or ())),
            total_bytes=(None if value.get("total_bytes") is None else int(value.get("total_bytes"))),
            started_at=float(value.get("started_at") or 0.0),
            completed_at=float(value.get("completed_at") or 0.0),
            status=str(value.get("status") or "completed"),
        )


def _looks_device_path(value: str) -> bool:
    text = str(value or "").strip().replace("/", "\\").lower()
    return text.startswith(("\\\\?\\", "\\\\.\\", "\\??\\", "\\\\globalroot\\"))


def _looks_unc(value: str) -> bool:
    text = str(value or "").strip().replace("/", "\\")
    return text.startswith("\\\\") and not _looks_device_path(text)


def validate_transfer_root(root: Union[str, os.PathLike[str]], *, allow_local: bool = False) -> str:
    """Validate a deployment UNC root or an explicit absolute test root."""

    text = str(root or "").strip()
    if not text or "\x00" in text:
        raise DirectTransferError("trusted transfer root is empty")
    if _looks_device_path(text):
        raise DirectTransferError("device paths are not valid transfer roots")
    if _looks_unc(text):
        normalized = text.replace("/", "\\").rstrip("\\")
        parts = normalized[2:].split("\\")
        if len(parts) < 2 or any(not part for part in parts) or any(part in {".", ".."} for part in parts):
            raise DirectTransferError("UNC root must contain a host and share")
        return "\\\\" + "\\".join(parts)
    if not allow_local:
        raise DirectTransferError("production transfer roots must be UNC paths")
    if ".." in text.replace("\\", "/").split("/"):
        raise DirectTransferError("local transfer root contains traversal")
    # Deployment files are often validated on a Windows SDK/test host while
    # ``server_probe_root`` intentionally names the Linux mount namespace.
    # Keep that namespace as POSIX text instead of interpreting it through the
    # host platform's ``Path`` rules.
    if text.startswith("/") and not text.startswith("//"):
        normalized_posix = posixpath.normpath(text)
        if normalized_posix == "/" or not normalized_posix.startswith("/"):
            raise DirectTransferError("local test transfer root must be an absolute non-root path")
        return normalized_posix
    path = Path(text).expanduser()
    if not path.is_absolute() or path == Path(path.anchor):
        raise DirectTransferError("local test transfer root must be an absolute non-root path")
    return os.path.normpath(str(path))


def _root_identity(root: str, *, allow_local: bool) -> str:
    validated = validate_transfer_root(root, allow_local=allow_local)
    if _looks_unc(validated):
        return validated.replace("/", "\\").rstrip("\\").casefold()
    return os.path.normcase(os.path.realpath(validated))


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _assert_no_reparse_components(base: Path, candidate: Path) -> None:
    try:
        relative = candidate.relative_to(base)
    except ValueError as exc:
        raise DirectTransferError("path escapes its trusted root") from exc
    current = base
    for part in (base,) + relative.parts:
        current = part if isinstance(part, Path) else current / part
        if os.path.lexists(str(current)) and _is_reparse_point(current):
            raise DirectTransferError("symlink or reparse-point paths are not allowed")


def _assert_no_reparse_ancestors(path: Path) -> None:
    if not path.is_absolute():
        raise DirectTransferError("path must be absolute")
    current = Path(path.parts[0])
    for part in path.parts[1:]:
        current = current / part
        if os.path.lexists(str(current)) and _is_reparse_point(current):
            raise DirectTransferError("symlink or reparse-point paths are not allowed")


def _safe_target_path(base: Path, relative_path: str) -> Path:
    normalized = _normalize_relative_path(relative_path)
    candidate = base.joinpath(*normalized.split("/"))
    _assert_no_reparse_components(base, candidate)
    if _looks_unc(str(base)):
        try:
            if os.path.commonpath([os.path.normcase(str(base)), os.path.normcase(str(candidate))]) != os.path.normcase(str(base)):
                raise DirectTransferError("target path escapes its trusted root")
        except ValueError as exc:
            raise DirectTransferError("target path escapes its trusted root") from exc
    else:
        try:
            candidate.resolve(strict=False).relative_to(base.resolve(strict=False))
        except ValueError as exc:
            raise DirectTransferError("target path escapes its trusted root") from exc
    return candidate


def _compute_target_dir(plan: TransferPlan, root: Union[str, os.PathLike[str]], *, allow_local: bool, namespace: str = "client") -> Path:
    # A Linux probe/mount is a deployment-local root even when the client root
    # is a UNC path.  Client roots still require the caller's explicit local
    # test opt-in.
    trusted = validate_transfer_root(root, allow_local=(allow_local or namespace == "probe"))
    if namespace == "client" and _root_identity(plan.client_target_root, allow_local=allow_local) != _root_identity(trusted, allow_local=allow_local):
        raise DirectTransferError("TransferPlan client_target_root is not the trusted root")
    if plan.relative_root != build_isolated_relative_root(plan.owner_scope, plan.job_id, plan.transfer_id):
        raise DirectTransferError("TransferPlan isolation prefix is invalid")
    base = Path(trusted)
    _assert_no_reparse_ancestors(base)
    return _safe_target_path(base, plan.relative_root)


def _cancelled(cancel_callback: Optional[Callable[[], bool]]) -> bool:
    return bool(cancel_callback and cancel_callback())


def _source_snapshot(path: Path) -> Tuple[int, int]:
    info = path.stat()
    if not path.is_file() or _is_reparse_point(path):
        raise DirectTransferError("source must be a regular non-reparse file")
    return int(info.st_size), int(info.st_mtime_ns)


def _check_source_snapshot(path: Path, expected_size: int, expected_mtime_ns: int) -> None:
    try:
        size, mtime = _source_snapshot(path)
    except OSError as exc:
        raise SourceChangedError("source became unavailable during transfer") from exc
    if size != expected_size or mtime != expected_mtime_ns:
        raise SourceChangedError("source size or mtime changed during transfer")


def _stream_digest(path: Path, *, cancel_callback: Optional[Callable[[], bool]], chunk_size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            if _cancelled(cancel_callback):
                raise TransferCancelled("transfer cancelled", code="transfer_cancelled")
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file_with_resume(
    source: Path,
    destination: Path,
    *,
    plan: TransferPlan,
    transfer_source: TransferSource,
    cancel_callback: Optional[Callable[[], bool]],
    progress_callback: Optional[Callable[[int, int], None]],
    chunk_size: int,
    now_fn: Callable[[], float],
) -> ManifestEntry:
    if chunk_size <= 0:
        raise DirectTransferError("chunk_size must be positive")
    started = float(now_fn())
    source_size, source_mtime = _source_snapshot(source)
    if transfer_source.size is not None and transfer_source.size != source_size:
        raise SourceChangedError("source size differs from the transfer plan")
    if transfer_source.mtime_ns is not None and transfer_source.mtime_ns != source_mtime:
        raise SourceChangedError("source mtime differs from the transfer plan")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(destination.parent, destination)
    partial = destination.with_name(destination.name + ".partial")
    _assert_no_reparse_components(destination.parent, partial)
    if destination.exists():
        if not destination.is_file() or _is_reparse_point(destination):
            raise DirectTransferError("destination is not a regular file")
        source_digest = _stream_digest(source, cancel_callback=cancel_callback, chunk_size=chunk_size)
        _check_source_snapshot(source, source_size, source_mtime)
        destination_digest = _stream_digest(destination, cancel_callback=cancel_callback, chunk_size=chunk_size)
        if destination.stat().st_size == source_size and destination_digest == source_digest:
            if transfer_source.sha256 and transfer_source.sha256 != source_digest:
                raise SourceChangedError("source digest differs from the transfer plan")
            if progress_callback:
                progress_callback(source_size, source_size)
            return ManifestEntry(
                transfer_source.relative_path,
                source_size,
                source_digest,
                make_storage_ref(source_digest, transfer_id=plan.transfer_id, relative_path=transfer_source.relative_path),
                source_mtime,
                "skipped",
                started,
                float(now_fn()),
            )
    try:
        offset = 0
        if plan.resume and partial.exists():
            if not partial.is_file() or _is_reparse_point(partial):
                raise DirectTransferError("partial destination is not a regular file")
            raw_offset = int(partial.stat().st_size)
            offset = raw_offset if raw_offset <= source_size else 0
        mode = "r+b" if partial.exists() else "w+b"
        digest = hashlib.sha256()
        with source.open("rb") as source_stream, partial.open(mode) as partial_stream:
            valid = offset > 0
            remaining = offset
            while remaining:
                if _cancelled(cancel_callback):
                    raise TransferCancelled("transfer cancelled", code="transfer_cancelled")
                amount = min(chunk_size, remaining)
                source_chunk = source_stream.read(amount)
                partial_chunk = partial_stream.read(amount)
                if source_chunk != partial_chunk or len(source_chunk) != amount:
                    valid = False
                    break
                digest.update(source_chunk)
                remaining -= amount
            if not valid:
                offset = 0
                digest = hashlib.sha256()
                source_stream.seek(0)
                partial_stream.seek(0)
                partial_stream.truncate(0)
            else:
                source_stream.seek(offset)
                partial_stream.seek(offset)
            processed = offset
            if progress_callback and offset:
                progress_callback(offset, source_size)
            while True:
                if _cancelled(cancel_callback):
                    raise TransferCancelled("transfer cancelled", code="transfer_cancelled")
                chunk = source_stream.read(chunk_size)
                if not chunk:
                    break
                partial_stream.write(chunk)
                digest.update(chunk)
                processed += len(chunk)
                if progress_callback:
                    progress_callback(processed, source_size)
            partial_stream.flush()
            try:
                os.fsync(partial_stream.fileno())
            except OSError:
                pass
        _check_source_snapshot(source, source_size, source_mtime)
        if int(partial.stat().st_size) != source_size:
            raise DirectTransferError("partial file size does not match source")
        actual = digest.hexdigest()
        if transfer_source.sha256 and transfer_source.sha256 != actual:
            raise SourceChangedError("source digest differs from the transfer plan")
        os.replace(str(partial), str(destination))
        return ManifestEntry(
            transfer_source.relative_path,
            source_size,
            actual,
            make_storage_ref(actual, transfer_id=plan.transfer_id, relative_path=transfer_source.relative_path),
            source_mtime,
            "completed",
            started,
            float(now_fn()),
        )
    except (TransferCancelled, SourceChangedError):
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except OSError as exc:
        raise DirectTransferError("direct transfer I/O failed: %s" % type(exc).__name__, code="transfer_io_error") from exc


def execute_transfer(
    plan: TransferPlan,
    source_root: Union[str, os.PathLike[str]],
    files: Iterable[Union[str, TransferSource, TransferPlanItem]],
    *,
    client_target_root: Optional[Union[str, os.PathLike[str]]] = None,
    trusted_root: Optional[Union[str, os.PathLike[str]]] = None,
    whitelist_root: Optional[Union[str, os.PathLike[str]]] = None,
    allow_local_test: bool = False,
    cancel_callback: Optional[Callable[[], bool]] = None,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    chunk_size: int = 1024 * 1024,
    now_fn: Optional[Callable[[], float]] = None,
) -> TransferManifest:
    """Copy source files directly into the deployment target."""

    clock = now_fn or time.time
    if plan.mode == "gateway_upload":
        raise GatewayUnavailableError("gateway_upload adapter is unavailable", code="cluster_direct_transfer_unavailable")
    if plan.expires_at <= clock():
        raise DirectTransferError("TransferPlan has expired", code="transfer_plan_expired")
    supplied = [root for root in (client_target_root, trusted_root, whitelist_root) if root is not None]
    if not supplied:
        raise DirectTransferError("client_target_root is required")
    if any(_root_identity(str(root), allow_local=allow_local_test) != _root_identity(str(supplied[0]), allow_local=allow_local_test) for root in supplied[1:]):
        raise DirectTransferError("conflicting trusted roots")
    target_base = _compute_target_dir(plan, supplied[0], allow_local=allow_local_test)
    source_text = str(source_root or "").strip()
    if not source_text or _looks_device_path(source_text):
        raise DirectTransferError("source_root is empty or is a device path")
    source_base = Path(source_text).expanduser()
    if not source_base.is_absolute() or not source_base.is_dir() or _is_reparse_point(source_base):
        raise DirectTransferError("source_root must be an existing absolute directory")
    _assert_no_reparse_ancestors(source_base)
    source_resolved = source_base.resolve(strict=True)
    sources: tuple[TransferSource, ...] = tuple(
        item if isinstance(item, TransferSource) else TransferSource(item.relative_path, size=item.size, sha256=item.checksum, mtime_ns=item.mtime_ns) if isinstance(item, TransferPlanItem) else TransferSource(str(item))
        for item in files
    )
    if not sources:
        raise DirectTransferError("at least one source file is required")
    paths = [item.relative_path for item in sources]
    if len(paths) != len(set(paths)):
        raise DirectTransferError("duplicate source paths are not allowed")
    started = clock()
    entries: list[ManifestEntry] = []
    for item in sources:
        source = source_resolved.joinpath(*item.relative_path.split("/"))
        _assert_no_reparse_components(source_resolved, source)
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(source_resolved)
        except (OSError, ValueError) as exc:
            raise DirectTransferError("source path escapes source_root") from exc
        destination = _safe_target_path(target_base, item.relative_path)
        report = (lambda processed, total, relative=item.relative_path: progress_callback(relative, processed, total) if progress_callback else None)
        entries.append(_copy_file_with_resume(resolved, destination, plan=plan, transfer_source=item, cancel_callback=cancel_callback, progress_callback=report, chunk_size=chunk_size, now_fn=clock))
    return TransferManifest(
        plan.transfer_id,
        entries,
        started,
        clock(),
        owner_scope=plan.owner_scope,
        job_id=plan.job_id,
        owner=plan.owner,
    )


def make_storage_ref(sha256_hex: str, *, transfer_id: str = "", relative_path: str = "") -> str:
    digest = str(sha256_hex or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise DirectTransferError("sha256 digest is invalid")
    if not transfer_id and not relative_path:
        return "cluster-staging://sha256/%s" % digest
    if not transfer_id or not relative_path:
        raise DirectTransferError("transfer_id and relative_path must be supplied together")
    relative = _normalize_relative_path(relative_path)
    return "cluster-staging://v1/t_%s/p_%s/sha256/%s" % (_digest_token(str(transfer_id), 64), _digest_token(relative, 64), digest)


def _parse_storage_ref(ref: str) -> Tuple[str, str, str]:
    text = str(ref or "").strip()
    legacy = "cluster-staging://sha256/"
    if text.startswith(legacy):
        digest = text[len(legacy):]
        if _SHA256_RE.fullmatch(digest):
            return "", "", digest
        raise DirectTransferError("storage_ref digest is invalid")
    match = re.fullmatch(r"cluster-staging://v1/t_([0-9a-f]{64})/p_([0-9a-f]{64})/sha256/([0-9a-f]{64})", text)
    if not match:
        raise DirectTransferError("storage_ref format is invalid")
    return match.group(1), match.group(2), match.group(3)


def resolve_storage_ref(
    ref: str,
    plan: TransferPlan,
    *,
    relative_path: str,
    server_probe_root: Optional[Union[str, os.PathLike[str]]] = None,
    trusted_root: Optional[Union[str, os.PathLike[str]]] = None,
    whitelist_root: Optional[Union[str, os.PathLike[str]]] = None,
    allow_local_test: bool = False,
    expected_size: Optional[int] = None,
    expected_sha256: str = "",
    require_exists: bool = False,
) -> Path:
    """Resolve a path-free ref under a bounded client or server root.

    A probe root is a deployment-only mapping.  It is deliberately not
    compared with the plan's Windows/UNC client root; the same opaque
    ``relative_root`` is used in both namespaces.
    """

    transfer_token, path_token, digest = _parse_storage_ref(ref)
    relative = _normalize_relative_path(relative_path)
    if transfer_token and transfer_token != _digest_token(plan.transfer_id, 64):
        raise DirectTransferError("storage_ref belongs to another transfer")
    if path_token and path_token != _digest_token(relative, 64):
        raise DirectTransferError("storage_ref belongs to another manifest entry")
    expected = str(expected_sha256 or "").strip().lower()
    if expected:
        if not _SHA256_RE.fullmatch(expected):
            raise DirectTransferError("expected_sha256 is invalid")
        if digest != expected:
            raise DirectTransferError("storage_ref digest does not match manifest")
    roots = [root for root in (server_probe_root, trusted_root, whitelist_root) if root is not None]
    if not roots:
        raise DirectTransferError("server_probe_root is required")
    if server_probe_root is None and any(_root_identity(str(root), allow_local=allow_local_test) != _root_identity(str(roots[0]), allow_local=allow_local_test) for root in roots[1:]):
        raise DirectTransferError("conflicting trusted roots")
    namespace = "probe" if server_probe_root is not None else "client"
    target = _compute_target_dir(plan, roots[0], allow_local=allow_local_test, namespace=namespace)
    resolved = _safe_target_path(target, relative)
    if require_exists:
        if not resolved.is_file() or _is_reparse_point(resolved):
            raise DirectTransferError("resolved storage object is unavailable")
        if expected_size is not None and resolved.stat().st_size != int(expected_size):
            raise DirectTransferError("resolved storage object size does not match manifest")
    return resolved


def generate_opaque_id(*, prefix: str = "transfer", entropy_bytes: int = 16) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", str(prefix or "")):
        raise DirectTransferError("opaque id prefix is invalid")
    if int(entropy_bytes) < 16:
        raise DirectTransferError("opaque ids require at least 128 bits of entropy")
    return "%s:sha256:%s" % (prefix, hashlib.sha256(secrets.token_bytes(int(entropy_bytes))).hexdigest())


def generate_owner_scope(owner: str, job_id: str) -> str:
    owner_text, job_text = str(owner or "").strip(), str(job_id or "").strip()
    if not owner_text or not job_text:
        raise DirectTransferError("owner and job_id are required")
    return hashlib.sha256(b"\x00".join((owner_text.encode(), job_text.encode(), secrets.token_bytes(16)))).hexdigest()
