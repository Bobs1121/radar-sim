from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.agent_artifact_lease import AgentArtifactLeaseError, AgentArtifactLeaseStore
from core.agent_artifact_staging import AuthorizedRoots
from core.agent_build_stage import PreparedSelenaBuild
from core.repo import WorkspaceFingerprint


def _fingerprint():
    return WorkspaceFingerprint(
        branch="feature/dirty",
        commit="1" * 40,
        dirty=True,
        sha256="2" * 64,
        staged_diff_sha256="3" * 64,
        staged_diff_bytes=1,
        unstaged_diff_sha256="4" * 64,
        unstaged_diff_bytes=1,
        untracked=(),
    )


def _prepared(tmp_path: Path):
    workspace = tmp_path / "workspace"
    output = workspace / "out"
    output.mkdir(parents=True)
    script = workspace / "build.bat"
    script.write_bytes(b"@echo off\n")
    artifact = output / "selena.exe"
    artifact.write_bytes(b"built-selena")
    authorized = AuthorizedRoots(workspace, (output,))
    prepared = PreparedSelenaBuild(
        project="ovrs25",
        binding_id="workspace:sha256:" + "a" * 24,
        build_mode="Release",
        clean=False,
        command=("cmd", "/c", str(script)),
        cwd=workspace,
        authorized=authorized,
        before=_fingerprint(),
        build_script_path=script,
        build_script_checksum="sha256:" + hashlib.sha256(script.read_bytes()).hexdigest(),
        artifact_path=artifact,
    )
    checksum = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    result = {
        "project": "ovrs25",
        "workspace_binding_id": prepared.binding_id,
        "artifact": {"logical_path": "selena.exe", "checksum": checksum, "size": artifact.stat().st_size},
    }
    return prepared, result, artifact


def test_lease_keeps_absolute_path_local_and_is_idempotent_per_attempt(tmp_path):
    prepared, result, _ = _prepared(tmp_path)
    store = AgentArtifactLeaseStore(tmp_path / "leases.db", now_fn=lambda: 100)

    first = store.create(prepared, result, build_stage_id="stage-build", build_attempt=1)
    second = store.create(prepared, result, build_stage_id="stage-build", build_attempt=1)

    assert second.lease_id == first.lease_id
    assert first.public_dict["build_evidence_ref"] == "stage-build:1"
    assert str(tmp_path) not in str(first.public_dict)
    assert store.get(first.lease_id, build_evidence_ref="stage-build:1").checksum == first.checksum


def test_lease_rejects_file_changed_before_upload(tmp_path):
    prepared, result, artifact = _prepared(tmp_path)
    store = AgentArtifactLeaseStore(tmp_path / "leases.db", now_fn=lambda: 100)
    lease = store.create(prepared, result, build_stage_id="stage-build", build_attempt=1)
    artifact.write_bytes(b"changed")

    with pytest.raises(AgentArtifactLeaseError, match="changed"):
        store.get(lease.lease_id, build_evidence_ref="stage-build:1")


def test_lease_requires_matching_build_evidence_and_shared_storage_ref(tmp_path):
    prepared, result, _ = _prepared(tmp_path)
    store = AgentArtifactLeaseStore(tmp_path / "leases.db", now_fn=lambda: 100)
    lease = store.create(prepared, result, build_stage_id="stage-build", build_attempt=2)

    with pytest.raises(AgentArtifactLeaseError, match="mismatch"):
        store.get(lease.lease_id, build_evidence_ref="stage-build:1")
    with pytest.raises(AgentArtifactLeaseError, match="storage"):
        store.mark_uploaded(lease.lease_id, "C:/server/path")
    uploaded = store.mark_uploaded(lease.lease_id, "shared://selena/ovrs25/team/a/selena.exe")
    assert uploaded.status == "uploaded"
