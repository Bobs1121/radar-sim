"""Tests for the Agent-facing extracted result directory contract."""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import zipfile

import httpx
import pytest

from radar_sim_sdk import RadarSimClient
from radar_sim_sdk.errors import RadarSimIntegrityError


def _archive(files: dict[str, bytes]) -> tuple[bytes, list[dict[str, object]]]:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as payload:
        for relative, content in files.items():
            payload.writestr(relative, content)
    archive = buffer.getvalue()
    metadata = [
        {
            "relative_path": relative,
            "size": len(content),
            "checksum": "sha256:" + sha256(content).hexdigest(),
        }
        for relative, content in files.items()
    ]
    return archive, metadata


def test_download_job_result_returns_verified_directory_and_reuses_it(tmp_path: Path):
    files = {"output/result.ini": b"successful=1\n", "output/result.MF4": b"mf4-output"}
    archive, result_files = _archive(files)
    result_ref = "result:sha256:abc"
    archive_checksum = "sha256:" + sha256(archive).hexdigest()
    configured = tmp_path / "results"
    calls = {"download": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/jobs/job-dir-result":
            return httpx.Response(
                200,
                json={
                    "id": "job-dir-result",
                    "status": "succeeded",
                    "spec": {"result": {"path": str(configured)}},
                },
            )
        if request.url.path == "/api/v1/jobs/job-dir-result/manifest":
            return httpx.Response(
                200,
                json={
                    "job_id": "job-dir-result",
                    "available": True,
                    "manifest": {"result_ref": result_ref},
                },
            )
        if request.url.path == f"/api/v1/results/{result_ref}":
            return httpx.Response(
                200,
                json={
                    "ref": result_ref,
                    "archive_checksum": archive_checksum,
                    "archive_size": len(archive),
                    "files": result_files,
                },
            )
        if request.url.path == f"/api/v1/results/{result_ref}/download":
            calls["download"] += 1
            return httpx.Response(200, content=archive)
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    sdk = RadarSimClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
        user="alice",
    )

    directory = sdk.download_job_result("job-dir-result", extract=True)

    assert directory == configured / "job-dir-result"
    assert directory.is_dir()
    assert (directory / "output/result.ini").read_bytes() == files["output/result.ini"]
    assert (directory / "output/result.MF4").read_bytes() == files["output/result.MF4"]
    marker = json.loads((directory / ".radar-sim/manifest.json").read_text(encoding="utf-8"))
    assert marker["result_ref"] == result_ref
    assert (directory / ".radar-sim/result.zip").is_file()

    reused = sdk.download_job_result("job-dir-result", extract=True)
    assert reused == directory
    assert calls["download"] == 1


def test_download_job_result_rejects_unsafe_archive_member(tmp_path: Path):
    files = {"../outside.txt": b"unsafe"}
    archive, result_files = _archive(files)
    result_ref = "result:sha256:unsafe"
    archive_checksum = "sha256:" + sha256(archive).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/jobs/job-unsafe":
            return httpx.Response(200, json={"id": "job-unsafe", "status": "succeeded", "spec": {}})
        if request.url.path == "/api/v1/jobs/job-unsafe/manifest":
            return httpx.Response(
                200,
                json={"job_id": "job-unsafe", "available": True, "manifest": {"result_ref": result_ref}},
            )
        if request.url.path == f"/api/v1/results/{result_ref}":
            return httpx.Response(
                200,
                json={"ref": result_ref, "archive_checksum": archive_checksum, "files": result_files},
            )
        if request.url.path == f"/api/v1/results/{result_ref}/download":
            return httpx.Response(200, content=archive)
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    sdk = RadarSimClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
        user="alice",
    )
    with pytest.raises(RadarSimIntegrityError) as caught:
        sdk.download_job_result("job-unsafe", tmp_path / "result", extract=True)

    assert caught.value.code == "result_file_metadata_invalid"
    assert not (tmp_path / "outside.txt").exists()
    assert not list(tmp_path.glob(".result.part.*"))
