"""Tests for core/existing_selena.py."""

from pathlib import Path

import pytest

from core.existing_selena import (
    ExistingSelenaError,
    ExistingSelenaResult,
    import_existing_selena,
)


_VALID_XML = '<?xml version="1.0"?><runtime><selena/></runtime>'
_NO_XML = "<unclosed"
_EMPTY_XML = ""
_WHITESPACE_XML = "   \n  "


def _now():
    return 1700000000.0


def _mk(tmp_path, name="ovrs25_workspace", nested=False, extra_dlls=None, xml=_VALID_XML):
    existing = tmp_path / name
    existing.mkdir()
    if nested:
        bd = existing / "output" / "bin"
        bd.mkdir(parents=True)
        xd = bd
    else:
        bd = existing
        xd = existing
    (bd / "Selena.exe").write_bytes(b"fake exe content 12345")
    for dn in ["mfc140.dll", "api-ms-win-core.dll"] + (extra_dlls or []):
        (bd / dn).write_bytes(b"dll")
    rx = xd / "Runtime.xml"
    rx.write_text(xml, encoding="utf-8")
    return existing, rx


def test_direct_selena_with_dll_succeeds(tmp_path):
    ex, rx = _mk(tmp_path)
    r = import_existing_selena(ex, rx, created_at=_now())
    assert isinstance(r, ExistingSelenaResult)
    assert r.internal_project == "ovrs25"
    s = r.public_summary()
    assert "runtime_bundle" in s
    assert "archive" in s


def test_nested_selena_exe_succeeds(tmp_path):
    ex, rx = _mk(tmp_path, nested=True)
    r = import_existing_selena(ex, rx, created_at=_now())
    assert isinstance(r, ExistingSelenaResult)


def test_no_selena_exe_raises(tmp_path):
    ex = tmp_path / "ovrs25_empty"
    ex.mkdir()
    rx = ex / "Runtime.xml"
    rx.write_text(_VALID_XML, encoding="utf-8")
    (ex / "some.dll").write_bytes(b"dll")
    with pytest.raises(ExistingSelenaError, match="not found"):
        import_existing_selena(ex, rx, created_at=_now())


def test_multiple_selena_exe_raises(tmp_path):
    ex = tmp_path / "ovrs25_multi"
    ex.mkdir()
    (ex / "selena.exe").write_bytes(b"direct exe")
    sub = ex / "sub" / "bin"
    sub.mkdir(parents=True)
    (sub / "selena.exe").write_bytes(b"nested exe")
    rx = ex / "Runtime.xml"
    rx.write_text(_VALID_XML, encoding="utf-8")
    (ex / "core.dll").write_bytes(b"dll")
    with pytest.raises(ExistingSelenaError, match="multiple"):
        import_existing_selena(ex, rx, created_at=_now())


def test_no_dll_raises(tmp_path):
    ex = tmp_path / "ovrs25_no_dll"
    ex.mkdir()
    (ex / "selena.exe").write_bytes(b"exe")
    rx = ex / "Runtime.xml"
    rx.write_text(_VALID_XML, encoding="utf-8")
    with pytest.raises(ExistingSelenaError, match="no colocated"):
        import_existing_selena(ex, rx, created_at=_now())


def test_invalid_xml_raises(tmp_path):
    ex, rx = _mk(tmp_path)
    rx.write_text(_NO_XML, encoding="utf-8")
    with pytest.raises(ExistingSelenaError, match="not valid XML"):
        import_existing_selena(ex, rx, created_at=_now())


def test_empty_xml_raises(tmp_path):
    ex, rx = _mk(tmp_path)
    rx.write_text(_EMPTY_XML, encoding="utf-8")
    with pytest.raises(ExistingSelenaError, match="not be empty"):
        import_existing_selena(ex, rx, created_at=_now())


def test_whitespace_xml_raises(tmp_path):
    ex, rx = _mk(tmp_path)
    rx.write_text(_WHITESPACE_XML, encoding="utf-8")
    with pytest.raises(ExistingSelenaError, match="not be empty"):
        import_existing_selena(ex, rx, created_at=_now())


def test_ovrs25_inference(tmp_path):
    ex, rx = _mk(tmp_path, name="ovrs25_project")
    r = import_existing_selena(ex, rx, created_at=_now())
    assert r.internal_project == "ovrs25"


def test_bydod25_inference(tmp_path):
    ex, rx = _mk(tmp_path, name="byd_od25_workspace", nested=True)
    r = import_existing_selena(ex, rx, created_at=_now())
    assert r.internal_project == "bydod25"


def test_ambiguous_inference_raises(tmp_path):
    ex = tmp_path / "shared_project"
    ex.mkdir()
    (ex / "selena.exe").write_bytes(b"exe")
    rx = ex / "Runtime.xml"
    rx.write_text(_VALID_XML, encoding="utf-8")
    (ex / "ovrs25_bydod25.dll").write_bytes(b"dll")
    with pytest.raises(ExistingSelenaError, match="ambiguous"):
        import_existing_selena(ex, rx, created_at=_now())


def test_deterministic_identity(tmp_path):
    ex, rx = _mk(tmp_path)
    r1 = import_existing_selena(ex, rx, created_at=_now())
    r2 = import_existing_selena(ex, rx, created_at=_now())
    assert r1.bundle.manifest.id == r2.bundle.manifest.id


def test_all_dlls_included(tmp_path):
    ex, rx = _mk(tmp_path, extra_dlls=["extra1.dll", "extra2.dll"])
    r = import_existing_selena(ex, rx, created_at=_now())
    filenames = {Path(it.relative_path).name.lower() for it in r.bundle.manifest.files}
    assert "selena.exe" in filenames
    assert "mfc140.dll" in filenames
    assert "extra1.dll" in filenames
    assert "extra2.dll" in filenames


def test_no_physical_paths_in_public_summary(tmp_path):
    ex, rx = _mk(tmp_path)
    r = import_existing_selena(ex, rx, created_at=_now())
    ss = str(r.public_summary())
    assert str(tmp_path) not in ss
    assert str(r.exe_path) not in ss
    assert str(r.runtime_path) not in ss


def test_no_pdb_ilk_in_bundle(tmp_path):
    ex = tmp_path / "ovrs25_no_pdb"
    ex.mkdir()
    (ex / "selena.exe").write_bytes(b"exe")
    (ex / "core.dll").write_bytes(b"dll")
    (ex / "selena.pdb").write_bytes(b"pdb")
    (ex / "app.ilk").write_bytes(b"ilk")
    rx = ex / "Runtime.xml"
    rx.write_text(_VALID_XML, encoding="utf-8")
    r = import_existing_selena(ex, rx, created_at=_now())
    filenames = {Path(f.relative_path).name for f in r.bundle.manifest.files}
    # PDB / ILK should be filtered by discover_runtime_bundle
    assert "selena.pdb" not in filenames
    assert "app.ilk" not in filenames


def test_internal_project_and_key_match(tmp_path):
    ex, rx = _mk(tmp_path, name="ovrs25_workspace")
    r = import_existing_selena(ex, rx, created_at=_now())
    assert r.internal_project
    assert r.adapter_key
    assert r.bundle.manifest.source.adapter_key == r.adapter_key
