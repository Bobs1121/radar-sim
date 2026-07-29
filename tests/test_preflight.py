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


def test_project_independent_execution_never_loads_project_signal_contract(
    base_config, monkeypatch
):
    loaded = []
    monkeypatch.setattr(
        "core.config.load_signals",
        lambda project: loaded.append(project) or [{"name": "PROJECT_ONLY_SIGNAL"}],
    )
    cfg = dict(base_config)
    cfg["_project_independent_execution"] = True

    result = preflight.check_signal_contract(cfg)

    assert result.passed is True
    assert result.level == "info"
    assert loaded == []


# ---------------------------------------------------------------------------
# Runtime.xml DataPlayer -> MF4 contract (V1 strict path)
# ---------------------------------------------------------------------------

def _write_runtime_dataplayer(path: Path, port: str) -> None:
    path.write_text(
        "<selena><outport runnable='DataPlayer' port='" + port + "' /></selena>",
        encoding="utf-8",
    )


def test_runtime_data_signal_contract_warns_but_allows_missing_dataplayer_port(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime.xml"
    mf4 = tmp_path / "input.MF4"
    _write_runtime_dataplayer(runtime, "g_DataPlayer_Fcta_ParallelLanes")
    mf4.write_bytes(b"MF4")
    monkeypatch.setattr(preflight, "_mf4_channel_names", lambda _path: {"Other"})

    result = preflight.check_runtime_data_signal_contract(
        {"simulation": {"runtime_xml": str(runtime), "input_mf4": str(mf4)}}
    )

    assert result.passed is True
    assert result.level == "warning"
    assert "DataPlayer" in result.detail
    assert "g_DataPlayer_Fcta_ParallelLanes" in result.detail


def test_runtime_data_signal_contract_requires_real_header_reader(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime.xml"
    mf4 = tmp_path / "input.MF4"
    _write_runtime_dataplayer(runtime, "Input_A")
    mf4.write_bytes(b"MF4")
    monkeypatch.setattr(preflight, "_mf4_channel_names", lambda _path: None)

    result = preflight.assert_runtime_data_signal_contract(
        tmp_path, preflight.runtime_data_player_ports(str(runtime))
    )

    assert result.passed is True
    assert result.level == "warning"
    assert "未能读取有效通道目录" in result.detail


def test_runtime_data_signal_contract_malformed_runtime_is_warning_only(tmp_path):
    runtime = tmp_path / "runtime.xml"
    mf4 = tmp_path / "input.MF4"
    runtime.write_text("<selena><connection>", encoding="utf-8")
    mf4.write_bytes(b"MF4")

    result = preflight.check_runtime_data_signal_contract(
        {"simulation": {"runtime_xml": str(runtime), "input_mf4": str(mf4)}}
    )

    assert result.passed is True
    assert result.level == "warning"
    assert "未验证" in result.detail


def test_runtime_data_signal_contract_scans_other_files_after_one_unreadable_header(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime.xml"
    first = tmp_path / "broken.MF4"
    second = tmp_path / "readable.MF4"
    _write_runtime_dataplayer(runtime, "Input_A")
    first.write_bytes(b"bad")
    second.write_bytes(b"mf4")
    calls: list[str] = []

    def channels(path: str):
        calls.append(Path(path).name)
        return None if Path(path).name == "broken.MF4" else {"Other"}

    monkeypatch.setattr(preflight, "_mf4_channel_names", channels)
    result, code = preflight._runtime_data_signal_check(tmp_path, ["Input_A"])

    assert calls == ["broken.MF4", "readable.MF4"]
    assert code == "runtime_data_signal_missing"
    assert "已验证读取 1 个 MF4" in result.detail
    assert "另有 1 个 MF4 未能读取有效通道目录" in result.detail
    assert "Input_A" in result.detail


def test_runtime_data_player_port_exact_match_records_method():
    match = preflight.match_runtime_data_player_port(
        "g_Active_m_Input_A", {"g_Active_m_Input_A", "Other"}
    )

    assert match.method == "exact"
    assert match.candidates == ("g_Active_m_Input_A",)


def test_runtime_data_player_port_normalized_unique_match_is_structural():
    match = preflight.match_runtime_data_player_port(
        "g_Active_m_Input_A",
        {
            "_g_Active.Payload._._m_Input_A.TBase._._m_value",
            "_g_Other.Payload._._m_Input_A.TBase._._m_value",
        },
    )

    assert match.method == "normalized_unique"
    assert match.candidates == ("g_Active.Payload._._m_Input_A",)


def test_runtime_data_player_port_multiple_normalized_endpoints_are_ambiguous(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime.xml"
    mf4 = tmp_path / "input.MF4"
    _write_runtime_dataplayer(runtime, "g_Active_m_Input_A")
    mf4.write_bytes(b"MF4")
    monkeypatch.setattr(
        preflight,
        "_mf4_channel_names",
        lambda _path: {
            "_g_Active.Left._m_Input_A.TBase",
            "_g_Active.Right._m_Input_A.TBase",
        },
    )

    result, code = preflight._runtime_data_signal_check(
        tmp_path, preflight.runtime_data_player_ports(str(runtime))
    )

    assert code == "runtime_data_signal_ambiguous"
    assert result.passed is True
    assert result.level == "warning"
    assert "2 个候选" in result.detail


def test_runtime_data_ports_only_cover_scheduled_runnable_closure(tmp_path):
    runtime = tmp_path / "runtime.xml"
    runtime.write_text(
        "<selena>"
        "<job task='t'><runnable name='g_Active' /></job>"
        "<connection><outport runnable='DataPlayer' port='g_Active_m_Input' />"
        "<inport runnable='g_Active' port='m_Input' /></connection>"
        "<connection><outport runnable='DataPlayer' port='g_Inactive_m_Missing' />"
        "<inport runnable='g_Inactive' port='m_Missing' /></connection>"
        "</selena>",
        encoding="utf-8",
    )

    assert preflight.runtime_active_runnables(str(runtime)) == {"g_Active"}
    assert preflight.runtime_data_player_ports(str(runtime)) == ["g_Active_m_Input"]


def test_runtime_data_ports_include_upstream_runnable_in_scheduled_closure(tmp_path):
    runtime = tmp_path / "runtime.xml"
    runtime.write_text(
        "<selena>"
        "<job task='t'><runnable name='g_Active' /></job>"
        "<connection><outport runnable='g_Upstream' port='m_Output' />"
        "<inport runnable='g_Active' port='m_Input' /></connection>"
        "<connection><outport runnable='DataPlayer' port='g_Upstream_m_Source' />"
        "<inport runnable='g_Upstream' port='m_Source' /></connection>"
        "</selena>",
        encoding="utf-8",
    )

    assert preflight.runtime_active_runnables(str(runtime)) == {"g_Active", "g_Upstream"}
    assert preflight.runtime_data_player_ports(str(runtime)) == ["g_Upstream_m_Source"]


def test_runtime_data_signal_contract_does_not_treat_empty_catalog_as_missing(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime.xml"
    first = tmp_path / "one.MF4"
    second = tmp_path / "two.MF4"
    _write_runtime_dataplayer(runtime, "Input_A")
    first.write_bytes(b"mf4")
    second.write_bytes(b"mf4")
    monkeypatch.setattr(preflight, "_mf4_channel_names", lambda _path: set())

    result, code = preflight._runtime_data_signal_check(tmp_path, ["Input_A"])

    assert code == "runtime_data_signal_unverified"
    assert result.passed is True
    assert result.level == "warning"
    assert "2 个 MF4" in result.detail
    assert "Input_A" not in result.detail


def test_strict_preflight_adds_runtime_data_contract(tmp_path, base_config, monkeypatch):
    runtime = tmp_path / "runtime.xml"
    mf4 = tmp_path / "input.MF4"
    _write_runtime_dataplayer(runtime, "Input_A")
    mf4.write_bytes(b"MF4")
    monkeypatch.setattr(preflight, "_mf4_channel_names", lambda _path: {"Input_A"})
    monkeypatch.setattr("core.config.load_signals", lambda project: [])
    cfg = dict(base_config)
    cfg["simulation"] = {"runtime_xml": str(runtime), "input_mf4": str(mf4)}

    result = preflight.run_preflight(cfg, strict_runtime_data_signals=True)

    assert result.ok is True
    assert [item.name for item in result.checks][-1] == "runtime_data_signal_contract"


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
