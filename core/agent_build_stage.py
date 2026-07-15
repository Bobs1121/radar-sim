"""Local-only v5 Selena build stage adapter kernel.

No subprocess execution, no network, no catalog, no upload.  Reuses existing
authorization, snapshot, and command-builder helpers from the core platform.
"""

from __future__ import annotations

import hashlib
import copy
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from core.agent_artifact_staging import (
    AgentArtifactStagingError,
    AuthorizedRoots,
    capture_source_snapshot,
    validate_and_hash_artifact,
    _assert_no_abs_paths,
)
from core.agent_bindings import (
    AgentBindingError,
    AgentBindingStore,
    make_workspace_binding_id,
)
from core.repo import WorkspaceFingerprint
from core.agent_asset_bindings import AgentAssetBindingStore, AgentAssetBindingError
from core.build_runner import _build_selena_command
from core.config import load_config, resolve_selena_executable
from core.spec.legacy_adapter import LegacyConfigAdapterError, adapt_legacy_config


class AgentBuildStageError(ValueError):
    """Stable build-stage error with path-free public messages."""


# ---------------------------------------------------------------------------
# Immutable prepared build state
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PreparedSelenaBuild:
    """Immutable local process state for a single Selena build stage.

    Contains only logical identifiers and resolved, authorized paths.
    No ``to_dict`` — callers use :func:`finish_selena_build` to obtain a
    redacted result dict.
    """

    project: str
    binding_id: str
    build_mode: str
    clean: bool
    command: tuple[str, ...]
    cwd: Path
    authorized: AuthorizedRoots
    before: WorkspaceFingerprint
    build_script_path: Path
    build_script_checksum: str
    artifact_path: Path
    package_build_script_path: Path | None = None
    contract: str = ""
    runtime_xml_path: Path | None = None
    adapter_path: Path | None = None
    mat_filter_path: Path | None = None
    adapter_key: str = ""
    source_lease_ref: str = ""
    source_branch: str = ""
    source_commit: str = ""

    def __post_init__(self) -> None:
        _validate_project(self.project)
        _validate_binding_id(self.binding_id)
        _validate_build_mode(self.build_mode)
        if not isinstance(self.clean, bool):
            raise AgentBuildStageError("clean must be true or false")
        if not isinstance(self.command, tuple) or not self.command:
            raise AgentBuildStageError("command must not be empty")
        if not isinstance(self.authorized, AuthorizedRoots):
            raise AgentBuildStageError("authorized roots are required")
        if not isinstance(self.before, WorkspaceFingerprint):
            raise AgentBuildStageError("before snapshot is required")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.build_script_checksum):
            raise AgentBuildStageError("build script checksum is invalid")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BUILD_MODE_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _validate_project(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise AgentBuildStageError("project must not be empty")
    if text != text.strip() or text in {".", ".."}:
        raise AgentBuildStageError("project must be a logical token")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,127}", text):
        raise AgentBuildStageError("project must be a logical token")
    return text


def _validate_binding_id(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"^workspace:sha256:[0-9a-f]{24}$", text):
        raise AgentBuildStageError("binding_id is invalid")
    return text


