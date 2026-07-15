import hashlib
from pathlib import Path

import pytest

from core.dataset_store import DatasetStore, DatasetStoreQuota
from core.dataset_upload_service import (
    DatasetUploadService,
    DatasetUploadServiceError,
    TrustedDataStageEvidence,
)
from core.datasets import DatasetCatalog
from core.control_service import ControlService, INTERNAL_V1_SCHEDULER_AGENT_ID
from core.dataset_upload_service import trusted_data_stage_evidence_from_control


def _file(path: str, data: bytes) -> dict:
    return {
        "relative_path": path,
        "size": len(data),
        "checksum": "sha256:" + hashlib.sha256(data).hexdigest(),
    }


def _service(tmp_path: Path) -> DatasetUploadService:
    quota = DatasetStoreQuota(min_free_bytes=0, chunk_size=8, max_file_size=100, max_total_size=100)
    return DatasetUploadService(
        DatasetStore(tmp_path / "store", quota=quota),
        DatasetCatalog(tmp_path / "catalog.db"),
        project_validator=lambda value: value == "ovrs25",
    )


def test_public_upload_source_kind_is_server_owned_and_returns_reusable_data_path(tmp_path: Path):
    service = _service(tmp_path)
    data = b"mf4"
    session = service.create("alice", project="ovrs25", files=[_file("scene/a.MF4", data)])
    uploaded = service.append(
        "alice", session["session_id"], session["files"][0]["file_id"], offset=0, data=data
    )
    assert uploaded["files"][0]["status"] == "uploaded"

    result = service.finalize("alice", session["session_id"])
    assert result["dataset"]["source_kind"] == "central_upload"
    assert result["data_path"].startswith("dataset://sha256/")
    assert "source_location" not in str(result)


def test_service_hides_cross_owner_session_as_not_found(tmp_path: Path):
    service = _service(tmp_path)
    session = service.create("alice", project="ovrs25", files=[_file("a.MF4", b"x")])
    with pytest.raises(DatasetUploadServiceError) as error:
        service.get("bob", session["session_id"])
    assert error.value.status_code == 404


def test_unknown_project_is_not_accepted(tmp_path: Path):
    with pytest.raises(DatasetUploadServiceError) as error:
        _service(tmp_path).create("alice", project="unknown", files=[_file("a.MF4", b"x")])
    assert error.value.code == "unknown_project"


def test_agent_upload_requires_matching_trusted_attempt_and_agent(tmp_path: Path):
    service = _service(tmp_path)
    evidence = TrustedDataStageEvidence(
        evidence_ref="job:stage:1",
        owner="alice",
        project="ovrs25",
        job_id="job_1",
        stage_id="stage_1",
        attempt=1,
        required_agent_id="agent_1",
    )
    session = service.create_for_agent(
        "alice",
        project="ovrs25",
        files=[_file("a.MF4", b"x")],
        evidence=evidence,
        requesting_agent_id="agent_1",
    )
    assert session["session_id"].startswith("dsup_")

    with pytest.raises(DatasetUploadServiceError, match="does not authorize"):
        service.create_for_agent(
            "alice",
            project="ovrs25",
            files=[_file("a.MF4", b"x")],
            evidence=evidence,
            requesting_agent_id="agent_2",
        )


def test_control_derived_running_prepare_data_evidence_authorizes_agent_session(tmp_path: Path):
    control = ControlService(tmp_path / "control.db")
    control.register_agent(
        "light",
        agent_id="agent_1",
        node_kind="windows_agent",
        capabilities=["data.local.read", "data.upload"],
        metadata={"node_kind": "windows_agent"},
    )
    job = control.create_job(
        "simulation.v1",
        owner="alice",
        assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
        spec={"project": "ovrs25", "data": {"path": "D:/data"}},
        tasks=[
            {"task_type": "resolve_spec", "stage_type": "resolve_spec", "status": "skipped"},
            {
                "task_type": "prepare_data",
                "stage_type": "prepare_data",
                "dependencies": ["resolve_spec"],
                "assigned_agent_id": "agent_1",
                "required_agent_id": "agent_1",
            },
        ],
    )
    claimed = control.claim_next_task("agent_1")
    evidence_ref = f"{claimed['stage_id']}:{claimed['attempt_count']}"
    service = _service(tmp_path / "upload")
    service._evidence_provider = lambda owner, ref: trusted_data_stage_evidence_from_control(
        control, owner, ref
    )

    session = service.create_agent_from_evidence(
        "alice",
        project="ovrs25",
        files=[_file("a.MF4", b"x")],
        evidence_ref=evidence_ref,
        requesting_agent_id="agent_1",
    )
    assert session["session_id"].startswith("dsup_")
    assert claimed["job_id"] == job["job_id"]
