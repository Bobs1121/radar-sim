"""Tests for core.agent_artifact_staging.

Uses a real temporary git repo and files. Covers clean/dirty before,
source changed during build, authorization root/drive root rejection,
output outside workspace, traversal, symlink escape, directory/non-selena/empty/
symlink artifact, streaming checksum, forced private dirty/changed,
no absolute paths in JSON, immutable evidence.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from core.agent_artifact_staging import (
    AgentArtifactStagingError,
    AuthorizedRoots,
    ArtifactEvidence,
    artifact_to_stage_result,
    capture_source_snapshot,
    stage_selena_artifact,
    validate_and_hash_artifact,
)
from core.artifacts import ArtifactValidationError, SelenaArtifact
from core.repo import RepoSourceError, WorkspaceFingerprint


@pytest.fixture
def tmp_git_repo(tmp_path: Path):
    """Fast filesystem-only workspace used by non-Git staging tests."""
    repo = tmp_path / "workspace"
    repo.mkdir()
    yield repo


@pytest.fixture
def real_git_repo():
    """Yield a temporary directory initialized as a git repo."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        (repo / "README.md").write_text("# hello\n", encoding="utf-8")
        (repo / ".gitignore").write_text("out/\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        (repo / "out").mkdir()
        yield repo


@pytest.fixture
def authorized(tmp_git_repo: Path):
    """Build an AuthorizedRoots for the temp repo with one output dir."""
    out = tmp_git_repo / "out"
    out.mkdir(exist_ok=True)
    return AuthorizedRoots(workspace_root=tmp_git_repo, output_roots=(out,))


# ---------------------------------------------------------------------------
# AuthorizedRoots
# ---------------------------------------------------------------------------

def test_authorized_roots_resolve_and_contain(tmp_git_repo: Path, authorized: AuthorizedRoots):
    assert authorized.workspace_root == tmp_git_repo.resolve()
    assert authorized.contains_workspace(tmp_git_repo)
    assert authorized.contains_output(tmp_git_repo / "out")
    assert not authorized.contains_workspace(tmp_git_repo.parent)
    assert not authorized.contains_output(tmp_git_repo)


def test_authorized_roots_rejects_drive_root():
    with pytest.raises(AgentArtifactStagingError):
        # On Windows, C:\ is a drive root. On POSIX, / is the root.
        if sys.platform == "win32":
            AuthorizedRoots(workspace_root=Path("C:/"), output_roots=(Path("C:/out"),))
        else:
            AuthorizedRoots(workspace_root=Path("/"), output_roots=(Path("/out"),))


def test_authorized_roots_rejects_empty_output_roots(tmp_git_repo: Path):
    with pytest.raises(AgentArtifactStagingError):
        AuthorizedRoots(workspace_root=tmp_git_repo, output_roots=())


def test_authorized_roots_rejects_empty_paths_and_workspace_as_output(tmp_git_repo: Path):
    out = tmp_git_repo / "out"
    out.mkdir()
    with pytest.raises(AgentArtifactStagingError, match="workspace_root"):
        AuthorizedRoots(workspace_root="", output_roots=(out,))
    with pytest.raises(AgentArtifactStagingError, match="output_root"):
        AuthorizedRoots(workspace_root=tmp_git_repo, output_roots=("",))
    with pytest.raises(AgentArtifactStagingError, match="narrower"):
        AuthorizedRoots(workspace_root=tmp_git_repo, output_roots=(tmp_git_repo,))


def test_authorized_roots_rejects_output_outside_workspace(tmp_git_repo: Path):
    sibling = tmp_git_repo.parent / "other_out"
    sibling.mkdir(exist_ok=True)
    with pytest.raises(AgentArtifactStagingError):
        AuthorizedRoots(workspace_root=tmp_git_repo, output_roots=(sibling,))


def test_authorized_roots_rejects_nonexistent_workspace():
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "nowhere"
        with pytest.raises(AgentArtifactStagingError):
            AuthorizedRoots(workspace_root=fake, output_roots=(fake / "out",))


def test_authorized_roots_rejects_nonexistent_output(tmp_git_repo: Path):
    with pytest.raises(AgentArtifactStagingError):
        AuthorizedRoots(
            workspace_root=tmp_git_repo,
            output_roots=(tmp_git_repo / "nonexistent",),
        )


def test_authorized_roots_traversal_escape(tmp_git_repo: Path):
    out = tmp_git_repo / "out"
    out.mkdir(exist_ok=True)
    # Pass a path with traversal that still resolves under workspace.
    tricky = tmp_git_repo / "out" / ".." / "out"
    auth = AuthorizedRoots(workspace_root=tmp_git_repo, output_roots=(tricky,))
    # Should resolve to the real out dir.
    assert auth.contains_output(out)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reparse/symlink privilege test")
def test_authorized_roots_symlink_escape_windows(tmp_git_repo: Path):
    """Skip if Windows privilege unavailable."""
    out = tmp_git_repo / "out"
    out.mkdir(exist_ok=True)
    outside = tmp_git_repo.parent / "outside_out"
    outside.mkdir()
    link = tmp_git_repo / "link_out"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires elevated privilege on Windows")
    with pytest.raises(AgentArtifactStagingError):
        AuthorizedRoots(workspace_root=tmp_git_repo, output_roots=(link,))


def test_authorized_roots_symlink_escape_posix(tmp_git_repo: Path):
    """On POSIX symlinks are always creatable."""
    if sys.platform == "win32":
        pytest.skip("POSIX-only symlink escape test")
    out = tmp_git_repo / "out"
    out.mkdir(exist_ok=True)
    outside = tmp_git_repo.parent / "outside_out"
    outside.mkdir()
    link = tmp_git_repo / "link_out"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AgentArtifactStagingError):
        AuthorizedRoots(workspace_root=tmp_git_repo, output_roots=(link,))


