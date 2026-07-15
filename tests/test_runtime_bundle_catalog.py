import pytest

from core.runtime_bundle import RuntimeSourceEvidence, discover_runtime_bundle
from core.runtime_bundle_catalog import (
    RuntimeBundleCatalog,
    RuntimeBundleCatalogError,
    RuntimeBundleRecord,
)


def _record(tmp_path, *, storage_ref="shared://selena-bundles/demo/a/runtime-bundle.zip"):
    output = tmp_path / "build"
    output.mkdir(exist_ok=True)
    (output / "selena.exe").write_bytes(b"exe")
    runtime = tmp_path / "Runtime.xml"
    runtime.write_text("<runtime/>", encoding="utf-8")
    manifest = discover_runtime_bundle(
        output / "selena.exe", runtime,
        source=RuntimeSourceEvidence("main", "a" * 40, False, "", "Release", "tool", "recipe:demo"),
        created_at=1,
    ).manifest
    return RuntimeBundleRecord(manifest, "demo", storage_ref, "sha256:" + "b" * 64, 100, "alice", "agent-1")


def test_shared_runtime_bundle_catalog_is_idempotent_and_public_hides_adapter(tmp_path):
    catalog = RuntimeBundleCatalog(tmp_path / "catalog.db")
    record = _record(tmp_path)
    assert catalog.register(record) == record
    assert catalog.register(record) == record
    public = catalog.get(record.manifest.id).public_dict
    assert public["visibility"] == "shared"
    assert public["source"].get("adapter_key") is None
    assert catalog.get_by_storage_ref(record.storage_ref) == record
    assert catalog.list() == [record]


def test_catalog_rejects_same_bundle_with_different_storage(tmp_path):
    catalog = RuntimeBundleCatalog(tmp_path / "catalog.db")
    record = _record(tmp_path)
    catalog.register(record)
    changed = RuntimeBundleRecord(
        record.manifest, "demo", "shared://selena-bundles/demo/b/runtime-bundle.zip",
        record.archive_checksum, record.archive_size, record.owner, record.created_by,
    )
    with pytest.raises(RuntimeBundleCatalogError, match="different metadata"):
        catalog.register(changed)
