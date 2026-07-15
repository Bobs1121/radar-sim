import hashlib
from pathlib import Path

from core.api_v1 import ApiV1Service
from core.control_service import ControlService
from core.dataset_store import DatasetStore, DatasetStoreQuota
from core.dataset_upload_service import DatasetUploadService
from core.datasets import DatasetCatalog, resolve_data_reference
from core.shared_namespace import SharedNamespaceRegistry


def _upload(tmp_path: Path, catalog: DatasetCatalog) -> str:
    data = b"mf4"
    checksum = "sha256:" + hashlib.sha256(data).hexdigest()
    store = DatasetStore(
        tmp_path / "store",
        quota=DatasetStoreQuota(min_free_bytes=0, chunk_size=4, max_file_size=100, max_total_size=100),
    )
    service = DatasetUploadService(store, catalog)
    session = service.create(
        "alice",
        project="ovrs25",
        files=[{"relative_path": "a.MF4", "size": len(data), "checksum": checksum}],
    )
    service.append("alice", session["session_id"], session["files"][0]["file_id"], offset=0, data=data)
    return service.finalize("alice", session["session_id"])["data_path"]


def _api(tmp_path: Path, catalog: DatasetCatalog) -> ApiV1Service:
    control = ControlService(tmp_path / "control.db")

    def provider(owner, spec):
        return resolve_data_reference(
            catalog,
            SharedNamespaceRegistry(),
            owner=owner,
            project=spec.project,
            data_path=spec.data.path,
            required_signals=spec.data.required_signals,
        )

    return ApiV1Service(control_service_factory=lambda _owner: control, data_resolution_provider=provider)


def test_uploaded_dataset_is_resolved_at_submit_and_prepare_data_is_skipped(tmp_path: Path):
    catalog = DatasetCatalog(tmp_path / "catalog.db")
    data_path = _upload(tmp_path, catalog)
    job = _api(tmp_path, catalog).submit_job(
        "alice", spec_payload={"project": "ovrs25", "data": {"path": data_path}}
    )
    prepare = next(stage for stage in job["stages"] if stage["stage_type"] == "prepare_data")
    assert prepare["status"] == "skipped"
    assert job["resolved_spec"]["decisions"]["data"]["code"] == "uploaded_dataset_resolved"
    assert job["metadata"]["data_resolution"]["route"] == "central"
    assert str(tmp_path / "store") not in str(job)


def test_windows_local_data_becomes_machine_work_not_user_business_config(tmp_path: Path):
    catalog = DatasetCatalog(tmp_path / "catalog.db")
    job = _api(tmp_path, catalog).submit_job(
        "alice", spec_payload={"project": "ovrs25", "data": {"path": "D:/measurements/case"}}
    )
    prepare = next(stage for stage in job["stages"] if stage["stage_type"] == "prepare_data")
    assert prepare["status"] == "queued"
    assert prepare["payload"]["dispatch_scope"] == "data_upload"
    assert job["resolved_spec"]["status"] == "pending_node"
    assert job["metadata"]["data_resolution"]["code"] == "agent_data_upload_required"


def test_untrusted_unc_blocks_only_prepare_data_with_upload_action(tmp_path: Path):
    catalog = DatasetCatalog(tmp_path / "catalog.db")
    api = _api(tmp_path, catalog)
    job = api.submit_job(
        "alice", spec_payload={"project": "ovrs25", "data": {"path": r"\\unknown\share\case"}}
    )
    by_type = {stage["stage_type"]: stage for stage in job["stages"]}
    assert by_type["prepare_data"]["status"] == "blocked"
    assert by_type["prepare_data"]["error"]["actions"][0]["type"] == "upload_data"
    assert by_type["prepare_source"]["status"] == "queued"
    assert job["status"] == "needs_input"
    assert [item["id"] for item in api.list_jobs("alice", status="needs_input")["jobs"]] == [job["id"]]
    assert api.list_jobs("alice", status="queued")["jobs"] == []