# ---------------------------------------------------------------------------
# capture_source_snapshot
# ---------------------------------------------------------------------------

def test_capture_clean_workspace(real_git_repo: Path):
    authorized = AuthorizedRoots(real_git_repo, (real_git_repo / "out",))
    snap = capture_source_snapshot(real_git_repo, authorized)
    assert isinstance(snap, WorkspaceFingerprint)
    assert snap.branch == "main" or snap.branch == "master"
    assert snap.dirty is False
    assert snap.sha256
    assert snap.commit


def test_capture_dirty_workspace(real_git_repo: Path):
    authorized = AuthorizedRoots(real_git_repo, (real_git_repo / "out",))
    (real_git_repo / "dirty.txt").write_text("dirty", encoding="utf-8")
    snap = capture_source_snapshot(real_git_repo, authorized)
    assert snap.dirty is True


def test_capture_unauthorized_workspace(tmp_git_repo: Path):
    other = tmp_git_repo.parent / "other_repo"
    other.mkdir()
    (tmp_git_repo / "out").mkdir()
    with pytest.raises(AgentArtifactStagingError):
        capture_source_snapshot(other, AuthorizedRoots(workspace_root=tmp_git_repo, output_roots=(tmp_git_repo / "out",)))


def test_capture_masks_git_error_paths(monkeypatch, tmp_git_repo: Path, authorized: AuthorizedRoots):
    monkeypatch.setattr(
        "core.agent_artifact_staging.inspect_workspace",
        lambda _path: (_ for _ in ()).throw(RepoSourceError(r"C:\secret\repo failed")),
    )
    with pytest.raises(AgentArtifactStagingError) as excinfo:
        capture_source_snapshot(tmp_git_repo, authorized)
    assert "secret" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# validate_and_hash_artifact
# ---------------------------------------------------------------------------

