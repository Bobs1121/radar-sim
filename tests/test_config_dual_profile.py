"""Tests for dual-profile local.yaml (local-build + existing-selena)."""

from pathlib import Path

import pytest


def _make_project(tmp_path, monkeypatch):
    project_dir = tmp_path / "projects" / "demo"
    project_dir.mkdir(parents=True)
    (project_dir / "config.yaml").write_text(
        "project:\n  name: demo\n  platform: gen5_selena\n"
        "paths:\n  project_root: '" + str(tmp_path).replace("\\", "/") + "'\n"
        "  build_output: '" + str(tmp_path / "build").replace("\\", "/") + "'\n"
        "selena:\n  exe_pattern: 'dc_tools/selena/core/{build_mode}/selena.exe'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("core.config.get_projects_dir", lambda: tmp_path / "projects")
    monkeypatch.setattr("core.config.get_config_dir", lambda: tmp_path)
    monkeypatch.setattr("core.config.get_default_project", lambda: "demo")
    return project_dir


def test_save_writes_dual_profiles(tmp_path, monkeypatch):
    from core.config import save_local_config
    _make_project(tmp_path, monkeypatch)
    save_local_config("demo", {
        "source": "build", "code_path": "C:/repo", "selena_build_script": "C:/jenkins.bat",
        "selena_branch": "dev", "env_build_script": "C:/cmake.bat",
        "runtime_path": "C:/rt.xml", "data_path": "D:/data",
        "selena_exe": "C:/selena.exe", "backend": "local",
    })
    import yaml
    local = yaml.safe_load((tmp_path / "projects" / "demo" / "local.yaml").read_text(encoding="utf-8"))
    names = [p["name"] for p in local["profiles"]]
    assert names == ["local-build", "existing-selena"]
    assert local["active_profile"] == "local-build"
    # both profiles share backend
    assert all(p["backend"] == "local" for p in local["profiles"])
    # existing-selena carries the exe
    ex = next(p for p in local["profiles"] if p["name"] == "existing-selena")
    assert ex["selena"]["exe"] == "C:/selena.exe"
    assert ex["selena"]["source"] == "path"


def test_get_user_config_reads_active_build(tmp_path, monkeypatch):
    from core.config import save_local_config, get_user_config
    _make_project(tmp_path, monkeypatch)
    save_local_config("demo", {
        "source": "build", "code_path": "C:/repo", "selena_branch": "dev",
        "selena_exe": "C:/selena.exe", "backend": "local",
        "env_build_script": "", "selena_build_script": "", "runtime_path": "", "data_path": "",
    })
    uc = get_user_config("demo")
    assert uc["source"] == "build"
    assert uc["active_profile"] == "local-build"
    assert uc["selena_branch"] == "dev"


def test_get_user_config_reads_active_path(tmp_path, monkeypatch):
    from core.config import save_local_config, get_user_config
    _make_project(tmp_path, monkeypatch)
    save_local_config("demo", {
        "source": "path", "code_path": "C:/repo", "selena_exe": "C:/selena.exe",
        "backend": "cluster", "env_build_script": "", "selena_build_script": "",
        "selena_branch": "", "runtime_path": "", "data_path": "",
    })
    uc = get_user_config("demo")
    assert uc["source"] == "path"
    assert uc["active_profile"] == "existing-selena"
    assert uc["selena_exe"] == "C:/selena.exe"
    assert uc["backend"] == "cluster"


def test_repeated_save_no_duplicate_profiles(tmp_path, monkeypatch):
    """Saving twice should update, not append, profiles."""
    from core.config import save_local_config
    _make_project(tmp_path, monkeypatch)
    for _ in range(2):
        save_local_config("demo", {
            "source": "build", "code_path": "C:/repo", "selena_branch": "dev",
            "selena_exe": "C:/selena.exe", "backend": "local",
            "env_build_script": "", "selena_build_script": "", "runtime_path": "", "data_path": "",
        })
    import yaml
    local = yaml.safe_load((tmp_path / "projects" / "demo" / "local.yaml").read_text(encoding="utf-8"))
    names = [p["name"] for p in local["profiles"]]
    assert names.count("local-build") == 1
    assert names.count("existing-selena") == 1


def test_repeated_save_updates_exe(tmp_path, monkeypatch):
    """Second save with a new selena_exe updates existing-selena profile."""
    from core.config import save_local_config
    _make_project(tmp_path, monkeypatch)
    save_local_config("demo", {"source": "build", "code_path": "C:/repo", "selena_exe": "C:/old.exe",
                               "backend": "local", "env_build_script": "", "selena_build_script": "",
                               "selena_branch": "", "runtime_path": "", "data_path": ""})
    save_local_config("demo", {"source": "path", "code_path": "C:/repo", "selena_exe": "C:/new.exe",
                               "backend": "local", "env_build_script": "", "selena_build_script": "",
                               "selena_branch": "", "runtime_path": "", "data_path": ""})
    import yaml
    local = yaml.safe_load((tmp_path / "projects" / "demo" / "local.yaml").read_text(encoding="utf-8"))
    ex = next(p for p in local["profiles"] if p["name"] == "existing-selena")
    assert ex["selena"]["exe"] == "C:/new.exe"
    assert local["active_profile"] == "existing-selena"


def test_legacy_single_profile_fallback(tmp_path, monkeypatch):
    """A legacy single-profile local.yaml (name: user) still loads."""
    _make_project(tmp_path, monkeypatch)
    (tmp_path / "projects" / "demo" / "local.yaml").write_text(
        "repos:\n  inner_repo_root: C:/legacy\n"
        "profiles:\n  - name: user\n    backend: local\n    selena:\n      source: build\n",
        encoding="utf-8",
    )
    from core.config import get_user_config
    uc = get_user_config("demo")
    # No active_profile marker → falls back to first non-default profile (user)
    assert uc["source"] == "build"
    assert uc["code_path"] == "C:/legacy"
