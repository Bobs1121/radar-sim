from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from core.api_v1 import ApiV1Service
from core.api_v1_fastapi import create_app
from core.artifact_store import ArtifactStore
from core.artifact_upload_service import (
    ArtifactUploadService,
    ArtifactUploadServiceError,
    TrustedBuildEvidence,
    trusted_build_evidence_from_control,
)
from core.artifacts import ArtifactAccessError, ArtifactCatalog
from core.control_service import ControlService
from radar_sim_sdk import RadarSimClient


def checksum(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def evidence(data: bytes, **patch) -> TrustedBuildEvidence:
    values = {
        "evidence_ref": "stage_build:1",
        "owner": "alice",
        "project": "demo",
        "build_mode": "Release",
        "source_kind": "current_workspace",
        "created_by": "agent-alice",
        "created_at": 100.0,
        "retain_until": 1000.0,
        "branch": "feature/x",
        "commit": "1" * 40,
        "dirty": False,
        "dirty_fingerprint": "a" * 64,
        "source_changed_during_build": False,
        "checksum": checksum(data),
        "size": len(data),
        "logical_path": "selena.exe",
    }
    values.update(patch)
    return TrustedBuildEvidence(**values)


def service_for(tmp_path, trusted: TrustedBuildEvidence):
    store = ArtifactStore(root=tmp_path / "content-root", db_path=tmp_path / "uploads.db", chunk_size=3)
    catalog = ArtifactCatalog(tmp_path / "catalog.db")

    def provider(owner: str, evidence_ref: str) -> TrustedBuildEvidence:
        if owner != trusted.owner or evidence_ref != trusted.evidence_ref:
            raise ArtifactUploadServiceError("build_evidence_mismatch", "mismatch", status_code=409)
        return trusted

    return ArtifactUploadService(store, catalog, provider), store, catalog


def test_clean_upload_uses_user_path_and_is_shared(tmp_path):
    data = b"selena-binary"
    service, _store, catalog = service_for(tmp_path, evidence(data))
    session = service.create("alice", evidence_ref="stage_build:1", publish_path="team/feature-x")
    assert session["storage_ref"] == "shared://selena/demo/team/feature-x/selena.exe"
    service.append("alice", session["session_id"], offset=0, data=data)
    result = service.finalize("alice", session["session_id"])
    assert result["artifact"]["visibility"] == "shared"
    assert catalog.get_by_storage_ref(session["storage_ref"], owner="bob").id == result["artifact"]["id"]
    assert str(tmp_path) not in str(result)


@pytest.mark.parametrize("dirty,changed", [(True, False), (False, True)])
def test_dirty_or_changed_build_remains_private(tmp_path, dirty, changed):
    data = b"private-binary"
    trusted = evidence(
        data,
        dirty=dirty,
        source_changed_during_build=changed,
        dirty_fingerprint="b" * 64,
    )
    service, _store, catalog = service_for(tmp_path, trusted)
    session = service.create("alice", evidence_ref=trusted.evidence_ref, publish_path="users/alice/wip")
    service.append("alice", session["session_id"], offset=0, data=data)
    result = service.finalize("alice", session["session_id"])
    assert result["artifact"]["visibility"] == "private"
    with pytest.raises(ArtifactAccessError):
        catalog.get_by_storage_ref(session["storage_ref"], owner="bob")
    assert catalog.get_by_storage_ref(session["storage_ref"], owner="alice").id == result["artifact"]["id"]


def test_owner_and_evidence_cannot_be_spoofed(tmp_path):
    data = b"selena"
    service, _store, _catalog = service_for(tmp_path, evidence(data))
    with pytest.raises(ArtifactUploadServiceError, match="mismatch"):
        service.create("bob", evidence_ref="stage_build:1", publish_path="team/a")
    with pytest.raises(ArtifactUploadServiceError, match="mismatch"):
        service.create("alice", evidence_ref="other:1", publish_path="team/a")


def test_default_publish_path_is_stable_and_server_managed(tmp_path):
    data = b"selena"
    trusted = evidence(data)
    service, _store, _catalog = service_for(tmp_path, trusted)
    first = service.create("alice", evidence_ref=trusted.evidence_ref)
    second = service.create("alice", evidence_ref=trusted.evidence_ref)
    assert first["publish_path"] == second["publish_path"]
    assert first["publish_path"].startswith("builds/feature-x/")
    assert first["publish_path"].endswith("/selena.exe")


def test_http_and_sdk_resumable_upload_and_finalize(tmp_path):
    data = b"abcdef"
    upload_service, _store, catalog = service_for(tmp_path, evidence(data))
    api = ApiV1Service(artifact_upload_service_factory=lambda _owner: upload_service)
    test_client = TestClient(create_app(api_service=api))
    sdk = RadarSimClient("http://testserver", client=test_client, user="alice")

    session = sdk.create_artifact_upload("stage_build:1", publish_path="team/sdk")
    assert session.received_bytes == 0
    session = sdk.append_artifact_upload(session.session_id, 0, data[:3])
    assert session.received_bytes == 3
    inspected = sdk.get_artifact_upload(session.session_id)
    assert inspected.received_bytes == 3
    session = sdk.append_artifact_upload(session.session_id, 3, data[3:])
    result = sdk.finalize_artifact_upload(session.session_id)
    assert result.artifact["storage_ref"] == "shared://selena/demo/team/sdk/selena.exe"
    assert catalog.get_by_storage_ref(result.artifact["storage_ref"], owner="bob").id == result.artifact["id"]


def test_sdk_file_convenience_uses_server_chunk_size(tmp_path):
    data = b"abcdefgh"
    source = tmp_path / "selena.exe"
    source.write_bytes(data)
    upload_service, _store, _catalog = service_for(tmp_path, evidence(data))
    test_client = TestClient(create_app(api_service=ApiV1Service(
        artifact_upload_service_factory=lambda _owner: upload_service
    )))
    sdk = RadarSimClient("http://testserver", client=test_client, user="alice")
    result = sdk.upload_artifact("stage_build:1", source, publish_path="team/file")
    assert result.session.received_bytes == len(data)
    assert result.artifact["binary_checksum"] == checksum(data)


def test_http_rejects_offset_gap_and_unconfigured_service(tmp_path):
    data = b"abcdef"
    upload_service, _store, _catalog = service_for(tmp_path, evidence(data))
    client = TestClient(create_app(api_service=ApiV1Service(
        artifact_upload_service_factory=lambda _owner: upload_service
    )))
    session = client.post(
        "/api/v1/artifact-uploads",
        headers={"X-Rsim-User": "alice"},
        json={"build_evidence_ref": "stage_build:1", "publish_path": "team/gap"},
    ).json()
    gap = client.patch(
        f"/api/v1/artifact-uploads/{session['session_id']}",
        headers={"X-Rsim-User": "alice", "Upload-Offset": "2"},
        content=b"abc",
    )
    assert gap.status_code == 409
    assert gap.json()["code"] == "artifact_upload_offset_conflict"

    unavailable = TestClient(create_app()).post(
        "/api/v1/artifact-uploads",
        json={"build_evidence_ref": "stage_build:1", "publish_path": "team/a"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "artifact_upload_unavailable"


def test_control_provider_accepts_only_succeeded_windows_build_attempt(tmp_path):
    control = ControlService(tmp_path / "control.db")
    job = control.create_job(
        "simulation.v1",
        owner="alice",
        spec={
            "project": "demo",
            "selena": {"mode": "current_workspace", "build_mode": "Release"},
            "result": {"retain_days": 30},
        },
        tasks=[{"task_type": "build_selena", "stage_type": "build_selena"}],
    )
    control.register_agent(
        "alice-light",
        agent_id="alice-light",
        node_kind="windows_agent",
        capabilities=["build.selena"],
    )
    claimed = control.claim_next_task("alice-light")
    assert claimed["stage_id"] == job["stages"][0]["stage_id"]
    data = b"binary"
    snapshot = {
        "branch": "feature/x",
        "commit": "1" * 40,
        "dirty": False,
        "sha256": "a" * 64,
        "evidence": {},
    }
    control.submit_task_result(
        claimed["stage_id"],
        agent_id="alice-light",
        returncode=0,
        result={
            "project": "demo",
            "workspace_binding_id": "workspace:sha256:" + "a" * 24,
            "build_mode": "Release",
            "before": snapshot,
            "after": snapshot,
            "source_changed_during_build": False,
            "artifact": {"logical_path": "selena.exe", "checksum": checksum(data), "size": len(data)},
        },
    )
    resolved = trusted_build_evidence_from_control(control, "alice", f"{claimed['stage_id']}:1")
    assert resolved.created_by == "alice-light"
    assert resolved.checksum == checksum(data)
    with pytest.raises(ArtifactUploadServiceError, match="does not belong"):
        trusted_build_evidence_from_control(control, "bob", f"{claimed['stage_id']}:1")
