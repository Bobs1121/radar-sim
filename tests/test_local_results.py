from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

import core.local_results as local_results
from core.local_results import ResultCatalog, ResultCatalogError


def _catalog(tmp_path: Path, *, now: float = 100.0) -> tuple[ResultCatalog, Path]:
    allowed = tmp_path / "controlled"
    allowed.mkdir()
    catalog = ResultCatalog(
        tmp_path / "private-store",
        tmp_path / "catalog.db",
        allowed_source_root=allowed,
        now_fn=lambda: now,
    )
    return catalog, allowed


def _result_tree(root: Path) -> Path:
    result = root / "run-1"
    (result / "nested").mkdir(parents=True)
    (result / "summary.json").write_text('{"ok":true}\n', encoding="utf-8")
    (result / "nested" / "output.mf4").write_bytes(b"mf4-result")
    return result


def test_default_result_catalog_enables_configurable_free_space_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RSIM_HOME", str(tmp_path / "rsim-home"))
    monkeypatch.setenv("RSIM_RESULT_MIN_FREE_BYTES", "123456")
    catalog = local_results.default_result_catalog()
    assert catalog._min_free_bytes == 123456


def test_publish_is_deterministic_and_public_metadata_is_path_free(tmp_path: Path) -> None:
    catalog, allowed = _catalog(tmp_path)
    source = _result_tree(allowed)

    first = catalog.publish(
        owner="alice", run_ref="local-run:one", source_root=source,
        files=["summary.json", "nested/output.mf4"], retain_until=500,
    )
    second = catalog.publish(
        owner="alice", run_ref="local-run:one", source_root=source,
        files=["nested/output.mf4", "summary.json"], retain_until=500,
    )

    assert first == second
    assert first.ref.startswith("result:sha256:")
    assert first.file_count == 2
    public = first.public_dict
    assert "owner" not in public
    assert "storage_ref" not in public
    assert "location" not in public
    assert str(tmp_path) not in repr(public)
    archive = catalog.resolve_archive(first.ref, owner="alice")
    assert archive.read_bytes()
    assert "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest() == first.archive_checksum
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.namelist() == ["nested/output.mf4", "summary.json"]


def test_same_content_produces_identical_archive_checksum_across_runs(tmp_path: Path) -> None:
    catalog, allowed = _catalog(tmp_path)
    source = _result_tree(allowed)

    first = catalog.publish(owner="alice", run_ref="local-run:one", source_root=source, files=["summary.json"])
    second = catalog.publish(owner="alice", run_ref="local-run:two", source_root=source, files=["summary.json"])

    assert first.ref != second.ref
    assert first.archive_checksum == second.archive_checksum
    assert catalog.resolve_archive(first.ref, owner="alice") == catalog.resolve_archive(second.ref, owner="alice")


def test_owner_isolation_applies_to_get_list_and_archive_resolution(tmp_path: Path) -> None:
    catalog, allowed = _catalog(tmp_path)
    source = _result_tree(allowed)
    result = catalog.publish(owner="alice", run_ref="local-run:one", source_root=source, files=["summary.json"])

    with pytest.raises(ResultCatalogError, match="unavailable"):
        catalog.get(result.ref, owner="bob")
    with pytest.raises(ResultCatalogError, match="unavailable"):
        catalog.resolve_archive(result.ref, owner="bob")
    assert catalog.list(owner="bob") == ()


@pytest.mark.parametrize(
    "relative",
    ["../secret.txt", "/absolute.txt", "C:/escape.txt", "nested\\output.mf4", "nested//output.mf4"],
)
def test_rejects_path_traversal_and_non_normalized_paths(tmp_path: Path, relative: str) -> None:
    catalog, allowed = _catalog(tmp_path)
    source = _result_tree(allowed)
    with pytest.raises(ResultCatalogError):
        catalog.publish(owner="alice", run_ref="local-run:one", source_root=source, files=[relative])


