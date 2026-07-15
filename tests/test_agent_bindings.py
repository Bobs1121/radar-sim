"""Tests for core.agent_bindings.

Filesystem-only, no Git, fast.  Covers ID parity, CRUD, revalidation,
concurrent idempotent registration, nonfinite clock, malformed JSON.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

import core.agent_bindings as bindings
from core.agent_artifact_staging import AgentArtifactStagingError, AuthorizedRoots


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_workspace(project: str = "bydod25", root: Path | None = None) -> Path:
    """Create a workspace with one output directory under *root*."""
    if root is None:
        root = Path(os.environ.get("RSIM_HOME", Path.home() / ".rsim")) / "test_workspace"
    ws = root / project / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    out = ws / "out"
    out.mkdir(exist_ok=True)
    return ws


def store(tmp_path: Path) -> bindings.AgentBindingStore:
    return bindings.AgentBindingStore(db_path=tmp_path / "bindings.db")


# ---------------------------------------------------------------------------
# make_workspace_binding_id parity with legacy facade
# ---------------------------------------------------------------------------

def test_make_id_matches_legacy_for_same_project_and_path():
    from core.source_resolution_runtime import logical_workspace_binding_id
    from core.spec import UserBindings

    ub = UserBindings(
        project="bydod25",
        workspace_path=r"D:\secret\workspace",
        selena_build_script="",
        environment_build_script="",
        existing_selena=(),
    )
    legacy = logical_workspace_binding_id(ub)
    pure = bindings.make_workspace_binding_id("bydod25", r"D:\secret\workspace")
    assert pure == legacy
    assert pure.startswith("workspace:sha256:")
    assert len(pure) == len("workspace:sha256:") + 24


def test_make_id_is_stable_across_windows_path_spelling():
    first = bindings.make_workspace_binding_id("bydod25", r"D:\\Secret\\Workspace\\")
    second = bindings.make_workspace_binding_id("bydod25", "d:/secret/workspace")
    third = bindings.make_workspace_binding_id("bydod25", "d:/secret//workspace")
    assert first == second == third


def test_make_id_empty_workspace_returns_empty():
    assert bindings.make_workspace_binding_id("proj", "") == ""
    assert bindings.make_workspace_binding_id("proj", "   ") == ""


def test_make_id_different_project_different_id():
    a = bindings.make_workspace_binding_id("a", "/ws")
    b = bindings.make_workspace_binding_id("b", "/ws")
    assert a != b


def test_make_id_different_path_different_id():
    a = bindings.make_workspace_binding_id("proj", "/ws1")
    b = bindings.make_workspace_binding_id("proj", "/ws2")
    assert a != b


# ---------------------------------------------------------------------------
# default_agent_binding_db_path
# ---------------------------------------------------------------------------

def test_default_path_uses_rsim_home_when_set(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RSIM_HOME", str(tmp_path / "rsim"))
    expected = tmp_path / "rsim" / "agent" / "bindings.db"
    assert bindings.default_agent_binding_db_path() == expected


def test_default_path_falls_back_to_user_home(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("RSIM_HOME", raising=False)
    expected = Path.home() / ".rsim" / "agent" / "bindings.db"
    assert bindings.default_agent_binding_db_path() == expected


def test_corrupt_database_error_is_path_free(tmp_path: Path):
    db_path = tmp_path / "secret-bindings.db"
    db_path.write_bytes(b"not a sqlite database")
    with pytest.raises(bindings.AgentBindingError) as excinfo:
        bindings.AgentBindingStore(db_path=db_path)
    assert "secret-bindings" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_project",
    [
        "",
        "  ",
        "a/b",
        r"a\b",
        "a\0b",
        " leading",
        "trailing ",
    ],
)
def test_register_rejects_bad_project_token(bad_project: str, tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    with pytest.raises(bindings.AgentBindingError):
        s.register(bad_project, ws, (ws / "out",))


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "workspace:sha256:short",
        "workspace:md5:" + "a" * 24,
        "workspace:sha256:" + "g" * 24,
        "workspace:sha256:" + "a" * 23,
        "workspace:sha256:" + "a" * 25,
    ],
)
def test_get_rejects_bad_binding_id(bad_id: str, tmp_path: Path):
    s = store(tmp_path)
    with pytest.raises(bindings.AgentBindingError):
        s.get(bad_id)


def test_delete_rejects_bad_binding_id(tmp_path: Path):
    s = store(tmp_path)
    with pytest.raises(bindings.AgentBindingError):
        s.delete("not-an-id")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def test_register_creates_binding(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    b = s.register("proj", ws, (ws / "out",))
    assert isinstance(b, bindings.WorkspaceBinding)
    assert b.project == "proj"
    assert b.workspace_root == ws.resolve()
    assert b.output_roots == (ws.resolve() / "out",)
    assert b.binding_id == bindings.make_workspace_binding_id("proj", str(ws.resolve()))
    assert b.created_at > 0 and math.isfinite(b.created_at)
    assert b.updated_at >= b.created_at


def test_register_public_dict_has_no_paths(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    b = s.register("proj", ws, (ws / "out",))
    d = b.public_dict
    raw = json.dumps(d, ensure_ascii=False, sort_keys=True)
    assert "workspace" not in raw.lower() or "root_count" in raw
    # Ensure no absolute path strings leaked.
    assert str(ws.resolve()) not in raw
    assert "id" in d
    assert "project" in d
    assert "output_root_count" in d
    assert "configured" in d
    assert "healthy" in d
    assert "created_at" in d
    assert "updated_at" in d


def test_get_returns_binding(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    registered = s.register("proj", ws, (ws / "out",))
    fetched = s.get(registered.binding_id)
    assert fetched.binding_id == registered.binding_id
    assert fetched.project == registered.project
    assert fetched.workspace_root == registered.workspace_root


def test_get_with_project_mismatch_raises(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    registered = s.register("proj", ws, (ws / "out",))
    with pytest.raises(bindings.AgentBindingError, match="mismatch"):
        s.get(registered.binding_id, project="other")


def test_get_missing_raises(tmp_path: Path):
    s = store(tmp_path)
    with pytest.raises(bindings.AgentBindingError, match="not found"):
        s.get("workspace:sha256:" + "a" * 24)


def test_list_returns_bindings(tmp_path: Path):
    ws1 = make_workspace("p1", root=tmp_path)
    ws2 = make_workspace("p2", root=tmp_path)
    s = store(tmp_path)
    b1 = s.register("p1", ws1, (ws1 / "out",))
    b2 = s.register("p2", ws2, (ws2 / "out",))
    all_bindings = s.list()
    assert len(all_bindings) == 2
    assert {b.project for b in all_bindings} == {"p1", "p2"}


def test_list_filters_by_project(tmp_path: Path):
    ws1 = make_workspace("p1", root=tmp_path)
    ws2 = make_workspace("p2", root=tmp_path)
    s = store(tmp_path)
    s.register("p1", ws1, (ws1 / "out",))
    s.register("p2", ws2, (ws2 / "out",))
    assert len(s.list(project="p1")) == 1
    assert s.list(project="p1")[0].project == "p1"


def test_delete_removes_binding(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    b = s.register("proj", ws, (ws / "out",))
    s.delete(b.binding_id)
    with pytest.raises(bindings.AgentBindingError, match="not found"):
        s.get(b.binding_id)


def test_delete_missing_raises(tmp_path: Path):
    s = store(tmp_path)
    with pytest.raises(bindings.AgentBindingError, match="not found"):
        s.delete("workspace:sha256:" + "a" * 24)


def test_register_update_retains_created_at(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    first = s.register("proj", ws, (ws / "out",))
    time.sleep(0.01)
    second = s.register("proj", ws, (ws / "out",))
    assert second.created_at == first.created_at
    assert second.updated_at > first.updated_at


def test_reopen_reads_existing_bindings(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s1 = store(tmp_path)
    b1 = s1.register("proj", ws, (ws / "out",))
    s2 = store(tmp_path)
    b2 = s2.get(b1.binding_id)
    assert b2.binding_id == b1.binding_id
    assert b2.project == b1.project
    assert b2.workspace_root == b1.workspace_root


# ---------------------------------------------------------------------------
# Revalidation / health
# ---------------------------------------------------------------------------

def test_get_skips_removed_workspace(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    b = s.register("proj", ws, (ws / "out",))
    # Remove workspace so revalidation fails.
    import shutil
    shutil.rmtree(ws)
    with pytest.raises(bindings.AgentBindingError, match="unhealthy|not found"):
        s.get(b.binding_id)


def test_list_skips_unhealthy_bindings(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    b = s.register("proj", ws, (ws / "out",))
    import shutil
    shutil.rmtree(ws)
    assert s.list() == ()


def test_resolve_authorized_roots_returns_authorized_roots(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    b = s.register("proj", ws, (ws / "out",))
    auth = s.resolve_authorized_roots(b.binding_id, "proj")
    assert isinstance(auth, AuthorizedRoots)
    assert auth.workspace_root == ws.resolve()
    assert auth.contains_output(ws / "out")


def test_resolve_authorized_roots_mismatch_raises(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    b = s.register("proj", ws, (ws / "out",))
    with pytest.raises(bindings.AgentBindingError, match="mismatch"):
        s.resolve_authorized_roots(b.binding_id, "other")


# ---------------------------------------------------------------------------
# Delegated validation (traversal / symlink / containment)
# ---------------------------------------------------------------------------

def test_register_delegates_traversal_validation(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    tricky = ws / "out" / ".." / "out"
    s = store(tmp_path)
    b = s.register("proj", ws, (tricky,))
    assert b.output_roots[0] == (ws / "out").resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reparse/symlink privilege test")
def test_register_delegates_symlink_escape_windows(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    outside = tmp_path / "outside_out"
    outside.mkdir()
    link = ws / "link_out"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires elevated privilege on Windows")
    s = store(tmp_path)
    with pytest.raises(bindings.AgentBindingError):
        s.register("proj", ws, (link,))


def test_register_delegates_symlink_escape_posix(tmp_path: Path):
    if sys.platform == "win32":
        pytest.skip("POSIX-only symlink escape test")
    ws = make_workspace(root=tmp_path)
    outside = tmp_path / "outside_out"
    outside.mkdir()
    link = ws / "link_out"
    link.symlink_to(outside, target_is_directory=True)
    s = store(tmp_path)
    with pytest.raises(bindings.AgentBindingError):
        s.register("proj", ws, (link,))


def test_register_rejects_output_outside_workspace(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    sibling = tmp_path / "other_out"
    sibling.mkdir()
    s = store(tmp_path)
    with pytest.raises(bindings.AgentBindingError):
        s.register("proj", ws, (sibling,))


def test_register_rejects_drive_root_workspace(tmp_path: Path):
    if sys.platform == "win32":
        root = Path("C:/")
    else:
        root = Path("/")
    s = store(tmp_path)
    with pytest.raises(bindings.AgentBindingError):
        s.register("proj", root, (root / "out",))


# ---------------------------------------------------------------------------
# Concurrent idempotent registration
# ---------------------------------------------------------------------------

def test_concurrent_register_is_idempotent(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    results: list[bindings.WorkspaceBinding] = []
    errors: list[Exception] = []

    def worker():
        try:
            b = s.register("proj", ws, (ws / "out",))
            results.append(b)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent registration raised: {errors}"
    assert len(results) == 8
    binding_ids = {b.binding_id for b in results}
    assert len(binding_ids) == 1
    created_at_set = {b.created_at for b in results}
    assert len(created_at_set) == 1


# ---------------------------------------------------------------------------
# Nonfinite clock
# ---------------------------------------------------------------------------

def test_register_rejects_nonfinite_clock(monkeypatch, tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    monkeypatch.setattr(s, "_now", lambda: float("inf"))
    with pytest.raises(bindings.AgentBindingError, match="clock"):
        s.register("proj", ws, (ws / "out",))


def test_register_rejects_negative_clock(monkeypatch, tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    monkeypatch.setattr(s, "_now", lambda: -1.0)
    with pytest.raises(bindings.AgentBindingError, match="clock"):
        s.register("proj", ws, (ws / "out",))


# ---------------------------------------------------------------------------
# Malformed DB JSON
# ---------------------------------------------------------------------------

def test_malformed_output_roots_json_raises_on_read(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    s.register("proj", ws, (ws / "out",))
    # Corrupt the JSON directly.
    with sqlite3.connect(str(s.db_path)) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "UPDATE workspace_bindings SET output_roots = ?",
            ("not-json",),
        )
    with pytest.raises(bindings.AgentBindingError, match="malformed"):
        s.list()


def test_non_list_output_roots_json_raises_on_read(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    s.register("proj", ws, (ws / "out",))
    with sqlite3.connect(str(s.db_path)) as conn:
        conn.execute(
            "UPDATE workspace_bindings SET output_roots = ?",
            (json.dumps({"path": str(ws / "out")}),),
        )
    with pytest.raises(bindings.AgentBindingError, match="shape"):
        s.list()


# ---------------------------------------------------------------------------
# Project isolation
# ---------------------------------------------------------------------------

def test_same_path_different_project_different_binding(tmp_path: Path):
    ws = make_workspace(root=tmp_path)
    s = store(tmp_path)
    a = s.register("proj-a", ws, (ws / "out",))
    b = s.register("proj-b", ws, (ws / "out",))
    assert a.binding_id != b.binding_id
    assert a.project == "proj-a"
    assert b.project == "proj-b"


# ---------------------------------------------------------------------------
# py_compile sanity
# ---------------------------------------------------------------------------

def test_module_compiles():
    import py_compile
    py_compile.compile(bindings.__file__, doraise=True)
