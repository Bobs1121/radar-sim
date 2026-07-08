"""Tests for core.tcc: itc2 detection, ITO mirror failover, toolcollection check."""

from pathlib import Path
from unittest.mock import patch

import pytest

import core.tcc as tcc
from core.tcc import (
    Itc2Status,
    ToolCollectionStatus,
    check_toolcollection,
    detect_itc2,
    detect_ito_share,
    ensure_itc2,
    get_init_bat_path,
    read_required_toolcollection,
)


def test_detect_itc2_missing(tmp_path):
    config = {"tcc": {"itc2_exe": str(tmp_path / "nope.exe")}}
    status = detect_itc2(config)
    assert status.installed is False
    assert "not found" in status.detail


def test_detect_itc2_present(tmp_path):
    exe = tmp_path / "itc2" / "itc2.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    (exe.parent / "version.json").write_text('{"version": {"major":1,"minor":18,"revision":3}}', encoding="utf-8")
    config = {"tcc": {"itc2_exe": str(exe)}}
    status = detect_itc2(config)
    assert status.installed is True
    assert status.version == "1.18.3"


def test_read_required_toolcollection_from_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "ip_if").mkdir(parents=True)
    (repo / "ip_if" / "tcc_toolversion_itc2.txt").write_text("IF:BTC-7.0.0\n", encoding="utf-8")
    config = {"repos": {"inner_repo_root": str(repo)}}
    assert read_required_toolcollection(config) == "IF:BTC-7.0.0"


def test_read_required_toolcollection_missing_returns_empty(tmp_path):
    config = {"repos": {"inner_repo_root": str(tmp_path / "norepo")}}
    assert read_required_toolcollection(config) == ""


def test_read_required_toolcollection_no_repo():
    assert read_required_toolcollection({}) == ""


def test_detect_ito_share_config_override(tmp_path):
    # Configured mirror should be tried first; if it has itc2, return it.
    config = {"tcc": {"ito_share": r"\\fake_share\ito"}}
    with patch("core.tcc._ito_mirror_has_itc2", side_effect=lambda m: m == r"\\fake_share\ito"):
        ok, share = detect_ito_share(config)
    assert ok is True
    assert share == r"\\fake_share\ito"


def test_detect_ito_share_failover(tmp_path):
    # First mirror down, second up.
    mirrors = [r"\\a\ito", r"\\b\ito"]
    with patch("core.tcc.ITO_MIRRORS", mirrors):
        with patch("core.tcc._ito_mirror_has_itc2", side_effect=lambda m: m == r"\\b\ito"):
            ok, share = detect_ito_share(None)
    assert ok is True
    assert share == r"\\b\ito"


def test_detect_ito_share_all_down():
    with patch("core.tcc._ito_mirror_has_itc2", return_value=False):
        ok, share = detect_ito_share(None)
    assert ok is False
    assert share == ""


def test_check_toolcollection_installed(tmp_path):
    exe = tmp_path / "itc2.exe"
    exe.write_text("")
    config = {"tcc": {"itc2_exe": str(exe)}}
    # returncode 0 + stdout with a path → installed
    completed = type("R", (), {"returncode": 0, "stdout": "C:\\TCC\\Tools\\boost\\1.63.0_WIN64\n", "stderr": ""})
    with patch("subprocess.run", return_value=completed):
        with patch("core.tcc.get_init_bat_path", return_value=""):
            status = check_toolcollection(config, "IF:BTC-7.0.0")
    assert status.installed is True
    assert "1.63.0_WIN64" in status.sample_tool_path


def test_check_toolcollection_not_installed(tmp_path):
    exe = tmp_path / "itc2.exe"
    exe.write_text("")
    config = {"tcc": {"itc2_exe": str(exe)}}
    # returncode 1, no path in stdout → not installed. stderr has node noise — must be ignored.
    completed = type("R", (), {"returncode": 1, "stdout": "", "stderr": "DeprecationWarning noise"})
    with patch("subprocess.run", return_value=completed):
        status = check_toolcollection(config, "IF:BTC-7.0.0")
    assert status.installed is False


