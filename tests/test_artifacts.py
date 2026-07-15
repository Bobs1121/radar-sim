from __future__ import annotations

import concurrent.futures
import json
import sqlite3

import pytest

from core.artifacts import ArtifactAccessError, ArtifactCatalog, ArtifactValidationError, SelenaArtifact
from core.control_service import ControlService


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64


def artifact(**patch) -> SelenaArtifact:
    data = {
        "id": "artifact-a",
        "project": "demo",
        "owner": "alice",
        "visibility": "shared",
        "branch": "main",
        "commit": "1" * 40,
        "source_kind": "branch",
        "dirty": False,
        "dirty_fingerprint": "",
        "source_changed_during_build": False,
        "build_mode": "Release",
        "toolchain_fingerprint": "toolchain:v1",
        "binary_checksum": SHA_A,
        "interface_manifest": {"interfaces": ["if-a"]},
        "signal_manifest": {"signals": ["sig-a"]},
        "storage_ref": "artifact://demo/a",
        "accessibility": "cluster",
        "health": "ready",
        "created_by": "builder-1",
        "created_at": 100.0,
        "retain_until": 1000.0,
    }
    data.update(patch)
    if "storage_ref" not in patch and "id" in patch:
        suffix = str(data["id"] or data["binary_checksum"][-12:])
        data["storage_ref"] = f"artifact://demo/{suffix}"
    return SelenaArtifact(**data)


def test_schema_is_additive_and_can_share_control_db(tmp_path):
    db_path = tmp_path / "shared.db"
    ControlService(db_path)

    catalog = ArtifactCatalog(db_path)
    registered = catalog.register(artifact())

    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"jobs", "tasks", "selena_artifacts"} <= tables
    assert catalog.get(registered.id).binary_checksum == SHA_A


def test_clean_shared_register_preserves_distinct_user_paths(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "catalog.db")
    first = catalog.register(artifact(id="first", storage_ref="artifact://first", created_at=100))
    second = catalog.register(artifact(id="second", storage_ref="artifact://second", created_at=200))

    assert second != first
    assert catalog.get(first.id).storage_ref == "artifact://first"
    assert {item.id for item in catalog.list(project="demo", owner="alice")} == {"first", "second"}


def test_same_storage_ref_and_checksum_is_idempotent(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "catalog.db")
    first = catalog.register(artifact(id="first", storage_ref="shared://selena/demo/team/a/selena.exe"))
    retried = catalog.register(artifact(id="retry", storage_ref=first.storage_ref))
    assert retried.id == first.id


def test_same_storage_ref_with_different_checksum_conflicts(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "catalog.db")
    ref = "shared://selena/demo/team/a/selena.exe"
    catalog.register(artifact(id="first", storage_ref=ref))
    with pytest.raises(ArtifactValidationError, match="storage_ref.*different identity"):
        catalog.register(artifact(id="second", storage_ref=ref, binary_checksum=SHA_B))


def test_private_identity_is_isolated_by_owner(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "catalog.db")
    alice = catalog.register(artifact(id="", visibility="private", owner="alice", storage_ref="artifact://demo/alice"))
    bob = catalog.register(artifact(id="", visibility="private", owner="bob", storage_ref="artifact://demo/bob"))

    assert alice.id != bob.id
    assert alice.binary_checksum == bob.binary_checksum == SHA_A
    assert catalog.verify_access(alice.id, owner="alice", now=100).id == alice.id
    with pytest.raises(ArtifactAccessError, match="private"):
        catalog.verify_access(alice.id, owner="bob")


def test_dirty_and_source_changed_are_forced_private_and_not_recommended(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "catalog.db")
    dirty = catalog.register(artifact(id="dirty", visibility="shared", dirty=True, dirty_fingerprint="sha256:dirty"))
    changed = catalog.register(
        artifact(
            id="changed",
            binary_checksum=SHA_B,
            visibility="shared",
            source_changed_during_build=True,
        )
    )
    clean = catalog.register(artifact(id="clean", binary_checksum=SHA_C, created_at=300))

    assert dirty.visibility == "private"
    assert changed.visibility == "private"
    assert [item.id for item in catalog.recommend(project="demo", owner="alice", target_accessibility="cluster", now=100)] == ["clean"]


def test_recommend_filters_visibility_accessibility_retain_and_build_mode(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "catalog.db")
    catalog.register(artifact(id="shared-cluster", binary_checksum=SHA_A, accessibility="shared", created_at=300))
    catalog.register(artifact(id="local", binary_checksum=SHA_B, accessibility="local", created_at=400))
    catalog.register(artifact(id="debug", binary_checksum=SHA_C, build_mode="Debug", created_at=500))
    catalog.register(
        artifact(
            id="expired",
            binary_checksum="sha256:" + "d" * 64,
            retain_until=50,
            created_at=600,
        )
    )
    catalog.register(
        artifact(
            id="private-bob",
            binary_checksum="sha256:" + "e" * 64,
            visibility="private",
            owner="bob",
            created_at=700,
        )
    )

    cluster = catalog.recommend(project="demo", owner="alice", build_mode="Release", target_accessibility="cluster", now=100)
    assert [item.id for item in cluster] == ["shared-cluster"]

    local = catalog.recommend(project="demo", owner="alice", build_mode="Release", target_accessibility="local", now=100)
    assert [item.id for item in local] == ["local"]

    bob = catalog.recommend(project="demo", owner="bob", build_mode="Release", target_accessibility="cluster", now=100)
    assert [item.id for item in bob] == ["private-bob", "shared-cluster"]


