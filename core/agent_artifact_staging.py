"""Pure/local Windows Agent staging boundary for Selena artifacts.

This module enforces filesystem authorization, evidence capture, and
immutable artifact construction without uploading, networking, or catalog
registration. It reuses :class:`core.repo.WorkspaceFingerprint`,
:func:`core.repo.inspect_workspace`, and :class:`core.artifacts.SelenaArtifact`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from core.artifacts import ArtifactValidationError, SelenaArtifact
from core.repo import RepoSourceError, WorkspaceFingerprint, inspect_workspace


class AgentArtifactStagingError(ValueError):
    """Stable staging error with path-free public messages."""


# ---------------------------------------------------------------------------
# Authorization model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthorizedRoots:
    """Immutable authorization boundary for a single staging operation.

    *workspace_root* is the resolved, existing directory that contains source.
    *output_roots* is a tuple of resolved, existing directories that may
    contain build outputs. Every output root must be inside *workspace_root*.
    """

    workspace_root: Path
    output_roots: tuple[Path, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_root, (str, os.PathLike)) or not str(self.workspace_root).strip():
            raise AgentArtifactStagingError("workspace_root must not be empty")
        if isinstance(self.output_roots, (str, bytes)) or not isinstance(self.output_roots, (list, tuple)):
            raise AgentArtifactStagingError("output_roots must be a list of directories")
        if any(not isinstance(path, (str, os.PathLike)) or not str(path).strip() for path in self.output_roots):
            raise AgentArtifactStagingError("output_root must not be empty")
        # Resolve strictly (follows symlinks on POSIX; on Windows resolve()
        # follows reparse points where the OS allows).
        try:
            ws = Path(self.workspace_root).resolve(strict=True)
        except OSError as exc:
            raise AgentArtifactStagingError("workspace_root must be an existing directory") from exc
        if not ws.is_dir():
            raise AgentArtifactStagingError("workspace_root must be an existing directory")
        # Reject empty / root-of-drive authorization.
        if ws == Path(ws.anchor):
            raise AgentArtifactStagingError("workspace_root must not be a drive root")

        try:
            outs: tuple[Path, ...] = tuple(Path(p).resolve(strict=True) for p in self.output_roots)
        except OSError as exc:
            raise AgentArtifactStagingError("each output_root must be an existing directory") from exc
        if not outs:
            raise AgentArtifactStagingError("at least one output_root is required")
        for o in outs:
            if not o.is_dir():
                raise AgentArtifactStagingError("each output_root must be an existing directory")
            # containment: output must be under workspace.
            try:
                o.relative_to(ws)
            except ValueError:
                raise AgentArtifactStagingError("output_root must be inside workspace_root")
            if o == ws:
                raise AgentArtifactStagingError("output_root must be narrower than workspace_root")
            # Reject symlink / reparse-style escape by comparing resolved paths.
            if not _is_fully_resolved_under(o, ws):
                raise AgentArtifactStagingError("output_root resolved outside workspace_root")
            # Reject drive root outputs.
            if o == Path(o.anchor):
                raise AgentArtifactStagingError("output_root must not be a drive root")

        # Re-freeze with resolved values (object.__setattr__ because frozen).
        object.__setattr__(self, "workspace_root", ws)
        object.__setattr__(self, "output_roots", outs)

    def contains_workspace(self, path: str | Path) -> bool:
        """Return *True* if *path* resolves to a location inside *workspace_root*."""
        try:
            resolved = Path(path).resolve()
            resolved.relative_to(self.workspace_root)
            return _is_fully_resolved_under(resolved, self.workspace_root)
        except (ValueError, OSError):
            return False

    def contains_output(self, path: str | Path) -> bool:
        """Return *True* if *path* resolves to a location inside any authorized output root."""
        try:
            resolved = Path(path).resolve()
            for o in self.output_roots:
                try:
                    resolved.relative_to(o)
                    if _is_fully_resolved_under(resolved, o):
                        return True
                except ValueError:
                    continue
            return False
        except OSError:
            return False


def _is_fully_resolved_under(candidate: Path, parent: Path) -> bool:
    """Verify *candidate* is truly under *parent* after resolving symlinks/reparse points.

    On Windows ``Path.resolve()`` may not follow all reparse-point types when
    the process lacks privilege. We therefore also compare the string prefix
    of the resolved absolute path as a fallback.
    """
    # Fast path: resolved paths already.
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    # Ensure candidate is not a symlink/reparse that jumps outside.
    # On POSIX, resolve() follows symlinks, so the resolved path already
    # reflects the real location. On Windows, if resolve() could not follow
    # a reparse point, the path may still look nested while actually
    # pointing elsewhere. We use os.path.realpath as a second opinion.
    real_candidate = Path(os.path.realpath(str(candidate)))
    real_parent = Path(os.path.realpath(str(parent)))
    try:
        real_candidate.relative_to(real_parent)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Snapshot capture
# ---------------------------------------------------------------------------

def capture_source_snapshot(workspace: str | Path, authorized: AuthorizedRoots) -> WorkspaceFingerprint:
    """Capture a workspace fingerprint after verifying authorization.

    Raises :class:`AgentArtifactStagingError` if *workspace* is not inside the
    authorized workspace root.
    """
    if not authorized.contains_workspace(workspace):
        raise AgentArtifactStagingError("workspace is not within authorized roots")
    try:
        return inspect_workspace(workspace)
    except (RepoSourceError, OSError, subprocess.SubprocessError) as exc:
        raise AgentArtifactStagingError("workspace inspection failed") from exc


# ---------------------------------------------------------------------------
# Artifact validation & hashing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArtifactEvidence:
    """Immutable evidence for a single validated artifact binary."""

    checksum: str          # sha256:<64-hex>
    size: int              # bytes
    logical_path: str      # relative to the output root, POSIX-style

    def __post_init__(self) -> None:
        checksum = str(self.checksum or "").strip().lower()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum):
            raise AgentArtifactStagingError("artifact checksum is invalid")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size <= 0:
            raise AgentArtifactStagingError("artifact size must be a positive integer")
        logical = str(self.logical_path or "").strip()
        posix = PurePosixPath(logical)
        windows = PureWindowsPath(logical)
        if (
            not logical
            or "\\" in logical
            or posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or any(part in {"", ".", ".."} for part in posix.parts)
            or posix.as_posix() != logical
        ):
            raise AgentArtifactStagingError("artifact logical path must be relative")
        if posix.name.lower() != "selena.exe":
            raise AgentArtifactStagingError("artifact logical path must name selena.exe")
        object.__setattr__(self, "checksum", checksum)
        object.__setattr__(self, "logical_path", logical)


def validate_and_hash_artifact(executable: str | Path, authorized: AuthorizedRoots) -> ArtifactEvidence:
    """Validate *executable* is an authorized artifact and return streaming SHA256 evidence.

    Checks:
    * path resolves inside an authorized output root
    * exists, is a regular file, not a symlink
    * filename is ``selena.exe`` (case-insensitive)
    * non-empty
    """
    path = Path(executable)
    try:
        original_stat = path.lstat()
    except OSError as exc:
        raise AgentArtifactStagingError("artifact path is not accessible") from exc
    if stat.S_ISLNK(original_stat.st_mode) or path.is_symlink():
        raise AgentArtifactStagingError("artifact must not be a symbolic link")
    if int(getattr(original_stat, "st_nlink", 1) or 1) != 1:
        raise AgentArtifactStagingError("artifact must not be a hard link")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AgentArtifactStagingError("artifact path is not accessible") from exc

    if not authorized.contains_output(resolved):
        raise AgentArtifactStagingError("artifact path is outside authorized output roots")

    # Must exist and be a regular non-symlink file.
    try:
        st = resolved.lstat()
    except OSError as exc:
        raise AgentArtifactStagingError("artifact path is not accessible") from exc

    if stat.S_ISLNK(st.st_mode):
        raise AgentArtifactStagingError("artifact must not be a symbolic link")
    if not stat.S_ISREG(st.st_mode):
        raise AgentArtifactStagingError("artifact must be a regular file")
    if st.st_size == 0:
        raise AgentArtifactStagingError("artifact must not be empty")

    # Filename must be selena.exe (case-insensitive).
    if resolved.name.lower() != "selena.exe":
        raise AgentArtifactStagingError("artifact filename must be selena.exe")

    # Streaming SHA256.
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AgentArtifactStagingError("artifact hashing failed") from exc
    try:
        after_stat = resolved.stat()
    except OSError as exc:
        raise AgentArtifactStagingError("artifact changed during hashing") from exc
    identity_before = (
        int(getattr(st, "st_dev", 0)),
        int(getattr(st, "st_ino", 0)),
        int(st.st_size),
        int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
    )
    identity_after = (
        int(getattr(after_stat, "st_dev", 0)),
        int(getattr(after_stat, "st_ino", 0)),
        int(after_stat.st_size),
        int(getattr(after_stat, "st_mtime_ns", int(after_stat.st_mtime * 1_000_000_000))),
    )
    if identity_before != identity_after:
        raise AgentArtifactStagingError("artifact changed during hashing")
    checksum = f"sha256:{digest.hexdigest()}"

    # Logical relative path: relative to the containing output root, POSIX slashes.
    containing_root: Path | None = None
    for o in authorized.output_roots:
        try:
            resolved.relative_to(o)
            containing_root = o
            break
        except ValueError:
            continue
    if containing_root is None:
        raise AgentArtifactStagingError("artifact path is outside authorized output roots")

    rel = resolved.relative_to(containing_root).as_posix()
    return ArtifactEvidence(checksum=checksum, size=st.st_size, logical_path=rel)


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def stage_selena_artifact(
    *,
    before: WorkspaceFingerprint,
    after: WorkspaceFingerprint,
    evidence: ArtifactEvidence,
    authorized: AuthorizedRoots,
    project: str,
    owner: str,
    build_mode: str,
    toolchain_fingerprint: str,
    source_kind: str,
    storage_ref: str,
    visibility: str,
    accessibility: str,
    created_by: str,
    created_at: float | None = None,
    retain_until: float | None = None,
    interface_manifest: dict[str, Any] | None = None,
    signal_manifest: dict[str, Any] | None = None,
) -> SelenaArtifact:
    """Construct a :class:`SelenaArtifact` from validated staging inputs.

    *branch* and *commit* come from *before* snapshot.
    *dirty_fingerprint* uses *before.sha256*.
    *source_changed_during_build* is ``True`` when *before.sha256* != *after.sha256*
    or when *before.commit* != *after.commit*.

    If the workspace is dirty or source changed, visibility is forced to
    ``private`` by :class:`SelenaArtifact` validation.

    Absolute workspace / output / exe paths are never placed into the artifact
    or manifests.
    """
    if not isinstance(authorized, AuthorizedRoots):
        raise AgentArtifactStagingError("authorized roots are required")
    if not isinstance(before, WorkspaceFingerprint) or not isinstance(after, WorkspaceFingerprint):
        raise AgentArtifactStagingError("workspace snapshots are required")
    if not isinstance(evidence, ArtifactEvidence):
        raise AgentArtifactStagingError("validated artifact evidence is required")
    for snapshot in (before, after):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", snapshot.commit):
            raise AgentArtifactStagingError("workspace snapshot commit is invalid")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", snapshot.sha256):
            raise AgentArtifactStagingError("workspace snapshot checksum is invalid")

    dirty = before.dirty
    changed = (before.sha256 != after.sha256) or (before.commit != after.commit)

    # If dirty or changed, SelenaArtifact forces private; we leave the caller's
    # visibility value to be overridden by SelenaArtifact.__post_init__.
    # We only validate that the caller didn't pass an invalid value.
    visibility_clean = str(visibility or "").strip().lower()
    if visibility_clean not in {"private", "shared"}:
        raise AgentArtifactStagingError("visibility must be private or shared")

    accessibility_clean = str(accessibility or "").strip().lower()
    if accessibility_clean not in {"local", "cluster", "shared"}:
        raise AgentArtifactStagingError("accessibility must be local, cluster, or shared")

    # Validate project/owner/build_mode through SelenaArtifact (it will raise
    # ArtifactValidationError on empty values). We pre-check only for clarity.
    for field_name, value in (
        ("project", project),
        ("owner", owner),
        ("build_mode", build_mode),
        ("source_kind", source_kind),
        ("created_by", created_by),
    ):
        if not str(value or "").strip():
            raise AgentArtifactStagingError(f"{field_name} must not be empty")
        _assert_no_abs_paths(str(value), field_name)

    # created_at / retain_until must be finite and non-negative (SelenaArtifact enforces too).
    now = time.time()
    try:
        created = float(created_at if created_at is not None else now)
        retain = float(retain_until if retain_until is not None else 0.0)
    except (TypeError, ValueError) as exc:
        raise AgentArtifactStagingError("artifact timestamps must be numeric") from exc
    if created < 0 or not math.isfinite(created):
        raise AgentArtifactStagingError("created_at must be a finite non-negative number")
    if retain < 0 or not math.isfinite(retain):
        raise AgentArtifactStagingError("retain_until must be a finite non-negative number")

    # Build manifests that contain only logical / public data.
    iface = dict(interface_manifest) if interface_manifest else {}
    sig = dict(signal_manifest) if signal_manifest else {}

    # Ensure no absolute paths leaked into manifests.
    _assert_no_abs_paths(iface, "interface_manifest")
    _assert_no_abs_paths(sig, "signal_manifest")

    try:
        artifact = SelenaArtifact(
            id="",  # catalog will generate if empty
            project=project,
            owner=owner,
            visibility=visibility,
            branch=before.branch,
            commit=before.commit,
            source_kind=source_kind,
            dirty=dirty,
            dirty_fingerprint=before.sha256,
            source_changed_during_build=changed,
            build_mode=build_mode,
            toolchain_fingerprint=toolchain_fingerprint,
            binary_checksum=evidence.checksum,
            interface_manifest=iface,
            signal_manifest=sig,
            storage_ref=storage_ref,
            accessibility=accessibility,
            health="ready",
            created_by=created_by,
            created_at=created,
            retain_until=retain,
        )
    except ArtifactValidationError as exc:
        raise AgentArtifactStagingError(f"artifact validation failed: {exc}") from exc

    return artifact


def _assert_no_abs_paths(value: Any, context: str) -> None:
    """Recursively assert that *value* contains no absolute Windows/Unix paths."""
    if isinstance(value, str):
        # Reject strings that look like absolute paths.
        v = value.strip()
        if len(v) >= 2 and v[1] == ":" and v[0].isalpha():
            raise AgentArtifactStagingError(f"absolute path detected in {context}")
        if v.startswith("/") or v.startswith("\\"):
            raise AgentArtifactStagingError(f"absolute path detected in {context}")
    elif isinstance(value, dict):
        for key, item in value.items():
            _assert_no_abs_paths(key, context)
            _assert_no_abs_paths(item, context)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_abs_paths(item, context)


# ---------------------------------------------------------------------------
# Redacted result / evidence dict
# ---------------------------------------------------------------------------

def artifact_to_stage_result(
    artifact: SelenaArtifact,
    before: WorkspaceFingerprint,
    after: WorkspaceFingerprint,
    evidence: ArtifactEvidence,
) -> dict[str, Any]:
    """Return a redacted dict suitable for a Stage result.

    Contains only:
    * logical relative path, checksum, size
    * before/after snapshot public dicts
    * source_changed flag
    * artifact.to_dict()

    Asserts that the resulting serialization contains no temporary absolute paths.
    """
    result = {
        "logical_path": evidence.logical_path,
        "checksum": evidence.checksum,
        "size": evidence.size,
        "before": before.to_dict(),
        "after": after.to_dict(),
        "source_changed_during_build": artifact.source_changed_during_build,
        "artifact": artifact.to_dict(),
    }
    _assert_no_abs_paths(result, "stage_result")
    return result


def _assert_no_abs_paths_in_json(raw: str) -> None:
    """Heuristic: scan JSON string for absolute path patterns."""
    # Simple token scan for C:\ or /tmp/ style absolute paths.
    # This is intentionally conservative.
    import re
    if re.search(r'"[A-Za-z]:\\', raw):
        raise AgentArtifactStagingError("stage result JSON contains absolute Windows path")
    if re.search(r'"/tmp/', raw):
        raise AgentArtifactStagingError("stage result JSON contains absolute temp path")
    if re.search(r'"/var/folders/', raw):
        raise AgentArtifactStagingError("stage result JSON contains absolute temp path")
    if re.search(r'"/[Uu]sers/[^"]+/[Ll]ibrary/', raw):
        raise AgentArtifactStagingError("stage result JSON contains absolute temp path")


__all__ = [
    "AgentArtifactStagingError",
    "AuthorizedRoots",
    "ArtifactEvidence",
    "capture_source_snapshot",
    "validate_and_hash_artifact",
    "stage_selena_artifact",
    "artifact_to_stage_result",
]
