import hashlib
from pathlib import Path

import pytest

from core.dataset_store import (
    DatasetStore,
    DatasetStoreQuota,
    DatasetUploadChecksumError,
    DatasetUploadPathError,
    DatasetUploadQuotaError,
    DatasetUploadSessionError,
)


def _file(path: str, data: bytes) -> dict:
    return {
        "relative_path": path,
        "size": len(data),
        "checksum": "sha256:" + hashlib.sha256(data).hexdigest(),
    }


def _store(tmp_path: Path, **quota) -> DatasetStore:
    values = {
        "min_free_bytes": 0,
        "chunk_size": 4,
        "max_file_size": 100,
        "max_total_size": 100,
        "max_owner_reserved_bytes": 100,
    }
    values.update(quota)
    return DatasetStore(tmp_path / "store", quota=DatasetStoreQuota(**values), now_fn=lambda: 100)


def test_multifile_upload_resumes_and_finalizes_without_public_physical_path(tmp_path: Path):
    store = _store(tmp_path)
    first = b"abcdef"
    second = b"12"
    session = store.create_session(
        owner="alice",
        project="ovrs25",
        files=[_file("scene/a.MF4", first), _file("b.mf4", second)],
    )

    by_path = {item.relative_path: item for item in session.files}
    session = store.append_file(
        session.session_id, by_path["scene/a.MF4"].file_id, owner="alice", offset=0, data=first[:4]
    )
    assert next(item for item in session.files if item.relative_path == "scene/a.MF4").received_bytes == 4
    store.append_file(
        session.session_id, by_path["scene/a.MF4"].file_id, owner="alice", offset=4, data=first[4:]
    )
    store.append_file(session.session_id, by_path["b.mf4"].file_id, owner="alice", offset=0, data=second)

    result = store.finalize(session.session_id, owner="alice")
    public = store.get_session(session.session_id, owner="alice").to_dict()
    assert result.storage_ref.startswith("shared://datasets/ovrs25/")
    assert result.source_location not in str(public)
    assert (Path(result.source_location) / "scene" / "a.MF4").read_bytes() == first
    assert store.finalize(session.session_id, owner="alice").reused is True


def test_chunk_retry_is_idempotent_but_different_data_is_rejected(tmp_path: Path):
    store = _store(tmp_path)
    data = b"abcd"
    session = store.create_session(owner="alice", project="ovrs25", files=[_file("a.MF4", data)])
    file_id = session.files[0].file_id
    store.append_file(session.session_id, file_id, owner="alice", offset=0, data=data)
    retried = store.append_file(session.session_id, file_id, owner="alice", offset=0, data=data)
    assert retried.files[0].received_bytes == 4
    with pytest.raises(DatasetUploadSessionError):
        store.append_file(session.session_id, file_id, owner="alice", offset=0, data=b"wxyz")


def test_owner_isolation_uses_unavailable_error(tmp_path: Path):
    store = _store(tmp_path)
    session = store.create_session(owner="alice", project="ovrs25", files=[_file("a.MF4", b"x")])
    with pytest.raises(DatasetUploadSessionError, match="unavailable"):
        store.get_session(session.session_id, owner="bob")


def test_same_manifest_has_owner_scoped_storage_reference(tmp_path: Path):
    store = _store(tmp_path)
    data = b"same"
    refs = []
    for owner in ("alice", "bob"):
        session = store.create_session(owner=owner, project="ovrs25", files=[_file("a.MF4", data)])
        store.append_file(session.session_id, session.files[0].file_id, owner=owner, offset=0, data=data)
        refs.append(store.finalize(session.session_id, owner=owner).storage_ref)
    assert refs[0] != refs[1]


@pytest.mark.parametrize(
    "path",
    [
        "../a.MF4",
        "/a.MF4",
        "D:/a.MF4",
        r"scene\a.MF4",
        "scene//a.MF4",
        "scene/aout.MF4",
        "scene/a.txt",
        "CON.MF4",
        "scene/a:stream.MF4",
        "scene/a?.MF4",
    ],
)
def test_manifest_rejects_unsafe_or_non_input_paths(tmp_path: Path, path: str):
    store = _store(tmp_path)
    with pytest.raises(DatasetUploadPathError):
        store.create_session(owner="alice", project="ovrs25", files=[_file(path, b"x")])


def test_manifest_rejects_casefold_collision(tmp_path: Path):
    store = _store(tmp_path)
    with pytest.raises(DatasetUploadPathError, match="collide"):
        store.create_session(
            owner="alice",
            project="ovrs25",
            files=[_file("A.MF4", b"a"), _file("a.mf4", b"b")],
        )


