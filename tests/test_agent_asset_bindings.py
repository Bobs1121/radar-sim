from pathlib import Path

import pytest

from core.agent_asset_bindings import (
    AgentAssetBindingError,
    AgentAssetBindingStore,
    candidate_asset_binding_ids,
    make_asset_binding_id,
)


def test_register_advertise_and_authorize_required_assets(tmp_path):
    root = tmp_path / "config"
    root.mkdir()
    runtime = root / "Runtime.xml"
    adapter = root / "adapter.txt"
    mat_filter = root / "signals.filter"
    runtime.write_text("<runtime/>", encoding="utf-8")
    adapter.write_text("adapter", encoding="utf-8")
    mat_filter.write_text("filter", encoding="utf-8")
    store = AgentAssetBindingStore(tmp_path / "bindings.db", now_fn=lambda: 10.0)

    binding = store.register(root)
    assert binding.public_dict == {"id": make_asset_binding_id(str(root)), "healthy": True}
    assert "root" not in binding.public_dict
    assert store.authorize_path(binding_id=binding.binding_id, asset_path=str(runtime), role="runtime_xml") == runtime.resolve()
    assert store.authorize_any(asset_path=str(adapter), role="adapter")[1] == adapter.resolve()
    assert store.authorize_any(asset_path=str(mat_filter), role="mat_filter")[1] == mat_filter.resolve()


def test_rejects_outside_symlink_and_wrong_runtime_type(tmp_path):
    root = tmp_path / "config"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    wrong = root / "Runtime.txt"
    wrong.write_text("x", encoding="utf-8")
    store = AgentAssetBindingStore(tmp_path / "bindings.db")
    binding = store.register(root)

    with pytest.raises(AgentAssetBindingError, match="outside"):
        store.authorize_path(binding_id=binding.binding_id, asset_path=str(outside), role="adapter")
    with pytest.raises(AgentAssetBindingError, match="file type"):
        store.authorize_path(binding_id=binding.binding_id, asset_path=str(wrong), role="runtime_xml")
    link = root / "link.xml"
    try:
        link.symlink_to(outside)
    except OSError:
        return
    with pytest.raises(AgentAssetBindingError, match="readable file"):
        store.authorize_path(binding_id=binding.binding_id, asset_path=str(link), role="runtime_xml")


def test_candidate_ids_include_parent_ancestors_without_paths():
    values = candidate_asset_binding_ids(r"D:\\data\\config\\Runtime.xml")
    assert values
    assert values[0] == make_asset_binding_id(r"D:\\data\\config")
    assert all(value.startswith("asset-root:sha256:") for value in values)
    assert candidate_asset_binding_ids("relative/Runtime.xml") == ()
