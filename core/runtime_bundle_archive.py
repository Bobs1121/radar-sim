"""Deterministic Agent-local archive for one immutable Selena Runtime Bundle.

The archive is an internal transport object.  It contains the public bundle
manifest plus the exact ``selena.exe``, colocated DLLs and Runtime XML that the
manifest hashes.  Adapter and MatFilter deliberately stay outside this file.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from core.runtime_bundle import RuntimeBundleLease, RuntimeBundleError, RuntimeBundleManifest, verify_runtime_bundle


class RuntimeBundleArchiveError(ValueError):
    """Stable archive construction or integrity failure."""


_FORMAT = "radar-sim.runtime-bundle-archive/1"
_MANIFEST_ENTRY = "runtime-bundle.json"


@dataclass(frozen=True)
class RuntimeBundleArchive:
    bundle_id: str
    path: Path
    checksum: str
    size: int
    file_count: int

    @property
    def public_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "checksum": self.checksum,
            "size": self.size,
            "file_count": self.file_count,
            "format": _FORMAT,
        }


def stage_runtime_bundle_archive(
    lease: RuntimeBundleLease,
    staging_root: str | Path | None = None,
) -> RuntimeBundleArchive:
    """Create or reuse a deterministic, content-addressed Runtime Bundle zip."""
    if not isinstance(lease, RuntimeBundleLease):
        raise RuntimeBundleArchiveError("runtime bundle lease is required")
    try:
        verify_runtime_bundle(lease)
    except (RuntimeBundleError, OSError) as exc:
        raise RuntimeBundleArchiveError("runtime bundle content is unavailable") from exc

    root = _staging_root(staging_root)
    digest = lease.manifest.id.rsplit(":", 1)[-1]
    target = root / f"runtime-bundle-{digest}.zip"
    if target.exists():
        return _inspect_archive(target, lease)

    fd, temporary_name = tempfile.mkstemp(prefix="runtime-bundle-", suffix=".tmp", dir=str(root))
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        _write_archive(temporary, lease)
        # Detect a build output or Runtime XML mutation that raced packaging.
        verify_runtime_bundle(lease)
        os.replace(temporary, target)
        return _inspect_archive(target, lease)
    except RuntimeBundleArchiveError:
        raise
    except (OSError, RuntimeBundleError, zipfile.BadZipFile) as exc:
        raise RuntimeBundleArchiveError("runtime bundle archive staging failed") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def verify_runtime_bundle_archive(
    archive: RuntimeBundleArchive,
    lease: RuntimeBundleLease | None = None,
) -> None:
    """Verify the archive checksum, manifest, paths and every embedded file."""
    if not isinstance(archive, RuntimeBundleArchive):
        raise RuntimeBundleArchiveError("runtime bundle archive is required")
    path = Path(archive.path)
    if not path.is_file() or path.is_symlink():
        raise RuntimeBundleArchiveError("runtime bundle archive is unavailable")
    if path.stat().st_size != archive.size or _sha256(path) != archive.checksum:
        raise RuntimeBundleArchiveError("runtime bundle archive changed")
    if lease is not None:
        inspected = _inspect_archive(path, lease)
        if inspected != archive:
            raise RuntimeBundleArchiveError("runtime bundle archive evidence changed")


def extract_runtime_bundle_archive(
    archive_path: str | Path,
    destination: str | Path,
    *,
    manifest: RuntimeBundleManifest,
    archive_checksum: str,
) -> dict[str, Path]:
    """Verify and atomically extract a catalogued Runtime Bundle for execution.

    The central Cluster executor calls this only after resolving a trusted
    catalog record.  User-controlled ZIP member names are never written
    directly: the expected member set comes from the immutable manifest and
    every extracted byte is checked before the directory becomes visible.
    """
    if not isinstance(manifest, RuntimeBundleManifest):
        raise RuntimeBundleArchiveError("runtime bundle manifest is required")
    source = Path(archive_path)
    target = Path(destination)
    if not source.is_file() or source.is_symlink():
        raise RuntimeBundleArchiveError("runtime bundle archive is unavailable")
    if _sha256(source) != str(archive_checksum or "").strip().lower():
        raise RuntimeBundleArchiveError("runtime bundle archive checksum changed")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(tempfile.mkdtemp(prefix=target.name + "-", dir=str(target.parent)))
    try:
        expected = {_MANIFEST_ENTRY, *(item.relative_path for item in manifest.files)}
        with zipfile.ZipFile(source, "r") as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or set(names) != expected:
                raise RuntimeBundleArchiveError("runtime bundle archive file set is invalid")
            for info in infos:
                logical = _safe_archive_path(info.filename)
                # Reject directories, symlinks and other special Unix file types.
                mode = (int(info.external_attr) >> 16) & 0o170000
                if info.is_dir() or mode not in {0, 0o100000}:
                    raise RuntimeBundleArchiveError("runtime bundle archive member type is invalid")
                output = temporary.joinpath(*PurePosixPath(logical).parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as reader, output.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
            payload = json.loads((temporary / _MANIFEST_ENTRY).read_text(encoding="utf-8"))
            if payload != {"format": _FORMAT, "bundle": manifest.to_dict()}:
                raise RuntimeBundleArchiveError("runtime bundle archive manifest is invalid")
        for item in manifest.files:
            extracted = temporary.joinpath(*PurePosixPath(item.relative_path).parts)
            if extracted.stat().st_size != item.size or _sha256(extracted) != item.checksum:
                raise RuntimeBundleArchiveError("runtime bundle archive member changed")
        (temporary / _MANIFEST_ENTRY).unlink()
        if target.exists():
            shutil.rmtree(target)
        os.replace(temporary, target)
        temporary = None
    except RuntimeBundleArchiveError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeBundleArchiveError("runtime bundle archive extraction failed") from exc
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return {item.relative_path: target.joinpath(*PurePosixPath(item.relative_path).parts) for item in manifest.files}


def _staging_root(value: str | Path | None) -> Path:
    if value is None:
        home = str(os.environ.get("RSIM_HOME") or "").strip()
        root = (Path(home).expanduser() if home else Path.home() / ".rsim") / "agent" / "runtime-bundles"
    else:
        root = Path(value).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeBundleArchiveError("runtime bundle staging directory is unavailable") from exc
    if not root.is_dir() or root.is_symlink():
        raise RuntimeBundleArchiveError("runtime bundle staging directory is invalid")
    return root


def _manifest_bytes(lease: RuntimeBundleLease) -> bytes:
    payload = {"format": _FORMAT, "bundle": lease.manifest.to_dict()}
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    info.create_system = 3
    return info


def _write_archive(path: Path, lease: RuntimeBundleLease) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.writestr(_entry(_MANIFEST_ENTRY), _manifest_bytes(lease))
        for item in sorted(lease.manifest.files, key=lambda value: value.relative_path.casefold()):
            logical = _safe_archive_path(item.relative_path)
            source = Path(lease.locations[item.relative_path])
            with source.open("rb") as reader, archive.open(_entry(logical), "w", force_zip64=True) as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)


def _inspect_archive(path: Path, lease: RuntimeBundleLease) -> RuntimeBundleArchive:
    expected = {_MANIFEST_ENTRY, *(item.relative_path for item in lease.manifest.files)}
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != expected:
                raise RuntimeBundleArchiveError("runtime bundle archive file set is invalid")
            for name in names:
                _safe_archive_path(name)
            manifest = json.loads(archive.read(_MANIFEST_ENTRY).decode("utf-8"))
            if manifest != {"format": _FORMAT, "bundle": lease.manifest.to_dict()}:
                raise RuntimeBundleArchiveError("runtime bundle archive manifest is invalid")
            by_path = {item.relative_path: item for item in lease.manifest.files}
            for logical, evidence in by_path.items():
                digest = hashlib.sha256()
                size = 0
                with archive.open(logical, "r") as reader:
                    for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                        size += len(chunk)
                        digest.update(chunk)
                if size != evidence.size or "sha256:" + digest.hexdigest() != evidence.checksum:
                    raise RuntimeBundleArchiveError("runtime bundle archive member changed")
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeBundleArchiveError("runtime bundle archive is invalid") from exc
    return RuntimeBundleArchive(
        bundle_id=lease.manifest.id,
        path=path,
        checksum=_sha256(path),
        size=path.stat().st_size,
        file_count=len(lease.manifest.files),
    )


def _safe_archive_path(value: str) -> str:
    text = str(value or "").replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or text.startswith("/"):
        raise RuntimeBundleArchiveError("runtime bundle archive path is invalid")
    return path.as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


__all__ = [
    "RuntimeBundleArchive",
    "RuntimeBundleArchiveError",
    "stage_runtime_bundle_archive",
    "extract_runtime_bundle_archive",
    "verify_runtime_bundle_archive",
]