def test_validate_and_hash_success(tmp_git_repo: Path, authorized: AuthorizedRoots):
    exe = tmp_git_repo / "out" / "selena.exe"
    exe.write_bytes(b"fake binary")
    ev = validate_and_hash_artifact(exe, authorized)
    assert isinstance(ev, ArtifactEvidence)
    assert ev.checksum.startswith("sha256:")
    assert len(ev.checksum) == 7 + 64
    assert ev.size == len(b"fake binary")
    assert ev.logical_path == "selena.exe"


def test_validate_and_hash_case_insensitive_filename(tmp_git_repo: Path, authorized: AuthorizedRoots):
    exe = tmp_git_repo / "out" / "SeLeNa.ExE"
    exe.write_bytes(b"x")
    ev = validate_and_hash_artifact(exe, authorized)
    assert ev.logical_path == "SeLeNa.ExE"


def test_validate_and_hash_rejects_wrong_name(tmp_git_repo: Path, authorized: AuthorizedRoots):
    bad = tmp_git_repo / "out" / "other.exe"
    bad.write_bytes(b"x")
    with pytest.raises(AgentArtifactStagingError):
        validate_and_hash_artifact(bad, authorized)


def test_validate_and_hash_rejects_directory(tmp_git_repo: Path, authorized: AuthorizedRoots):
    d = tmp_git_repo / "out" / "selena.exe"
    d.mkdir()
    with pytest.raises(AgentArtifactStagingError):
        validate_and_hash_artifact(d, authorized)


def test_validate_and_hash_rejects_empty_file(tmp_git_repo: Path, authorized: AuthorizedRoots):
    empty = tmp_git_repo / "out" / "selena.exe"
    empty.write_bytes(b"")
    with pytest.raises(AgentArtifactStagingError):
        validate_and_hash_artifact(empty, authorized)


def test_validate_and_hash_rejects_symlink(tmp_git_repo: Path, authorized: AuthorizedRoots):
    real = tmp_git_repo / "out" / "real.exe"
    real.write_bytes(b"x")
    link = tmp_git_repo / "out" / "selena.exe"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlink creation requires privilege on this platform")
    with pytest.raises(AgentArtifactStagingError):
        validate_and_hash_artifact(link, authorized)


def test_validate_and_hash_rejects_hardlink(tmp_git_repo: Path, authorized: AuthorizedRoots):
    real = tmp_git_repo / "out" / "real.exe"
    real.write_bytes(b"x")
    link = tmp_git_repo / "out" / "selena.exe"
    try:
        os.link(real, link)
    except OSError:
        pytest.skip("hardlink creation is unavailable on this filesystem")
    with pytest.raises(AgentArtifactStagingError, match="hard link"):
        validate_and_hash_artifact(link, authorized)


def test_validate_and_hash_rejects_outside_output(tmp_git_repo: Path, authorized: AuthorizedRoots):
    outside = tmp_git_repo / "selena.exe"
    outside.write_bytes(b"x")
    with pytest.raises(AgentArtifactStagingError):
        validate_and_hash_artifact(outside, authorized)


def test_validate_and_hash_streaming_checksum(tmp_git_repo: Path, authorized: AuthorizedRoots):
    """Ensure streaming SHA256 matches hashlib.sha256 of whole file."""
    data = b"a" * (3 * 1024 * 1024 + 17)
    exe = tmp_git_repo / "out" / "selena.exe"
    exe.write_bytes(data)
    ev = validate_and_hash_artifact(exe, authorized)
    expected = "sha256:" + __import__("hashlib").sha256(data).hexdigest()
    assert ev.checksum == expected


# ---------------------------------------------------------------------------
# stage_selena_artifact
# ---------------------------------------------------------------------------

def _snapshot(*, dirty: bool, sha: str, commit: str = "b" * 40) -> WorkspaceFingerprint:
    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    return WorkspaceFingerprint(
        branch="feature/test",
        commit=commit,
        dirty=dirty,
        sha256=sha,
        staged_diff_sha256=empty,
        staged_diff_bytes=0,
        unstaged_diff_sha256=empty,
        unstaged_diff_bytes=0,
        untracked=(),
    )


