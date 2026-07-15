import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from core.api_v1 import ApiV1Service
from core.api_v1_fastapi import create_app
from core.dataset_store import DatasetStore, DatasetStoreQuota
from core.dataset_upload_service import DatasetUploadService
from core.datasets import DatasetCatalog
from radar_sim_sdk import RadarSimClient


def _manifest(path: str, data: bytes) -> dict:
    return {
        "relative_path": path,
        "size": len(data),
        "checksum": "sha256:" + hashlib.sha256(data).hexdigest(),
    }


def _client(tmp_path: Path) -> TestClient:
    quota = DatasetStoreQuota(min_free_bytes=0, chunk_size=4, max_file_size=100, max_total_size=100)
    upload = DatasetUploadService(
        DatasetStore(tmp_path / "store", quota=quota),
        DatasetCatalog(tmp_path / "catalog.db"),
        project_validator=lambda value: value == "ovrs25",
    )
    api = ApiV1Service(dataset_upload_service_factory=lambda _owner: upload)
    return TestClient(create_app(api_service=api))


def test_dataset_upload_http_contract_and_owner_isolation(tmp_path: Path):
    client = _client(tmp_path)
    headers = {"X-Rsim-User": "alice"}
    data = b"mf4"
    created = client.post(
        "/api/v1/dataset-uploads",
        headers=headers,
        json={"project": "ovrs25", "files": [_manifest("scene/a.MF4", data)]},
    )
    assert created.status_code == 201
    session = created.json()
    file_id = session["files"][0]["file_id"]
    appended = client.patch(
        f"/api/v1/dataset-uploads/{session['session_id']}/files/{file_id}",
        headers={**headers, "Upload-Offset": "0"},
        content=data,
    )
    assert appended.status_code == 200
    finalized = client.post(
        f"/api/v1/dataset-uploads/{session['session_id']}/finalize", headers=headers
    )
    assert finalized.status_code == 200
    assert finalized.json()["data_path"].startswith("dataset://sha256/")
    assert "source_location" not in finalized.text

    hidden = client.get(
        f"/api/v1/dataset-uploads/{session['session_id']}", headers={"X-Rsim-User": "bob"}
    )
    assert hidden.status_code == 404


def test_browser_dataset_upload_may_omit_file_checksum(tmp_path: Path):
    client = _client(tmp_path)
    headers = {"X-Rsim-User": "alice"}
    data = b"large-browser-file"
    created = client.post(
        "/api/v1/dataset-uploads",
        headers=headers,
        json={
            "project": "ovrs25",
            "files": [{"relative_path": "scene/a.MF4", "size": len(data)}],
        },
    )
    assert created.status_code == 201
    session = created.json()
    assert session["files"][0]["expected_checksum"] == ""
    file_id = session["files"][0]["file_id"]
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + session["chunk_size"]]
        response = client.patch(
            f"/api/v1/dataset-uploads/{session['session_id']}/files/{file_id}",
            headers={**headers, "Upload-Offset": str(offset)},
            content=chunk,
        )
        assert response.status_code == 200
        offset += len(chunk)
    finalized = client.post(
        f"/api/v1/dataset-uploads/{session['session_id']}/finalize",
        headers=headers,
    )
    assert finalized.status_code == 200
    assert finalized.json()["dataset"]["files"][0]["checksum"].startswith("sha256:")


def test_dataset_upload_rejects_client_source_kind_and_oversized_chunk(tmp_path: Path):
    client = _client(tmp_path)
    headers = {"X-Rsim-User": "alice"}
    request = {"project": "ovrs25", "files": [_manifest("a.MF4", b"12345")]}
    rejected = client.post(
        "/api/v1/dataset-uploads", headers=headers, json={**request, "source_kind": "agent_upload"}
    )
    assert rejected.status_code == 422

    session = client.post("/api/v1/dataset-uploads", headers=headers, json=request).json()
    response = client.patch(
        f"/api/v1/dataset-uploads/{session['session_id']}/files/{session['files'][0]['file_id']}",
        headers={**headers, "Upload-Offset": "0"},
        content=b"12345",
    )
    assert response.status_code == 413
    assert response.json()["code"] == "dataset_upload_chunk_too_large"


def test_sdk_upload_dataset_discovers_all_nested_inputs_and_excludes_outputs(tmp_path: Path):
    client = _client(tmp_path)
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "a.MF4").write_bytes(b"aaa")
    (source / "nested" / "b.mf4").write_bytes(b"bbb")
    (source / "nested" / "bout.MF4").write_bytes(b"generated")
    sdk = RadarSimClient("http://testserver", client=client, user="alice")

    result = sdk.upload_dataset("ovrs25", source)
    assert result.data_path.startswith("dataset://sha256/")
    assert [item["relative_path"] for item in result.dataset["files"]] == ["a.MF4", "nested/b.mf4"]


def test_dataset_upload_routes_are_explicitly_unavailable_without_service():
    response = TestClient(create_app()).post(
        "/api/v1/dataset-uploads",
        headers={"X-Rsim-User": "alice"},
        json={"project": "ovrs25", "files": [_manifest("a.MF4", b"x")]},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "dataset_upload_unavailable"
