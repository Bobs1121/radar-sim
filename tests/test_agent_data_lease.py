from pathlib import Path

import pytest

from core.agent_data_bindings import AgentDataBindingStore
from core.agent_data_lease import AgentDataLeaseError, AgentDataLeaseStore


def test_authorized_discovery_creates_path_free_immutable_lease(tmp_path: Path):
    root = tmp_path / "data"
    nested = root / "scene"
    nested.mkdir(parents=True)
    (nested / "a.MF4").write_bytes(b"mf4")
    bindings = AgentDataBindingStore(tmp_path / "bindings.db")
    binding = bindings.register(project="ovrs25", root_path=root)
    leases = AgentDataLeaseStore(tmp_path / "leases.db", now_fn=lambda: 10)
    lease = leases.create(
        {
            "project": "ovrs25",
            "data_binding_id": binding.binding_id,
            "data_path": str(root),
            "required_signals": [],
        },
        bindings,
        stage_id="stage_data",
        attempt=1,
    )
    assert lease.files[0].relative_path == "scene/a.MF4"
    assert lease.files[0].checksum.startswith("sha256:")
    assert str(root) not in str(lease.public_dict)
    assert leases.create(
        {
            "project": "ovrs25",
            "data_binding_id": binding.binding_id,
            "data_path": str(root),
        },
        bindings,
        stage_id="stage_data",
        attempt=1,
    ).lease_id == lease.lease_id


def test_lease_rejects_changed_file_before_upload(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    mf4 = root / "a.MF4"
    mf4.write_bytes(b"one")
    bindings = AgentDataBindingStore(tmp_path / "bindings.db")
    binding = bindings.register(project="ovrs25", root_path=root)
    leases = AgentDataLeaseStore(tmp_path / "leases.db")
    lease = leases.create(
        {"project": "ovrs25", "data_binding_id": binding.binding_id, "data_path": str(root)},
        bindings,
        stage_id="stage_data",
        attempt=1,
    )
    mf4.write_bytes(b"changed")
    with pytest.raises(AgentDataLeaseError, match="changed"):
        leases.get(lease.lease_id)


def test_lease_rejects_path_outside_bound_root(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.MF4").write_bytes(b"x")
    bindings = AgentDataBindingStore(tmp_path / "bindings.db")
    binding = bindings.register(project="ovrs25", root_path=root)
    with pytest.raises(AgentDataLeaseError, match="discovery"):
        AgentDataLeaseStore(tmp_path / "leases.db").create(
            {"project": "ovrs25", "data_binding_id": binding.binding_id, "data_path": str(outside)},
            bindings,
            stage_id="stage_data",
            attempt=1,
        )
