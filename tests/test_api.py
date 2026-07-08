"""Tests for core.api — stable public API surface."""

import inspect
from pathlib import Path

import pytest

import core.api as api
from core.api import (
    API_VERSION,
    CheckReport,
    PreparedRun,
    RunResult,
    SubmitResult,
    check_environment,
    list_profiles,
    load_project,
    prepare_simulation,
    run_local,
    submit_cluster,
)


def _config_with_build(tmp_path):
    build_dir = tmp_path / "build" / "dc_tools" / "selena" / "core" / "RelWithDebInfo"
    build_dir.mkdir(parents=True)
    (build_dir / "selena.exe").write_text("", encoding="utf-8")
    runtime = tmp_path / "rt.xml"
    runtime.write_text("<rt/>", encoding="utf-8")
    mf4 = tmp_path / "case.MF4"
    mf4.write_text("data", encoding="utf-8")
    return {
        "_meta": {"project": "test"},
        "project": {"name": "test", "platform": "gen5_selena"},
        "paths": {"build_output": str(tmp_path / "build")},
        "assets": {"runtime_xml": str(runtime)},
        "simulation": {"runtime_xml": str(runtime), "datasets": [{"name": "ds", "input_dir": str(tmp_path)}]},
        "selena": {"exe_pattern": "dc_tools/selena/core/{build_mode}/selena.exe", "build_mode": "RelWithDebInfo"},
    }


def test_api_exports_stable():
    assert API_VERSION == "1.0"
    for name in ["PreparedRun", "RunResult", "SubmitResult", "CheckReport",
                 "load_project", "list_profiles", "prepare_simulation",
                 "run_local", "submit_cluster", "check_environment"]:
        assert name in api.__all__, f"{name} missing from __all__"


def test_prepare_simulation_signature_stable():
    sig = inspect.signature(prepare_simulation)
    params = list(sig.parameters.keys())
    assert params == ["project", "profile", "input_path", "dataset", "backend"]
    # profile/input_path/dataset/backend are keyword-only
    for p in ["profile", "input_path", "dataset", "backend"]:
        assert sig.parameters[p].kind == inspect.Parameter.KEYWORD_ONLY


def test_run_local_signature_stable():
    sig = inspect.signature(run_local)
    assert list(sig.parameters.keys()) == ["prepared", "dry_run", "timeout", "output_mf4", "input_mf4"]
    for p in ["dry_run", "timeout", "output_mf4", "input_mf4"]:
        assert sig.parameters[p].kind == inspect.Parameter.KEYWORD_ONLY


def test_check_environment_returns_report(tmp_path, monkeypatch):
    config = _config_with_build(tmp_path)
    monkeypatch.setattr("core.api.load_project", lambda project="": config)
    report = check_environment("test", backend="local")
    assert isinstance(report, CheckReport)
    assert report.backend == "local"


def test_list_profiles_returns_list(tmp_path, monkeypatch):
    config = _config_with_build(tmp_path)
    monkeypatch.setattr("core.api.load_project", lambda project="": config)
    profiles = list_profiles("test")
    assert isinstance(profiles, list)
    assert profiles[0]["name"] == "default"


def test_prepare_simulation_resolves_inputs(tmp_path, monkeypatch):
    config = _config_with_build(tmp_path)
    monkeypatch.setattr("core.api.load_project", lambda project="": config)
    prepared = prepare_simulation("test", dataset="ds")
    assert isinstance(prepared, PreparedRun)
    assert prepared.backend == "local"
    assert len(prepared.input_files) == 1  # the one MF4 in tmp_path
    assert prepared.selena_exe.endswith("selena.exe")


def test_prepare_simulation_missing_input_warns(tmp_path, monkeypatch):
    config = _config_with_build(tmp_path)
    monkeypatch.setattr("core.api.load_project", lambda project="": config)
    prepared = prepare_simulation("test", input_path=str(tmp_path / "nope.MF4"))
    assert prepared.input_files == []
    assert any("not found" in w for w in prepared.warnings)


def test_run_local_no_input_returns_error(tmp_path, monkeypatch):
    config = _config_with_build(tmp_path)
    monkeypatch.setattr("core.api.load_project", lambda project="": config)
    prepared = prepare_simulation("test", dataset="ds")
    prepared.input_files = []  # simulate empty
    result = run_local(prepared, dry_run=True)
    assert isinstance(result, RunResult)
    assert not result.success
    assert result.return_code == 1


def test_run_local_dry_run(tmp_path, monkeypatch):
    config = _config_with_build(tmp_path)
    monkeypatch.setattr("core.api.load_project", lambda project="": config)
    prepared = prepare_simulation("test", dataset="ds")
    # dry_run invokes rsim.py run --dry-run; the project name "test" won't load,
    # but dry_run path still constructs the command. We just assert it returns a RunResult.
    result = run_local(prepared, dry_run=True)
    assert isinstance(result, RunResult)
