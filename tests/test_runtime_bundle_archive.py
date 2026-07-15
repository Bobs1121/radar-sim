import json
import zipfile
from pathlib import Path

import pytest

from core.runtime_bundle import RuntimeSourceEvidence, discover_runtime_bundle
from core.runtime_bundle_archive import (
    RuntimeBundleArchiveError,
    extract_runtime_bundle_archive,
    stage_runtime_bundle_archive,
    verify_runtime_bundle_archive,
)


def _lease(tmp_path: Path):
    output = tmp_path / "build"
    output.mkdir()
    (output / "selena.exe").write_bytes(b"selena")
    (output / "alpha.dll").write_bytes(b"alpha")
    (output / "beta.DLL").write_bytes(b"beta")
    runtime = tmp_path / "Runtime.xml"
    runtime.write_text("<runtime />", encoding="utf-8")
    return discover_runtime_bundle(
        output / "selena.exe",
        runtime,
        source=RuntimeSourceEvidence(
            branch="feature/demo",
            commit="a" * 40,
            dirty=False,
            dirty_fingerprint="",
            build_mode="Release",
            toolchain_fingerprint="msvc-14",
            adapter_key="recipe:demo",
        ),
        created_at=123.0,
    )


def test_stages_deterministic_complete_runtime_bundle(tmp_path):
    lease = _lease(tmp_path)
    root = tmp_path / "staging"
    first = stage_runtime_bundle_archive(lease, root)
    second = stage_runtime_bundle_archive(lease, root)

    assert first == second
    assert first.bundle_id == lease.manifest.id
    assert first.file_count == 4
    assert "path" not in first.public_dict
    verify_runtime_bundle_archive(first, lease)

    with zipfile.ZipFile(first.path) as archive:
        assert set(archive.namelist()) == {
            "runtime-bundle.json",
            "bin/selena.exe",
            "bin/alpha.dll",
            "bin/beta.DLL",
            "runtime/Runtime.xml",
        }
        manifest = json.loads(archive.read("runtime-bundle.json"))
        assert manifest["format"] == "radar-sim.runtime-bundle-archive/1"
        assert manifest["bundle"]["id"] == lease.manifest.id
        assert "adapter_key" not in json.dumps(manifest)


def test_rejects_changed_source_and_changed_archive(tmp_path):
    lease = _lease(tmp_path)
    archive = stage_runtime_bundle_archive(lease, tmp_path / "staging")
    Path(lease.locations["bin/alpha.dll"]).write_bytes(b"changed")
    with pytest.raises(RuntimeBundleArchiveError, match="content is unavailable"):
        stage_runtime_bundle_archive(lease, tmp_path / "other")

    archive.path.write_bytes(b"corrupt")
    with pytest.raises(RuntimeBundleArchiveError, match="changed"):
        verify_runtime_bundle_archive(archive)


def test_existing_archive_is_revalidated_before_reuse(tmp_path):
    lease = _lease(tmp_path)
    archive = stage_runtime_bundle_archive(lease, tmp_path / "staging")
    archive.path.write_bytes(b"not a zip")
    with pytest.raises(RuntimeBundleArchiveError, match="invalid"):
        stage_runtime_bundle_archive(lease, tmp_path / "staging")


def test_extracts_catalogued_bundle_after_full_verification(tmp_path):
    lease = _lease(tmp_path)
    archive = stage_runtime_bundle_archive(lease, tmp_path / "staging")
    extracted = extract_runtime_bundle_archive(
        archive.path,
        tmp_path / "cluster-runtime",
        manifest=lease.manifest,
        archive_checksum=archive.checksum,
    )

    assert extracted["bin/selena.exe"].read_bytes() == b"selena"
    assert extracted["runtime/Runtime.xml"].read_text(encoding="utf-8") == "<runtime />"
    assert not (tmp_path / "cluster-runtime" / "runtime-bundle.json").exists()
    assert set(extracted) == {item.relative_path for item in lease.manifest.files}


def test_extract_rejects_catalog_checksum_mismatch(tmp_path):
    lease = _lease(tmp_path)
    archive = stage_runtime_bundle_archive(lease, tmp_path / "staging")
    with pytest.raises(RuntimeBundleArchiveError, match="checksum changed"):
        extract_runtime_bundle_archive(
            archive.path,
            tmp_path / "cluster-runtime",
            manifest=lease.manifest,
            archive_checksum="sha256:" + "0" * 64,
        )
    assert not (tmp_path / "cluster-runtime").exists()
