from pathlib import Path

import pytest

from core.agent_data_bindings import (
    AgentDataBindingError,
    AgentDataBindingStore,
    candidate_data_binding_ids,
    make_data_binding_id,
)


def test_data_root_binding_is_path_free_public_and_authorizes_descendant(tmp_path: Path):
    root = tmp_path / "data"
    nested = root / "scene"
    nested.mkdir(parents=True)
    target = nested / "a.MF4"
    target.write_bytes(b"x")
    store = AgentDataBindingStore(tmp_path / "bindings.db", now_fn=lambda: 10)
    binding = store.register(project="ovrs25", root_path=root)

    assert str(root) not in str(binding.public_dict)
    assert store.authorize_path(
        project="ovrs25", binding_id=binding.binding_id, data_path=str(target)
    ) == target.resolve()


def test_register_is_idempotent_for_one_click_retry(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    store = AgentDataBindingStore(tmp_path / "bindings.db", now_fn=lambda: 10)

    first = store.register(project="ovrs25", root_path=root)
    second = store.register(project="ovrs25", root_path=root)

    assert second.binding_id == first.binding_id
    assert second.root_path == first.root_path


def test_data_root_binding_rejects_sibling_and_project_mismatch(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    sibling = tmp_path / "other"
    sibling.mkdir()
    store = AgentDataBindingStore(tmp_path / "bindings.db")
    binding = store.register(project="ovrs25", root_path=root)

    with pytest.raises(AgentDataBindingError, match="outside"):
        store.authorize_path(project="ovrs25", binding_id=binding.binding_id, data_path=str(sibling))
    with pytest.raises(AgentDataBindingError, match="unavailable"):
        store.authorize_path(project="other", binding_id=binding.binding_id, data_path=str(root))


def test_central_candidate_ids_match_windows_ancestor_root():
    candidates = candidate_data_binding_ids("ovrs25", r"D:\measurements\case\input.MF4")
    assert make_data_binding_id("ovrs25", r"D:\measurements") in candidates
    assert make_data_binding_id("ovrs25", r"D:\other") not in candidates


def test_non_windows_path_has_no_central_candidate_binding():
    assert candidate_data_binding_ids("ovrs25", "/mnt/data/case") == ()


def test_owner_device_binding_survives_internal_project_change(tmp_path: Path):
    root = tmp_path / "measurements"
    root.mkdir()
    store = AgentDataBindingStore(tmp_path / "bindings.db")

    binding = store.register(owner="alice", device_id="agent-1", root_path=root)
    assert binding.public_dict == {
        "id": binding.binding_id,
        "healthy": True,
        "owner": "alice",
        "device_id": "agent-1",
    }
    # Selena recognition is intentionally absent from the v2 identity.  A
    # changed internal project token still resolves the same owner/device root.
    assert store.get(binding.binding_id, project="new-recognized-project").binding_id == binding.binding_id
    assert store.list(project="other-project") == []
    assert store.list(owner="alice", device_id="agent-1")[0].binding_id == binding.binding_id


def test_legacy_project_binding_remains_readable_after_store_migration(tmp_path: Path):
    root = tmp_path / "measurements"
    root.mkdir()
    store = AgentDataBindingStore(tmp_path / "bindings.db")
    legacy = store.register(project="ovrs25", root_path=root)

    assert store.get(legacy.binding_id, project="ovrs25").project == "ovrs25"
    with pytest.raises(AgentDataBindingError, match="unavailable"):
        store.get(legacy.binding_id, project="other")