def test_artifact_is_deeply_immutable_and_to_dict_returns_new_objects():
    item = artifact(interface_manifest={"nested": {"values": ["a"]}})

    with pytest.raises(Exception):
        item.project = "other"
    with pytest.raises(TypeError):
        item.interface_manifest["nested"] = {}
    with pytest.raises(TypeError):
        item.interface_manifest["nested"]["values"] = []

    first = item.to_dict()
    second = item.to_dict()
    first["interface_manifest"]["nested"]["values"].append("mutated")
    assert second["interface_manifest"]["nested"]["values"] == ["a"]
    assert item.to_dict()["interface_manifest"]["nested"]["values"] == ["a"]


def test_concurrent_register_returns_one_idempotent_record(tmp_path):
    db_path = tmp_path / "race.db"
    payload = artifact(id="").to_dict()

    def register_once(_index: int) -> str:
        return ArtifactCatalog(db_path).register(payload).id

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(register_once, range(16)))

    assert len(set(ids)) == 1
    assert len(ArtifactCatalog(db_path).list(project="demo", owner="alice")) == 1


def test_legacy_explicit_artifact_id_requires_checksum_and_uses_logical_storage_ref(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "catalog.db")
    registered = catalog.register(
        artifact(
            id="legacy:demo:existing",
            source_kind="legacy",
            storage_ref="legacy://demo/existing",
        )
    )
    snapshot_json = json.dumps(catalog.snapshot(project="demo", owner="alice"), sort_keys=True)

    assert registered.id == "legacy:demo:existing"
    assert "legacy://demo/existing" in snapshot_json
    assert "C:/shared/selena.exe" not in snapshot_json
    with pytest.raises(ArtifactValidationError, match="binary_checksum"):
        catalog.register(artifact(id="bad", binary_checksum="not-a-checksum"))


def test_ownerless_reads_never_expose_private_artifacts(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "catalog.db")
    shared = catalog.register(artifact(id="shared", binary_checksum=SHA_A))
    private = catalog.register(
        artifact(id="private", binary_checksum=SHA_B, visibility="private", owner="alice")
    )

    assert [item.id for item in catalog.list(project="demo")] == [shared.id]
    assert [item["id"] for item in catalog.snapshot(project="demo")] == [shared.id]
    with pytest.raises(ArtifactAccessError, match="private"):
        catalog.get(private.id)
    assert catalog.get(private.id, owner="alice").id == private.id
    assert catalog.get_privileged(private.id).id == private.id


def test_storage_ref_lookup_is_first_class_and_preserves_private_access(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "catalog.db")
    shared = catalog.register(
        artifact(id="shared-path", storage_ref="shared://selena/demo/team/shared/selena.exe")
    )
    private = catalog.register(
        artifact(
            id="private-path",
            visibility="private",
            owner="alice",
            dirty=True,
            dirty_fingerprint="sha256:dirty",
            storage_ref="shared://selena/demo/users/alice/wip/selena.exe",
        )
    )

    assert catalog.get_by_storage_ref(shared.storage_ref, owner="bob").id == shared.id
    assert catalog.verify_storage_access(
        shared.storage_ref, owner="bob", target_accessibility="cluster", now=100
    ).id == shared.id
    with pytest.raises(ArtifactAccessError, match="private"):
        catalog.get_by_storage_ref(private.storage_ref, owner="bob")
    assert catalog.get_by_storage_ref(private.storage_ref, owner="alice").id == private.id
    assert catalog.get_by_storage_ref_privileged(private.storage_ref).id == private.id


def test_explicit_id_collision_cannot_cross_private_owner_identity(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "catalog.db")
    catalog.register(artifact(id="fixed", visibility="private", owner="alice"))

    with pytest.raises(ArtifactValidationError, match="different identity"):
        catalog.register(artifact(id="fixed", visibility="private", owner="bob"))


def test_same_checksum_can_gain_distinct_local_and_cluster_locations(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "catalog.db")
    local = catalog.register(
        artifact(id="local-copy", accessibility="local", storage_ref="artifact://demo/local")
    )
    cluster = catalog.register(
        artifact(id="cluster-copy", accessibility="cluster", storage_ref="cluster://demo/cluster")
    )

    assert local.id != cluster.id
    assert [item.id for item in catalog.recommend(
        project="demo", owner="alice", target_accessibility="cluster", now=100
    )] == [cluster.id]


@pytest.mark.parametrize("storage_ref", [r"D:\\selena\\selena.exe", r"\\\\server\\share\\selena.exe", "file:///tmp/selena"])
def test_storage_ref_rejects_raw_local_or_unc_paths(storage_ref):
    with pytest.raises(ArtifactValidationError, match="storage_ref"):
        artifact(storage_ref=storage_ref)


def test_verify_access_rejects_expired_or_unhealthy_artifact(tmp_path):
    catalog = ArtifactCatalog(tmp_path / "catalog.db")
    expired = catalog.register(artifact(id="expired", retain_until=50))
    unhealthy = catalog.register(artifact(id="unhealthy", binary_checksum=SHA_B, health="degraded"))

    with pytest.raises(ArtifactAccessError, match="expired"):
        catalog.verify_access(expired.id, owner="alice", now=100)
    with pytest.raises(ArtifactAccessError, match="not ready"):
        catalog.verify_access(unhealthy.id, owner="alice", now=100)


@pytest.mark.parametrize("patch", [{"dirty": "false"}, {"created_at": float("nan")}, {"retain_until": float("inf")}])
def test_artifact_rejects_ambiguous_bool_and_non_finite_times(patch):
    with pytest.raises(ArtifactValidationError):
        artifact(**patch)
