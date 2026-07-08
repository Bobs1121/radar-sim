"""Repository context checks and branch preparation.

Extracted from cli/check.py::_check_repo_context and cli/build.py::
_prepare_repo_context so both the environment checker and the build command
share one implementation. Returns CheckItem lists (category="repo") for the
unified check pipeline, and prepare_repo_context returns an error string for
the build command's existing contract.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from core.cluster import CheckItem


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
            # git refuses checkout when untracked files would be overwritten.
            # This happens when switching between branches where the target
            # branch tracks files the current branch doesn't (e.g. residue
            # from a prior branch switch). Since these files belong to the
            # target branch, force-overwrite is safe — it replaces local
            # residue with the target branch's tracked version.
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


def _git(repo: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        timeout=10,
    )


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
