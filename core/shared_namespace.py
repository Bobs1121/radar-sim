"""Trusted mapping between user-visible shared paths and deployment paths.

An arbitrary UNC path is never assumed to be Cluster-accessible.  Only paths
under an administrator-configured namespace can be resolved.  The resulting
central/worker paths are private execution details and are not serializable as
part of a public DatasetRef.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping


class SharedNamespaceError(ValueError):
    """Stable shared namespace configuration or resolution error."""


_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class SharedNamespace:
    namespace_id: str
    shared_prefix: str
    central_root: str
    worker_root: str

    def __post_init__(self) -> None:
        namespace_id = str(self.namespace_id or "").strip()
        if not _NAME_RE.fullmatch(namespace_id):
            raise SharedNamespaceError("shared namespace id is invalid")
        shared_parts = _unc_parts(self.shared_prefix)
        worker_parts = _unc_parts(self.worker_root)
        central = str(self.central_root or "").strip().replace("\\", "/")
        central_path = PurePosixPath(central)
        if (
            not (central_path.is_absolute() or PureWindowsPath(central).is_absolute())
            or any(part in {"", ".", ".."} for part in central_path.parts)
        ):
            raise SharedNamespaceError("shared namespace central root must be an absolute mount path")
        object.__setattr__(self, "namespace_id", namespace_id)
        object.__setattr__(self, "shared_prefix", "//" + "/".join(shared_parts))
        object.__setattr__(self, "central_root", central_path.as_posix().rstrip("/") or "/")
        object.__setattr__(self, "worker_root", "\\\\" + "\\".join(worker_parts))


@dataclass(frozen=True)
class ResolvedSharedPath:
    """Private mapping result; do not put this object in API responses."""

    namespace_id: str
    relative_path: str
    central_probe_path: str
    worker_path: str


class SharedNamespaceRegistry:
    """Longest-prefix registry built exclusively from administrator config."""

    def __init__(self, namespaces: Iterable[SharedNamespace] = ()) -> None:
        entries = list(namespaces or ())
        if any(not isinstance(item, SharedNamespace) for item in entries):
            raise SharedNamespaceError("shared namespace entry is invalid")
        ids: set[str] = set()
        prefixes: set[str] = set()
        for item in entries:
            prefix_key = item.shared_prefix.casefold()
            if item.namespace_id in ids or prefix_key in prefixes:
                raise SharedNamespaceError("shared namespace ids and prefixes must be unique")
            ids.add(item.namespace_id)
            prefixes.add(prefix_key)
        self._entries = tuple(sorted(entries, key=lambda item: len(item.shared_prefix), reverse=True))

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "SharedNamespaceRegistry":
        payload = dict(config or {})
        cluster = dict(payload.get("cluster") or {})
        raw = payload.get("shared_namespaces") or cluster.get("shared_namespaces") or []
        if raw:
            return cls(
                SharedNamespace(
                    namespace_id=str(item.get("id") or item.get("name") or ""),
                    shared_prefix=str(item.get("shared_prefix") or item.get("prefix") or ""),
                    central_root=str(item.get("central_root") or item.get("central_mount") or ""),
                    worker_root=str(item.get("worker_root") or item.get("worker_unc") or item.get("shared_prefix") or ""),
                )
                for item in raw
            )
        return cls.from_linux_mount_map(cluster.get("linux_mount_map") or payload.get("linux_mount_map") or {})

    @classmethod
    def from_linux_mount_map(cls, mount_map: Mapping[str, str]) -> "SharedNamespaceRegistry":
        """Migrate the existing ``UNC prefix -> Linux mount`` deployment map."""
        entries = []
        for index, (prefix, mount) in enumerate(dict(mount_map or {}).items(), start=1):
            entries.append(
                SharedNamespace(
                    namespace_id=f"mount_{index}",
                    shared_prefix=str(prefix),
                    central_root=str(mount),
                    worker_root=str(prefix),
                )
            )
        return cls(entries)

    def resolve(self, shared_path: str) -> ResolvedSharedPath:
        parts = _unc_parts(shared_path)
        normalized = "//" + "/".join(parts)
        folded = normalized.casefold()
        for entry in self._entries:
            prefix = entry.shared_prefix
            prefix_folded = prefix.casefold()
            if folded == prefix_folded:
                suffix_parts: tuple[str, ...] = ()
            elif folded.startswith(prefix_folded + "/"):
                prefix_count = len(_unc_parts(prefix))
                suffix_parts = parts[prefix_count:]
            else:
                continue
            relative = "/".join(suffix_parts)
            central = entry.central_root
            worker = entry.worker_root
            if relative:
                central = central.rstrip("/") + "/" + relative
                worker = worker.rstrip("\\") + "\\" + relative.replace("/", "\\")
            return ResolvedSharedPath(
                namespace_id=entry.namespace_id,
                relative_path=relative,
                central_probe_path=central,
                worker_path=worker,
            )
        raise SharedNamespaceError("shared path is not under an authorized namespace")

    def is_authorized(self, shared_path: str) -> bool:
        try:
            self.resolve(shared_path)
            return True
        except SharedNamespaceError:
            return False

    def public_summary(self) -> list[dict[str, str]]:
        """Return identifiers only; physical prefixes and mounts stay private."""
        return [{"id": item.namespace_id} for item in self._entries]


def looks_like_shared_path(value: str) -> bool:
    try:
        _unc_parts(value)
        return True
    except SharedNamespaceError:
        return False


def _unc_parts(value: str) -> tuple[str, ...]:
    text = str(value or "").strip()
    if text.startswith("\\\\"):
        rest = text[2:].replace("\\", "/")
    elif text.startswith("//"):
        rest = text[2:].replace("\\", "/")
    else:
        raise SharedNamespaceError("shared path must be UNC-style")
    raw = rest.split("/")
    if len(raw) < 2 or any(part in {"", ".", ".."} for part in raw):
        raise SharedNamespaceError("shared path must include host/share and contain no traversal")
    for part in raw:
        if any(ord(char) < 32 or ord(char) == 127 for char in part):
            raise SharedNamespaceError("shared path contains control characters")
    return tuple(raw)


__all__ = [
    "ResolvedSharedPath",
    "SharedNamespace",
    "SharedNamespaceError",
    "SharedNamespaceRegistry",
    "looks_like_shared_path",
]