def test_manifest_and_chunk_quota_are_enforced(tmp_path: Path):
    store = _store(tmp_path, max_total_size=3, max_owner_reserved_bytes=3)
    with pytest.raises(DatasetUploadQuotaError):
        store.create_session(owner="alice", project="ovrs25", files=[_file("a.MF4", b"1234")])

    store = _store(tmp_path / "second", chunk_size=2)
    session = store.create_session(owner="alice", project="ovrs25", files=[_file("a.MF4", b"123")])
    with pytest.raises(DatasetUploadSessionError, match="chunk size"):
        store.append_file(session.session_id, session.files[0].file_id, owner="alice", offset=0, data=b"123")


def test_finalize_rehashes_each_file(tmp_path: Path):
    store = _store(tmp_path)
    session = store.create_session(owner="alice", project="ovrs25", files=[_file("a.MF4", b"good")])
    store.append_file(session.session_id, session.files[0].file_id, owner="alice", offset=0, data=b"bad!")
    with pytest.raises(DatasetUploadChecksumError, match="checksum"):
        store.finalize(session.session_id, owner="alice")


def test_finalize_recovers_after_atomic_move_before_catalog_insert(tmp_path: Path):
    store = _store(tmp_path)
    data = b"recover"
    session = store.create_session(owner="alice", project="ovrs25", files=[_file("a.MF4", data)])
    store.append_file(session.session_id, session.files[0].file_id, owner="alice", offset=0, data=data[:4])
    store.append_file(session.session_id, session.files[0].file_id, owner="alice", offset=4, data=data[4:])
    staging = store._staging_path(session.session_id)
    target = store._content_path(session.owner, session.project, session.manifest_fingerprint)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging.replace(target)

    result = store.finalize(session.session_id, owner="alice")
    assert result.reused is True
    assert Path(result.source_location) == target


def test_existing_finalized_content_is_revalidated_before_reuse(tmp_path: Path):
    store = _store(tmp_path)
    data = b"good"
    first = store.create_session(owner="alice", project="ovrs25", files=[_file("a.MF4", data)])
    store.append_file(first.session_id, first.files[0].file_id, owner="alice", offset=0, data=data)
    finalized = store.finalize(first.session_id, owner="alice")
    (Path(finalized.source_location) / "a.MF4").write_bytes(b"evil")

    second = store.create_session(owner="alice", project="ovrs25", files=[_file("a.MF4", data)])
    store.append_file(second.session_id, second.files[0].file_id, owner="alice", offset=0, data=data)
    with pytest.raises(DatasetUploadChecksumError):
        store.finalize(second.session_id, owner="alice")


def test_expired_session_staging_is_cleaned_and_reservation_released(tmp_path: Path):
    clock = {"now": 100.0}
    quota = DatasetStoreQuota(
        min_free_bytes=0,
        chunk_size=4,
        max_file_size=100,
        max_total_size=100,
        max_owner_reserved_bytes=4,
        session_ttl_seconds=10,
    )
    store = DatasetStore(tmp_path / "store", quota=quota, now_fn=lambda: clock["now"])
    first = store.create_session(owner="alice", project="ovrs25", files=[_file("a.MF4", b"1234")])
    store.append_file(first.session_id, first.files[0].file_id, owner="alice", offset=0, data=b"12")
    assert store._staging_path(first.session_id).exists()
    clock["now"] = 111

    second = store.create_session(owner="alice", project="ovrs25", files=[_file("b.MF4", b"1234")])
    assert second.status == "active"
    assert not store._staging_path(first.session_id).exists()


def test_agent_upload_requires_trusted_stage_evidence(tmp_path: Path):
    store = _store(tmp_path)
    with pytest.raises(DatasetUploadSessionError, match="evidence"):
        store.create_session(
            owner="alice",
            project="ovrs25",
            files=[_file("a.MF4", b"x")],
            source_kind="agent_upload",
        )


def test_browser_upload_can_defer_checksum_to_streaming_server_finalize(tmp_path: Path):
    store = _store(tmp_path)
    data = b"browser-mf4"
    session = store.create_session(
        owner="alice",
        project="ovrs25",
        files=[{"relative_path": "nested/a.MF4", "size": len(data), "checksum": ""}],
    )
    assert session.to_dict()["files"][0]["expected_checksum"] == ""
    for offset in range(0, len(data), session.chunk_size):
        store.append_file(
            session.session_id,
            session.files[0].file_id,
            owner="alice",
            offset=offset,
            data=data[offset:offset + session.chunk_size],
        )

    result = store.finalize(session.session_id, owner="alice")
    expected = "sha256:" + hashlib.sha256(data).hexdigest()
    assert result.files[0].expected_checksum == expected
    assert result.manifest_fingerprint != session.manifest_fingerprint
    assert Path(result.source_location, "nested", "a.MF4").read_bytes() == data


def test_agent_upload_cannot_defer_checksum_to_server(tmp_path: Path):
    store = _store(tmp_path)
    with pytest.raises(DatasetUploadChecksumError, match="checksum"):
        store.create_session(
            owner="alice",
            project="ovrs25",
            files=[{"relative_path": "a.MF4", "size": 1, "checksum": ""}],
            source_kind="agent_upload",
            evidence_ref="stage:1",
        )
