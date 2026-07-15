"""Selena Runtime Bundle discovery and immutable manifest contract.

One usable Selena is not a bare executable.  The executable, its colocated
runtime DLLs and the selected Runtime XML form one content-addressed bundle.
MatFilter is a required simulation input; Adapter is optional.  Both are
deliberately outside the branch-bound bundle because users may reuse them
across builds.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


class RuntimeBundleError(ValueError):
    """Stable bundle discovery or integrity failure."""


_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUNDLE_ID_RE = re.compile(r"^selena-bundle:sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class RuntimeFile:
    role: str
    relative_path: str
    size: int
    checksum: str

    def __post_init__(self) -> None:
        path = str(self.relative_path or "").strip().replace("\\", "/")
        posix = PurePosixPath(path)
        if not path or posix.is_absolute() or ".." in posix.parts:
            raise RuntimeBundleError("runtime bundle file path must be relative")
        if self.role not in {"entrypoint", "runtime_library", "runtime_config"}:
            raise RuntimeBundleError("runtime bundle file role is invalid")
        if int(self.size) <= 0:
            raise RuntimeBundleError("runtime bundle file size is invalid")
        checksum = str(self.checksum or "").strip().lower()
        if not _CHECKSUM_RE.fullmatch(checksum):
            raise RuntimeBundleError("runtime bundle checksum is invalid")
        object.__setattr__(self, "relative_path", posix.as_posix())
        object.__setattr__(self, "size", int(self.size))
        object.__setattr__(self, "checksum", checksum)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeSourceEvidence:
    branch: str
    commit: str
    dirty: bool
    dirty_fingerprint: str
    build_mode: str
    toolchain_fingerprint: str
    adapter_key: str = ""

    def __post_init__(self) -> None:
        commit = str(self.commit or "").strip().lower()
        if commit and not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise RuntimeBundleError("runtime source commit is invalid")
        dirty_fingerprint = str(self.dirty_fingerprint or "").strip().lower()
        if bool(self.dirty) and not _CHECKSUM_RE.fullmatch(dirty_fingerprint):
            raise RuntimeBundleError("dirty runtime source requires a fingerprint")
        if not bool(self.dirty) and dirty_fingerprint:
            raise RuntimeBundleError("clean runtime source must not have a dirty fingerprint")
        if not str(self.build_mode or "").strip():
            raise RuntimeBundleError("runtime build mode is required")
        adapter_key = str(self.adapter_key or "").strip()
        if adapter_key and not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", adapter_key):
            raise RuntimeBundleError("runtime internal adapter key is invalid")
        object.__setattr__(self, "commit", commit)
        object.__setattr__(self, "dirty_fingerprint", dirty_fingerprint)
        object.__setattr__(self, "adapter_key", adapter_key)

    def to_dict(self) -> dict[str, Any]:
        """Public source evidence; internal recipe identity stays server-side."""
        value = asdict(self)
        value.pop("adapter_key", None)
        return value

    def identity_dict(self) -> dict[str, Any]:
        """Private identity material used when hashing and dispatching a bundle."""
        return asdict(self)


@dataclass(frozen=True)
class RuntimeBundleManifest:
    id: str
    files: tuple[RuntimeFile, ...]
    source: RuntimeSourceEvidence
    created_at: float

    def __post_init__(self) -> None:
        if not _BUNDLE_ID_RE.fullmatch(str(self.id or "")):
            raise RuntimeBundleError("runtime bundle id is invalid")
        files = tuple(self.files or ())
        roles = [item.role for item in files]
        if roles.count("entrypoint") != 1 or roles.count("runtime_config") != 1:
            raise RuntimeBundleError("runtime bundle requires one Selena entrypoint and one Runtime XML")
        if len({item.relative_path.casefold() for item in files}) != len(files):
            raise RuntimeBundleError("runtime bundle paths must be unique")
        if not math.isfinite(float(self.created_at)) or float(self.created_at) < 0:
            raise RuntimeBundleError("runtime bundle timestamp is invalid")
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "created_at", float(self.created_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "files": [item.to_dict() for item in self.files],
            "source": self.source.to_dict(),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RuntimeBundleLease:
    manifest: RuntimeBundleManifest
    locations: Mapping[str, Path]

    @property
    def public_dict(self) -> dict[str, Any]:
        return self.manifest.to_dict()


@dataclass(frozen=True)
class SimulationAsset:
    role: str
    name: str
    size: int
    checksum: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SimulationAssetsLease:
    adapter: SimulationAsset | None
    mat_filter: SimulationAsset
    locations: Mapping[str, Path]

    @property
    def public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"mat_filter": self.mat_filter.to_dict()}
        if self.adapter is not None:
            result["adapter"] = self.adapter.to_dict()
        return result


def discover_runtime_bundle(
    selena_exe: str | Path,
    runtime_xml: str | Path,
    *,
    source: RuntimeSourceEvidence,
    created_at: float,
) -> RuntimeBundleLease:
    """Hash one actual Selena output directory into a branch-bound bundle.

    P0 intentionally bundles every colocated DLL.  It is deterministic and
    safer than a hand-maintained DLL allowlist; PE import analysis can later be
    used to explain dependencies but must not silently drop delay-loaded or
    plugin DLLs.
    """
    exe = Path(selena_exe).resolve(strict=False)
    runtime = Path(runtime_xml).resolve(strict=False)
    if not exe.is_file() or exe.name.casefold() != "selena.exe":
        raise RuntimeBundleError("Selena entrypoint is unavailable")
    if not runtime.is_file() or runtime.suffix.casefold() != ".xml":
        raise RuntimeBundleError("Runtime XML is unavailable")
    binaries = [exe]
    binaries.extend(
        sorted(
            (item for item in exe.parent.iterdir() if item.is_file() and item.suffix.casefold() == ".dll"),
            key=lambda item: item.name.casefold(),
        )
    )
    files: list[RuntimeFile] = []
    locations: dict[str, Path] = {}
    for item in binaries:
        role = "entrypoint" if item == exe else "runtime_library"
        logical = "bin/" + item.name
        files.append(_runtime_file(role, logical, item))
        locations[logical] = item
    runtime_logical = "runtime/" + runtime.name
    files.append(_runtime_file("runtime_config", runtime_logical, runtime))
    locations[runtime_logical] = runtime
    files_tuple = tuple(files)
    payload = {
        "files": [item.to_dict() for item in files_tuple],
        # The hidden adapter identity participates in the immutable bundle ID
        # even though it is not part of the user's reusable YAML or public API.
        "source": source.identity_dict(),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = RuntimeBundleManifest(
        id="selena-bundle:sha256:" + digest,
        files=files_tuple,
        source=source,
        created_at=float(created_at),
    )
    return RuntimeBundleLease(manifest=manifest, locations=locations)


def discover_simulation_assets(adapter_file: str | Path, mat_filter: str | Path) -> SimulationAssetsLease:
    """Discover the reusable user-selected simulation assets.

    adapter_file is optional; pass an empty string to skip adapter discovery.
    mat_filter is always required.
    """
    adapter_text = str(adapter_file or "").strip()
    mat_filter = Path(mat_filter).resolve(strict=False)
    if not mat_filter.is_file():
        raise RuntimeBundleError("MatFilter file is unavailable")
    filter_ref = _simulation_asset("mat_filter", mat_filter)
    locations: dict[str, Path] = {"mat_filter": mat_filter}
    adapter_ref: SimulationAsset | None = None
    if adapter_text:
        adapter = Path(adapter_text).resolve(strict=False)
        if not adapter.is_file():
            raise RuntimeBundleError("Adapter file is unavailable")
        adapter_ref = _simulation_asset("adapter", adapter)
        locations["adapter"] = adapter
    return SimulationAssetsLease(
        adapter=adapter_ref,
        mat_filter=filter_ref,
        locations=locations,
    )


def verify_runtime_bundle(lease: RuntimeBundleLease) -> None:
    """Fail closed if any leased file disappeared or changed after discovery."""
    expected = {item.relative_path: item for item in lease.manifest.files}
    if set(expected) != set(lease.locations):
        raise RuntimeBundleError("runtime bundle lease file set changed")
    for logical, ref in expected.items():
        path = Path(lease.locations[logical])
        if not path.is_file() or int(path.stat().st_size) != ref.size or _sha256(path) != ref.checksum:
            raise RuntimeBundleError("runtime bundle content changed")


def _runtime_file(role: str, logical: str, path: Path) -> RuntimeFile:
    return RuntimeFile(role=role, relative_path=logical, size=path.stat().st_size, checksum=_sha256(path))


def _simulation_asset(role: str, path: Path) -> SimulationAsset:
    return SimulationAsset(role=role, name=path.name, size=path.stat().st_size, checksum=_sha256(path))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


__all__ = [
    "RuntimeBundleError",
    "RuntimeBundleLease",
    "RuntimeBundleManifest",
    "RuntimeFile",
    "RuntimeSourceEvidence",
    "SimulationAsset",
    "SimulationAssetsLease",
    "discover_runtime_bundle",
    "discover_simulation_assets",
    "verify_runtime_bundle",
]
