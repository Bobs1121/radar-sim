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
from core.repo import (
    RepoSourceError,
    WorkspaceFingerprint,
    inspect_workspace,
    inspect_workspace_identity,
)
from core.agent_asset_bindings import AgentAssetBindingStore, AgentAssetBindingError
from core.build_script_policy import (
    BuildScriptPolicyError,
    adapt_build_script_for_incremental,
    has_existing_build_artifact,
)
from core.build_runner import _build_selena_command
from core.config import load_config, resolve_selena_executable
from core.spec.legacy_adapter import LegacyConfigAdapterError, adapt_legacy_config


class AgentBuildStageError(ValueError):
    """Stable build-stage error with path-free public messages."""


@dataclass(frozen=True)
class _V2BuildBindings:
    """Agent-local paths derived only from the authorized v2 task.

    This deliberately does not reuse ``legacy_adapter.UserBindings``.  The
    public v2 pipeline must not pass through project/profile migration code.
    """

    project: str
    workspace_path: str
    selena_build_script: str
    environment_build_script: str = ""


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
    branch_repo_path: Path | None = None
    branch_before: WorkspaceFingerprint | None = None
    workspace_identity_mode: str = "full"
    full_rebuild_required: bool = False
    full_rebuild_reason: str = ""
    requested_build_branch: str = ""
    previous_build_branch: str = ""

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
        if not isinstance(self.full_rebuild_required, bool):
            raise AgentBuildStageError("full_rebuild_required must be true or false")


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


def _hash_regular_file(path: Path) -> str:
    """Hash one existing regular artifact for provenance comparison."""

    if not _is_regular_non_symlink(path):
        return ""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return "sha256:" + digest.hexdigest()


def _has_existing_build_state(
    artifact_path: Path,
    output_roots: tuple[Path, ...] | list[Path] | tuple[str, ...] | list[str],
) -> bool:
    """Detect any existing state below an authorized build output root.

    Looking only for ``selena.exe`` is insufficient: a previous build can
    have left CMake/MSBuild object files behind while the expected executable
    is in a configuration-specific subdirectory or was removed by an aborted
    wrapper.  The old bounded recursive scan could also miss a valid nested
    executable after an arbitrary number of unrelated files.  Output roots
    are already explicitly authorized, so an immediate non-empty check is a
    deterministic and cheap conservative signal; provenance then decides
    whether that state is safe to reuse incrementally.
    """

    if has_existing_build_artifact(artifact_path, output_roots, max_candidates=512):
        return True
    for raw_root in output_roots or ():
        root = Path(raw_root)
        try:
            if not root.is_dir() or root.is_symlink():
                continue
            next(root.iterdir())
            return True
        except (OSError, StopIteration):
            continue
    return False


def _requested_build_identity(payload: Mapping[str, Any], source_lease: Any) -> tuple[str, str]:
    """Return the branch/commit selected for this build without reading paths."""

    if source_lease is not None:
        return (
            str(getattr(source_lease, "requested_ref", "") or "").strip(),
            str(getattr(source_lease, "commit", "") or "").strip().lower(),
        )
    return (
        str(
            payload.get("actual_branch")
            or payload.get("branch")
            or payload.get("expected_branch")
            or ""
        ).strip(),
        str(payload.get("actual_commit") or payload.get("commit") or "").strip().lower(),
    )