def _make_snapshots(_repo: Path, dirty: bool = False, changed: bool = False) -> tuple[WorkspaceFingerprint, WorkspaceFingerprint]:
    before = _snapshot(dirty=dirty, sha="a" * 64)
    after = _snapshot(dirty=dirty, sha=("c" * 64 if changed else "a" * 64))
    return before, after


def test_stage_clean_shared(tmp_git_repo: Path, authorized: AuthorizedRoots):
    before, after = _make_snapshots(tmp_git_repo, dirty=False, changed=False)
    exe = tmp_git_repo / "out" / "selena.exe"
    exe.write_bytes(b"bin")
    ev = validate_and_hash_artifact(exe, authorized)
    artifact = stage_selena_artifact(
        before=before,
        after=after,
        evidence=ev,
        authorized=authorized,
        project="proj",
        owner="owner",
        build_mode="release",
        toolchain_fingerprint="tc-1",
        source_kind="git",
        storage_ref="artifact://bucket/key",
        visibility="shared",
        accessibility="local",
        created_by="agent-1",
    )
    assert isinstance(artifact, SelenaArtifact)
    assert artifact.dirty is False
    assert artifact.source_changed_during_build is False
    assert artifact.visibility == "shared"
    assert artifact.branch == before.branch
    assert artifact.commit == before.commit
    assert artifact.dirty_fingerprint == before.sha256


def test_stage_dirty_forces_private(tmp_git_repo: Path, authorized: AuthorizedRoots):
    before, after = _make_snapshots(tmp_git_repo, dirty=True, changed=False)
    exe = tmp_git_repo / "out" / "selena.exe"
    exe.write_bytes(b"bin")
    ev = validate_and_hash_artifact(exe, authorized)
    artifact = stage_selena_artifact(
        before=before,
        after=after,
        evidence=ev,
        authorized=authorized,
        project="proj",
        owner="owner",
        build_mode="release",
        toolchain_fingerprint="tc-1",
        source_kind="git",
        storage_ref="artifact://bucket/key",
        visibility="shared",  # will be forced to private
        accessibility="local",
        created_by="agent-1",
    )
    assert artifact.dirty is True
    assert artifact.visibility == "private"


def test_stage_source_changed_forces_private(tmp_git_repo: Path, authorized: AuthorizedRoots):
    before, after = _make_snapshots(tmp_git_repo, dirty=True, changed=True)
    exe = tmp_git_repo / "out" / "selena.exe"
    exe.write_bytes(b"bin")
    ev = validate_and_hash_artifact(exe, authorized)
    artifact = stage_selena_artifact(
        before=before,
        after=after,
        evidence=ev,
        authorized=authorized,
        project="proj",
        owner="owner",
        build_mode="release",
        toolchain_fingerprint="tc-1",
        source_kind="git",
        storage_ref="artifact://bucket/key",
        visibility="shared",
        accessibility="local",
        created_by="agent-1",
    )
    assert artifact.source_changed_during_build is True
    assert artifact.visibility == "private"


def test_stage_rejects_empty_project(tmp_git_repo: Path, authorized: AuthorizedRoots):
    before, after = _make_snapshots(tmp_git_repo)
    exe = tmp_git_repo / "out" / "selena.exe"
    exe.write_bytes(b"bin")
    ev = validate_and_hash_artifact(exe, authorized)
    with pytest.raises(AgentArtifactStagingError):
        stage_selena_artifact(
            before=before,
            after=after,
            evidence=ev,
            authorized=authorized,
            project="",
            owner="owner",
            build_mode="release",
            toolchain_fingerprint="tc-1",
            source_kind="git",
            storage_ref="artifact://bucket/key",
            visibility="shared",
            accessibility="local",
            created_by="agent-1",
        )


