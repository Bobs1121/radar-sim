"""Tests for core.environment CheckReport + core.repo checks."""

from pathlib import Path

from core.cluster import CheckItem
from core.environment import CheckReport, check_for_backend, check_local_environment
from core.repo import check_repo_context, prepare_repo_context


def _config_with_build(tmp_path):
    """Config with a real local selena.exe + assets. No repos (skip git checks)."""
    build_dir = tmp_path / "build" / "dc_tools" / "selena" / "core" / "RelWithDebInfo"
    build_dir.mkdir(parents=True)
    (build_dir / "selena.exe").write_text("", encoding="utf-8")
    runtime = tmp_path / "rt.xml"
    runtime.write_text("<rt/>", encoding="utf-8")
    return {
        "_meta": {"project": "test"},
        "project": {"name": "test", "platform": "gen5_selena"},
        "paths": {"build_output": str(tmp_path / "build")},
        "assets": {"runtime_xml": str(runtime)},
        "simulation": {"runtime_xml": str(runtime), "datasets": []},
        "selena": {"exe_pattern": "dc_tools/selena/core/{build_mode}/selena.exe", "build_mode": "RelWithDebInfo"},
    }


def test_check_item_severity_defaults():
    item = CheckItem("x", True, "ok")
    assert item.severity == "error"
    assert item.category == ""
    info = CheckItem("y", True, "ok", "info", "repo")
    assert info.severity == "info"
    assert info.category == "repo"


def test_check_report_ok_ignores_warnings():
    report = CheckReport("local", "default", [
        CheckItem("a", True, "fine", "info"),
        CheckItem("b", False, "a warning", "warning"),
    ])
    assert report.ok is True
    assert len(report.warnings) == 1
    assert report.errors == []


def test_check_report_not_ok_on_error():
    report = CheckReport("local", "default", [
        CheckItem("a", False, "broken", "error"),
        CheckItem("b", True, "fine", "info"),
    ])
    assert report.ok is False
    assert len(report.errors) == 1


def test_check_report_iterable_for_compat():
    report = CheckReport("local", "default", [CheckItem("a", True, "x", "info")])
    assert [i.name for i in report] == ["a"]


def test_check_repo_context_missing_inner():
    config = {"repos": {"outer_repo_root": "C:/x", "inner_repo_root": "C:/missing/y"}}
    items = check_repo_context(config)
    inner_item = next(i for i in items if "Inner repo" in i.name and not i.ok)
    assert i_severity(inner_item) == "error"
    assert "inner repo not found" in inner_item.detail.lower()


def test_check_repo_context_no_target_branch_clean(tmp_path):
    # Real git repo with no target branch → only info items, no warnings.
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
    config = {"repos": {"outer_repo_root": str(repo), "inner_repo_root": str(repo)}}
    items = check_repo_context(config)
    assert all(i.ok for i in items)


def test_check_repo_context_branch_mismatch_warning(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "branch", "feature"], cwd=str(repo), capture_output=True)
    config = {
        "repos": {"outer_repo_root": str(repo), "inner_repo_root": str(repo)},
        "build": {"selena_branch": "feature"},
    }
    items = check_repo_context(config)
    branch_item = next(i for i in items if "branch" in i.name.lower() and not i.ok)
    assert i_severity(branch_item) == "warning"
    assert "feature" in branch_item.detail


def test_prepare_repo_context_no_branch_returns_empty():
    assert prepare_repo_context({"repos": {"inner_repo_root": "C:/x"}}) == ""


def test_check_local_environment_passes_with_build(tmp_path):
    config = _config_with_build(tmp_path)
    items = check_local_environment(config)
    # selena.exe exists → no error-severity selena failure
    selena_items = [i for i in items if i.category == "selena"]
    assert any(i.ok for i in selena_items)
    assert not any(i.severity == "error" and not i.ok for i in items)


def test_check_local_environment_missing_selena_is_error(tmp_path):
    config = _config_with_build(tmp_path)
    config["paths"]["build_output"] = str(tmp_path / "nope")  # selena.exe won't resolve
    items = check_local_environment(config)
    selena_item = next(i for i in items if i.name == "Selena executable")
    assert not selena_item.ok
    assert i_severity(selena_item) == "error"


def test_check_for_backend_returns_report(tmp_path):
    config = _config_with_build(tmp_path)
    report = check_for_backend(config, "local")
    assert isinstance(report, CheckReport)
    assert report.backend == "local"
    assert report.profile == "default"


def i_severity(item: CheckItem) -> str:
    return item.severity
