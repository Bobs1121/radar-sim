from pathlib import Path

import pytest

from core.runtime_bundle import (
    RuntimeBundleError,
    RuntimeSourceEvidence,
    discover_runtime_bundle,
    discover_simulation_assets,
    verify_runtime_bundle,
)


def _source(**patch):
    values = {
        "branch": "feature/a",
        "commit": "1" * 40,
        "dirty": False,
        "dirty_fingerprint": "",
        "build_mode": "RelWithDebInfo",
        "toolchain_fingerprint": "tcc:selena-devlatest",
        "adapter_key": "internal:bydod25",
    }
    values.update(patch)
    return RuntimeSourceEvidence(**values)


def test_runtime_xml_is_content_bound_with_exe_and_all_colocated_dlls(tmp_path):
    bin_dir = tmp_path / "output"
    bin_dir.mkdir()
    exe = bin_dir / "selena.exe"
    exe.write_bytes(b"exe")
    (bin_dir / "selena_core.dll").write_bytes(b"core")
    (bin_dir / "plugin.dll").write_bytes(b"plugin")
    (bin_dir / "debug.pdb").write_bytes(b"pdb")
    runtime = tmp_path / "Runtime_branch_a.xml"
    runtime.write_text("<selena/>", encoding="utf-8")

    lease = discover_runtime_bundle(exe, runtime, source=_source(), created_at=100)
    paths = {item.relative_path for item in lease.manifest.files}
    assert paths == {
        "bin/selena.exe",
        "bin/selena_core.dll",
        "bin/plugin.dll",
        "runtime/Runtime_branch_a.xml",
    }
    assert "pdb" not in str(lease.public_dict).lower()
    assert str(tmp_path) not in str(lease.public_dict)
    verify_runtime_bundle(lease)


def test_runtime_change_creates_a_different_bundle_id(tmp_path):
    exe = tmp_path / "selena.exe"
    exe.write_bytes(b"exe")
    runtime = tmp_path / "Runtime.xml"
    runtime.write_text("<selena version='a'/>", encoding="utf-8")
    first = discover_runtime_bundle(exe, runtime, source=_source(), created_at=100)
    runtime.write_text("<selena version='b'/>", encoding="utf-8")
    second = discover_runtime_bundle(exe, runtime, source=_source(), created_at=101)
    assert first.manifest.id != second.manifest.id


def test_source_evidence_is_part_of_bundle_identity(tmp_path):
    exe = tmp_path / "selena.exe"
    exe.write_bytes(b"exe")
    runtime = tmp_path / "Runtime.xml"
    runtime.write_text("<selena/>", encoding="utf-8")
    first = discover_runtime_bundle(exe, runtime, source=_source(branch="a"), created_at=100)
    second = discover_runtime_bundle(exe, runtime, source=_source(branch="b"), created_at=100)
    assert first.manifest.id != second.manifest.id


def test_internal_adapter_identity_is_bound_but_not_public(tmp_path):
    exe = tmp_path / "selena.exe"
    exe.write_bytes(b"exe")
    runtime = tmp_path / "Runtime.xml"
    runtime.write_text("<selena/>", encoding="utf-8")
    first = discover_runtime_bundle(exe, runtime, source=_source(adapter_key="internal:a"), created_at=100)
    second = discover_runtime_bundle(exe, runtime, source=_source(adapter_key="internal:b"), created_at=100)
    assert first.manifest.id != second.manifest.id
    assert "adapter_key" not in str(first.public_dict)
    assert "internal:a" not in str(first.public_dict)


def test_bundle_verification_fails_after_dll_changes(tmp_path):
    exe = tmp_path / "selena.exe"
    exe.write_bytes(b"exe")
    dll = tmp_path / "plugin.dll"
    dll.write_bytes(b"plugin")
    runtime = tmp_path / "Runtime.xml"
    runtime.write_text("<selena/>", encoding="utf-8")
    lease = discover_runtime_bundle(exe, runtime, source=_source(), created_at=100)
    dll.write_bytes(b"changed")
    with pytest.raises(RuntimeBundleError, match="content changed"):
        verify_runtime_bundle(lease)


def test_adapter_and_matfilter_are_required_but_not_inside_runtime_bundle(tmp_path):
    adapter = tmp_path / "adapter.txt"
    mat_filter = tmp_path / "signals.filter"
    adapter.write_text("map a b", encoding="utf-8")
    mat_filter.write_text("include .*", encoding="utf-8")
    assets = discover_simulation_assets(adapter, mat_filter)
    assert assets.public_dict["adapter"]["name"] == "adapter.txt"
    assert assets.public_dict["mat_filter"]["name"] == "signals.filter"
    assert str(tmp_path) not in str(assets.public_dict)


def test_adapter_file_is_optional_in_discover_simulation_assets(tmp_path):
    mat_filter = tmp_path / "signals.filter"
    mat_filter.write_text("include .*", encoding="utf-8")
    assets = discover_simulation_assets("", mat_filter)
    assert assets.adapter is None
    assert "adapter" not in assets.public_dict
    assert assets.public_dict["mat_filter"]["name"] == "signals.filter"
    assert "adapter" not in assets.locations


def test_dirty_source_requires_fingerprint():
    with pytest.raises(RuntimeBundleError, match="requires a fingerprint"):
        _source(dirty=True)