def test_check_toolcollection_no_itc2():
    config = {"tcc": {"itc2_exe": "C:/nope/itc2.exe"}}
    status = check_toolcollection(config, "IF:BTC-7.0.0")
    assert status.installed is False
    assert "itc2 not found" in status.detail


def test_check_toolcollection_ignores_stderr_noise(tmp_path):
    """itc2 is a node app; stderr may contain DeprecationWarning — must not misjudge."""
    exe = tmp_path / "itc2.exe"
    exe.write_text("")
    config = {"tcc": {"itc2_exe": str(exe)}}
    # Even with noisy stderr, returncode 0 + valid stdout path → installed.
    completed = type("R", (), {"returncode": 0, "stdout": "C:\\TCC\\Tools\\boost\\1.63.0_WIN64", "stderr": "(node:1234) DeprecationWarning"})
    with patch("subprocess.run", return_value=completed):
        with patch("core.tcc.get_init_bat_path", return_value=""):
            status = check_toolcollection(config, "IF:BTC-7.0.0")
    assert status.installed is True


def test_get_init_bat_path_fuzzy_match():
    # Glob over tcc_init dir should match the version segment.
    with patch("glob.glob", return_value=[r"C:\TCC\Tools\tcc_init\TCC_IF_Windows_BTC-7.0.0\init.bat"]):
        path = get_init_bat_path("IF:BTC-7.0.0")
    assert path.endswith(r"TCC_IF_Windows_BTC-7.0.0\init.bat")


def test_get_init_bat_path_empty():
    assert get_init_bat_path("") == ""


def test_ensure_itc2_already_installed(tmp_path):
    exe = tmp_path / "itc2.exe"
    exe.write_text("")
    (tmp_path / "version.json").write_text('{"version":{"major":1,"minor":18,"revision":3}}', encoding="utf-8")
    config = {"tcc": {"itc2_exe": str(exe)}}
    logs = []
    status = ensure_itc2(config, log=logs.append)
    assert status.installed is True
    assert any("already installed" in m for m in logs)


def test_ensure_itc2_ito_unreachable(tmp_path):
    config = {"tcc": {"itc2_exe": str(tmp_path / "nope.exe")}}
    with patch("core.tcc.detect_ito_share", return_value=(False, "")):
        status = ensure_itc2(config)
    assert status.installed is False
    assert "ITO" in status.detail or "intranet" in status.detail


def test_ensure_environment_short_circuit_when_installed(tmp_path):
    exe = tmp_path / "itc2.exe"
    exe.write_text("")
    (tmp_path / "version.json").write_text('{"version":{"major":1,"minor":18,"revision":3}}', encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / "ip_if").mkdir(parents=True)
    (repo / "ip_if" / "tcc_toolversion_itc2.txt").write_text("IF:BTC-7.0.0\n", encoding="utf-8")
    config = {"tcc": {"itc2_exe": str(exe)}, "repos": {"inner_repo_root": str(repo)}}
    completed = type("R", (), {"returncode": 0, "stdout": "C:\\TCC\\Tools\\boost\\1.63.0_WIN64", "stderr": ""})
    with patch("subprocess.run", return_value=completed):
        with patch("core.tcc.get_init_bat_path", return_value=""):
            itc2, tc = tcc.ensure_environment(config)
    assert itc2.installed is True
    assert tc.installed is True
    assert tc.name == "IF:BTC-7.0.0"