def test_rejects_source_outside_controlled_root(tmp_path: Path) -> None:
    catalog, _ = _catalog(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.txt").write_text("unsafe", encoding="utf-8")
    with pytest.raises(ResultCatalogError, match="controlled root"):
        catalog.publish(owner="alice", run_ref="local-run:one", source_root=outside, files=["result.txt"])


def test_rejects_symlink_and_directory_members(tmp_path: Path) -> None:
    catalog, allowed = _catalog(tmp_path)
    source = _result_tree(allowed)
    (source / "folder").mkdir()
    with pytest.raises(ResultCatalogError, match="regular non-symlink"):
        catalog.publish(owner="alice", run_ref="local-run:one", source_root=source, files=["folder"])

    link = source / "linked.txt"
    try:
        link.symlink_to(source / "summary.json")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ResultCatalogError, match="regular non-symlink"):
        catalog.publish(owner="alice", run_ref="local-run:two", source_root=source, files=["linked.txt"])


def test_retain_until_hides_expired_results_but_can_be_audited(tmp_path: Path) -> None:
    catalog, allowed = _catalog(tmp_path, now=100)
    source = _result_tree(allowed)
    result = catalog.publish(
        owner="alice", run_ref="local-run:one", source_root=source,
        files=["summary.json"], retain_until=150,
    )

    assert catalog.get(result.ref, owner="alice", now=150) == result
    with pytest.raises(ResultCatalogError, match="expired"):
        catalog.get(result.ref, owner="alice", now=151)
    assert catalog.list(owner="alice", now=151) == ()
    assert catalog.list(owner="alice", now=151, include_expired=True) == (result,)


def test_same_run_cannot_be_replaced_with_different_content(tmp_path: Path) -> None:
    catalog, allowed = _catalog(tmp_path)
    source = _result_tree(allowed)
    catalog.publish(owner="alice", run_ref="local-run:one", source_root=source, files=["summary.json"])
    (source / "summary.json").write_text('{"ok":false}\n', encoding="utf-8")

    with pytest.raises(ResultCatalogError, match="immutable content"):
        catalog.publish(owner="alice", run_ref="local-run:one", source_root=source, files=["summary.json"])


def test_publish_rejects_source_identity_change_during_single_pass_archive(
    tmp_path: Path, monkeypatch
) -> None:
    catalog, allowed = _catalog(tmp_path)
    source = _result_tree(allowed)
    original = local_results._source_signature
    calls = 0

    def changed_after_open(details):
        nonlocal calls
        calls += 1
        signature = original(details)
        if calls == 4:
            return (*signature[:-1], signature[-1] + 1)
        return signature

    monkeypatch.setattr(local_results, "_source_signature", changed_after_open)

    with pytest.raises(ResultCatalogError, match="changed while it was archived"):
        catalog.publish(
            owner="alice", run_ref="local-run:race", source_root=source,
            files=["summary.json"],
        )


def test_collect_expired_removes_only_expired_rows_and_shared_files(tmp_path: Path) -> None:
    catalog, allowed = _catalog(tmp_path, now=100)
    source = _result_tree(allowed)
    shared = catalog.publish(
        owner="alice", run_ref="local-run:shared", source_root=source,
        files=["summary.json"], retain_until=0,
    )
    expiring = catalog.publish(
        owner="alice", run_ref="local-run:expiring", source_root=source,
        files=["nested/output.mf4"], retain_until=150,
    )
    future = catalog.publish(
        owner="alice", run_ref="local-run:future", source_root=source,
        files=["summary.json"], retain_until=1000,
    )

    assert catalog.collect_expired(now=200) == 1
    # Never-expire rows and future rows are preserved.
    remaining = set(catalog.list(owner="alice", now=200, include_expired=True))
    assert remaining == {future, shared}
    # The expiring archive file is actually removed from disk.
    assert not list(
        (tmp_path / "private-store" / "content").rglob(
            expiring.archive_checksum.removeprefix("sha256:") + ".zip"
        )
    )
    # A second pass is a no-op.
    assert catalog.collect_expired(now=200) == 0


def test_watermark_blocks_publish_below_free_space(tmp_path: Path, monkeypatch) -> None:
    allowed = tmp_path / "controlled"
    allowed.mkdir()
    catalog = ResultCatalog(
        tmp_path / "private-store",
        tmp_path / "catalog.db",
        allowed_source_root=allowed,
        min_free_bytes=10_000_000,
    )
    source = _result_tree(allowed)
    monkeypatch.setattr(
        local_results.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 1_000, "used": 0, "total": 1_000_000})(),
    )
    with pytest.raises(ResultCatalogError, match="free-space watermark"):
        catalog.publish(owner="alice", run_ref="local-run:one", source_root=source, files=["summary.json"])