def _branch_rebuild_policy(
    payload: Mapping[str, Any],
    *,
    contract: str,
    source_lease: Any,
    project: str,
    binding_id: str,
    build_mode: str,
    artifact_path: Path,
    authorized: AuthorizedRoots,
) -> dict[str, Any]:
    """Decide whether an existing Selena output may be reused incrementally.

    A build tree is reusable only when its persisted Runtime Bundle provenance
    proves the same Selena branch and build mode.  Missing provenance is not
    treated as "probably the same"; it forces a full build so an old branch's
    object files cannot contaminate a new branch.
    """

    requested_branch, requested_commit = _requested_build_identity(payload, source_lease)
    existing = _has_existing_build_state(artifact_path, authorized.output_roots)
    result: dict[str, Any] = {
        "existing_build_detected": bool(existing),
        "full_rebuild_required": False,
        "full_rebuild_reason": "",
        "requested_build_branch": requested_branch,
        "requested_build_commit": requested_commit,
        "previous_build_branch": "",
        "previous_build_mode": "",
        "previous_entrypoint_checksum": "",
    }
    if contract != "user-run-config/2.0" or not existing:
        return result

    try:
        from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseStore

        previous = AgentRuntimeBundleLeaseStore().latest_build_provenance(
            project=project,
            workspace_binding_id=binding_id,
        )
    except Exception:
        previous = None
    if previous:
        result["previous_build_branch"] = str(previous.get("branch") or "").strip()
        result["previous_build_mode"] = str(previous.get("build_mode") or "").strip()
        result["previous_entrypoint_checksum"] = str(
            previous.get("entrypoint_checksum") or ""
        ).strip().lower()

    reason = ""
    if not previous:
        reason = "existing_artifact_provenance_unavailable"
    elif not requested_branch or not result["previous_build_branch"]:
        reason = "selena_branch_identity_unavailable"
    elif requested_branch.casefold() != result["previous_build_branch"].casefold():
        reason = "selena_branch_changed"
    elif result["previous_build_mode"] and result["previous_build_mode"].casefold() != str(build_mode).casefold():
        reason = "selena_build_mode_changed"
    elif not result["previous_entrypoint_checksum"]:
        reason = "existing_artifact_provenance_incomplete"
    else:
        current_checksum = _hash_regular_file(artifact_path)
        if not current_checksum:
            reason = "existing_artifact_location_unverified"
        elif current_checksum != result["previous_entrypoint_checksum"]:
            reason = "existing_artifact_content_changed"
    if reason:
        result["full_rebuild_required"] = True
        result["full_rebuild_reason"] = reason
    return result


def _uninspected_workspace_evidence() -> WorkspaceFingerprint:
    """Return path-free outer-workspace evidence without invoking Git.

    A selected nested Selena repository supplies the real branch and commit.
    The surrounding product root is intentionally not queried in that case:
    it may contain unrelated submodules, generated files, and local work.
    """
    digest = hashlib.sha256(b"radar-sim.outer-workspace-not-inspected.v1").hexdigest()
    return WorkspaceFingerprint(
        branch="",
        commit="0" * 40,
        dirty=False,
        sha256=digest,
        staged_diff_sha256=hashlib.sha256(b"").hexdigest(),
        staged_diff_bytes=0,
        unstaged_diff_sha256=hashlib.sha256(b"").hexdigest(),
        unstaged_diff_bytes=0,
        untracked=(),
    )


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


def _locate_built_artifact(prepared: "PreparedSelenaBuild") -> Path:
    """Locate the actual post-build Selena.exe inside authorized output roots.

    Static script inference remains the first choice. Wrapper scripts that do
    not expose their output directory are supported by a bounded post-build
    search. The selected build mode wins, then the newest executable (the one
    the just-finished build normally touched), with a stable path tie-breaker.
    """
    expected = prepared.artifact_path
    if _is_regular_non_symlink(expected) and prepared.authorized.contains_output(expected):
        return expected

    candidates: list[tuple[int, int, int, str, Path]] = []
    for output_root in prepared.authorized.output_roots:
        try:
            iterator = output_root.rglob("*")
            for item in iterator:
                if item.name.casefold() != "selena.exe" or not _is_regular_non_symlink(item):
                    continue
                try:
                    resolved = item.resolve(strict=True)
                    relative = resolved.relative_to(output_root).parts
                    metadata = resolved.stat()
                except (OSError, ValueError):
                    continue
                if len(relative) > 10 or not prepared.authorized.contains_output(resolved):
                    continue
                candidates.append(
                    (
                        1 if resolved.parent.name.casefold() == prepared.build_mode.casefold() else 0,
                        int(metadata.st_mtime_ns),
                        int(metadata.st_size),
                        resolved.as_posix().casefold(),
                        resolved,
                    )
                )
                if len(candidates) > 512:
                    raise AgentBuildStageError("too many Selena artifacts under authorized build roots")
        except OSError:
            continue
    if not candidates:
        raise AgentBuildStageError("Selena.exe was not produced under the authorized build roots")
    candidates.sort(key=lambda item: item[:4], reverse=True)
    return candidates[0][4]


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