def test_stage_rejects_bad_visibility(tmp_git_repo: Path, authorized: AuthorizedRoots):
    before, after = _make_snapshots(tmp_git_repo)
    exe = tmp_git_repo / "out" / "selena.exe"
    exe.write_bytes(b"bin")
    ev = validate_and_hash_artifact(exe, authorized)
    with pytest.raises(AgentArtifactStagingError):
        stage_selena_artifact(
            before=before,
            after=after,
            evidence=ev,
            authorized=authorized,
            project="proj",
            owner="owner",
            build_mode="release",
            toolchain_fingerprint="tc-1",
            source_kind="git",
            storage_ref="artifact://bucket/key",
            visibility="public",
            accessibility="local",
            created_by="agent-1",
        )


def test_stage_rejects_bad_accessibility(tmp_git_repo: Path, authorized: AuthorizedRoots):
    before, after = _make_snapshots(tmp_git_repo)
    exe = tmp_git_repo / "out" / "selena.exe"
    exe.write_bytes(b"bin")
    ev = validate_and_hash_artifact(exe, authorized)
    with pytest.raises(AgentArtifactStagingError):
        stage_selena_artifact(
            before=before,
            after=after,
            evidence=ev,
            authorized=authorized,
            project="proj",
            owner="owner",
            build_mode="release",
            toolchain_fingerprint="tc-1",
            source_kind="git",
            storage_ref="artifact://bucket/key",
            visibility="shared",
            accessibility="global",
            created_by="agent-1",
        )


def test_stage_rejects_nonfinite_created_at(tmp_git_repo: Path, authorized: AuthorizedRoots):
    before, after = _make_snapshots(tmp_git_repo)
    exe = tmp_git_repo / "out" / "selena.exe"
    exe.write_bytes(b"bin")
    ev = validate_and_hash_artifact(exe, authorized)
    with pytest.raises(AgentArtifactStagingError):
        stage_selena_artifact(
            before=before,
            after=after,
            evidence=ev,
            authorized=authorized,
            project="proj",
            owner="owner",
            build_mode="release",
            toolchain_fingerprint="tc-1",
            source_kind="git",
            storage_ref="artifact://bucket/key",
            visibility="shared",
            accessibility="local",
            created_by="agent-1",
            created_at=float("inf"),
        )


def test_stage_rejects_negative_retain_until(tmp_git_repo: Path, authorized: AuthorizedRoots):
    before, after = _make_snapshots(tmp_git_repo)
    exe = tmp_git_repo / "out" / "selena.exe"
    exe.write_bytes(b"bin")
    ev = validate_and_hash_artifact(exe, authorized)
    with pytest.raises(AgentArtifactStagingError):
        stage_selena_artifact(
            before=before,
            after=after,
            evidence=ev,
            authorized=authorized,
            project="proj",
            owner="owner",
            build_mode="release",
            toolchain_fingerprint="tc-1",
            source_kind="git",
            storage_ref="artifact://bucket/key",
            visibility="shared",
            accessibility="local",
            created_by="agent-1",
            retain_until=-1.0,
        )


def test_stage_rejects_forged_snapshot_and_absolute_metadata(tmp_git_repo: Path, authorized: AuthorizedRoots):
    before, after = _make_snapshots(tmp_git_repo)
    ev = ArtifactEvidence("sha256:" + "a" * 64, 1, "selena.exe")
    forged = WorkspaceFingerprint(
        branch=before.branch,
        commit="not-a-commit",
        dirty=False,
        sha256=before.sha256,
        staged_diff_sha256=before.staged_diff_sha256,
        staged_diff_bytes=0,
        unstaged_diff_sha256=before.unstaged_diff_sha256,
        unstaged_diff_bytes=0,
        untracked=(),
    )
    common = dict(
        after=after,
        evidence=ev,
        authorized=authorized,
        project="proj",
        owner="owner",
        build_mode="release",
        toolchain_fingerprint="tc-1",
        source_kind="git",
        storage_ref="artifact://bucket/key",
        visibility="shared",
        accessibility="local",
        created_by="agent-1",
    )
    with pytest.raises(AgentArtifactStagingError, match="commit"):
        stage_selena_artifact(before=forged, **common)
    with pytest.raises(AgentArtifactStagingError, match="absolute path"):
        stage_selena_artifact(before=before, **{**common, "created_by": r"C:\secret\agent"})


