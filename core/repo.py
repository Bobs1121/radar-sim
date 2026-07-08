"""Repository context checks and branch preparation.

Extracted from cli/check.py::_check_repo_context and cli/build.py::
_prepare_repo_context so both the environment checker and the build command
share one implementation. Returns CheckItem lists (category="repo") for the
unified check pipeline, and prepare_repo_context returns an error string for
the build command's existing contract.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from core.cluster import CheckItem


def _git(repo: str, args: list[str], *, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def check_repo_context(config: dict[str, Any], *, allow_switch: bool = False) -> list[CheckItem]:
    """Check outer/inner repo existence, branch match, cleanliness, submodules.

    Returns CheckItem list with category="repo":
      - outer/inner repo exists (error)
      - inner repo is a git repo (error)
      - current branch == configured target branch (warning if mismatch)
      - working tree clean (warning if dirty; info if switch would be needed)
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
            status = _git(inner_repo, ["status", "--porcelain"])
            dirty = status.returncode == 0 and _has_tracked_changes(status.stdout)
            if dirty:
                items.append(CheckItem(
                    f"Inner repo branch (target {target_branch})",
                    False,
                    f"current is '{current_branch}', target '{target_branch}', working tree dirty — cannot auto-switch",
                    "warning", "repo",
                    repair_hint="Commit or stash uncommitted changes, then retry. The working tree must be clean before switching branches.",
                ))
            else:
                items.append(CheckItem(
                    f"Inner repo branch (target {target_branch})",
                    allow_switch,
                    f"current is '{current_branch}', target '{target_branch}', clean — {'will switch at build' if allow_switch else 'switch at build time'}",
                    "info" if allow_switch else "warning", "repo",
                    repair_hint="Switch the inner repo to the target Selena branch.",
                    auto_repairable=allow_switch,
                    repair_action="switch_branch" if allow_switch else "",
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
    """Ensure inner repo is on the target Selena branch before build.

    Returns "" on success or an error message string (preserves the original
    build-command contract). Moved verbatim from cli/build.py::
    _prepare_repo_context.

    Concurrency: holds a cross-process file lock on ``<inner_repo>/.git/.rsim_lock``
    so two builds on the same repo (same machine, shared inner_repo_root) serialize
    their checkouts instead of racing on HEAD. For true parallel builds on different
    branches, use ``prepare_repo_worktree`` instead.
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

    lock_path = repo_path / ".git" / ".rsim_lock"
    lock = _acquire_repo_lock(lock_path)
    try:
        return _prepare_repo_context_locked(inner_repo, target_branch)
    finally:
        _release_repo_lock(lock)


def _prepare_repo_context_locked(inner_repo: str, target_branch: str) -> str:
    """In-place checkout — caller holds the repo lock."""
    try:
        current_branch = _git(inner_repo, ["branch", "--show-current"])
        if current_branch.returncode != 0:
            return f"Inner repo is not a valid git repo: {inner_repo}"
        current_branch_name = current_branch.stdout.strip()

        if current_branch_name == target_branch:
            return ""

        status = _git(inner_repo, ["status", "--porcelain"])
        if status.returncode != 0:
            return f"Failed to inspect inner repo status: {inner_repo}"
        if _has_tracked_changes(status.stdout):
            return (
                f"Inner repo has uncommitted changes to tracked files and cannot switch branch automatically: "
                f"{inner_repo}. Commit or stash changes, then retry. (Untracked files do not block.)"
            )

        branch_exists = _git(inner_repo, ["rev-parse", "--verify", target_branch])
        if branch_exists.returncode != 0:
            return f"Configured Selena branch not found locally in inner repo: {target_branch}"

        # checkout can be slow on large repos / when migrating untracked dirs.
        checkout = subprocess.run(
            ["git", "-C", inner_repo, "checkout", target_branch],
            capture_output=True, text=True, timeout=60,
        )
        if checkout.returncode != 0:
            stderr = checkout.stderr.strip() or checkout.stdout.strip()
            if "untracked working tree files would be overwritten" in stderr:
                checkout = subprocess.run(
                    ["git", "-C", inner_repo, "checkout", "-f", target_branch],
                    capture_output=True, text=True, timeout=60,
                )
                if checkout.returncode != 0:
                    stderr = checkout.stderr.strip() or checkout.stdout.strip()
                    return f"Failed to switch inner repo to branch '{target_branch}': {stderr}"
            else:
                return f"Failed to switch inner repo to branch '{target_branch}': {stderr}"
        return ""
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"Failed to prepare inner repo context: {exc}"


def _acquire_repo_lock(lock_path: Path):
    """Cross-process file lock. Uses fcntl on POSIX, msvcrt on Windows."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    if os.name == "nt":
        import msvcrt
        # Lock 1 byte at offset 0; blocks until acquired.
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                break
            except OSError:
                import time as _t
                _t.sleep(0.1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release_repo_lock(fd) -> None:
    if fd is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def prepare_repo_worktree(config: dict[str, Any]) -> tuple[str, str]:
    """Create an isolated git worktree for the target Selena branch.

    Returns ``(error_msg, worktree_path)``. On success error_msg is "" and
    worktree_path is a temp dir holding the target branch checkout. The caller
    MUST call ``cleanup_repo_worktree(worktree_path)`` when done (e.g. in a
    finally block) so worktrees don't accumulate.

    Why: ``prepare_repo_context`` checks out the branch in-place on
    ``inner_repo_root``, which races when two builds target different branches
    on the same repo (same-user multi-build, or a shared CI box). A worktree
    gives each build its own working tree on the same shared object store.
    """
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
        # Verify it's a git repo.
        current = _git(inner_repo, ["branch", "--show-current"])
        if current.returncode != 0:
            return f"Inner repo is not a valid git repo: {inner_repo}", ""
        branch_exists = _git(inner_repo, ["rev-parse", "--verify", target_branch])
        if branch_exists.returncode != 0:
            return f"Configured Selena branch not found locally in inner repo: {target_branch}", ""

        # Create an isolated worktree on the target branch. The worktree shares
        # the main repo's .git objects/refs but has its own working tree + HEAD,
        # so concurrent builds on different branches don't collide.
        worktree_path = tempfile.mkdtemp(prefix="rsim_wt_")
        # Remove the empty dir — git worktree add wants to create it itself.
        os.rmdir(worktree_path)
        add = _git(inner_repo, ["worktree", "add", "--detach", worktree_path, target_branch], timeout=120)
        if add.returncode != 0:
            stderr = add.stderr.strip() or add.stdout.strip()
            # Clean up the temp path on failure.
            shutil.rmtree(worktree_path, ignore_errors=True)
            _git(inner_repo, ["worktree", "prune"])
            return f"Failed to create worktree for branch '{target_branch}': {stderr}", ""
        return "", worktree_path
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return f"Failed to prepare worktree: {exc}", ""


def cleanup_repo_worktree(worktree_path: str) -> None:
    """Remove a worktree created by prepare_repo_worktree and prune."""
    if not worktree_path:
        return
    try:
        # `git worktree remove` is cleaner than rmtree (updates admin files).
        wt_dir = Path(worktree_path)
        # The worktree's .git is a file pointing back to the main repo; find the
        # main repo via the worktree's .git file to prune correctly.
        git_file = wt_dir / ".git"
        main_repo = ""
        if git_file.is_file():
            line = git_file.read_text(encoding="utf-8", errors="replace").strip()
            if line.startswith("gitdir:"):
                # gitdir: /path/to/main/.git/worktrees/<name>
                main_git = Path(line.split(":", 1)[1].strip())
                # Resolve the main repo root (parents: <wt>/.git -> main/.git/worktrees/<name> -> main/.git -> main)
                main_repo = str(main_git.parents[1])
        shutil.rmtree(worktree_path, ignore_errors=True)
        if main_repo and Path(main_repo).exists():
            _git(main_repo, ["worktree", "prune"])
        elif wt_dir.exists():
            # Fallback: try rmtree on the worktree path directly.
            shutil.rmtree(worktree_path, ignore_errors=True)
    except Exception:
        # Best-effort cleanup — never let it mask the real result.
        pass


def _has_tracked_changes(porcelain_output: str) -> bool:
    """True if any tracked file is modified/staged/deleted.

    Untracked files (lines starting with '??') do NOT count — git checkout
    can switch branches with untracked files present, so they should not block
    automatic branch switching.
    """
    for line in (porcelain_output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("??"):
            continue
        return True
    return False
