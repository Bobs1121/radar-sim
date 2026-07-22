"""Repository context checks and branch preparation.

Extracted from cli/check.py::_check_repo_context and cli/build.py::
_prepare_repo_context so both the environment checker and the build command
share one implementation. Returns CheckItem lists (category="repo") for the
unified check pipeline, and prepare_repo_context returns an error string for
the build command's existing contract.
"""

from __future__ import annotations

import os
import hashlib
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from core.cluster import CheckItem


class RepoSourceError(ValueError):
    """Base stable exception for safe source/worktree operations."""


class ResolveGitRefError(RepoSourceError):
    """Raised when a requested Git ref is invalid, unsafe, or unresolved."""


class WorktreeSafetyError(RepoSourceError):
    """Raised when a worktree path fails controlled-root validation."""


@dataclass(frozen=True)
class UntrackedFileEvidence:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class WorkspaceFingerprint:
    branch: str
    commit: str
    dirty: bool
    sha256: str
    staged_diff_sha256: str
    staged_diff_bytes: int
    unstaged_diff_sha256: str
    unstaged_diff_bytes: int
    untracked: tuple[UntrackedFileEvidence, ...]

    def to_dict(self) -> dict[str, Any]:
        """Public evidence dictionary; intentionally omits absolute repo paths."""
        return {
            "branch": self.branch,
            "commit": self.commit,
            "dirty": self.dirty,
            "sha256": self.sha256,
            "evidence": {
                "staged_diff_sha256": self.staged_diff_sha256,
                "staged_diff_bytes": self.staged_diff_bytes,
                "unstaged_diff_sha256": self.unstaged_diff_sha256,
                "unstaged_diff_bytes": self.unstaged_diff_bytes,
                "untracked": [
                    {"path": item.path, "sha256": item.sha256, "size": item.size}
                    for item in self.untracked
                ],
            },
        }


@dataclass(frozen=True)
class DetachedWorktreeHandle:
    repo: str
    path: str
    root: str
    commit: str
    ref: str
    job_id: str
    stage_id: str

    def cleanup(self) -> None:
        cleanup_detached_worktree(self)

    def __enter__(self) -> "DetachedWorktreeHandle":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.cleanup()


_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_WORKTREE_REGISTRY: dict[str, tuple[Path, Path]] = {}
_CONTROLLED_WORKTREE_ROOTS: set[Path] = set()
_WORKTREE_LOCK = threading.Lock()
_WORKSPACE_METADATA_TIMEOUT = 60
_WORKSPACE_DIFF_TIMEOUT = 180
_WORKSPACE_STATUS_TIMEOUT = 180


def _git(repo: str, args: list[str], *, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "core.longpaths=true", "-C", repo, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_bytes(repo: str, args: list[str], *, timeout: int = 10) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", "-c", "core.longpaths=true", "-C", repo, *args],
        capture_output=True,
        text=False,
        timeout=timeout,
        env=env,
    )


def _git_text_checked(repo: str, args: list[str], *, timeout: int = 10) -> str:
    result = _git(repo, args, timeout=timeout)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise RepoSourceError(detail)
    return result.stdout.strip()


def _git_bytes_checked(repo: str, args: list[str], *, timeout: int = 10) -> bytes:
    result = _git_bytes(repo, args, timeout=timeout)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        if not detail:
            detail = result.stdout.decode("utf-8", "replace").strip()
        raise RepoSourceError(detail or "git command failed")
    return result.stdout