def _resolve_branch_repo_snapshot(
    branch_repo_ref: Any,
    authorized: AuthorizedRoots,
    *,
    identity_only: bool = False,
) -> tuple[Path | None, WorkspaceFingerprint | None]:
    """Resolve a workspace-relative Selena branch repository without escape.

    The reference is created only by the local Agent from the selected build
    script.  It is never an absolute task payload.  Older jobs omit it and
    retain the previous workspace-as-branch behavior.
    """
    text = str(branch_repo_ref or "").strip().replace("\\", "/")
    if not text:
        return None, None
    relative = Path(text)
    if relative.is_absolute() or text.startswith("/") or ".." in relative.parts:
        raise AgentBuildStageError("branch repository reference is invalid")
    try:
        candidate = (authorized.workspace_root / relative).resolve(strict=True)
    except OSError as exc:
        raise AgentBuildStageError("branch repository is unavailable") from exc
    if not candidate.is_dir() or candidate.is_symlink() or not authorized.contains_workspace(candidate):
        raise AgentBuildStageError("branch repository is outside authorized workspace")
    try:
        inspector = inspect_workspace_identity if identity_only else inspect_workspace
        return candidate, inspector(candidate)
    except (RepoSourceError, OSError) as exc:
        raise AgentBuildStageError("branch repository is unavailable") from exc


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
    machine = rebased.get("machine")
    if isinstance(machine, dict) and "project_root" in machine:
        machine["project_root"] = mapped(machine["project_root"], "machine.project_root")
    build = rebased.setdefault("build", {})
    for key in ("selena_build_script", "env_build_script", "build_output"):
        if key in build:
            build[key] = mapped(build[key], key)
    paths = rebased.get("paths")
    if isinstance(paths, dict):
        for key in ("project_root", "source_root", "build_output"):
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


def _generic_build_config(project: str, binding: Any) -> dict[str, Any]:
    """Build the minimal local config for a script-recognized workspace.

    Absolute paths remain Agent-local.  The caller replaces the script fields
    from authorized relative references before any command is constructed.
    No product catalog or ``config/projects`` entry is consulted.
    """
    workspace = str(binding.workspace_root)
    output = str(binding.output_roots[0])
    return {
        "_meta": {"project": project},
        "project": {"name": project, "platform": "gen5_selena"},
        "machine": {"platform": "gen5_selena", "project_root": workspace},
        "project_root": workspace,
        "repos": {
            "inner_repo_root": workspace,
            "outer_repo_root": workspace,
        },
        "build": {
            "build_output": output,
            # The script is user-selected and owns its arguments/config.  Do
            # not guess a product-specific positional CLI contract.
            "script_args_template": [],
        },
        "paths": {
            "project_root": workspace,
            "source_root": workspace,
            "build_output": output,
        },
        "selena": {
            "executable_name": "selena.exe",
            "exe_pattern": "dc_tools/selena/core/{build_mode}",
        },
        "simulation": {},
        "assets": {},
        "cluster": {},
    }


def _v2_build_bindings(config: Mapping[str, Any], project: str) -> _V2BuildBindings:
    """Read the four local build paths from the v2-generated config only."""
    build = dict(config.get("build") or {})
    machine = dict(config.get("machine") or {})
    paths = dict(config.get("paths") or {})
    workspace = str(
        machine.get("project_root")
        or config.get("project_root")
        or paths.get("project_root")
        or ""
    ).strip()
    return _V2BuildBindings(
        project=project,
        workspace_path=workspace,
        selena_build_script=str(build.get("selena_build_script") or "").strip(),
        environment_build_script=str(build.get("env_build_script") or "").strip(),
    )

