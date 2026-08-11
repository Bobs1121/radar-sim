"""V2 data-plane boundary: user data never enters the Linux HTTP body path."""

from fastapi.testclient import TestClient

from core.api_v1_fastapi import create_app
from radar_sim_sdk import RadarSimClient


def test_v2_removes_user_dataset_upload_creation_routes():
    client = TestClient(create_app())
    headers = {"X-Rsim-User": "alice"}
    body = {"files": [{"relative_path": "a.MF4", "size": 1}]}

    assert client.post("/api/v1/run-data-uploads", headers=headers, json=body).status_code == 404
    assert client.post(
        "/api/v1/dataset-uploads",
        headers=headers,
        json={"project": "legacy", **body},
    ).status_code == 404


def test_v2_sdk_has_no_linux_body_upload_shortcut():
    assert not hasattr(RadarSimClient, "create_run_data_upload")
    assert not hasattr(RadarSimClient, "upload_run_data")
    assert not hasattr(RadarSimClient, "create_dataset_upload")
    assert not hasattr(RadarSimClient, "upload_dataset")
