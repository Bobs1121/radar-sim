from pathlib import Path

import pytest

from core.config_assets import ConfigAssetError, ConfigAssetStore, config_asset_id


def test_configuration_assets_are_reusable_owner_scoped_refs(tmp_path: Path):
    store = ConfigAssetStore(tmp_path / "assets", tmp_path / "catalog.db", now_fn=lambda: 10.0)
    first = store.put(owner="alice", kind="adapter", filename="adapter.txt", content=b"adapter=1\n")
    reused = store.put(owner="alice", kind="adapter", filename="adapter.txt", content=b"adapter=1\n")

    assert first == reused
    assert first.uri.startswith("config-asset://sha256/")
    assert config_asset_id(first.uri) == first.id
    assert store.resolve_location(first.uri, owner="alice", kind="adapter").read_bytes() == b"adapter=1\n"
    assert "location" not in first.public_dict
    with pytest.raises(ConfigAssetError, match="unavailable"):
        store.get(first.uri, owner="bob", kind="adapter")


def test_same_content_may_have_distinct_mandatory_roles(tmp_path: Path):
    store = ConfigAssetStore(tmp_path / "assets", tmp_path / "catalog.db")
    adapter = store.put(owner="alice", kind="adapter", filename="adapter.txt", content=b"same\n")
    mat_filter = store.put(owner="alice", kind="mat_filter", filename="signals.filter", content=b"same\n")

    assert adapter.id == mat_filter.id
    assert store.get(adapter.uri, owner="alice", kind="adapter").kind == "adapter"
    assert store.get(mat_filter.uri, owner="alice", kind="mat_filter").kind == "mat_filter"
    with pytest.raises(ConfigAssetError, match="kind is required"):
        store.get(adapter.uri, owner="alice")


@pytest.mark.parametrize(
    ("kind", "filename", "content"),
    [
        ("runtime_xml", "Runtime.xml", b"<runtime/>") ,
        ("adapter", "../adapter.txt", b"a"),
        ("mat_filter", "signals.filter", b"a\x00b"),
        ("adapter", "adapter.txt", b""),
    ],
)
def test_rejects_runtime_xml_unsafe_name_binary_or_empty(tmp_path: Path, kind, filename, content):
    store = ConfigAssetStore(tmp_path / "assets", tmp_path / "catalog.db")
    with pytest.raises(ConfigAssetError):
        store.put(owner="alice", kind=kind, filename=filename, content=content)
