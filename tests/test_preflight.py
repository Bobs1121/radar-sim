"""Tests for the pre-flight compatibility engine (PRD §1.6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import preflight


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_runtime_xml(tmp_path: Path) -> Path:
    p = tmp_path / "runtime.xml"
    p.write_text(
        "<?xml version='1.0' encoding='UTF-8'?>\n<selena>\n"
        "  <runnable name='DataPlayer' />\n"
        "  <runnable name='DataRecorder' />\n"
        "  <runnable name='g_Golf_Fct_Spp_RunnableSpp' />\n"
        "</selena>\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def base_config(tmp_path: Path, tmp_runtime_xml: Path) -> dict:
    return {
        "_meta": {"project": "testproj"},
        "project": {"name": "testproj"},
        "build": {"selena_branch": "BL03RC01"},
        "paths": {"build_output": str(tmp_path)},
        "assets": {"runtime_xml": str(tmp_runtime_xml)},
        "simulation": {"runtime_xml": str(tmp_runtime_xml)},
    }


def _patch_exe(monkeypatch, exe_path: str) -> None:
    monkeypatch.setattr(
        "core.config.resolve_selena_executable",
        lambda config, build_mode=None: exe_path,
    )


def _write_sig(exe_path: Path, branch: str, commit: str = "abc12345") -> None:
    exe_path.write_text("MZ", encoding="latin-1")  # tiny fake binary
    sig = exe_path.with_suffix(exe_path.suffix + ".sig.json")
    sig.write_text(json.dumps({"branch": branch, "commit": commit}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Check 1: fingerprint
# ---------------------------------------------------------------------------

def test_fingerprint_match(base_config, tmp_path, monkeypatch):
    exe = tmp_path / "selena.exe"
    _write_sig(exe, "BL03RC01")
    _patch_exe(monkeypatch, str(exe))

    r = preflight.check_fingerprint(base_config)
    assert r.passed is True
    assert r.level == "info"
    assert "一致" in r.detail


def test_fingerprint_mismatch_hard_fails(base_config, tmp_path, monkeypatch):
    exe = tmp_path / "selena.exe"
    # Binary was actually built from 'develop', but config declares 'BL03RC01'.
    _write_sig(exe, "develop")
    _patch_exe(monkeypatch, str(exe))

    r = preflight.check_fingerprint(base_config)
    assert r.level == "error"
    assert r.passed is False
    assert "develop" in r.detail and "BL03RC01" in r.detail
    assert r.repair_hint


def test_fingerprint_degrades_without_signature(base_config, tmp_path, monkeypatch):
    exe = tmp_path / "selena.exe"
    exe.write_text("MZ", encoding="latin-1")  # no .sig.json
    _patch_exe(monkeypatch, str(exe))

    r = preflight.check_fingerprint(base_config)
    # Degrade to warning — must NOT hard-fail when info is simply absent.
    assert r.level == "warning"
    assert r.passed is True


def test_fingerprint_skipped_without_branch(tmp_path, monkeypatch):
    cfg = {"build": {}, "paths": {"build_output": str(tmp_path)}}
    _patch_exe(monkeypatch, str(tmp_path / "selena.exe"))
    r = preflight.check_fingerprint(cfg)
    assert r.level == "info"
    assert r.passed is True


# ---------------------------------------------------------------------------
# Check 2: interface consistency
# ---------------------------------------------------------------------------

def _write_interfaces(exe_path: Path, runnables: list[str]) -> None:
    exe_path.write_text("MZ", encoding="latin-1")
    p = exe_path.with_name("selena.interfaces.json")
    p.write_text(json.dumps({"runnables": runnables}), encoding="utf-8")


def test_interface_match(base_config, tmp_path, monkeypatch):
    exe = tmp_path / "selena.exe"
    _write_interfaces(exe, ["DataPlayer", "DataRecorder", "g_Golf_Fct_Spp_RunnableSpp", "Extra"])
    _patch_exe(monkeypatch, str(exe))

    r = preflight.check_interface(base_config)
    assert r.level == "info"
    assert r.passed is True
    assert "全部" in r.detail


def test_interface_missing_hard_fails(base_config, tmp_path, monkeypatch):
    exe = tmp_path / "selena.exe"
    # Binary exports only DataPlayer; Runtime.xml references 3 runnables.
    _write_interfaces(exe, ["DataPlayer"])
    _patch_exe(monkeypatch, str(exe))

    r = preflight.check_interface(base_config)
    assert r.level == "error"
    assert r.passed is False
    assert "DataRecorder" in r.detail or "RunnableSpp" in r.detail


def test_interface_degrades_without_manifest(base_config, tmp_path, monkeypatch):
    exe = tmp_path / "selena.exe"
    exe.write_text("MZ", encoding="latin-1")  # no interfaces.json
    _patch_exe(monkeypatch, str(exe))

    r = preflight.check_interface(base_config)
    assert r.level == "warning"
    assert r.passed is True


def test_interface_degrades_without_runtime_xml(tmp_path, monkeypatch):
    cfg = {"simulation": {}, "assets": {}, "paths": {"build_output": str(tmp_path)}}
    _patch_exe(monkeypatch, str(tmp_path / "selena.exe"))
    r = preflight.check_interface(cfg)
    assert r.level == "warning"
    assert r.passed is True


def test_parse_runtime_runnables_handles_malformed_xml(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<selena><runnable name='A'><runnable name='B' /></broken>", encoding="utf-8")
    # Tolerant regex sweep should still recover declared names.
    names = preflight.parse_runtime_runnables(str(bad))
    assert "A" in names and "B" in names


# ---------------------------------------------------------------------------
# Check 3: signal contract
# ---------------------------------------------------------------------------

def test_signal_contract_no_required_signals(base_config, monkeypatch):
    monkeypatch.setattr("core.config.load_signals", lambda project: [])
    r = preflight.check_signal_contract(base_config)
    assert r.level == "info"
    assert r.passed is True


def test_signal_contract_missing_input_degrades(base_config, monkeypatch):
    monkeypatch.setattr("core.config.load_signals", lambda project: [{"name": "FCTA_State"}])
    cfg = dict(base_config)
    cfg["simulation"] = {}
    cfg["paths"] = {"build_output": cfg["paths"]["build_output"]}
    r = preflight.check_signal_contract(cfg)
    assert r.level == "warning"
    assert r.passed is True


def test_signal_contract_unreadable_mf4_degrades(base_config, tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.load_signals", lambda project: [{"name": "FCTA_State"}])
    monkeypatch.setattr(preflight, "_mf4_channel_names", lambda p: None)
    cfg = dict(base_config)
    cfg["simulation"] = {"input_mf4": str(tmp_path / "nope.MF4")}
    r = preflight.check_signal_contract(cfg)
    assert r.level == "warning"
    assert r.passed is True


def test_signal_contract_all_present_passes(base_config, tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.load_signals", lambda project: [{"name": "FCTA_State"}, {"name": "BSD_Alarm"}])
    monkeypatch.setattr(preflight, "_mf4_channel_names", lambda p: {"FCTA_State", "BSD_Alarm", "Other"})
    cfg = dict(base_config)
    cfg["simulation"] = {"input_mf4": str(tmp_path / "in.MF4"), "runtime_xml": cfg["simulation"]["runtime_xml"]}
    r = preflight.check_signal_contract(cfg)
    assert r.level == "info"
    assert r.passed is True


def test_signal_contract_missing_signal_hard_fails(base_config, tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.load_signals", lambda project: [{"name": "FCTA_State"}, {"name": "MISSING_SIG"}])
    monkeypatch.setattr(preflight, "_mf4_channel_names", lambda p: {"FCTA_State", "Other"})
    cfg = dict(base_config)
    cfg["simulation"] = {"input_mf4": str(tmp_path / "in.MF4"), "runtime_xml": cfg["simulation"]["runtime_xml"]}
    r = preflight.check_signal_contract(cfg)
    assert r.level == "error"
    assert r.passed is False
    assert "MISSING_SIG" in r.detail


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def test_run_preflight_all_pass(base_config, tmp_path, monkeypatch):
    exe = tmp_path / "selena.exe"
    _write_sig(exe, "BL03RC01")
    _write_interfaces(exe, ["DataPlayer", "DataRecorder", "g_Golf_Fct_Spp_RunnableSpp"])
    _patch_exe(monkeypatch, str(exe))
    monkeypatch.setattr("core.config.load_signals", lambda project: [{"name": "FCTA_State"}])
    monkeypatch.setattr(preflight, "_mf4_channel_names", lambda p: {"FCTA_State"})

    cfg = dict(base_config)
    cfg["simulation"] = {"runtime_xml": cfg["simulation"]["runtime_xml"], "input_mf4": str(tmp_path / "in.MF4")}
    result = preflight.run_preflight(cfg)
    assert result.ok is True
    assert result.diagnostics == []
    assert len(result.checks) == 3


def test_run_preflight_hard_fails_on_mismatch(base_config, tmp_path, monkeypatch):
    exe = tmp_path / "selena.exe"
    _write_sig(exe, "develop")  # wrong branch
    _write_interfaces(exe, ["DataPlayer"])  # missing runnables
    _patch_exe(monkeypatch, str(exe))
    monkeypatch.setattr("core.config.load_signals", lambda project: [{"name": "FCTA_State"}, {"name": "MISSING"}])
    monkeypatch.setattr(preflight, "_mf4_channel_names", lambda p: {"FCTA_State"})

    cfg = dict(base_config)
    cfg["simulation"] = {"runtime_xml": cfg["simulation"]["runtime_xml"], "input_mf4": str(tmp_path / "in.MF4")}
    result = preflight.run_preflight(cfg)
    assert result.ok is False
    diags = result.diagnostics
    # All three checks should surface a human-readable diagnostic.
    assert any("指纹" in d for d in diags)
    assert any("接口" in d for d in diags)
    assert any("信号" in d for d in diags)
    d = result.to_dict()
    assert d["ok"] is False
    assert len(d["checks"]) == 3