def test_derive_dependencies_from_itc2_install(tmp_path):
    """Parse a build script that calls itc2.exe install."""
    script = tmp_path / "build.bat"
    script.write_text(
        "set /p TOOLCOLLECTION=<tcc_toolversion_itc2.txt\n"
        "C:\\TCC\\itc2\\itc2.exe install IF:BTC-7.0.0\n"
        "call c:\\TCC\\Tools\\tcc_init\\TCC_IF_Windows_BTC-7.0.0\\init.bat\n"
        "set BOOST_ROOT=%TCCPATH_boost%\n"
        "python3 R2D2.py -m config\n",
        encoding="utf-8",
    )
    config = {"build": {"env_build_script": str(script)}}
    deps = tcc.derive_dependencies_from_build_script(config)
    kinds = {d["kind"] for d in deps}
    assert "toolcollection" in kinds
    tc_dep = next(d for d in deps if d["kind"] == "toolcollection")
    assert tc_dep["name"] == "IF:BTC-7.0.0"
    assert "init_bat" in kinds
    assert any(d["name"] == "TCCPATH_boost" for d in deps if d["kind"] == "env_var")
    assert any(d["name"] == "R2D2.py" for d in deps if d["kind"] == "build_entry")


def test_derive_dependencies_set_toolcollection(tmp_path):
    """Parse a build script using 'set TOOLCOLLECTION=' instead of itc2 install."""
    script = tmp_path / "build.bat"
    script.write_text(
        "set TOOLCOLLECTION=TCC_IF_Windows_DevLatest\n"
        "call c:\\TCC\\Tools\\tcc_init\\TCC_IF_Windows_DevLatest\\init.bat\n",
        encoding="utf-8",
    )
    config = {"build": {"env_build_script": str(script)}}
    deps = tcc.derive_dependencies_from_build_script(config)
    tc_dep = next(d for d in deps if d["kind"] == "toolcollection")
    assert tc_dep["name"] == "TCC_IF_Windows_DevLatest"


def test_derive_dependencies_no_script_returns_empty():
    config = {"build": {}}
    assert tcc.derive_dependencies_from_build_script(config) == []


def test_derive_dependencies_falls_back_to_selena_script(tmp_path):
    """When env_build_script is absent, fall back to selena_build_script."""
    script = tmp_path / "jenkins.bat"
    script.write_text("itc2.exe install IF:BTC-7.0.0\n", encoding="utf-8")
    config = {"build": {"selena_build_script": str(script)}}
    deps = tcc.derive_dependencies_from_build_script(config)
    assert any(d["name"] == "IF:BTC-7.0.0" for d in deps if d["kind"] == "toolcollection")


def test_auto_repair_all_installed(tmp_path, monkeypatch):
    """itc2 + toolcollection both ready → ok, no install call."""
    exe = tmp_path / "itc2.exe"
    exe.write_text("")
    (tmp_path / "version.json").write_text('{"version":{"major":1,"minor":18,"revision":3}}', encoding="utf-8")
    config = {"tcc": {"itc2_exe": str(exe)}, "build": {}}
    monkeypatch.setattr("core.tcc.detect_itc2", lambda c: tcc.Itc2Status(True, str(exe), "1.18.3"))
    monkeypatch.setattr("core.tcc.derive_dependencies_from_build_script", lambda c: [])
    monkeypatch.setattr("core.tcc.read_required_toolcollection", lambda c: "IF:BTC-7.0.0")
    completed = type("R", (), {"returncode": 0, "stdout": "C:\\TCC\\Tools\\boost\\1.63.0_WIN64", "stderr": ""})
    monkeypatch.setattr("core.tcc.subprocess.run", lambda *a, **k: completed)
    monkeypatch.setattr("core.tcc.get_init_bat_path", lambda tc: "")
    report = tcc.auto_repair_environment(config)
    assert report.ok is True
    assert report.toolcollection == "IF:BTC-7.0.0"


