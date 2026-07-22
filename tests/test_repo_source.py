from __future__ import annotations

import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import core.repo as repo_module

from core.repo import (
    DetachedWorktreeHandle,
    ResolveGitRefError,
    WorktreeSafetyError,
    cleanup_detached_worktree,
    cleanup_repo_worktree,
    inspect_workspace,
    prepare_detached_worktree,
    resolve_git_ref,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
    )
    return result.stdout


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Repo Source Test")
    (repo / ".gitignore").write_text("ignored.txt\nignored.bin\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "branch", "-M", "main")
    return repo


def test_inspect_workspace_fingerprint_tracks_dirty_inputs_without_abs_paths(tmp_path):
    repo = _make_repo(tmp_path)

    clean = inspect_workspace(repo)
    assert clean.branch == "main"
    assert clean.commit == _git(repo, "rev-parse", "HEAD")
    assert clean.dirty is False
    assert clean.untracked == ()

    (repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    ignored = inspect_workspace(repo)
    assert ignored.sha256 == clean.sha256
    assert ignored.to_dict() == clean.to_dict()

    (repo / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    unstaged = inspect_workspace(repo)
    assert unstaged.dirty is True
    assert unstaged.unstaged_diff_bytes > 0
    assert unstaged.sha256 != clean.sha256

    (repo / "staged.bin").write_bytes(b"\x00\x01binary\xff")
    _git(repo, "add", "staged.bin")
    staged_binary = inspect_workspace(repo)
    assert staged_binary.staged_diff_bytes > 0
    assert staged_binary.staged_diff_sha256 != hashlib.sha256(b"").hexdigest()

    (repo / "new.txt").write_text("untracked\n", encoding="utf-8")
    untracked = inspect_workspace(repo)
    evidence = {item.path: item for item in untracked.untracked}
    assert "new.txt" in evidence
    assert "ignored.txt" not in evidence
    assert evidence["new.txt"].sha256 == hashlib.sha256((repo / "new.txt").read_bytes()).hexdigest()
    assert inspect_workspace(repo).sha256 == untracked.sha256
    assert str(repo) not in repr(untracked.to_dict())


def test_inspect_workspace_allows_cold_large_repository_scans(tmp_path, monkeypatch):
    text_calls = []
    bytes_calls = []
    monkeypatch.setattr(repo_module, "_repo_root", lambda _repo: tmp_path)

    def fake_text(repo, args, *, timeout=10):
        text_calls.append((tuple(args), timeout))
        return "main" if args[0] == "branch" else "a" * 40

    def fake_bytes(repo, args, *, timeout=10):
        bytes_calls.append((tuple(args), timeout))
        return b""

    monkeypatch.setattr(repo_module, "_git_text_checked", fake_text)
    monkeypatch.setattr(repo_module, "_git_bytes_checked", fake_bytes)

    snapshot = inspect_workspace(tmp_path)

    assert snapshot.branch == "main"
    assert all(timeout == 60 for _args, timeout in text_calls)
    assert [timeout for _args, timeout in bytes_calls] == [180, 180, 180]


def test_inspect_workspace_hashes_untracked_symlink_without_following_target(tmp_path):
    repo = _make_repo(tmp_path)
    outside = tmp_path / "outside-secret.bin"
    outside.write_bytes(b"outside-v1")
    link = repo / "external-link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not available for this Windows user")

    first = inspect_workspace(repo)
    evidence = {item.path: item for item in first.untracked}
    assert "external-link" in evidence

    outside.write_bytes(b"outside-v2")
    second = inspect_workspace(repo)
    assert second.sha256 == first.sha256


def test_resolve_git_ref_allows_exact_local_and_origin_refs(tmp_path):
    repo = _make_repo(tmp_path)
    commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "feature")
    _git(repo, "update-ref", "refs/remotes/origin/remote-feature", commit)

    assert resolve_git_ref(repo, commit) == commit
    assert resolve_git_ref(repo, "main") == commit
    assert resolve_git_ref(repo, "refs/heads/main") == commit
    assert resolve_git_ref(repo, "origin/remote-feature") == commit
    assert resolve_git_ref(repo, "refs/remotes/origin/remote-feature") == commit


def test_resolve_git_ref_rejects_malicious_or_ambiguous_refs(tmp_path):
    repo = _make_repo(tmp_path)
    short_sha = _git(repo, "rev-parse", "--short", "HEAD")
    bad_refs = [
        "",
        "-main",
        "HEAD",
        "main^{commit}",
        "origin/../main",
        "refs/tags/v1",
        short_sha,
        "main feature",
        "main\0feature",
    ]

    for ref in bad_refs:
        with pytest.raises(ResolveGitRefError):
            resolve_git_ref(repo, ref)


def test_prepare_detached_worktree_is_concurrent_unique_and_preserves_main_workspace(tmp_path):
    repo = _make_repo(tmp_path)
    root = tmp_path / "controlled-worktrees"
    before_branch = _git(repo, "branch", "--show-current")
    before_head = _git(repo, "rev-parse", "HEAD")
    before_status = _git_bytes(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    handles: list[DetachedWorktreeHandle] = []

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(prepare_detached_worktree, repo, "main", "job/one", "stage:build", root)
                for _ in range(2)
            ]
            first, second = [future.result() for future in futures]
            handles.extend([first, second])

        first_path = Path(first.path)
        second_path = Path(second.path)
        assert first_path != second_path
        assert first_path.exists()
        assert second_path.exists()
        first_path.relative_to(root)
        second_path.relative_to(root)
        assert _git(first_path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
        assert _git(second_path, "rev-parse", "HEAD") == before_head
    finally:
        for handle in handles:
            cleanup_detached_worktree(handle)

    assert not Path(first.path).exists()
    assert not Path(second.path).exists()
    worktrees = _git(repo, "worktree", "list", "--porcelain")
    assert first.path not in worktrees
    assert second.path not in worktrees
    assert _git(repo, "branch", "--show-current") == before_branch
    assert _git(repo, "rev-parse", "HEAD") == before_head
    assert _git_bytes(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all") == before_status


def test_prepare_detached_worktree_initializes_submodules(tmp_path, monkeypatch):
    child = tmp_path / "child"
    child.mkdir()
    _git(child, "init", "-b", "main")
    _git(child, "config", "user.email", "test@example.com")
    _git(child, "config", "user.name", "Test")
    (child / "required.txt").write_text("runtime dependency", encoding="utf-8")
    _git(child, "add", "required.txt")
    _git(child, "commit", "-m", "child")

    parent = tmp_path / "parent"
    parent.mkdir()
    repo = _make_repo(parent)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "dependency")
    _git(repo, "commit", "-am", "add submodule")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")

    handle = prepare_detached_worktree(
        repo,
        "main",
        "job",
        "source",
        tmp_path / "controlled-submodule-worktrees",
    )
    try:
        assert (Path(handle.path) / "dependency" / "required.txt").read_text(encoding="utf-8") == "runtime dependency"
    finally:
        cleanup_detached_worktree(handle)


def test_cleanup_detached_worktree_rejects_out_of_bounds_path(tmp_path):
    repo = _make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    handle = DetachedWorktreeHandle(
        repo=str(repo),
        path=str(outside),
        root=str(tmp_path / "controlled"),
        commit=_git(repo, "rev-parse", "HEAD"),
        ref="main",
        job_id="job",
        stage_id="stage",
    )

    with pytest.raises(WorktreeSafetyError):
        cleanup_detached_worktree(handle)
    assert outside.exists()


def test_default_worktree_cleanup_recovers_after_process_registry_loss(tmp_path):
    import core.repo as repo_module

    repo = _make_repo(tmp_path)
    handle = prepare_detached_worktree(repo, "main", "restart", "build")
    path = Path(handle.path)
    try:
        with repo_module._WORKTREE_LOCK:
            repo_module._WORKTREE_REGISTRY.clear()
            repo_module._CONTROLLED_WORKTREE_ROOTS.clear()
        cleanup_repo_worktree(str(path))
        assert not path.exists()
    finally:
        if path.exists():
            cleanup_detached_worktree(handle)
