"""Discover and bundle an existing Selena directory for simulation.

Public input: existing_path (directory) plus runtime_xml (file).
"""

from __future__ import annotations

import hashlib
import re
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
    artifact_recognition = _infer_project_adapter(root, runtime, exe)
    workspace_recognition = _recognize_workspace_product(
        code_path=code_path,
        selena_build_script=selena_build_script,
        package_build_script=package_build_script,
    )
    recognized = _merge_product_evidence(
        artifact_recognition,
        workspace_recognition,
    )
    adapter = recognized[1] if recognized is not None else _GENERIC_ADAPTER_KEY
    source = _build_existing_source_evidence(exe, runtime, adapter)
    project = (
        recognized[0]
        if recognized is not None
        else _generic_existing_project(source.toolchain_fingerprint)
    )
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


_KNOWN_ADAPTERS: dict[str, tuple[str, tuple[str, ...]]] = {
    # Product-specific evidence only.  Generic markers such as ``od25`` and
    # ``ovs`` are deliberately excluded: they occur in other products and
    # must never silently route an artifact through a BYD adapter.
    "ovrs25": ("ovrs25", ("ovrs25", "byd_ovs", "ovrs")),
    "bydod25": ("bydod25", ("bydod25", "byd_od25", "g3n_fvg3_od25")),
}
_GENERIC_ADAPTER_KEY = "generic:existing-selena"


def _infer_project_adapter(
    root: Path,
    runtime: Path,
    exe: Path,
) -> tuple[str, str] | None:
    """Return one proven adapter, otherwise leave the artifact project-free.

    ``config/projects`` is an administrator implementation detail, not a
    registry a user must keep up to date.  Unknown products therefore use a
    stable opaque workspace namespace after their Runtime Bundle is hashed.
    Folder names such as ``pl-xpeng`` or ``gac_od25`` are not enough evidence
    to invent an adapter or append a product suffix.
    """
    tokens: list[str] = []
    _extract_path_tokens(str(root), tokens)
    _extract_path_tokens(str(runtime), tokens)
    _extract_path_tokens(runtime.name, tokens)
    _extract_path_tokens(exe.name, tokens)
    for item in exe.parent.iterdir():
        if item.is_file() and item.suffix.casefold() == ".dll":
            _extract_path_tokens(item.name, tokens)
    try:
        content = runtime.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ExistingSelenaError("cannot read runtime_xml: " + str(exc)) from exc
    tokens.append(content[:8192].casefold())

    matches: list[tuple[str, str]] = []
    for project, aliases_tuple in _KNOWN_ADAPTERS.items():
        adapters = aliases_tuple[1]
        key = (project, "recipe:g3n_fvg3_od25" if project == "bydod25" else "project:ovrs25")
        if any(_contains_alias(token, alias) for alias in adapters for token in tokens):
            matches.append(key)
    if not matches:
        return None
    if len(matches) > 1:
        names = ", ".join(p + "/" + a for p, a in matches)
        raise ExistingSelenaError("Selena project recognition is ambiguous: " + names)
    return matches[0]


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


def _recognize_workspace_product(
    *,
    code_path: str | Path,
    selena_build_script: str | Path,
    package_build_script: str | Path,
) -> tuple[str, str] | None:
    """Reuse build-workspace recognition as optional product evidence."""
    root = str(code_path or "").strip()
    selena_script = str(selena_build_script or "").strip()
    package_script = str(package_build_script or "").strip()
    if not any((root, selena_script, package_script)):
        return None
    if not root:
        raise ExistingSelenaError(
            "code_path is required when existing Selena build-script evidence is provided"
        )

    from core.workspace_recognizer import WorkspaceRecognizer

    outcome = WorkspaceRecognizer().recognize(
        root,
        selena_build_script=selena_script,
        package_build_script=package_script,
    )
    if outcome.status == "ambiguous":
        raise ExistingSelenaError(
            "Selena product evidence conflicts: multiple workspace adapters match; "
            "confirm the code path and both build scripts"
        )
    if outcome.status == "unresolved":
        hard_errors = {
            "build_script_outside_workspace",
            "package_build_script_outside_workspace",
            "configured_build_script_outside_workspace",
            "code_path_must_be_absolute",
        }
        if hard_errors.intersection(outcome.evidence):
            raise ExistingSelenaError(
                "existing Selena workspace evidence is invalid: use an absolute code_path "
                "and select build scripts inside that repository"
            )
        # A code path by itself may carry no product-specific marker.  That is
        # insufficient evidence, not an error; artifact identity remains the
        # safe anonymous fallback.
        return None
    if outcome.adapter_key == "generic:selena-script":
        return None
    return outcome.internal_project, outcome.adapter_key


def _merge_product_evidence(
    artifact: tuple[str, str] | None,
    workspace: tuple[str, str] | None,
) -> tuple[str, str] | None:
    """Prefer proven evidence and fail closed when two products disagree."""
    if artifact is not None and workspace is not None and artifact != workspace:
        raise ExistingSelenaError(
            "Selena product evidence conflicts: the existing folder/Runtime and "
            "the code repository/build scripts identify different products; "
            "confirm that all selected paths belong to the same product"
        )
    return workspace or artifact


def _extract_path_tokens(text: str, out: list[str]) -> None:
    normalized = text.casefold()
    normalized = normalized.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]
    for part in parts:
        out.append(part)


def _contains_alias(token: str, alias: str) -> bool:
    """Match a product marker without accepting it inside another word."""
    return re.search(
        rf"(?<![a-z0-9]){re.escape(alias.casefold())}(?![a-z0-9])",
        token.casefold(),
    ) is not None


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