def _repo_root(repo: str | Path) -> Path:
    root = _git_text_checked(
        str(repo), ["rev-parse", "--show-toplevel"], timeout=_WORKSPACE_METADATA_TIMEOUT,
    )
    return Path(root).resolve()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_untracked_path(path: Path) -> tuple[str, int]:
    """Hash one untracked entry without following links outside the repo."""
    digest = hashlib.sha256()
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path)
            payload = os.fsencode(target)
            digest.update(b"symlink\0")
            digest.update(payload)
            return digest.hexdigest(), len(payload)
        if not stat.S_ISREG(metadata.st_mode):
            raise RepoSourceError(f"Unsupported untracked entry type: {path.name}")
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest(), metadata.st_size
    except OSError as exc:
        raise RepoSourceError(f"Failed to fingerprint untracked entry '{path.name}': {exc}") from exc


def _digest_part(digest: "hashlib._Hash", label: str, data: bytes) -> None:
    digest.update(label.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(len(data)).encode("ascii"))
    digest.update(b"\0")
    digest.update(data)
    digest.update(b"\0")


def _parse_untracked_from_status_z(status: bytes) -> list[str]:
    entries = status.split(b"\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        code = entry[:2]
        if len(entry) > 3 and code == b"??":
            paths.append(entry[3:].decode("utf-8", "surrogateescape"))
            continue
        if code[:1] in {b"R", b"C"} or code[1:2] in {b"R", b"C"}:
            i += 1
    return paths


def inspect_workspace(repo: str | Path) -> WorkspaceFingerprint:
    """Read-only fingerprint of HEAD plus staged/unstaged/untracked content."""
    repo_root = _repo_root(repo)
    repo_s = str(repo_root)
    branch = _git_text_checked(
        repo_s, ["branch", "--show-current"], timeout=_WORKSPACE_METADATA_TIMEOUT,
    ) or "HEAD"
    commit = _git_text_checked(
        repo_s, ["rev-parse", "HEAD"], timeout=_WORKSPACE_METADATA_TIMEOUT,
    )
    staged_diff = _git_bytes_checked(
        repo_s, ["diff", "--binary", "--cached", "--no-ext-diff"],
        timeout=_WORKSPACE_DIFF_TIMEOUT,
    )
    unstaged_diff = _git_bytes_checked(
        repo_s, ["diff", "--binary", "--no-ext-diff"],
        timeout=_WORKSPACE_DIFF_TIMEOUT,
    )
    status = _git_bytes_checked(
        repo_s,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        timeout=_WORKSPACE_STATUS_TIMEOUT,
    )

    untracked: list[UntrackedFileEvidence] = []
    for rel in sorted(_parse_untracked_from_status_z(status)):
        path = repo_root / rel
        content_sha256, size = _hash_untracked_path(path)
        untracked.append(UntrackedFileEvidence(rel.replace("\\", "/"), content_sha256, size))

    digest = hashlib.sha256()
    _digest_part(digest, "format", b"radar-sim.workspace-fingerprint.v1")
    _digest_part(digest, "head", commit.encode("ascii"))
    _digest_part(digest, "staged-diff", staged_diff)
    _digest_part(digest, "unstaged-diff", unstaged_diff)
    for item in untracked:
        _digest_part(digest, "untracked-path", item.path.encode("utf-8", "surrogateescape"))
        _digest_part(digest, "untracked-sha256", item.sha256.encode("ascii"))

    dirty = bool(staged_diff or unstaged_diff or untracked)
    return WorkspaceFingerprint(
        branch=branch,
        commit=commit,
        dirty=dirty,
        sha256=digest.hexdigest(),
        staged_diff_sha256=_sha256_bytes(staged_diff),
        staged_diff_bytes=len(staged_diff),
        unstaged_diff_sha256=_sha256_bytes(unstaged_diff),
        unstaged_diff_bytes=len(unstaged_diff),
        untracked=tuple(untracked),
    )


def _reject_unsafe_ref_text(ref: str) -> None:
    if not ref:
        raise ResolveGitRefError("Git ref is required")
    if "\0" in ref:
        raise ResolveGitRefError("Git ref contains NUL")
    if ref.startswith("-"):
        raise ResolveGitRefError("Git ref must not start with '-'")
    if ref in {"HEAD", "@", "FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD"}:
        raise ResolveGitRefError(f"Ambiguous Git ref is not allowed: {ref}")
    if ref.strip() != ref or any(ch.isspace() for ch in ref):
        raise ResolveGitRefError("Git ref must not contain whitespace")
    if any(ch in ref for ch in "~^:?*[\\"):
        raise ResolveGitRefError(f"Dangerous Git ref syntax is not allowed: {ref}")
    if ".." in ref or "@{" in ref or "//" in ref:
        raise ResolveGitRefError(f"Dangerous Git ref syntax is not allowed: {ref}")


def _branch_name_from_ref(ref: str) -> tuple[str, str]:
    if ref.startswith("refs/heads/"):
        return "local", ref[len("refs/heads/"):]
    if ref.startswith("refs/remotes/origin/"):
        return "origin", ref[len("refs/remotes/origin/"):]
    if ref.startswith("origin/"):
        return "origin", ref[len("origin/"):]
    if ref.startswith("refs/"):
        raise ResolveGitRefError(f"Only refs/heads and refs/remotes/origin refs are allowed: {ref}")
    return "local", ref


def _validate_branch_segment(branch: str) -> None:
    if not branch or branch.startswith("/") or branch.endswith("/") or branch.endswith("."):
        raise ResolveGitRefError(f"Invalid branch ref: {branch}")
    if branch.endswith(".lock"):
        raise ResolveGitRefError(f"Invalid branch ref: {branch}")
    for part in branch.split("/"):
        if (
            not part
            or part == "."
            or part == ".."
            or part.startswith(".")
            or part.startswith("-")
            or part.endswith(".lock")
        ):
            raise ResolveGitRefError(f"Invalid branch ref: {branch}")


def _resolve_commit(repo: str, commitish: str) -> str:
    result = _git(repo, ["rev-parse", "--verify", f"{commitish}^{{commit}}"])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"Git ref not found: {commitish}"
        raise ResolveGitRefError(detail)
    commit = result.stdout.strip()
    if not _FULL_SHA_RE.fullmatch(commit):
        raise ResolveGitRefError(f"Resolved ref is not a full commit SHA: {commitish}")
    return commit


def resolve_git_ref(repo: str | Path, ref: str) -> str:
    """Resolve only exact commits, local branches, or origin tracking branches."""
    if ref is None:
        raise ResolveGitRefError("Git ref is required")
    ref_text = str(ref)
    _reject_unsafe_ref_text(ref_text)
    repo_root = _repo_root(repo)
    repo_s = str(repo_root)

    if _FULL_SHA_RE.fullmatch(ref_text):
        return _resolve_commit(repo_s, ref_text.lower())

    kind, branch = _branch_name_from_ref(ref_text)
    _validate_branch_segment(branch)
    if kind == "local":
        return _resolve_commit(repo_s, f"refs/heads/{branch}")
    return _resolve_commit(repo_s, f"refs/remotes/origin/{branch}")


def _safe_segment(value: Any, fallback: str) -> str:
    raw = str(value or "").strip()
    segment = _SEGMENT_RE.sub("-", raw).strip(" .-_")
    if not segment:
        segment = fallback
    return segment[:80]


def _default_worktree_root(repo_root: Path) -> Path:
    repo_key = hashlib.sha256(str(repo_root).lower().encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "radar-sim-worktrees" / repo_key


def _resolve_for_validation(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _assert_within_root(path: Path, root: Path) -> tuple[Path, Path]:
    resolved_root = _resolve_for_validation(root)
    resolved_path = _resolve_for_validation(path)
    if resolved_path == resolved_root:
        raise WorktreeSafetyError(f"Refusing to operate on controlled root itself: {resolved_path}")
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise WorktreeSafetyError(f"Worktree path is outside controlled root: {resolved_path}") from exc
    return resolved_path, resolved_root


def _register_worktree(path: Path, root: Path, repo: Path) -> None:
    resolved_path, resolved_root = _assert_within_root(path, root)
    with _WORKTREE_LOCK:
        _WORKTREE_REGISTRY[str(resolved_path)] = (resolved_root, repo.resolve())
        _CONTROLLED_WORKTREE_ROOTS.add(resolved_root)


def _unregister_worktree(path: Path) -> None:
    resolved_path = _resolve_for_validation(path)
    with _WORKTREE_LOCK:
        _WORKTREE_REGISTRY.pop(str(resolved_path), None)


def _known_root_for_path(path: Path) -> tuple[Path, Optional[Path]]:
    resolved_path = _resolve_for_validation(path)
    with _WORKTREE_LOCK:
        registered = _WORKTREE_REGISTRY.get(str(resolved_path))
        if registered:
            return registered
        roots = tuple(_CONTROLLED_WORKTREE_ROOTS)
    for root in roots:
        try:
            resolved_path.relative_to(root)
            return root, None
        except ValueError:
            continue

    # The default root is deterministic, so a restarted worker can recover a
    # worktree without trusting an arbitrary caller-provided path.  Require the
    # exact layout produced by prepare_detached_worktree before inferring the
    # repository from the worktree's .git file.
    default_base = _resolve_for_validation(Path(tempfile.gettempdir()) / "radar-sim-worktrees")
    try:
        relative = resolved_path.relative_to(default_base)
    except ValueError:
        relative = None
    if relative is not None and len(relative.parts) == 4:
        repo_key, job_dir, stage_dir, unique_dir = relative.parts
        if (
            re.fullmatch(r"[0-9a-f]{16}", repo_key)
            and job_dir.startswith("job-")
            and stage_dir.startswith("stage-")
            and re.fullmatch(r"[0-9a-f]{32}", unique_dir)
        ):
            return default_base / repo_key, None
    raise WorktreeSafetyError(f"Worktree path is not registered under a controlled root: {resolved_path}")


def _infer_repo_from_git_file(worktree_path: Path) -> Path:
    git_file = worktree_path / ".git"
    if not git_file.is_file():
        raise WorktreeSafetyError(f"Cannot infer main repo for worktree: {worktree_path}")
    line = git_file.read_text(encoding="utf-8", errors="replace").strip()
    if not line.startswith("gitdir:"):
        raise WorktreeSafetyError(f"Unexpected worktree .git file format: {worktree_path}")
    gitdir = Path(line.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = worktree_path / gitdir
    gitdir = gitdir.resolve(strict=False)
    if gitdir.parent.name != "worktrees" or gitdir.parent.parent.name != ".git":
        raise WorktreeSafetyError(f"Unexpected worktree gitdir layout: {gitdir}")
    return gitdir.parent.parent.parent


def prepare_detached_worktree(
    repo: str | Path,
    ref: str,
    job_id: str,
    stage_id: str,
    root: str | Path | None = None,
) -> DetachedWorktreeHandle:
    """Create a detached Git worktree for a safe resolved commit."""
    repo_root = _repo_root(repo)
    commit = resolve_git_ref(repo_root, ref)
    controlled_root = Path(root).resolve() if root is not None else _default_worktree_root(repo_root)
    controlled_root.mkdir(parents=True, exist_ok=True)
    with _WORKTREE_LOCK:
        _CONTROLLED_WORKTREE_ROOTS.add(_resolve_for_validation(controlled_root))

    job_segment = _safe_segment(job_id, "job")
    stage_segment = _safe_segment(stage_id, "stage")
    worktree_path = controlled_root / f"job-{job_segment}" / f"stage-{stage_segment}" / uuid.uuid4().hex
    resolved_path, resolved_root = _assert_within_root(worktree_path, controlled_root)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Large Selena repositories can contain tens of thousands of files.
        # A cold Windows checkout regularly exceeds two minutes even when Git
        # long-path support is enabled, so this must not use the small timeout
        # intended for read-only ref inspection.
        add = _git(
            str(repo_root),
            ["worktree", "add", "--detach", str(resolved_path), commit],
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        # git records the worktree before checkout completes. Remove both that
        # registration and any partial directory so a retry starts cleanly.
        _git(
            str(repo_root),
            ["worktree", "remove", "--force", str(resolved_path)],
            timeout=300,
        )
        _git(str(repo_root), ["worktree", "prune"], timeout=120)
        if resolved_path.exists():
            shutil.rmtree(resolved_path)
        raise RepoSourceError(f"Timed out creating detached worktree for '{ref}'") from exc
    if add.returncode != 0:
        if resolved_path.exists():
            shutil.rmtree(resolved_path)
        _git(str(repo_root), ["worktree", "prune"])
        detail = add.stderr.strip() or add.stdout.strip() or "git worktree add failed"
        raise RepoSourceError(f"Failed to create detached worktree for '{ref}': {detail}")

    # A Git worktree contains only the superproject checkout. Selena build
    # scripts expect ip_dc/ip_if/ip_rc and the other registered submodules to
    # contain their pinned files; leaving gitlink directories empty can make
    # legacy XCOPY commands wait for interactive F/D input forever.
    submodules = _git(
        str(resolved_path),
        ["submodule", "update", "--init", "--recursive", "--jobs", "4"],
        timeout=1200,
    )
    if submodules.returncode != 0:
        detail = submodules.stderr.strip() or submodules.stdout.strip() or "git submodule update failed"
        _git(
            str(repo_root),
            ["worktree", "remove", "--force", str(resolved_path)],
            timeout=300,
        )
        _git(str(repo_root), ["worktree", "prune"], timeout=120)
        if resolved_path.exists():
            shutil.rmtree(resolved_path)
        raise RepoSourceError(f"Failed to initialize submodules for '{ref}': {detail}")

    handle = DetachedWorktreeHandle(
        repo=str(repo_root),
        path=str(resolved_path),
        root=str(resolved_root),
        commit=commit,
        ref=ref,
        job_id=str(job_id),
        stage_id=str(stage_id),
    )
    _register_worktree(resolved_path, resolved_root, repo_root)
    return handle


def cleanup_detached_worktree(handle: DetachedWorktreeHandle) -> None:
    if not isinstance(handle, DetachedWorktreeHandle):
        raise WorktreeSafetyError("cleanup_detached_worktree requires a DetachedWorktreeHandle")
    _cleanup_detached_worktree_path(Path(handle.path), Path(handle.root), Path(handle.repo))


def _cleanup_detached_worktree_path(path: Path, root: Path, repo: Optional[Path]) -> None:
    resolved_path, resolved_root = _assert_within_root(path, root)
    repo_root = repo.resolve() if repo is not None else None
    if repo_root is None and resolved_path.exists():
        repo_root = _infer_repo_from_git_file(resolved_path)
    if repo_root is None:
        _unregister_worktree(resolved_path)
        return

    remove = _git(str(repo_root), ["worktree", "remove", "--force", str(resolved_path)], timeout=120)
    prune = _git(str(repo_root), ["worktree", "prune"], timeout=120)
    if remove.returncode != 0 and resolved_path.exists():
        detail = remove.stderr.strip() or remove.stdout.strip() or "git worktree remove failed"
        raise RepoSourceError(f"Failed to remove worktree: {detail}")
    if prune.returncode != 0:
        detail = prune.stderr.strip() or prune.stdout.strip() or "git worktree prune failed"
        raise RepoSourceError(f"Failed to prune worktrees: {detail}")
    if resolved_path.exists():
        _assert_within_root(resolved_path, resolved_root)
        shutil.rmtree(resolved_path)
    _unregister_worktree(resolved_path)


def check_repo_context(config: dict[str, Any], *, allow_switch: bool = False) -> list[CheckItem]:
    """Check outer/inner repo existence, branch match, cleanliness, submodules.

    ``allow_switch`` is kept for backward-compatible callers, but WP0 safety
    freeze ignores it: this check never enables automatic branch switching.

    Returns CheckItem list with category="repo":
      - outer/inner repo exists (error)
      - inner repo is a git repo (error)
      - current branch == configured target branch (warning if mismatch)
      - configured branch exists locally (warning if not)
      - submodules initialized (warning per uninitialized submodule)
    """
    items: list[CheckItem] = []
    repos = config.get("repos", {})
    outer_repo = repos.get("outer_repo_root") or config.get("project_root", "")
    inner_repo = repos.get("inner_repo_root", "")
    target_branch = (
        config.get("_profile_selena_branch")
        or config.get("build", {}).get("selena_branch", "")
        or repos.get("inner_repo_branch", "")
    )

    if outer_repo and not Path(outer_repo).exists():
        items.append(CheckItem("Outer repo", False, f"not found: {outer_repo}", "error", "repo"))
    elif outer_repo:
        items.append(CheckItem("Outer repo", True, str(outer_repo), "info", "repo"))

    if not inner_repo:
        return items
    if not Path(inner_repo).exists():
        items.append(CheckItem("Inner repo", False, f"inner repo not found: {inner_repo}", "error", "repo"))
        return items

    git_dir = Path(inner_repo) / ".git"
    if not git_dir.exists():
        items.append(CheckItem("Inner repo git", False, f"not a git repo: {inner_repo}", "error", "repo"))
        return items
    items.append(CheckItem("Inner repo git", True, str(inner_repo), "info", "repo"))

    if not target_branch:
        return items

    try:
        current = _git(inner_repo, ["branch", "--show-current"])
        if current.returncode != 0:
            items.append(CheckItem("Inner repo branch", False, "could not read current branch", "warning", "repo"))
            return items
        current_branch = current.stdout.strip()
        if current_branch != target_branch:
            items.append(CheckItem(
                f"Inner repo branch (target {target_branch})",
                False,
                f"current is '{current_branch}', target '{target_branch}'. "
                "Automatic branch switching is disabled to protect the main workspace.",
                "warning", "repo",
                repair_hint="Use the current workspace branch, or run branch builds through an isolated worktree path.",
            ))
        else:
            items.append(CheckItem("Inner repo branch", True, f"on '{target_branch}'", "info", "repo"))

        branch_exists = _git(inner_repo, ["rev-parse", "--verify", target_branch])
        if branch_exists.returncode != 0:
            items.append(CheckItem(
                f"Target branch exists ({target_branch})",
                False,
                f"branch not found locally: {target_branch}",
                "warning", "repo",
            ))
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        items.append(CheckItem("Inner repo branch", False, f"failed to inspect: {exc}", "warning", "repo"))

    # Submodule check (outer or inner repo)
    submodule_repo = outer_repo if outer_repo and Path(outer_repo).exists() else inner_repo
    if submodule_repo:
        try:
            result = _git(submodule_repo, ["submodule", "status"])
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if line.startswith("-"):
                        parts = line.split()
                        name = parts[1] if len(parts) > 1 else "?"
                        items.append(CheckItem(
                            f"Submodule '{name}'",
                            False,
                            "not initialized",
                            "warning", "repo",
                        ))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return items


def prepare_repo_context(config: dict[str, Any]) -> str:
    """Verify the current workspace branch before build without switching it.

    Returns "" on success or an error message string (preserves the original
    build-command contract).

    WP0 safety freeze: this function never mutates the user's main workspace.
    A dirty/staged/untracked current branch is allowed when it already matches
    the configured branch, so current dirty workspace builds can proceed. A
    different target branch is rejected with guidance to use an isolated
    worktree branch build path (wired in WP4).
    """
    repos = config.get("repos", {})
    inner_repo = repos.get("inner_repo_root", "")
    target_branch = (
        config.get("_profile_selena_branch")
        or config.get("build", {}).get("selena_branch", "")
        or repos.get("inner_repo_branch", "")
    )
    if not inner_repo or not target_branch:
        return ""

    repo_path = Path(inner_repo)
    if not repo_path.exists():
        return f"Configured inner repo not found: {inner_repo}"

    return _verify_repo_context(inner_repo, target_branch)


def _verify_repo_context(inner_repo: str, target_branch: str) -> str:
    """Verify branch only. Never checkout/stash/reset the user's workspace."""
    try:
        current_branch = _git(inner_repo, ["branch", "--show-current"])
        if current_branch.returncode != 0:
            return f"Inner repo is not a valid git repo: {inner_repo}"
        current_branch_name = current_branch.stdout.strip()

        if current_branch_name == target_branch:
            return ""

        branch_exists = _git(inner_repo, ["rev-parse", "--verify", target_branch])
        if branch_exists.returncode != 0:
            return f"Configured Selena branch not found locally in inner repo: {target_branch}"

        return (
            f"Inner repo is on '{current_branch_name}', but configured Selena branch is '{target_branch}'. "
            "Automatic branch switching is disabled to protect the user's main workspace. "
            "Build the current workspace branch, or use the isolated worktree branch build path when it is available."
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"Failed to prepare inner repo context: {exc}"

def prepare_repo_worktree(config: dict[str, Any]) -> tuple[str, str]:
    """Create an isolated detached worktree for the configured Selena ref."""
    repos = config.get("repos", {})
    inner_repo = repos.get("inner_repo_root", "")
    target_branch = (
        config.get("_profile_selena_branch")
        or config.get("build", {}).get("selena_branch", "")
        or repos.get("inner_repo_branch", "")
    )
    if not inner_repo or not target_branch:
        return "", ""
    repo_path = Path(inner_repo)
    if not repo_path.exists():
        return f"Configured inner repo not found: {inner_repo}", ""
    try:
        handle = prepare_detached_worktree(
            inner_repo,
            target_branch,
            config.get("_job_id") or config.get("job_id") or "legacy",
            config.get("_stage_id") or config.get("stage_id") or "build",
        )
        return "", handle.path
    except (RepoSourceError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return f"Failed to prepare worktree: {exc}", ""


def cleanup_repo_worktree(worktree_path: str) -> None:
    """Remove a registered/controlled worktree created by prepare_repo_worktree."""
    if not worktree_path:
        return
    wt_dir = Path(worktree_path)
    root, repo = _known_root_for_path(wt_dir)
    _cleanup_detached_worktree_path(wt_dir, root, repo)


def _has_tracked_changes(porcelain_output: str) -> bool:
    """True if any tracked file is modified/staged/deleted.

    Untracked files (lines starting with '??') do NOT count. This helper is
    retained for callers that only need tracked-change detection; WP0 branch
    verification no longer uses it to permit automatic switching.
    """
    for line in (porcelain_output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("??"):
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Selena runtime package harvesting (PRD §1.7.2)
# ---------------------------------------------------------------------------

# Suffixes we deep-harvest alongside the .exe so Cluster nodes never hit
# "missing Qt/Boost DLL" errors. Intermediate build artifacts are excluded.
HARVEST_SUFFIXES = (".exe", ".dll", ".cfg", ".xml")
HARVEST_EXCLUDE_SUFFIXES = (".obj", ".lib", ".pdb", ".ilk", ".exp", ".idb")
HARVEST_PACKAGE_NAME = "selena_runtime_package.zip"


def _resolve_build_output(config: dict[str, Any]) -> str:
    """Resolve the build_output dir from layered or legacy config shapes."""
    build = config.get("build", {}) or {}
    paths = config.get("paths", {}) or {}
    return str(
        build.get("build_output")
        or paths.get("build_output")
        or config.get("build_output", "")
        or ""
    ).strip()


def harvest_runtime_package(
    config: dict[str, Any],
    build_output_dir: Optional[str] = None,
    *,
    dest_dir: Optional[str] = None,
) -> str:
    """Deep-harvest the Selena runtime into ``selena_runtime_package.zip``.

    Scans ``build_output_dir`` (defaults to config's ``build_output``) for the
    executable and its companion DLLs / configs / XMLs (Qt, Boost, private
    algorithm modules, etc.), packs them into ``selena_runtime_package.zip``,
    and stages the zip to ``dest_dir`` (defaults to the cluster
    ``workspace_root`` when configured and reachable, else a ``packages/`` dir
    beside the build output).

    Returns the absolute path of the produced zip. Raises ``FileNotFoundError``
    if no harvestable artifacts are found (so callers fail loud rather than
    silently shipping an empty package to Cluster nodes).

    PRD §1.7.2: the staged zip is later unzipped into each job's run directory
    so ``selena.exe`` finds every dependency DLL in its own working dir.
    """
    import zipfile

    src_dir = Path(build_output_dir or _resolve_build_output(config))
    if not src_dir or not src_dir.exists():
        raise FileNotFoundError(f"build_output dir not found for harvest: {src_dir!r}")

    target = Path(dest_dir) if dest_dir else _default_harvest_dest(config, src_dir)
    target.mkdir(parents=True, exist_ok=True)
    zip_path = target / HARVEST_PACKAGE_NAME

    harvested: list[tuple[str, str]] = []  # (arcname, abs_path)
    seen: set[Path] = set()
    for root, _dirs, files in os.walk(src_dir):
        # Skip common intermediate/build cache dirs to keep the zip lean.
        base = Path(root).name.lower()
        if base in {"cmakefiles", ".git", "caches", "intermediate"}:
            continue
        for name in files:
            p = Path(root) / name
            if p in seen:
                continue
            suffix = p.suffix.lower()
            if suffix not in HARVEST_SUFFIXES:
                continue
            if suffix in HARVEST_EXCLUDE_SUFFIXES:
                continue
            seen.add(p)
            # Arcname relative to build_output so the layout is flat-ish but
            # subdir names that matter (e.g. plugins/) are preserved.
            arc = p.relative_to(src_dir).as_posix()
            harvested.append((arc, str(p)))

    if not harvested:
        raise FileNotFoundError(
            f"No harvestable Selena runtime artifacts (.exe/.dll/.cfg/.xml) under {src_dir}"
        )

    # Write atomically: build to a temp file then replace, so a concurrent
    # harvester (multi-user) never reads a half-written zip.
    tmp_zip = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc, abs_path in sorted(harvested):
            zf.write(abs_path, arcname=arc)
    os.replace(tmp_zip, zip_path)
    return str(zip_path)


def _default_harvest_dest(config: dict[str, Any], build_output: Path) -> Path:
    """Default staging dir: cluster workspace_root if configured & reachable,
    else a ``packages/`` dir beside the build output."""
    cluster = config.get("cluster", {}) or {}
    ws = str(cluster.get("workspace_root") or "").strip()
    if ws:
        # Only stage to workspace_root if it actually exists locally (the UNC
        # may not be mounted on this host). Otherwise fall back to local.
        ws_local = ws
        try:
            from core.cluster import _unc_to_local, get_cluster_config
            ws_local = _unc_to_local(get_cluster_config(config), ws)
        except Exception:
            pass
        if Path(ws_local).exists():
            project_key = str(
                config.get("_meta", {}).get("project")
                or config.get("project", {}).get("name")
                or "default"
            )
            project_folder = str(cluster.get("project_folder") or "radar-sim")
            return Path(ws_local) / project_folder / project_key / "packages"
    return build_output.parent / "packages"
