"""Discover and bundle an existing Selena directory for simulation.

Public input: existing_path (directory) plus runtime_xml (file).
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as _ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.runtime_bundle import (
    RuntimeBundleLease,
    RuntimeSourceEvidence,
    discover_runtime_bundle,
)
from core.runtime_bundle_archive import (
    RuntimeBundleArchive,
    stage_runtime_bundle_archive,
)


class ExistingSelenaError(ValueError):
    """Stable actionable failure when validating an existing Selena directory."""


@dataclass(frozen=True)
class ExistingSelenaResult:
    """Frozen outcome of locating an existing Selena directory.

    Private fields carry paths that never appear in public_summary.
    """

    bundle: RuntimeBundleLease
    archive: RuntimeBundleArchive
    internal_project: str
    adapter_key: str
    exe_path: Path
    runtime_path: Path

    def public_summary(self) -> dict[str, Any]:
        return {
            "runtime_bundle": self.bundle.manifest.to_dict(),
            "archive": self.archive.public_dict,
        }


def import_existing_selena(
    existing_path: str | Path,
    runtime_xml: str | Path,
    *,
    code_path: str | Path = "",
    selena_build_script: str | Path = "",
    package_build_script: str | Path = "",
    staging_root: str | Path | None = None,
    created_at: float = 0.0,
) -> ExistingSelenaResult:
    root = _resolve_directory(existing_path)
    exe = _find_unique_exe(root)
    _require_colocated_dll(exe)
    runtime = _resolve_runtime_xml(runtime_xml)
    # Existing Selena identity comes only from the executable, colocated DLLs
    # and Runtime XML.  Code/script paths may help other inference steps, but
    # they never select a product adapter or change the simulation contract.
    del code_path, selena_build_script, package_build_script
    adapter = _GENERIC_ADAPTER_KEY
    source = _build_existing_source_evidence(exe, runtime, adapter)
    project = _generic_existing_project(source.toolchain_fingerprint)
    lease = discover_runtime_bundle(exe, runtime, source=source, created_at=float(created_at))
    archive = stage_runtime_bundle_archive(lease, staging_root=staging_root)
    return ExistingSelenaResult(
        bundle=lease, archive=archive, internal_project=project,
        adapter_key=adapter, exe_path=exe, runtime_path=runtime,
    )


def _resolve_directory(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve(strict=False)
    if not path.is_dir():
        raise ExistingSelenaError("existing Selena folder does not exist or is not a directory")
    return path


def _find_unique_exe(root: Path) -> Path:
    candidates = sorted(
        (
            item.resolve(strict=False)
            for item in root.rglob("*")
            if item.is_file()
            and item.name.casefold() == "selena.exe"
            and len(item.relative_to(root).parts) <= 7
        ),
        key=lambda item: item.as_posix().casefold(),
    )
    if not candidates:
        raise ExistingSelenaError("Selena.exe was not found in the existing Selena folder")
    if len(candidates) > 1:
        raise ExistingSelenaError("multiple Selena.exe files were found; select the exact Selena output folder")
    return candidates[0]


def _exe_in_dir(directory: Path) -> Path | None:
    for item in sorted(directory.iterdir(), key=lambda p: p.name.casefold()):
        if item.is_file() and item.name.casefold() == "selena.exe":
            return item
    return None


def _require_colocated_dll(exe: Path) -> None:
    for item in exe.parent.iterdir():
        if item.is_file() and item.suffix.casefold() == ".dll":
            return
    raise ExistingSelenaError("no colocated DLL was found next to Selena.exe")


def _resolve_runtime_xml(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve(strict=False)
    if not path.is_file():
        raise ExistingSelenaError("Runtime XML does not exist or is not an XML file")
    raw = path.read_bytes()
    if not raw.strip():
        raise ExistingSelenaError("Runtime XML must not be empty")
    try:
        _ET.fromstring(raw)
    except _ET.ParseError as exc:
        raise ExistingSelenaError("Runtime XML is not valid XML") from exc
    return path


_GENERIC_ADAPTER_KEY = "generic:existing-selena"


def _generic_existing_project(toolchain_fingerprint: str) -> str:
    """Create a path-free namespace for one unregistered Runtime Bundle.

    The source fingerprint covers Selena.exe, Runtime XML and colocated DLL
    bytes.  Hashing it with a versioned domain makes the namespace deterministic
    when the same bundle is relocated while avoiding product-name guesses.
    """
    fingerprint = str(toolchain_fingerprint or "").strip()
    if not fingerprint.startswith("sha256:"):
        raise ExistingSelenaError("existing Selena fingerprint is unavailable")
    digest = hashlib.sha256(
        ("radar-sim.existing-selena-project.v1\0" + fingerprint).encode("utf-8")
    ).hexdigest()[:24]
    return f"workspace-{digest}"


def _build_existing_source_evidence(
    exe: Path, runtime: Path, adapter_key: str
) -> RuntimeSourceEvidence:
    digest = hashlib.sha256()
    paths = [exe, runtime, *sorted(
        (item for item in exe.parent.iterdir() if item.is_file() and item.suffix.casefold() == ".dll"),
        key=lambda item: item.name.casefold(),
    )]
    for path in paths:
        digest.update(path.name.casefold().encode("utf-8"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    fingerprint = "sha256:" + digest.hexdigest()
    return RuntimeSourceEvidence(
        branch="existing", commit="", dirty=False,
        dirty_fingerprint="", build_mode="existing",
        toolchain_fingerprint=fingerprint, adapter_key=adapter_key,
    )


__all__ = [
    "ExistingSelenaError",
    "ExistingSelenaResult",
    "import_existing_selena",
]