def _validate_build_mode(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise AgentBuildStageError("build_mode must not be empty")
    if not _BUILD_MODE_RE.fullmatch(text):
        raise AgentBuildStageError("build_mode contains invalid characters")
    return text


def _reject_path_like_payload(payload: Mapping[str, Any]) -> None:
    """Reject path-bearing keys; central tasks carry logical references only."""
    path_keys = {
        "workspace_path",
        "workspace_root",
        "output_root",
        "output_roots",
        "build_output",
        "project_root",
        "selena_build_script",
        "r2d2_script",
        "build_config",
        "selena_exe",
        "exe_path",
        "cwd",
        "data_path",
    }
    for key in payload:
        if str(key).strip().lower() in path_keys:
            raise AgentBuildStageError("payload must not contain local path fields")


def _is_regular_non_symlink(path: Path) -> bool:
    """Return True if *path* exists, is a regular file, and is not a symlink."""
    try:
        st = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or int(getattr(st, "st_nlink", 1) or 1) != 1:
        return False
    return stat.S_ISREG(st.st_mode)


def _hash_script(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AgentBuildStageError("selena build script hashing failed") from exc
    return "sha256:" + digest.hexdigest()


def _resolve_artifact_path(exe_path: str, authorized: AuthorizedRoots) -> Path:
    """Resolve the artifact path and ensure it is authorized.

    The path may not exist yet (build hasn't run), but it must resolve
    lexically and via realpath under an authorized output root and end in
    ``selena.exe``.
    """
    if not exe_path:
        raise AgentBuildStageError("artifact path is empty")
    # Reject absolute paths that come from outside the authorized workspace.
    # The resolver is expected to return a normalized path under build_output.
    path = Path(exe_path)
    # Lexical resolve (no strict=True — file may not exist yet).
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise AgentBuildStageError("artifact path is not resolvable") from exc

    # Must end in selena.exe (case-insensitive).
    if resolved.name.lower() != "selena.exe":
        raise AgentBuildStageError("artifact filename must be selena.exe")

    # Must be under an authorized output root.
    if not authorized.contains_output(resolved):
        raise AgentBuildStageError("artifact path is outside authorized output roots")

    # Also verify realpath doesn't escape via symlinks (defense in depth).
    real = Path(os.path.realpath(str(resolved)))
    if not authorized.contains_output(real):
        raise AgentBuildStageError("artifact path resolves outside authorized output roots")

    return resolved


def _resolve_cwd(cwd: str | None, authorized: AuthorizedRoots) -> Path:
    """Resolve cwd from command builder; default to workspace_root if None."""
    if cwd is None or str(cwd).strip() == "":
        return authorized.workspace_root
    path = Path(str(cwd).strip())
    if not path.is_absolute():
        path = authorized.workspace_root / path
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise AgentBuildStageError("working directory is not resolvable") from exc
    if not authorized.contains_workspace(resolved):
        raise AgentBuildStageError("working directory is outside authorized workspace")
    if not resolved.is_dir() or resolved.is_symlink():
        raise AgentBuildStageError("working directory is unavailable")
    return resolved


def _rebase_branch_config(
    config: Mapping[str, Any],
    *,
    base_workspace: Path,
    worktree: Path,
    base_output_roots: tuple[Path, ...],
) -> tuple[dict[str, Any], AuthorizedRoots]:
    """Rebase only known executable path fields from one repo to its worktree."""
    rebased = copy.deepcopy(dict(config))
    base = base_workspace.resolve(strict=True)
    target = worktree.resolve(strict=True)

    def mapped(value: Any, label: str, *, required_inside: bool = True) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        path = Path(text)
        if not path.is_absolute():
            return str(target / path)
        try:
            relative = path.resolve(strict=False).relative_to(base)
        except ValueError as exc:
            if required_inside:
                raise AgentBuildStageError(f"{label} cannot be isolated inside the branch worktree") from exc
            return text
        return str(target / relative)

    repos = rebased.setdefault("repos", {})
    for key in ("inner_repo_root", "outer_repo_root"):
        if key in repos:
            repos[key] = mapped(repos[key], key)
    if "project_root" in rebased:
        rebased["project_root"] = mapped(rebased["project_root"], "project_root")
    build = rebased.setdefault("build", {})
    for key in ("selena_build_script", "env_build_script", "build_output"):
        if key in build:
            build[key] = mapped(build[key], key)
    paths = rebased.get("paths")
    if isinstance(paths, dict):
        for key in ("project_root", "build_output"):
            if key in paths:
                paths[key] = mapped(paths[key], key)

    output_roots = []
    for root in base_output_roots:
        try:
            relative = root.resolve(strict=False).relative_to(base)
        except ValueError as exc:
            raise AgentBuildStageError("configured build output cannot be isolated") from exc
        output = target / relative
        output.mkdir(parents=True, exist_ok=True)
        output_roots.append(output)
    return rebased, AuthorizedRoots(workspace_root=target, output_roots=tuple(output_roots))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def prepare_selena_build(
    payload: Mapping[str, Any],
    binding_store: AgentBindingStore,
    *,
    config_loader: Callable[[str], dict[str, Any]] = load_config,
    command_builder: Callable[[dict[str, Any], str, bool], tuple[list[str], str | None]] = _build_selena_command,
    artifact_resolver: Callable[[dict[str, Any], str | None], str] = resolve_selena_executable,
    asset_binding_store: AgentAssetBindingStore | None = None,
    source_lease: Any = None,
) -> PreparedSelenaBuild:
    """Prepare an authorized, immutable Selena build stage.

    Steps:
    1. Validate payload (project, workspace_binding_id, build_mode; optional clean/profile).
    2. Resolve binding by id+project and load local config.
    3. Adapt legacy config locally with a harmless logical *data_path* to obtain
       :class:`UserBindings`.
    4. Compute binding id from configured workspace and require exact match.
    5. Validate configured workspace resolves equal to binding workspace.
    6. If a configured Selena build script is used, ensure it exists as a
       regular non-symlink inside the authorized workspace.
    7. Require the configured build-script path; v5 Agent Stage rejects the
       legacy R2D2 fallback until it receives its own authorization adapter.
    8. Obtain actual command/cwd from injected *command_builder*.
    9. Resolve artifact path (may not exist yet) and validate it is under an
       authorized output root and named ``selena.exe``.
    10. Capture *before* snapshot only after all authorization checks pass.
    """
    if not isinstance(payload, Mapping):
        raise AgentBuildStageError("payload must be a mapping")
    if not isinstance(binding_store, AgentBindingStore):
        raise AgentBuildStageError("agent binding store is required")

    _reject_path_like_payload(payload)

    project = _validate_project(payload.get("project"))
    binding_id = _validate_binding_id(payload.get("workspace_binding_id"))
    build_mode = _validate_build_mode(payload.get("build_mode"))
    clean_value = payload.get("clean", False)
    if not isinstance(clean_value, bool):
        raise AgentBuildStageError("clean must be true or false")
    clean = clean_value
    profile = str(payload.get("profile") or "").strip()
    if profile and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", profile):
        raise AgentBuildStageError("profile must be a logical token")
    contract = str(payload.get("contract") or "").strip()
    runtime_xml_path = None
    adapter_path = None
    mat_filter_path = None
    adapter_key = str(payload.get("adapter_key") or "").strip()
    if contract == "user-run-config/2.0":
        bindings = dict(payload.get("asset_bindings") or {})
        if set(bindings) != {"runtime_xml"}:
            raise AgentBuildStageError("Runtime XML is not authorized")
        store = asset_binding_store or AgentAssetBindingStore()
        try:
            runtime_xml_path = store.authorize_path(
                binding_id=str(bindings["runtime_xml"]),
                asset_path=str(payload.get("runtime_xml") or ""),
                role="runtime_xml",
            )
        except (AgentAssetBindingError, OSError) as exc:
            raise AgentBuildStageError("Runtime XML authorization failed") from exc
        if not adapter_key:
            raise AgentBuildStageError("internal workspace adapter identity is required")

    # Resolve binding.
    try:
        binding = binding_store.get(binding_id, project=project)
    except AgentBindingError as exc:
        raise AgentBuildStageError(str(exc)) from exc

    # Load config.
    try:
        config = config_loader(project)
    except (FileNotFoundError, ValueError) as exc:
        raise AgentBuildStageError("config loading failed") from exc
    except Exception as exc:
        raise AgentBuildStageError("config loading failed") from exc
    config = copy.deepcopy(config)
    if contract == "user-run-config/2.0":
        repos = config.setdefault("repos", {})
        repos["inner_repo_root"] = str(binding.workspace_root)
        repos["outer_repo_root"] = str(binding.workspace_root)
        build = config.setdefault("build", {})
        build["build_output"] = str(binding.output_roots[0])
        for payload_key, config_key, label in (
            ("selena_build_script_ref", "selena_build_script", "Selena build script"),
            ("package_build_script_ref", "env_build_script", "package build script"),
        ):
            ref = str(payload.get(payload_key) or "").strip().replace("\\", "/")
            if not ref or Path(ref).is_absolute() or ".." in Path(ref).parts:
                raise AgentBuildStageError(f"{label} reference is invalid")
            try:
                target = (binding.workspace_root / Path(ref)).resolve(strict=True)
                target.relative_to(binding.workspace_root.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise AgentBuildStageError(f"{label} is outside the authorized workspace") from exc
            if not _is_regular_non_symlink(target):
                raise AgentBuildStageError(f"{label} is missing or not a regular file")
            build[config_key] = str(target)

    # Adapt legacy config with a harmless logical data_path to obtain UserBindings.
    try:
        bundle = adapt_legacy_config(
            config,
            project=project,
            profile=profile or None,
            data_path="binding://agent-build",
        )
    except (LegacyConfigAdapterError, ValueError, TypeError) as exc:
        raise AgentBuildStageError("legacy config adaptation failed") from exc

    user_bindings = bundle.user_bindings

    # Compute binding id from configured workspace and require exact match.
    computed_id = make_workspace_binding_id(project, user_bindings.workspace_path)
    if computed_id != binding_id:
        raise AgentBuildStageError("configured workspace does not match binding")

    # Build authorized roots from the permanent binding, or rebase known paths
    # into a trusted isolated Source Lease without changing the binding identity.
    source_lease_ref = ""
    source_branch = ""
    source_commit = ""
    if source_lease is None:
        try:
            authorized = binding_store.resolve_authorized_roots(binding_id, project=project)
        except AgentBindingError as exc:
            raise AgentBuildStageError(str(exc)) from exc
    else:
        source_lease_ref = str(getattr(source_lease, "lease_id", "") or "")
        source_branch = str(getattr(source_lease, "requested_ref", "") or "")
        source_commit = str(getattr(source_lease, "commit", "") or "")
        if (
            getattr(source_lease, "project", "") != project
            or getattr(source_lease, "workspace_binding_id", "") != binding_id
            or source_lease_ref != str(payload.get("source_lease_ref") or "")
            or source_branch != str(payload.get("branch") or "")
            or source_commit != str(payload.get("commit") or "")
        ):
            raise AgentBuildStageError("isolated source lease does not match the build task")
        config, authorized = _rebase_branch_config(
            config,
            base_workspace=binding.workspace_root,
            worktree=Path(source_lease.worktree_path),
            base_output_roots=binding.output_roots,
        )
        try:
            bundle = adapt_legacy_config(
                config, project=project, profile=profile or None, data_path="binding://agent-build"
            )
        except (LegacyConfigAdapterError, ValueError, TypeError) as exc:
            raise AgentBuildStageError("isolated config adaptation failed") from exc
        user_bindings = bundle.user_bindings

    # Validate configured workspace resolves equal to binding workspace.
    if str(getattr(user_bindings, "project", "") or "").strip() != project:
        raise AgentBuildStageError("configured project does not match binding")
    try:
        configured_ws = Path(user_bindings.workspace_path).resolve(strict=False)
        expected_ws = authorized.workspace_root if source_lease is not None else binding.workspace_root.resolve(strict=False)
        if configured_ws != expected_ws and os.path.realpath(str(configured_ws)) != os.path.realpath(str(expected_ws)):
            raise AgentBuildStageError("configured workspace does not match authorized workspace")
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise AgentBuildStageError("workspace resolution failed") from exc

    # P0 v5 Agent Stage only supports the configured build-script path. The
    # legacy CLI retains its R2D2 fallback until that path gets a separate
    # authorization adapter.
    script = user_bindings.selena_build_script
    if not script:
        raise AgentBuildStageError("v5 agent build requires a configured Selena build script")
    script_path = Path(script)
    if not script_path.is_absolute():
        script_path = authorized.workspace_root / script_path
    if not _is_regular_non_symlink(script_path):
        raise AgentBuildStageError("selena build script is missing or not a regular file")
    try:
        resolved_script = script_path.resolve(strict=True)
    except OSError as exc:
        raise AgentBuildStageError("selena build script is not accessible") from exc
    if not authorized.contains_workspace(resolved_script):
        raise AgentBuildStageError("selena build script is outside authorized workspace")

    # Obtain command and cwd from injected builder.
    try:
        cmd_list, cwd_raw = command_builder(config, build_mode, clean)
    except Exception as exc:
        raise AgentBuildStageError("command builder failed") from exc

    if not cmd_list:
        raise AgentBuildStageError("command must not be empty")
    # Reject empty strings and NUL in command.
    for item in cmd_list:
        if not isinstance(item, str) or "\x00" in item or item.strip() == "":
            raise AgentBuildStageError("command contains invalid entries")
    cwd = _resolve_cwd(cwd_raw, authorized)
    if len(cmd_list) < 3 or cmd_list[0].strip().lower() not in {"cmd", "cmd.exe"} or cmd_list[1].strip().lower() != "/c":
        raise AgentBuildStageError("command must execute the configured Selena build script")
    try:
        command_script_path = Path(cmd_list[2])
        if not command_script_path.is_absolute():
            command_script_path = cwd / command_script_path
        command_script = command_script_path.resolve(strict=True)
    except OSError as exc:
        raise AgentBuildStageError("command build script is unavailable") from exc
    if command_script != resolved_script:
        raise AgentBuildStageError("command must execute the configured Selena build script")
    script_checksum = _hash_script(resolved_script)
    package_script_path: Path | None = None
    package_script = str(getattr(user_bindings, "environment_build_script", "") or "").strip()
    if contract == "user-run-config/2.0":
        if not package_script:
            raise AgentBuildStageError("package build script is required")
        package_candidate = Path(package_script)
        if not package_candidate.is_absolute():
            package_candidate = authorized.workspace_root / package_candidate
        try:
            package_script_path = package_candidate.resolve(strict=True)
        except OSError as exc:
            raise AgentBuildStageError("package build script is unavailable") from exc
        if (
            not _is_regular_non_symlink(package_script_path)
            or not authorized.contains_workspace(package_script_path)
        ):
            raise AgentBuildStageError("package build script is outside authorized workspace")

    # Resolve artifact path (may not exist yet).
    try:
        exe_path = artifact_resolver(config, build_mode)
    except Exception as exc:
        raise AgentBuildStageError("artifact resolution failed") from exc

    artifact_path = _resolve_artifact_path(exe_path, authorized)

    # Capture before snapshot only after all authorization checks.
    try:
        before = capture_source_snapshot(authorized.workspace_root, authorized)
    except AgentArtifactStagingError as exc:
        raise AgentBuildStageError(str(exc)) from exc

    return PreparedSelenaBuild(
        project=project,
        binding_id=binding_id,
        build_mode=build_mode,
        clean=clean,
        command=tuple(cmd_list),
        cwd=cwd,
        authorized=authorized,
        before=before,
        build_script_path=resolved_script,
        build_script_checksum=script_checksum,
        package_build_script_path=package_script_path,
        artifact_path=artifact_path,
        contract=contract,
        runtime_xml_path=runtime_xml_path,
        adapter_path=adapter_path,
        mat_filter_path=mat_filter_path,
        adapter_key=adapter_key,
        source_lease_ref=source_lease_ref,
        source_branch=source_branch,
        source_commit=source_commit,
    )


def verify_prepared_build(prepared: PreparedSelenaBuild) -> None:
    """Re-check authorized script identity immediately before subprocess start."""
    if not isinstance(prepared, PreparedSelenaBuild):
        raise AgentBuildStageError("prepared build is required")
    if not _is_regular_non_symlink(prepared.build_script_path):
        raise AgentBuildStageError("selena build script changed after preparation")
    if not prepared.authorized.contains_workspace(prepared.build_script_path):
        raise AgentBuildStageError("selena build script changed after preparation")
    if _hash_script(prepared.build_script_path) != prepared.build_script_checksum:
        raise AgentBuildStageError("selena build script changed after preparation")


def finish_selena_build(prepared: PreparedSelenaBuild) -> dict[str, Any]:
    """Finish a prepared build stage and return a redacted result dict.

    Steps:
    1. Capture *after* snapshot.
    2. Validate and hash the actual artifact file.
    3. Detect source changes via before/after sha256 or commit comparison.
    4. Return an immutable/redacted result dict containing only logical/public
       data: project, workspace_binding_id, build_mode, before/after public snapshots,
       source_changed flag, and artifact logical_path/checksum/size.

    No :class:`core.artifacts.SelenaArtifact`, storage_ref, catalog, or network
    operations are performed here.
    """
    if not isinstance(prepared, PreparedSelenaBuild):
        raise AgentBuildStageError("prepared build is required")

    # Capture after snapshot.
    try:
        after = capture_source_snapshot(prepared.authorized.workspace_root, prepared.authorized)
    except AgentArtifactStagingError as exc:
        raise AgentBuildStageError(str(exc)) from exc

    # Validate and hash the actual artifact.
    try:
        evidence = validate_and_hash_artifact(prepared.artifact_path, prepared.authorized)
    except AgentArtifactStagingError as exc:
        raise AgentBuildStageError(str(exc)) from exc

    # Source changed detection.
    source_changed = (
        prepared.before.sha256 != after.sha256
        or prepared.before.commit != after.commit
    )

    before_public = prepared.before.to_dict()
    after_public = after.to_dict()
    if prepared.source_lease_ref:
        before_public["branch"] = prepared.source_branch
        before_public["commit"] = prepared.source_commit
        after_public["branch"] = prepared.source_branch
        after_public["commit"] = prepared.source_commit
    result: dict[str, Any] = {
        "project": prepared.project,
        "workspace_binding_id": prepared.binding_id,
        "build_mode": prepared.build_mode,
        "before": before_public,
        "after": after_public,
        "source_changed_during_build": source_changed,
        "artifact": {
            "logical_path": evidence.logical_path,
            "checksum": evidence.checksum,
            "size": evidence.size,
        },
    }

    # Ensure no absolute paths leaked into the result.
    try:
        _assert_no_abs_paths(result, "finish_result")
    except AgentArtifactStagingError as exc:
        raise AgentBuildStageError(str(exc)) from exc

    return result


def stage_runtime_bundle_from_build(
    prepared: PreparedSelenaBuild,
    build_result: Mapping[str, Any],
    *,
    created_at: float,
    staging_root: str | Path | None = None,
    lease_store: Any = None,
    build_stage_id: str = "",
    build_attempt: int = 0,
) -> dict[str, Any]:
    """Discover and persist the branch-bound v2 Runtime Bundle transport."""
    from core.runtime_bundle import (
        RuntimeSourceEvidence,
        discover_runtime_bundle,
    )
    from core.runtime_bundle_archive import stage_runtime_bundle_archive

    if prepared.contract != "user-run-config/2.0":
        raise AgentBuildStageError("runtime bundle staging requires user-run-config/2.0")
    if build_result.get("source_changed_during_build") is not False:
        raise AgentBuildStageError("source changed during build; Runtime Bundle identity is ambiguous")
    if prepared.runtime_xml_path is None:
        raise AgentBuildStageError("Runtime XML is unavailable")
    before = dict(build_result.get("before") or {})
    dirty = bool(before.get("dirty"))
    dirty_fingerprint = "sha256:" + str(before.get("sha256") or "") if dirty else ""
    toolchain = "sha256:" + hashlib.sha256(
        "\0".join((prepared.build_script_checksum, prepared.build_mode)).encode("utf-8")
    ).hexdigest()
    source = RuntimeSourceEvidence(
        branch=str(before.get("branch") or ""),
        commit=str(before.get("commit") or ""),
        dirty=dirty,
        dirty_fingerprint=dirty_fingerprint,
        build_mode=prepared.build_mode,
        toolchain_fingerprint=toolchain,
        adapter_key=prepared.adapter_key,
    )
    try:
        bundle = discover_runtime_bundle(
            prepared.artifact_path,
            prepared.runtime_xml_path,
            source=source,
            created_at=float(created_at),
        )
        archive = stage_runtime_bundle_archive(bundle, staging_root)
    except (ValueError, OSError) as exc:
        raise AgentBuildStageError("Runtime Bundle staging failed") from exc
    result = {
        "runtime_bundle": bundle.public_dict,
        "runtime_bundle_archive": archive.public_dict,
        "runtime_bundle_identity": {"adapter_key": prepared.adapter_key},
        "toolchain_fingerprint": toolchain,
    }
    if lease_store is not None:
        lease = lease_store.create(
            project=prepared.project,
            workspace_binding_id=prepared.binding_id,
            build_stage_id=build_stage_id,
            build_attempt=build_attempt,
            manifest=bundle.manifest,
            archive=archive,
        )
        result["runtime_bundle_lease_ref"] = lease.lease_id
    return result


__all__ = [
    "AgentBuildStageError",
    "PreparedSelenaBuild",
    "prepare_selena_build",
    "verify_prepared_build",
    "finish_selena_build",
    "stage_runtime_bundle_from_build",
]
