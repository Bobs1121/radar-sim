from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.result_delivery import (
    ResultDeliveryError,
    materialize_result_directory,
    resolve_result_destination,
)


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "run-root"
    (root / "outputs").mkdir(parents=True)
    (root / "outputs" / "one.MF4").write_bytes(b"result-one")
    (root / "summary.json").write_text('{"ok":true}\n', encoding="utf-8")
    return root


def test_empty_result_path_resolves_only_on_receiver(tmp_path: Path) -> None:
    target = resolve_result_destination("", "job-123", home=tmp_path)
    assert target == (tmp_path / "RadarSim" / "results" / "job-123").resolve()
    assert not target.exists()

    explicit_root = tmp_path / "chosen-results"
    assert resolve_result_destination(explicit_root, "job-123") == (
        explicit_root / "job-123"
    ).resolve()


@pytest.mark.parametrize("value", ["..\\outside", "../outside", "/", "C:/", "NUL"])
def test_result_destination_rejects_traversal_roots_and_devices(tmp_path: Path, value: str) -> None:
    with pytest.raises(ResultDeliveryError, match="destination"):
        resolve_result_destination(value, "job-123", home=tmp_path)


def test_result_destination_rejects_existing_file_and_symlink(tmp_path: Path) -> None:
    file_target = tmp_path / "file"
    file_target.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ResultDeliveryError):
        resolve_result_destination(str(file_target), "job-123", home=tmp_path)

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ResultDeliveryError):
        resolve_result_destination(str(link), "job-123", home=tmp_path)


def test_materialize_is_atomic_idempotent_and_preserves_manifest(tmp_path: Path) -> None:
    source = _source(tmp_path)
    destination = resolve_result_destination("", "job-123", home=tmp_path)
    manifest = {
        "job_id": "job-123",
        "status": "partial",
        "result_ref": "result:sha256:" + "a" * 64,
        "summary": {"file_count": 2, "output_path": "must-not-leak"},
        "input_results": [
            {"index": 1, "input_relative_path": "input/one.MF4", "status": "succeeded"}
        ],
    }
    files = [
        {"relative_path": "outputs/one.MF4"},
        {"relative_path": "summary.json"},
    ]
    first = materialize_result_directory(
        source,
        destination,
        files=files,
        manifest=manifest,
    )
    assert first["status"] == "delivered"
    assert first["file_count"] == 2
    assert first["checksum"].startswith("sha256:")
    assert (destination / "outputs" / "one.MF4").read_bytes() == b"result-one"
    public_manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert public_manifest["status"] == "partial"
    assert "output_path" not in json.dumps(public_manifest)
    assert str(source) not in json.dumps(public_manifest)
    assert not (source / "manifest.json").exists()

    second = materialize_result_directory(
        source,
        destination,
        files=files,
        manifest=manifest,
    )
    assert second["status"] == "already_present"
    assert second["checksum"] == first["checksum"]


def test_materialize_does_not_overwrite_unrelated_destination(tmp_path: Path) -> None:
    source = _source(tmp_path)
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "user.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ResultDeliveryError, match="different content"):
        materialize_result_directory(
            source,
            destination,
            files=["outputs/one.MF4"],
            manifest={"job_id": "job-123"},
        )
    assert (destination / "user.txt").read_text(encoding="utf-8") == "keep"


def test_materialize_rejects_source_destination_overlap(tmp_path: Path) -> None:
    source = _source(tmp_path)
    with pytest.raises(ResultDeliveryError, match="overlaps"):
        materialize_result_directory(source, source / "outputs", files=["outputs/one.MF4"])
