"""Tests for Selena runtime package harvesting (PRD §1.7.2)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from core.repo import (
    HARVEST_PACKAGE_NAME,
    HARVEST_SUFFIXES,
    harvest_runtime_package,
)


def _make_build_output(tmp_path: Path) -> Path:
    """Create a fake build_output tree with an exe, DLLs, and noise."""
    bo = tmp_path / "build_output"
    bo.mkdir()
    (bo / "selena.exe").write_bytes(b"MZ")
    (bo / "Qt5Core.dll").write_bytes(b"qt")
    (bo / "boost_thread.dll").write_bytes(b"boost")
    (bo / "Config.cfg").write_text("nogui=true", encoding="utf-8")
    (bo / "runtime.xml").write_text("<selena/>", encoding="utf-8")
    # Noise that must NOT be harvested.
    (bo / "main.obj").write_bytes(b"obj")
    (bo / "selena.pdb").write_bytes(b"pdb")
    (bo / "CMakeCache.txt").write_text("cache", encoding="utf-8")
    # Subdir with a private DLL — must be harvested with subdir arc.
    sub = bo / "plugins"
    sub.mkdir()
    (sub / "algmodule.dll").write_bytes(b"alg")
    return bo


def test_harvest_collects_exe_dll_cfg_xml(tmp_path):
    bo = _make_build_output(tmp_path)
    config = {"build": {"build_output": str(bo)}}
    zip_path = harvest_runtime_package(config, dest_dir=str(tmp_path / "pkg"))
    assert Path(zip_path).name == HARVEST_PACKAGE_NAME
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "selena.exe" in names
    assert "Qt5Core.dll" in names
    assert "boost_thread.dll" in names
    assert "Config.cfg" in names
    assert "runtime.xml" in names
    assert "plugins/algmodule.dll" in names


def test_harvest_excludes_intermediate_artifacts(tmp_path):
    bo = _make_build_output(tmp_path)
    config = {"build": {"build_output": str(bo)}}
    zip_path = harvest_runtime_package(config, dest_dir=str(tmp_path / "pkg"))
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "main.obj" not in names
    assert "selena.pdb" not in names
    assert "CMakeCache.txt" not in names


def test_harvest_fails_loud_when_no_artifacts(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    config = {"build": {"build_output": str(empty)}}
    with pytest.raises(FileNotFoundError):
        harvest_runtime_package(config, dest_dir=str(tmp_path / "pkg"))


def test_harvest_fails_loud_when_build_output_missing(tmp_path):
    config = {"build": {"build_output": str(tmp_path / "nope")}}
    with pytest.raises(FileNotFoundError):
        harvest_runtime_package(config, dest_dir=str(tmp_path / "pkg"))


def test_harvest_uses_config_build_output_by_default(tmp_path):
    bo = _make_build_output(tmp_path)
    # No explicit build_output_dir argument — must derive from config.
    config = {"build": {"build_output": str(bo)}}
    zip_path = harvest_runtime_package(config, dest_dir=str(tmp_path / "pkg"))
    assert Path(zip_path).exists()


def test_harvest_default_dest_local_packages_dir(tmp_path):
    bo = _make_build_output(tmp_path)
    config = {"build": {"build_output": str(bo)}}  # no cluster.workspace_root
    zip_path = harvest_runtime_package(config)
    # Default dest is build_output.parent / "packages".
    assert Path(zip_path).parent.name == "packages"
    assert Path(zip_path).parent.parent == bo.parent


def test_harvest_atomic_overwrite(tmp_path):
    """Re-harvesting replaces the existing zip atomically (no .tmp leftover)."""
    bo = _make_build_output(tmp_path)
    config = {"build": {"build_output": str(bo)}}
    dest = tmp_path / "pkg"
    first = harvest_runtime_package(config, dest_dir=str(dest))
    second = harvest_runtime_package(config, dest_dir=str(dest))
    assert first == second
    assert not (dest / "selena_runtime_package.zip.tmp").exists()
    assert Path(second).exists()


def test_harvest_suffixes_constant_covers_exe_dll_cfg_xml():
    for s in (".exe", ".dll", ".cfg", ".xml"):
        assert s in HARVEST_SUFFIXES
