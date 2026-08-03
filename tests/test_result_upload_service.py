from __future__ import annotations

import time

from fastapi.testclient import TestClient

from core.api_v1 import ApiV1Service
from core.api_v1_fastapi import create_app
from core.artifact_store import ArtifactStore
from core.local_results import ResultCatalog
from core.result_upload_service import ResultUploadService
from radar_sim_sdk import RadarSimClient


def test_windows_result_archive_is_uploaded_and_downloadable(tmp_path):
    source = tmp_path / "windows-run"
    source.mkdir()
    (source / "output.MF4").write_bytes(b"simulated-output")

    local_catalog = ResultCatalog(
        tmp_path / "windows-archives",
        tmp_path / "windows.db",
        allowed_source_root=source,
    )
    local = local_catalog.publish(
        owner="alice",
        run_ref="local-run-lease:sha256:" + "a" * 64,
        source_root=source,
        files=["output.MF4"],
        retain_until=time.time() + 3600,
    )
    archive = local_catalog.resolve_archive(local.ref, owner="alice")

    central_catalog = ResultCatalog(
        tmp_path / "central-archives",
        tmp_path / "central.db",
        allowed_source_root=tmp_path / "central-archives",
    )
    upload_store = ArtifactStore(
        root=central_catalog.storage_root,
        db_path=central_catalog.storage_root / ".store" / "uploads.db",
        object_filename="result.zip",
        storage_ref_prefix="shared://results/",
    )
    upload_service = ResultUploadService(upload_store, central_catalog)
    api = ApiV1Service(
        result_catalog=central_catalog,
        result_upload_service_factory=lambda _owner: upload_service,
    )
    http_client = TestClient(create_app(api_service=api))
    with RadarSimClient("http://testserver", client=http_client, user="alice") as sdk:
        uploaded = sdk.upload_result_archive(
            archive,
            run_ref=local.run_ref,
            files=[item.to_dict() for item in local.files],
            retain_until=local.retain_until,
        )
        result_ref = uploaded["result_ref"]
        metadata = sdk.get_result(result_ref)
        assert metadata["archive_checksum"] == local.archive_checksum
        downloaded_path = sdk.download_result(result_ref, tmp_path / "download")

    assert result_ref == local.ref
    assert downloaded_path.read_bytes() == archive.read_bytes()
