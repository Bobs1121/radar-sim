"""Targeted tests for the central artifact store.

Coverage:
- traversal/absolute/UNC/drive/device paths blocked
- symlink escapes blocked (skipped on Windows without symlink privilege)
- owner isolation
- restart resume via SQLite
- offset/chunk semantics
- checksum/size mismatch on finalize
- same-checksum idempotency
- different-checksum collision
- logical ref lookup
- shared multi-user access (all finalized artifacts visible)
- no physical path leakage in public interfaces
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from core.artifact_store import (
    ArtifactChecksumError,
    ArtifactConflictError,
    ArtifactPathError,
    ArtifactSessionError,
    ArtifactStore,
    ArtifactStoreError,
    UploadSession,
    DEFAULT_CHUNK_SIZE,
)
from core.user import normalize_user


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _make_store() -> tuple[ArtifactStore, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="rsim_art_"))
    db = tmp / "sessions.db"
    store = ArtifactStore(root=tmp, db_path=db)
    return store, tmp


class TestPathValidation:
    def test_empty_logical_path_rejected(self) -> None:
        store, _ = _make_store()
        with pytest.raises(ArtifactPathError):
            store.create_upload_session("alice", "p1", "", 100, _sha256(b"x"))

    def test_absolute_unix_rejected(self) -> None:
        store, _ = _make_store()
        with pytest.raises(ArtifactPathError):
            store.create_upload_session("alice", "p1", "/etc/passwd", 100, _sha256(b"x"))

    def test_absolute_windows_rejected(self) -> None:
        store, _ = _make_store()
        with pytest.raises(ArtifactPathError):
            store.create_upload_session("alice", "p1", "C:\\Windows\\System32", 100, _sha256(b"x"))

    def test_unc_rejected(self) -> None:
        store, _ = _make_store()
        with pytest.raises(ArtifactPathError):
            store.create_upload_session("alice", "p1", "\\\\server\\share", 100, _sha256(b"x"))

    def test_traversal_rejected(self) -> None:
        store, _ = _make_store()
        with pytest.raises(ArtifactPathError):
            store.create_upload_session("alice", "p1", "../escape", 100, _sha256(b"x"))

    def test_traversal_in_segment_rejected(self) -> None:
        store, _ = _make_store()
        with pytest.raises(ArtifactPathError):
            store.create_upload_session("alice", "p1", "a/../b", 100, _sha256(b"x"))

    def test_reserved_name_rejected(self) -> None:
        store, _ = _make_store()
        with pytest.raises(ArtifactPathError):
            store.create_upload_session("alice", "p1", "NUL", 100, _sha256(b"x"))

    def test_reserved_internal_namespace_rejected(self) -> None:
        store, _ = _make_store()
        with pytest.raises(ArtifactPathError, match="reserved internal namespace"):
            store.create_upload_session("alice", "p1", ".store/selena.exe", 100, _sha256(b"x"))
        with pytest.raises(ArtifactPathError, match="reserved internal namespace"):
            store.create_upload_session("alice", "p1", "temp/selena.exe", 100, _sha256(b"x"))

    def test_valid_relative_path_accepted(self) -> None:
        store, _ = _make_store()
        session = store.create_upload_session("alice", "p1", "rel/path/selena.exe", 100, _sha256(b"x"))
        assert session.logical_path == "rel/path/selena.exe"

    def test_publish_path_normalization_appends_selena(self) -> None:
        store, _ = _make_store()
        session = store.create_upload_session("alice", "p1", "a/b", 100, _sha256(b"x"))
        assert session.logical_path == "a/b/selena.exe"

    def test_publish_path_preserves_existing_selena_exe(self) -> None:
        store, _ = _make_store()
        session = store.create_upload_session("alice", "p1", "a/b/selena.exe", 100, _sha256(b"x"))
        assert session.logical_path == "a/b/selena.exe"

    def test_publish_path_case_insensitive_selena_exe(self) -> None:
        store, _ = _make_store()
        session = store.create_upload_session("alice", "p1", "a/b/SeLeNa.ExE", 100, _sha256(b"x"))
        assert session.logical_path == "a/b/SeLeNa.ExE"


class TestPhysicalLayout:
    def test_project_isolation_in_content_dir(self) -> None:
        store, tmp = _make_store()
        data = b"hello"
        checksum = _sha256(data)
        session = store.create_upload_session("alice", "p1", "a/b", len(data), checksum)
        store.append_chunk(session.session_id, 0, data, owner="alice")
        store.finalize_upload(session.session_id, owner="alice")
        # Physical file must be under root/content/<project>/
        physical = tmp / "content" / "p1" / "a" / "b" / "selena.exe"
        assert physical.exists()
        assert physical.read_bytes() == data

    def test_same_path_different_projects_no_collision(self) -> None:
        store, tmp = _make_store()
        data1 = b"hello p1"
        data2 = b"hello p2"
        s1 = store.create_upload_session("alice", "p1", "a/b", len(data1), _sha256(data1))
        store.append_chunk(s1.session_id, 0, data1, owner="alice")
        store.finalize_upload(s1.session_id, owner="alice")
        s2 = store.create_upload_session("alice", "p2", "a/b", len(data2), _sha256(data2))
        store.append_chunk(s2.session_id, 0, data2, owner="alice")
        store.finalize_upload(s2.session_id, owner="alice")
        assert (tmp / "content" / "p1" / "a" / "b" / "selena.exe").read_bytes() == data1
        assert (tmp / "content" / "p2" / "a" / "b" / "selena.exe").read_bytes() == data2


class TestOwnerIsolation:
    def test_owner_mismatch_on_get(self) -> None:
        store, _ = _make_store()
        session = store.create_upload_session("alice", "p1", "a/b", 100, _sha256(b"x"))
        with pytest.raises(ArtifactSessionError, match="owner mismatch"):
            store.get_session(session.session_id, owner="bob")

    def test_owner_mismatch_on_append(self) -> None:
        store, _ = _make_store()
        session = store.create_upload_session("alice", "p1", "a/b", 100, _sha256(b"x"))
        with pytest.raises(ArtifactSessionError, match="owner mismatch"):
            store.append_chunk(session.session_id, 0, b"x", owner="bob")

    def test_owner_mismatch_on_finalize(self) -> None:
        store, _ = _make_store()
        session = store.create_upload_session("alice", "p1", "a/b", 1, _sha256(b"x"))
        store.append_chunk(session.session_id, 0, b"x", owner="alice")
        with pytest.raises(ArtifactSessionError, match="owner mismatch"):
            store.finalize_upload(session.session_id, owner="bob")


class TestUploadLifecycle:
    def test_create_and_get_session(self) -> None:
        store, _ = _make_store()
        checksum = _sha256(b"hello")
        session = store.create_upload_session("alice", "p1", "a/b", 5, checksum)
        assert session.status == "active"
        assert session.received_bytes == 0
        got = store.get_session(session.session_id, owner="alice")
        assert got.session_id == session.session_id

    def test_append_chunk_updates_received(self) -> None:
        store, _ = _make_store()
        data = b"hello world"
        checksum = _sha256(data)
        session = store.create_upload_session("alice", "p1", "a/b", len(data), checksum)
        updated = store.append_chunk(session.session_id, 0, data, owner="alice")
        assert updated.received_bytes == len(data)

    def test_append_chunk_at_offset(self) -> None:
        store, _ = _make_store()
        data = b"hello world"
        checksum = _sha256(data)
        session = store.create_upload_session("alice", "p1", "a/b", len(data), checksum)
        store.append_chunk(session.session_id, 0, b"hello", owner="alice")
        store.append_chunk(session.session_id, 5, b" world", owner="alice")
        got = store.get_session(session.session_id, owner="alice")
        assert got.received_bytes == len(data)

    def test_overwrite_offset_idempotent_exact_match(self) -> None:
        store, _ = _make_store()
        data = b"hello world"
        checksum = _sha256(data)
        session = store.create_upload_session("alice", "p1", "a/b", len(data), checksum)
        store.append_chunk(session.session_id, 0, b"xxxxx", owner="alice")
        # Overwrite with different data at same offset must fail.
        with pytest.raises(ArtifactSessionError, match="different size/checksum"):
            store.append_chunk(session.session_id, 0, b"hello", owner="alice")
        # Exact retry with same data must succeed.
        store.append_chunk(session.session_id, 0, b"xxxxx", owner="alice")
        store.append_chunk(session.session_id, 5, b" world", owner="alice")
        got = store.get_session(session.session_id, owner="alice")
        assert got.received_bytes == len(data)

    def test_non_contiguous_offset_rejected(self) -> None:
        store, _ = _make_store()
        data = b"hello world"
        checksum = _sha256(data)
        session = store.create_upload_session("alice", "p1", "a/b", len(data), checksum)
        store.append_chunk(session.session_id, 0, b"hello", owner="alice")
        with pytest.raises(ArtifactSessionError, match="contiguous"):
            store.append_chunk(session.session_id, 10, b"x", owner="alice")

    def test_finalize_success(self) -> None:
        store, tmp = _make_store()
        data = b"hello world"
        checksum = _sha256(data)
        session = store.create_upload_session("alice", "p1", "a/b", len(data), checksum)
        store.append_chunk(session.session_id, 0, data, owner="alice")
        result = store.finalize_upload(session.session_id, owner="alice")
        assert result["status"] == "finalized"
        assert result["checksum"] == checksum
        assert result["size"] == len(data)
        assert result["reused"] is False
        # Physical file must exist under root/content/<project>.
        physical = tmp / "content" / "p1" / "a" / "b" / "selena.exe"
        assert physical.exists()
        assert physical.read_bytes() == data

    def test_finalize_size_mismatch(self) -> None:
        store, _ = _make_store()
        data = b"hello"
        checksum = _sha256(data)
        session = store.create_upload_session("alice", "p1", "a/b", 10, checksum)
        store.append_chunk(session.session_id, 0, data, owner="alice")
        with pytest.raises(ArtifactChecksumError, match="size mismatch"):
            store.finalize_upload(session.session_id, owner="alice")

    def test_finalize_checksum_mismatch(self) -> None:
        store, _ = _make_store()
        data = b"hello"
        bad_checksum = _sha256(b"other")
        session = store.create_upload_session("alice", "p1", "a/b", len(data), bad_checksum)
        store.append_chunk(session.session_id, 0, data, owner="alice")
        with pytest.raises(ArtifactChecksumError, match="checksum mismatch"):
            store.finalize_upload(session.session_id, owner="alice")

    def test_same_checksum_idempotent(self) -> None:
        store, _ = _make_store()
        data = b"hello"
        checksum = _sha256(data)
        s1 = store.create_upload_session("alice", "p1", "a/b", len(data), checksum)
        store.append_chunk(s1.session_id, 0, data, owner="alice")
        r1 = store.finalize_upload(s1.session_id, owner="alice")
        # Second finalize with same path and checksum reuses.
        s2 = store.create_upload_session("alice", "p1", "a/b", len(data), checksum)
        store.append_chunk(s2.session_id, 0, data, owner="alice")
        r2 = store.finalize_upload(s2.session_id, owner="alice")
        assert r2["reused"] is True
        assert r2["storage_ref"] == r1["storage_ref"]
        assert r2["artifact_id"] == r1["artifact_id"]

    def test_same_session_finalize_retry_is_idempotent(self) -> None:
        store, _ = _make_store()
        data = b"hello"
        session = store.create_upload_session("alice", "p1", "a/b", len(data), _sha256(data))
        store.append_chunk(session.session_id, 0, data, owner="alice")
        first = store.finalize_upload(session.session_id, owner="alice")
        second = store.finalize_upload(session.session_id, owner="alice")
        assert second["reused"] is True
        assert second["artifact_id"] == first["artifact_id"]
        assert store.get_session(session.session_id, owner="alice").status == "finalized"

    def test_different_checksum_collision(self) -> None:
        store, _ = _make_store()
        data1 = b"hello"
        data2 = b"world"
        s1 = store.create_upload_session("alice", "p1", "a/b", len(data1), _sha256(data1))
        store.append_chunk(s1.session_id, 0, data1, owner="alice")
        store.finalize_upload(s1.session_id, owner="alice")
        s2 = store.create_upload_session("alice", "p1", "a/b", len(data2), _sha256(data2))
        store.append_chunk(s2.session_id, 0, data2, owner="alice")
        with pytest.raises(ArtifactConflictError, match="different checksum already exists"):
            store.finalize_upload(s2.session_id, owner="alice")

    def test_lookup_by_storage_ref(self) -> None:
        store, _ = _make_store()
        data = b"hello"
        checksum = _sha256(data)
        session = store.create_upload_session("alice", "p1", "a/b", len(data), checksum)
        store.append_chunk(session.session_id, 0, data, owner="alice")
        result = store.finalize_upload(session.session_id, owner="alice")
        ref = result["storage_ref"]
        info = store.lookup_by_storage_ref(ref)
        assert info["checksum"] == checksum
        assert info["size"] == len(data)

    def test_list_finalized_filter_project(self) -> None:
        store, _ = _make_store()
        data = b"x"
        s1 = store.create_upload_session("alice", "p1", "a/b", 1, _sha256(data))
        store.append_chunk(s1.session_id, 0, data, owner="alice")
        store.finalize_upload(s1.session_id, owner="alice")
        s2 = store.create_upload_session("bob", "p2", "c/d", 1, _sha256(data))
        store.append_chunk(s2.session_id, 0, data, owner="bob")
        store.finalize_upload(s2.session_id, owner="bob")
        p1_items = store.list_finalized(project="p1")
        assert len(p1_items) == 1
        assert p1_items[0]["project"] == "p1"

    def test_multi_user_shared_visibility(self) -> None:
        store, _ = _make_store()
        data = b"shared"
        s = store.create_upload_session("alice", "p1", "shared/selena.exe", len(data), _sha256(data))
        store.append_chunk(s.session_id, 0, data, owner="alice")
        store.finalize_upload(s.session_id, owner="alice")
        # Bob can look it up by ref.
        ref = "shared://selena/p1/shared/selena.exe"
        info = store.lookup_by_storage_ref(ref)
        assert info["owner"] == normalize_user("alice")
        # Bob can list it.
        items = store.list_finalized(project="p1")
        assert len(items) == 1

    def test_delete_session_cleans_temp(self) -> None:
        store, _ = _make_store()
        data = b"temp"
        s = store.create_upload_session("alice", "p1", "a/b", len(data), _sha256(data))
        store.append_chunk(s.session_id, 0, data, owner="alice")
        temp = store._temp_path(s.session_id)
        assert temp.exists()
        store.delete_session(s.session_id, owner="alice")
        assert not temp.exists()
        with pytest.raises(ArtifactSessionError):
            store.get_session(s.session_id)


class TestRestartResume:
    def test_session_survives_recreate_store(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="rsim_art_"))
        db = tmp / "sessions.db"
        store1 = ArtifactStore(root=tmp, db_path=db)
        data = b"resume me"
        checksum = _sha256(data)
        s = store1.create_upload_session("alice", "p1", "a/b", len(data), checksum)
        store1.append_chunk(s.session_id, 0, b"resume ", owner="alice")
        # Simulate process restart by creating a new store instance on the same DB.
        store2 = ArtifactStore(root=tmp, db_path=db)
        got = store2.get_session(s.session_id, owner="alice")
        assert got.received_bytes == len(b"resume ")
        store2.append_chunk(s.session_id, len(b"resume "), b"me", owner="alice")
        result = store2.finalize_upload(s.session_id, owner="alice")
        assert result["status"] == "finalized"


class TestNoPhysicalPathLeakage:
    def test_public_interfaces_no_physical_path(self) -> None:
        store, tmp = _make_store()
        data = b"secret"
        checksum = _sha256(data)
        s = store.create_upload_session("alice", "p1", "a/b", len(data), checksum)
        store.append_chunk(s.session_id, 0, data, owner="alice")
        result = store.finalize_upload(s.session_id, owner="alice")
        # No public method returns the physical root path.
        assert "storage_ref" in result
        assert str(tmp) not in str(result.get("storage_ref", ""))
        info = store.lookup_by_storage_ref(result["storage_ref"])
        assert str(tmp) not in str(info.get("storage_ref", ""))
        for item in store.list_finalized():
            assert str(tmp) not in str(item.get("storage_ref", ""))


class TestSymlinkEscape:
    @pytest.mark.skipif(os.name != "posix", reason="Unix symlink test only")
    def test_symlink_escape_blocked_unix(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="rsim_art_"))
        db = tmp / "sessions.db"
        store = ArtifactStore(root=tmp, db_path=db)
        # Create a symlink inside the root that points outside.
        evil = tmp / "evil"
        evil.symlink_to("/tmp")
        with pytest.raises(ArtifactPathError, match="escape"):
            store.create_upload_session("alice", "p1", "evil/../../etc/passwd", 1, _sha256(b"x"))

    @pytest.mark.skipif(os.name == "posix", reason="Windows reparse test only")
    def test_reparse_or_symlink_escape_blocked_windows(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="rsim_art_"))
        db = tmp / "sessions.db"
        store = ArtifactStore(root=tmp, db_path=db)
        # If we can create a junction, test it; otherwise skip.
        outside = Path(tempfile.mkdtemp(prefix="rsim_outside_"))
        junction_parent = tmp / "content" / "p1"
        junction_parent.mkdir(parents=True)
        junction = junction_parent / "junction"
        try:
            import subprocess
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                check=True,
                capture_output=True,
            )
        except Exception:
            pytest.skip("Cannot create junction on this Windows environment")
        # Junction points outside root, so path under it must be rejected.
        with pytest.raises(ArtifactPathError, match="escape"):
            store.create_upload_session("alice", "p1", "junction/a/b", 1, _sha256(b"x"))


class TestUntrackedTarget:
    def test_untracked_existing_same_checksum_recovered(self) -> None:
        store, tmp = _make_store()
        data = b"preexisting"
        checksum = _sha256(data)
        # Write a file directly to the target path without DB record.
        target = tmp / "content" / "p1" / "a" / "b" / "selena.exe"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        # Now create a session and finalize: should recover idempotently.
        session = store.create_upload_session("alice", "p1", "a/b", len(data), checksum)
        store.append_chunk(session.session_id, 0, data, owner="alice")
        result = store.finalize_upload(session.session_id, owner="alice")
        assert result["reused"] is True
        assert result["checksum"] == checksum

    def test_untracked_existing_different_checksum_conflicts(self) -> None:
        store, tmp = _make_store()
        data1 = b"preexisting"
        data2 = b"newcontent"
        # Write a file directly to the target path without DB record.
        target = tmp / "content" / "p1" / "a" / "b" / "selena.exe"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data1)
        # Now create a session with different checksum and finalize: must conflict.
        session = store.create_upload_session("alice", "p1", "a/b", len(data2), _sha256(data2))
        store.append_chunk(session.session_id, 0, data2, owner="alice")
        with pytest.raises(ArtifactConflictError, match="different checksum already exists"):
            store.finalize_upload(session.session_id, owner="alice")


class TestEdgeCases:
    def test_expected_size_must_be_positive(self) -> None:
        store, _ = _make_store()
        with pytest.raises(ArtifactSessionError, match="expected_size"):
            store.create_upload_session("alice", "p1", "a/b", 0, _sha256(b"hello"))
        with pytest.raises(ArtifactSessionError, match="expected_size"):
            store.create_upload_session("alice", "p1", "a/b", -1, _sha256(b"hello"))

    def test_expected_checksum_must_be_exact_sha256(self) -> None:
        store, _ = _make_store()
        with pytest.raises(ArtifactSessionError, match="expected_checksum"):
            store.create_upload_session("alice", "p1", "a/b", 5, "sha1:abc")
        with pytest.raises(ArtifactSessionError, match="expected_checksum"):
            store.create_upload_session("alice", "p1", "a/b", 5, "sha256:ABC123")
        with pytest.raises(ArtifactSessionError, match="expected_checksum"):
            store.create_upload_session("alice", "p1", "a/b", 5, "")

    def test_chunk_exceeds_expected_size(self) -> None:
        store, _ = _make_store()
        s = store.create_upload_session("alice", "p1", "a/b", 5, _sha256(b"hello"))
        with pytest.raises(ArtifactSessionError, match="exceeds expected total size"):
            store.append_chunk(s.session_id, 0, b"hello world", owner="alice")

    def test_empty_chunk_rejected(self) -> None:
        store, _ = _make_store()
        s = store.create_upload_session("alice", "p1", "a/b", 5, _sha256(b"hello"))
        with pytest.raises(ArtifactSessionError, match="chunk data must not be empty"):
            store.append_chunk(s.session_id, 0, b"", owner="alice")

    def test_negative_offset_rejected(self) -> None:
        store, _ = _make_store()
        s = store.create_upload_session("alice", "p1", "a/b", 5, _sha256(b"hello"))
        with pytest.raises(ArtifactSessionError, match="offset must be non-negative"):
            store.append_chunk(s.session_id, -1, b"x", owner="alice")

    def test_expired_session_rejected(self) -> None:
        store, _ = _make_store()
        s = store.create_upload_session("alice", "p1", "a/b", 5, _sha256(b"hello"), expires_after_seconds=-1)
        with pytest.raises(ArtifactSessionError, match="expired"):
            store.get_session(s.session_id, owner="alice")

    def test_declared_size_mismatch_on_finalize(self) -> None:
        store, _ = _make_store()
        data = b"hello"
        s = store.create_upload_session("alice", "p1", "a/b", len(data), _sha256(data))
        store.append_chunk(s.session_id, 0, data, owner="alice")
        with pytest.raises(ArtifactChecksumError, match="declared size"):
            store.finalize_upload(s.session_id, owner="alice", declared_size=99)

    def test_declared_checksum_mismatch_on_finalize(self) -> None:
        store, _ = _make_store()
        data = b"hello"
        s = store.create_upload_session("alice", "p1", "a/b", len(data), _sha256(data))
        store.append_chunk(s.session_id, 0, data, owner="alice")
        with pytest.raises(ArtifactChecksumError, match="declared checksum"):
            store.finalize_upload(s.session_id, owner="alice", declared_checksum=_sha256(b"other"))

    def test_concurrent_finalize_is_safe(self) -> None:
        import concurrent.futures
        store, _ = _make_store()
        data = b"concurrent"
        checksum = _sha256(data)
        results = []

        def finalize_once(index: int) -> dict:
            s = store.create_upload_session(f"user{index}", "p1", "a/b", len(data), checksum)
            store.append_chunk(s.session_id, 0, data, owner=f"user{index}")
            return store.finalize_upload(s.session_id, owner=f"user{index}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(finalize_once, i) for i in range(4)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        # All should succeed; at least one should be reused.
        assert all(r["status"] == "finalized" for r in results)
        assert any(r["reused"] for r in results)


class TestPrivateLocationResolution:
    def test_resolves_finalized_content_without_public_path_leak(self) -> None:
        store, root = _make_store()
        data = b"selena-binary"
        session = store.create_upload_session(
            "alice", "p1", "build/selena.exe", len(data), _sha256(data)
        )
        store.append_chunk(session.session_id, 0, data, owner="alice")
        finalized = store.finalize_upload(session.session_id, owner="alice")

        location = store.resolve_location(finalized["storage_ref"])
        assert location.read_bytes() == data
        assert root.resolve() in location.resolve().parents
        assert str(location) not in str(store.lookup_by_storage_ref(finalized["storage_ref"]))

    def test_rejects_content_replaced_after_finalize(self) -> None:
        store, _ = _make_store()
        data = b"selena-binary"
        session = store.create_upload_session(
            "alice", "p1", "build/selena.exe", len(data), _sha256(data)
        )
        store.append_chunk(session.session_id, 0, data, owner="alice")
        finalized = store.finalize_upload(session.session_id, owner="alice")
        location = store.resolve_location(finalized["storage_ref"])
        location.write_bytes(b"tampered-binary")

        with pytest.raises(ArtifactStoreError, match="size mismatch|checksum mismatch"):
            store.resolve_location(finalized["storage_ref"])