# ---------------------------------------------------------------------------
# artifact_to_stage_result
# ---------------------------------------------------------------------------

def test_result_dict_no_abs_paths(tmp_git_repo: Path, authorized: AuthorizedRoots):
    before, after = _make_snapshots(tmp_git_repo)
    exe = tmp_git_repo / "out" / "selena.exe"
    exe.write_bytes(b"bin")
    ev = validate_and_hash_artifact(exe, authorized)
    artifact = stage_selena_artifact(
        before=before,
        after=after,
        evidence=ev,
        authorized=authorized,
        project="proj",
        owner="owner",
        build_mode="release",
        toolchain_fingerprint="tc-1",
        source_kind="git",
        storage_ref="artifact://bucket/key",
        visibility="shared",
        accessibility="local",
        created_by="agent-1",
    )
    result = artifact_to_stage_result(artifact, before, after, ev)
    assert result["logical_path"] == "selena.exe"
    assert result["checksum"] == ev.checksum
    assert result["size"] == ev.size
    assert result["source_changed_during_build"] is False
    assert "artifact" in result
    assert "before" in result
    assert "after" in result
    # Ensure no absolute paths leaked into JSON.
    raw = json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert tmp_git_repo.resolve().as_posix() not in raw


def test_result_dict_with_manifest_no_abs_paths(tmp_git_repo: Path, authorized: AuthorizedRoots):
    before, after = _make_snapshots(tmp_git_repo)
    exe = tmp_git_repo / "out" / "selena.exe"
    exe.write_bytes(b"bin")
    ev = validate_and_hash_artifact(exe, authorized)
    with pytest.raises(AgentArtifactStagingError):
        stage_selena_artifact(
            before=before,
            after=after,
            evidence=ev,
            authorized=authorized,
            project="proj",
            owner="owner",
            build_mode="release",
            toolchain_fingerprint="tc-1",
            source_kind="git",
            storage_ref="artifact://bucket/key",
            visibility="shared",
            accessibility="local",
            created_by="agent-1",
            interface_manifest={"path": str(tmp_git_repo / "secret")},
        )


# ---------------------------------------------------------------------------
# ArtifactEvidence immutability
# ---------------------------------------------------------------------------

def test_evidence_is_frozen():
    ev = ArtifactEvidence(checksum="sha256:" + "a" * 64, size=1, logical_path="selena.exe")
    with pytest.raises(AttributeError):
        ev.size = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "patch",
    [
        {"checksum": "bad"},
        {"size": 0},
        {"logical_path": "../selena.exe"},
        {"logical_path": "C:/secret/selena.exe"},
        {"logical_path": "/secret/selena.exe"},
        {"logical_path": r"folder\selena.exe"},
        {"logical_path": "folder//selena.exe"},
        {"logical_path": "other.exe"},
    ],
)
def test_evidence_rejects_forged_or_absolute_values(patch):
    values = {
        "checksum": "sha256:" + "a" * 64,
        "size": 1,
        "logical_path": "selena.exe",
    }
    values.update(patch)
    with pytest.raises(AgentArtifactStagingError):
        ArtifactEvidence(**values)


# ---------------------------------------------------------------------------
# py_compile sanity
# ---------------------------------------------------------------------------

def test_module_compiles():
    import py_compile
    import core.agent_artifact_staging as mod
    py_compile.compile(mod.__file__, doraise=True)
