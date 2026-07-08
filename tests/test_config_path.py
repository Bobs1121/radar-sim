"""Tests for load_config_from_path (path-driven config loading)."""

from pathlib import Path

import pytest


def _make_standard_project(tmp_path, monkeypatch, name="demostd"):
    """Create config/projects/<name>/ with config.yaml + local.yaml."""
    projects = tmp_path / "projects"
    pdir = projects / name
    pdir.mkdir(parents=True)
    (pdir / "config.yaml").write_text(
        f"project:\n  name: {name}\n  platform: gen5_selena\n"
        f"paths:\n  project_root: '{tmp_path}'\n  build_output: '{tmp_path / 'build'}'\n"
        "selena:\n  exe_pattern: 'dc_tools/selena/core/{build_mode}/selena.exe'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("core.config.get_projects_dir", lambda: projects)
    monkeypatch.setattr("core.config.get_config_dir", lambda: tmp_path)
    return pdir


def test_load_config_from_path_standard(tmp_path, monkeypatch):
    """Path-driven load from standard layout == load_config(project)."""
    from core.config import load_config, load_config_from_path
    pdir = _make_standard_project(tmp_path, monkeypatch, "demostd")
    (pdir / "local.yaml").write_text(
        "repos:\n  inner_repo_root: C:/repo\nbuild:\n  selena_branch: dev\n",
        encoding="utf-8",
    )
    by_name = load_config("demostd")
    by_path = load_config_from_path(pdir / "local.yaml")
    assert by_path["_meta"]["project"] == "demostd"
    assert by_path["repos"]["inner_repo_root"] == by_name["repos"]["inner_repo_root"]
    assert by_path["build"]["selena_branch"] == "dev"


def test_load_config_from_path_free_layout(tmp_path, monkeypatch):
    """local.yaml in arbitrary location + explicit assets.root → loads without config/projects/."""
    from core.config import load_config_from_path
    monkeypatch.setattr("core.config.get_projects_dir", lambda: tmp_path / "projects")
    monkeypatch.setattr("core.config.get_config_dir", lambda: tmp_path)
    assets_dir = tmp_path / "myassets"
    assets_dir.mkdir()
    (assets_dir / "runtime.xml").write_text("<rt/>", encoding="utf-8")

    local = tmp_path / "freepath.yaml"
    local.write_text(
        f"project:\n  name: freeproj\n  platform: gen5_selena\n"
        f"paths:\n  project_root: '{tmp_path}'\n  build_output: '{tmp_path / 'build'}'\n"
        f"assets:\n  root: '{assets_dir}'\n  runtime_xml: '{assets_dir / 'runtime.xml'}'\n"
        "repos:\n  inner_repo_root: C:/repo\n"
        "selena:\n  exe_pattern: 'dc_tools/selena/core/{build_mode}/selena.exe'\n",
        encoding="utf-8",
    )
    cfg = load_config_from_path(local)
    assert cfg["_meta"]["project"] == "freeproj"
    assert cfg["assets"]["root"] == str(assets_dir)
    assert cfg["repos"]["inner_repo_root"] == "C:/repo"


def test_infer_project_name_from_path_standard(tmp_path, monkeypatch):
    from core.config import _infer_project_name_from_path, get_projects_dir
    projects = tmp_path / "projects"
    (projects / "myproj").mkdir(parents=True)
    monkeypatch.setattr("core.config.get_projects_dir", lambda: projects)
    local = projects / "myproj" / "local.yaml"
    local.write_text("", encoding="utf-8")
    assert _infer_project_name_from_path(local) == "myproj"


def test_infer_project_name_from_content(tmp_path):
    from core.config import _infer_project_name_from_path
    local = tmp_path / "x.yaml"
    local.write_text("project:\n  name: explicit_name\n", encoding="utf-8")
    assert _infer_project_name_from_path(local, {"project": {"name": "explicit_name"}}) == "explicit_name"


def test_infer_project_name_from_stem(tmp_path):
    from core.config import _infer_project_name_from_path
    local = tmp_path / "customproj.yaml"
    local.write_text("", encoding="utf-8")
    # Not under projects/, no project.name → use stem
    assert _infer_project_name_from_path(local) == "customproj"


def test_resolve_project_assets_explicit_root(tmp_path):
    from core.config import _resolve_project_assets
    assets = _resolve_project_assets("any", {"assets": {"root": str(tmp_path)}})
    assert assets == tmp_path


def test_resolve_project_assets_fallback(tmp_path, monkeypatch):
    from core.config import _resolve_project_assets, get_projects_dir
    projects = tmp_path / "projects"
    monkeypatch.setattr("core.config.get_projects_dir", lambda: projects)
    assets = _resolve_project_assets("fallback_proj", None)
    assert assets == projects / "fallback_proj" / "assets"


def test_load_config_from_path_missing_file(tmp_path):
    from core.config import load_config_from_path
    with pytest.raises(FileNotFoundError):
        load_config_from_path(tmp_path / "nope.yaml")
