from pathlib import Path

import pytest

from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseError, AgentRuntimeBundleLeaseStore
from core.runtime_bundle import RuntimeSourceEvidence, discover_runtime_bundle
from core.runtime_bundle_archive import stage_runtime_bundle_archive


def _bundle(tmp_path: Path):
    output = tmp_path / "build"
    output.mkdir()
    (output / "selena.exe").write_bytes(b"exe")
    (output / "runtime.dll").write_bytes(b"dll")
    runtime = tmp_path / "Runtime.xml"
    runtime.write_text("<runtime/>", encoding="utf-8")
    bundle = discover_runtime_bundle(
        output / "selena.exe",
        runtime,
        source=RuntimeSourceEvidence("feature/a", "a" * 40, False, "", "Release", "tool", "recipe:demo"),
        created_at=10,
    )
    return bundle, stage_runtime_bundle_archive(bundle, tmp_path / "staging")


def test_runtime_bundle_lease_is_path_free_idempotent_and_revalidated(tmp_path):
    bundle, archive = _bundle(tmp_path)
    store = AgentRuntimeBundleLeaseStore(tmp_path / "leases.db", now_fn=lambda: 20.0)
    first = store.create(
        project="demo", workspace_binding_id="workspace:sha256:" + "a" * 24,
        build_stage_id="stage-1", build_attempt=1, manifest=bundle.manifest, archive=archive,
    )
    second = store.create(
        project="demo", workspace_binding_id="workspace:sha256:" + "a" * 24,
        build_stage_id="stage-1", build_attempt=1, manifest=bundle.manifest, archive=archive,
    )
    assert first.lease_id == second.lease_id
    assert "archive_path" not in first.public_dict
    assert str(tmp_path) not in str(first.public_dict)
    assert first.public_dict["runtime_bundle"]["source"].get("adapter_key") is None
    uploaded = store.mark_uploaded(first.lease_id, "shared://selena-bundles/demo/runtime-bundle.zip")
    assert uploaded.status == "uploaded"

    archive.path.write_bytes(b"changed")
    with pytest.raises(AgentRuntimeBundleLeaseError, match="changed"):
        store.get(first.lease_id)


def test_runtime_bundle_lease_evidence_and_expiry_are_enforced(tmp_path):
    bundle, archive = _bundle(tmp_path)
    now = [20.0]
    store = AgentRuntimeBundleLeaseStore(tmp_path / "leases.db", now_fn=lambda: now[0])
    lease = store.create(
        project="demo", workspace_binding_id="workspace:sha256:" + "a" * 24,
        build_stage_id="stage-1", build_attempt=1, manifest=bundle.manifest, archive=archive, ttl_seconds=5,
    )
    with pytest.raises(AgentRuntimeBundleLeaseError, match="evidence"):
        store.get(lease.lease_id, build_evidence_ref="other:1")
    now[0] = 26.0
    with pytest.raises(AgentRuntimeBundleLeaseError, match="expired"):
        store.get(lease.lease_id)