def test_auto_repair_install_missing(tmp_path, monkeypatch):
    """itc2 ok, tc missing → install called → ok."""
    exe = tmp_path / "itc2.exe"
    exe.write_text("")
    config = {"tcc": {"itc2_exe": str(exe)}, "build": {}}
    monkeypatch.setattr("core.tcc.detect_itc2", lambda c: tcc.Itc2Status(True, str(exe), "1.18.3"))
    monkeypatch.setattr("core.tcc.derive_dependencies_from_build_script",
                        lambda c: [{"kind": "toolcollection", "name": "IF:BTC-7.0.0"}])
    # First check_toolcollection → not installed; after install → installed.
    call_count = {"n": 0}
    def fake_check(c, tc):
        call_count["n"] += 1
        return tcc.ToolCollectionStatus(name=tc, installed=(call_count["n"] > 1), sample_tool_path="C:/boost" if call_count["n"] > 1 else "")
    monkeypatch.setattr("core.tcc.check_toolcollection", fake_check)
    monkeypatch.setattr("core.tcc.install_toolcollection",
                        lambda c, tc, log=None: tcc.InstallResult(True, 0, "installed", "", "ok"))
    report = tcc.auto_repair_environment(config)
    assert report.ok is True
    assert report.toolcollection == "IF:BTC-7.0.0"


def test_auto_repair_no_tc_derivable(tmp_path, monkeypatch):
    """derive + read both return empty → fail with clear message."""
    exe = tmp_path / "itc2.exe"
    exe.write_text("")
    config = {"tcc": {"itc2_exe": str(exe)}, "build": {}, "repos": {}}
    monkeypatch.setattr("core.tcc.detect_itc2", lambda c: tcc.Itc2Status(True, str(exe), "1.18.3"))
    monkeypatch.setattr("core.tcc.derive_dependencies_from_build_script", lambda c: [])
    monkeypatch.setattr("core.tcc.read_required_toolcollection", lambda c: "")
    report = tcc.auto_repair_environment(config)
    assert report.ok is False
    assert "toolcollection" in report.summary or "推导" in report.summary


def test_auto_repair_derive_preferred_over_read(tmp_path, monkeypatch):
    """Script has 'itc2 install IF:BTC-7.0.0', repo has IF:BTC-8.0.0 → use script's 7.0.0."""
    exe = tmp_path / "itc2.exe"
    exe.write_text("")
    config = {"tcc": {"itc2_exe": str(exe)}, "build": {}}
    monkeypatch.setattr("core.tcc.detect_itc2", lambda c: tcc.Itc2Status(True, str(exe), "1.18.3"))
    monkeypatch.setattr("core.tcc.derive_dependencies_from_build_script",
                        lambda c: [{"kind": "toolcollection", "name": "IF:BTC-7.0.0"}])
    monkeypatch.setattr("core.tcc.read_required_toolcollection", lambda c: "IF:BTC-8.0.0")
    completed = type("R", (), {"returncode": 0, "stdout": "C:\\boost", "stderr": ""})
    monkeypatch.setattr("core.tcc.subprocess.run", lambda *a, **k: completed)
    monkeypatch.setattr("core.tcc.get_init_bat_path", lambda tc: "")
    report = tcc.auto_repair_environment(config)
    assert report.toolcollection == "IF:BTC-7.0.0"  # derive wins


def test_auto_repair_itc2_missing_ito_down(tmp_path, monkeypatch):
    """itc2 missing + ITO unreachable → short-circuit, no derive/install attempt."""
    config = {"tcc": {"itc2_exe": str(tmp_path / "nope.exe")}, "build": {}}
    monkeypatch.setattr("core.tcc.detect_itc2", lambda c: tcc.Itc2Status(False, str(tmp_path / "nope.exe")))
    monkeypatch.setattr("core.tcc.detect_ito_share", lambda c: (False, ""))
    derive_called = {"v": False}
    monkeypatch.setattr("core.tcc.derive_dependencies_from_build_script",
                        lambda c: derive_called.__setitem__("v", True) or [])
    report = tcc.auto_repair_environment(config)
    assert report.ok is False
    assert derive_called["v"] is False  # short-circuited, derive not called