def prepare_selena_build(
    payload: Mapping[str, Any],
    binding_store: AgentBindingStore,
    *,
    config_loader: Callable[[str], dict[str, Any]] = load_config,
    command_builder: Callable[[dict[str, Any], str, bool], tuple[list[str], str | None]] = _build_selena_command,
    artifact_resolver: Callable[[dict[str, Any], str | None], str] = resolve_selena_executable,
    asset_binding_store: AgentAssetBindingStore | None = None,
    source_lease: Any = None,
    enforce_incremental_policy: bool = True,
) -> PreparedSelenaBuild:
    """Prepare an authorized, immutable Selena build stage.

    Steps:
    1. Validate payload (project, workspace_binding_id, build_mode; optional clean/profile).
    2. Resolve binding by id+project and load local config.
    3. For v2, derive local bindings directly from the authorized workspace
       and selected scripts. Legacy adaptation is never entered by v2.
    4. Compute binding id from configured workspace and require exact match.
    5. Validate configured workspace resolves equal to binding workspace.
    6. If a configured Selena build script is used, ensure it exists as a
       regular non-symlink inside the authorized workspace.
    7. Require the configured build-script path; v5 Agent Stage rejects the
       legacy R2D2 fallback until it receives its own authorization adapter.
    8. Obtain actual command/cwd from injected *command_builder*.
    9. Resolve artifact path (may not exist yet) and validate it is under an
       authorized output root and named ``selena.exe``.
    10. Capture *before* source evidence only after all authorization checks
       pass.  User-run configs use bounded branch/HEAD evidence: they never
       scan the product root for diffs or untracked files.
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
    # The public run-config contract intentionally compiles the user's current
    # workspace.  Product roots can be very large and contain generated files
    # from unrelated components, so a full Git fingerprint here made an
    # environment check take minutes before a compiler was even started.
    # Keep legacy callers unchanged; v1 user-run builds use only bounded
    # branch+HEAD evidence and record that local changes were not inspected.
    identity_only = contract == "user-run-config/2.0"
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

    # Resolve binding.
    try:
        binding = binding_store.get(binding_id, project=project)
    except AgentBindingError as exc:
        raise AgentBuildStageError(str(exc)) from exc

    # The v2 build contract is the authorized workspace plus the exact scripts
    # selected by the user.  Registered project adapters are legacy-only and
    # must not inject arguments, output paths or environment defaults.
    if contract == "user-run-config/2.0":
        config = _generic_build_config(project, binding)
    else:
        try:
            config = config_loader(project)
        except (FileNotFoundError, ValueError) as exc:
            raise AgentBuildStageError("config loading failed") from exc
        except Exception as exc:
            raise AgentBuildStageError("config loading failed") from exc
    config = copy.deepcopy(config)
    if contract == "user-run-config/2.0":
        workspace_root = str(binding.workspace_root)
        output_root = str(binding.output_roots[0])
        repos = config.setdefault("repos", {})
        repos["inner_repo_root"] = workspace_root
        repos["outer_repo_root"] = workspace_root
        # Registered adapters may have been authored against another checkout
        # or drive.  The user-selected workspace and the locally inferred
        # output binding are authoritative for this run.
        config["project_root"] = workspace_root
        paths = config.setdefault("paths", {})
        paths["project_root"] = workspace_root
        paths["source_root"] = workspace_root
        paths["build_output"] = output_root
        machine = config.setdefault("machine", {})
        machine["project_root"] = workspace_root
        build = config.setdefault("build", {})
        build["build_output"] = output_root
        for payload_key, config_key, label, required in (
            ("selena_build_script_ref", "selena_build_script", "Selena build script", True),
            ("package_build_script_ref", "env_build_script", "package build script", False),
        ):
            ref = str(payload.get(payload_key) or "").strip().replace("\\", "/")
            if not ref:
                if required:
                    raise AgentBuildStageError(f"{label} reference is invalid")
                build.pop(config_key, None)
                continue
            if Path(ref).is_absolute() or ".." in Path(ref).parts:
                raise AgentBuildStageError(f"{label} reference is invalid")
            try:
                target = (binding.workspace_root / Path(ref)).resolve(strict=True)
                target.relative_to(binding.workspace_root.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise AgentBuildStageError(f"{label} is outside the authorized workspace") from exc
            if not _is_regular_non_symlink(target):
                raise AgentBuildStageError(f"{label} is missing or not a regular file")
            build[config_key] = str(target)

    if contract == "user-run-config/2.0":
        user_bindings = _v2_build_bindings(config, project)
    else:
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
        if contract == "user-run-config/2.0":
            user_bindings = _v2_build_bindings(config, project)
        else:
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

    script_checksum = _hash_script(resolved_script)
    package_script_path: Path | None = None
    package_script = str(getattr(user_bindings, "environment_build_script", "") or "").strip()
    if contract == "user-run-config/2.0" and package_script:
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

    branch_policy = _branch_rebuild_policy(
        payload,
        contract=contract,
        source_lease=source_lease,
        project=project,
        binding_id=binding_id,
        build_mode=build_mode,
        artifact_path=artifact_path,
        authorized=authorized,
    )
    full_rebuild_required = bool(branch_policy.get("full_rebuild_required"))
    full_rebuild_reason = str(branch_policy.get("full_rebuild_reason") or "")
    requested_build_branch = str(branch_policy.get("requested_build_branch") or "")
    previous_build_branch = str(branch_policy.get("previous_build_branch") or "")
    if full_rebuild_required:
        clean = True

    # Obtain command and cwd only after branch provenance has decided whether
    # this is an incremental or full build.
    try:
        cmd_list, cwd_raw = command_builder(config, build_mode, clean)
    except Exception as exc:
        raise AgentBuildStageError("command builder failed") from exc

    if not cmd_list:
        raise AgentBuildStageError("command must not be empty")
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

    # This is the last local-only point before the immutable script checksum is
    # captured.  Enforce incremental mode here as well as in the environment
    # inspection path so a caller cannot bypass the safety policy by submitting
    # a build Stage directly or by using a legacy command builder.
    if enforce_incremental_policy:
        try:
            policy_result = adapt_build_script_for_incremental(
                resolved_script,
                existing_build=has_existing_build_artifact(
                    artifact_path,
                    getattr(authorized, "output_roots", ()) or (),
                ),
                allow_clean=clean,
            )
            if full_rebuild_required and not policy_result.clean_command_lines:
                raise AgentBuildStageError(
                    "full rebuild is required but no clean command is available"
                )
        except BuildScriptPolicyError as exc:
            raise AgentBuildStageError("incremental build policy failed") from exc

    # Capture before snapshot only after all authorization checks.  The v1
    # user-facing flow deliberately avoids ``git diff``/``git status`` on the
    # outer product root; selecting a Selena branch is enough for its initial
    # executable identity and does not disturb local modifications.
    has_selected_branch_repo = bool(str(payload.get("branch_repo_ref") or "").strip())
    try:
        before = (
            _uninspected_workspace_evidence()
            if identity_only and has_selected_branch_repo
            else inspect_workspace_identity(authorized.workspace_root)
            if identity_only
            else capture_source_snapshot(authorized.workspace_root, authorized)
        )
    except (AgentArtifactStagingError, RepoSourceError, OSError) as exc:
        raise AgentBuildStageError(
            "workspace identity check failed" if identity_only else str(exc)
        ) from exc
    branch_repo_path, branch_before = _resolve_branch_repo_snapshot(
        payload.get("branch_repo_ref"), authorized, identity_only=identity_only,
    )

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
        branch_repo_path=branch_repo_path,
        branch_before=branch_before,
        workspace_identity_mode="branch_head_only" if identity_only else "full",
        full_rebuild_required=full_rebuild_required,
        full_rebuild_reason=full_rebuild_reason,
        requested_build_branch=requested_build_branch,
        previous_build_branch=previous_build_branch,
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

    # Capture after snapshot.  For the user-run-config flow this remains the
    # same bounded branch/HEAD probe used before the compiler ran.  Do not
    # turn a successful build into another full product-root diff/status scan.
    try:
        after = (
            _uninspected_workspace_evidence()
            if prepared.workspace_identity_mode == "branch_head_only" and prepared.branch_before is not None
            else inspect_workspace_identity(prepared.authorized.workspace_root)
            if prepared.workspace_identity_mode == "branch_head_only"
            else capture_source_snapshot(prepared.authorized.workspace_root, prepared.authorized)
        )
    except (AgentArtifactStagingError, RepoSourceError, OSError) as exc:
        raise AgentBuildStageError(
            "workspace identity check failed after build"
            if prepared.workspace_identity_mode == "branch_head_only"
            else str(exc)
        ) from exc

    # Validate and hash the actual artifact.
    try:
        artifact_path = _locate_built_artifact(prepared)
        evidence = validate_and_hash_artifact(artifact_path, prepared.authorized)
    except AgentArtifactStagingError as exc:
        raise AgentBuildStageError(str(exc)) from exc

    # Source changed detection.
    workspace_changed = (
        prepared.before.sha256 != after.sha256
        or prepared.before.commit != after.commit
    )
    branch_before = prepared.branch_before
    branch_after = None
    branch_repository_changed = False
    if prepared.branch_repo_path is not None:
        try:
            inspector = (
                inspect_workspace_identity
                if prepared.workspace_identity_mode == "branch_head_only"
                else inspect_workspace
            )
            branch_after = inspector(prepared.branch_repo_path)
        except (RepoSourceError, OSError) as exc:
            raise AgentBuildStageError("branch repository is unavailable after build") from exc
        if branch_before is None:
            raise AgentBuildStageError("branch repository evidence is unavailable")
        branch_repository_changed = (
            branch_before.sha256 != branch_after.sha256
            or branch_before.commit != branch_after.commit
        )
    # When the selected script identified a nested Selena repository, that
    # repository is the artifact's branch identity.  The outer workspace can
    # contain unrelated product work and must not turn an otherwise stable
    # Selena Bundle into an ambiguous one.  We still persist its change bit as
    # diagnostic evidence without using it as the Bundle identity gate.
    source_changed = branch_repository_changed if branch_before is not None else workspace_changed

    before_public = (branch_before or prepared.before).to_dict()
    after_public = (branch_after or after).to_dict()
    if prepared.source_lease_ref:
        before_public["branch"] = prepared.source_branch
        before_public["commit"] = prepared.source_commit
        after_public["branch"] = prepared.source_branch
        after_public["commit"] = prepared.source_commit
    source_change_evidence: dict[str, Any] = {
        "workspace_changed": workspace_changed,
        "branch_repository_changed": branch_repository_changed,
        "identity_scope": (
            "selena_branch_repository" if branch_before is not None else "workspace"
        ),
    }
    if prepared.workspace_identity_mode == "branch_head_only":
        source_change_evidence["local_changes_checked"] = False
    result: dict[str, Any] = {
        "project": prepared.project,
        "workspace_binding_id": prepared.binding_id,
        "build_mode": prepared.build_mode,
        "build_policy": {
            "mode": "full" if prepared.full_rebuild_required else "incremental",
            "full_rebuild_required": prepared.full_rebuild_required,
            "reason": prepared.full_rebuild_reason,
            "requested_branch": prepared.requested_build_branch,
            "previous_branch": prepared.previous_build_branch,
        },
        "before": before_public,
        "after": after_public,
        "source_changed_during_build": source_changed,
        # ``before``/``after`` are always the Selena branch repository when
        # one was identified.  Keep the outer workspace integrity gate
        # explicit instead of making a true source_changed flag appear to
        # contradict those public branch snapshots.
        "source_change_evidence": source_change_evidence,
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
        artifact_path = _locate_built_artifact(prepared)
        bundle = discover_runtime_bundle(
            artifact_path,
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
