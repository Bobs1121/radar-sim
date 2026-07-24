import os
from pathlib import Path

import pytest

from core.datasets import (
    DatasetCatalog,
    DatasetDiscoveryCancelled,
    DatasetError,
    DatasetFileRef,
    classify_data_path,
    dataset_fingerprint,
    discover_dataset_files,
    resolve_shared_data,
)
from core.shared_namespace import SharedNamespace, SharedNamespaceRegistry


def _registry(source: Path) -> SharedNamespaceRegistry:
    return SharedNamespaceRegistry(
        [SharedNamespace("test", r"\\server\share", source.as_posix(), r"\\worker\share")]
    )


@pytest.mark.parametrize(
    ("path", "route"),
    [
        (r"D:\data\case", "agent"),
        (r"\\server\share\case", "shared"),
        ("//server/share/case", "shared"),
        ("shared://datasets/demo/a", "shared"),
        ("/mnt/cluster/case", "central"),
        ("BYD_SR", "unknown"),
    ],
)
def test_classify_data_path_is_syntax_only(path, route):
    assert classify_data_path(path) == route


def test_discover_dataset_files_recurses_and_excludes_outputs(tmp_path: Path):
    source = tmp_path / "case"
    nested = source / "level1" / "level2"
    nested.mkdir(parents=True)
    (nested / "a.MF4").write_bytes(b"header VehicleSpeed tail")
    (nested / "aout.MF4").write_bytes(b"generated")
    (source / "b.mf4").write_bytes(b"VehicleSpeed")

    files = discover_dataset_files(source, ["VehicleSpeed"], max_read_mb=1)

    assert [item.relative_path for item in files] == ["b.mf4", "level1/level2/a.MF4"]
    assert all(item.signal_status == "present" for item in files)
    assert all(not item.checksum for item in files)


def test_discover_uploaded_files_can_calculate_checksums(tmp_path: Path):
    mf4 = tmp_path / "input.MF4"
    mf4.write_bytes(b"mf4-data")
    files = discover_dataset_files(mf4, checksum=True)
    assert files[0].relative_path == "input.MF4"
    assert files[0].checksum.startswith("sha256:")


def test_large_file_checksum_stops_cooperatively_between_chunks(tmp_path: Path):
    mf4 = tmp_path / "large.MF4"
    mf4.write_bytes(b"x" * (3 * 1024 * 1024))
    checks = 0

    def cancel_after_first_chunk() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(DatasetDiscoveryCancelled, match="cancelled"):
        discover_dataset_files(
            mf4,
            checksum=True,
            cancel_requested=cancel_after_first_chunk,
        )
    assert checks >= 3


def test_dataset_file_rejects_absolute_or_traversal_path():
    with pytest.raises(DatasetError):
        DatasetFileRef("D:/secret.MF4", 1)
    with pytest.raises(DatasetError):
        DatasetFileRef("../secret.MF4", 1)


def test_shared_dataset_catalog_hides_physical_path_from_public_ref(tmp_path: Path):
    source = tmp_path / "shared"
    source.mkdir()
    (source / "a.MF4").write_bytes(b"data")
    catalog = DatasetCatalog(tmp_path / "datasets.db", now_fn=lambda: 100)

    outcome = resolve_shared_data(
        catalog,
        _registry(source),
        owner="alice",
        project="ovrs25",
        source_path=r"\\server\share",
    )

    assert outcome.status == "resolved"
    public = outcome.dataset.to_dict()
    assert public["storage_ref"].startswith("shared-path:sha256:")
    assert str(source) not in str(public)
    assert catalog.resolve_location(outcome.dataset.id, owner="alice") == r"\\worker\share"
    assert catalog.resolve_probe_location(outcome.dataset.id, owner="alice") == source.as_posix()


def test_shared_dataset_missing_path_returns_actionable_resolution(tmp_path: Path):
    catalog = DatasetCatalog(tmp_path / "datasets.db")
    missing = tmp_path / "missing"
    outcome = resolve_shared_data(
        catalog,
        _registry(missing),
        owner="alice",
        project="ovrs25",
        source_path=r"\\server\share",
    )
    assert outcome.status == "needs_input"
    assert outcome.code == "shared_dataset_unavailable"


def test_untrusted_unc_is_never_probed(tmp_path: Path):
    source = tmp_path / "shared"
    source.mkdir()
    (source / "a.MF4").write_bytes(b"data")
    outcome = resolve_shared_data(
        DatasetCatalog(tmp_path / "datasets.db"),
        _registry(source),
        owner="alice",
        project="ovrs25",
        source_path=r"\\other\share",
    )
    assert outcome.status == "needs_input"
    assert "authorized" in outcome.action


def test_discovery_inventory_ignores_simulation_limit(tmp_path: Path):
    for index in range(3):
        (tmp_path / f"{index}.MF4").write_bytes(str(index).encode())
    assert len(discover_dataset_files(tmp_path, limit=1)) == 3


def test_shared_fingerprint_changes_with_mtime_without_file_hash(tmp_path: Path):
    path = tmp_path / "a.MF4"
    path.write_bytes(b"same")
    first = discover_dataset_files(tmp_path)
    current = path.stat().st_mtime_ns
    os.utime(path, ns=(current + 1_000_000_000, current + 1_000_000_000))
    second = discover_dataset_files(tmp_path)
    assert first[0].checksum == ""
    assert first[0].mtime_ns < second[0].mtime_ns
    assert dataset_fingerprint(first) != dataset_fingerprint(second)


def test_dataset_ref_rejects_case_insensitive_path_collision(tmp_path: Path):
    catalog = DatasetCatalog(tmp_path / "datasets.db")
    with pytest.raises(DatasetError, match="case-insensitively"):
        catalog.register_uploaded(
            project="ovrs25",
            owner="alice",
            source_kind="central_upload",
            source_path="private",
            storage_ref="shared://datasets/ovrs25/opaque",
            files=(
                DatasetFileRef("A.MF4", 1, "sha256:" + "a" * 64),
                DatasetFileRef("a.mf4", 1, "sha256:" + "b" * 64),
            ),
        )
