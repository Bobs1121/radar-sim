"""Tests for wizard config functions (core/config.py)."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def _tmp_projects(monkeypatch, tmp_path):
    """Redirect get_projects_dir to a temp directory so tests don't touch real config."""
    projects = tmp_path / "config" / "projects"
    projects.mkdir(parents=True)
    from core import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "get_projects_dir", lambda: projects)
    # Also redirect local_yaml_path_for_project to stay inside tmp.
    monkeypatch.setattr(cfg_mod, "local_yaml_path_for_project",
                        lambda project: projects / project / "local.yaml")
    return projects


class TestValidateWizardFields:
    def test_empty_name_fails(self, _tmp_projects):
        from core.config import validate_wizard_fields
        r = validate_wizard_fields({"project_name": "", "outer_repo_root": "C:/src"})
        assert not r["ok"]
        assert any("项目名称" in e for e in r["errors"])

    def test_invalid_name_chars(self, _tmp_projects):
        from core.config import validate_wizard_fields
        r = validate_wizard_fields({"project_name": "my project!", "outer_repo_root": "C:/src"})
        assert not r["ok"]

    def test_missing_repo_fails(self, _tmp_projects):
        from core.config import validate_wizard_fields
        r = validate_wizard_fields({"project_name": "test_proj", "outer_repo_root": ""})
        assert not r["ok"]
        assert any("源码仓" in e for e in r["errors"])

    def test_valid_minimal_passes(self, _tmp_projects):
        from core.config import validate_wizard_fields
        r = validate_wizard_fields({"project_name": "test_proj", "outer_repo_root": "C:/src"})
        assert r["ok"]
        assert not r["errors"]

    def test_duplicate_project_fails(self, _tmp_projects):
        from core.config import validate_wizard_fields
        # Create an existing project dir with config.yaml.
        (_tmp_projects / "existing_proj").mkdir()
        (_tmp_projects / "existing_proj" / "config.yaml").write_text("project:\n  name: existing_proj\n")
        r = validate_wizard_fields({"project_name": "existing_proj", "outer_repo_root": "C:/src"})
        assert not r["ok"]
        assert any("已存在" in e for e in r["errors"])

    def test_no_script_produces_warning(self, _tmp_projects):
        from core.config import validate_wizard_fields
        r = validate_wizard_fields({"project_name": "p1", "outer_repo_root": "C:/src"})
        assert r["ok"]
        assert any("编译脚本" in w for w in r["warnings"])


class TestCreateProjectFromWizard:
    def test_minimal_creates_files(self, _tmp_projects):
        from core.config import create_project_from_wizard
        result = create_project_from_wizard({
            "project_name": "wiz_test",
            "outer_repo_root": "C:/src/repo",
        })
        assert result["config_yaml_path"]
        assert Path(result["config_yaml_path"]).exists()
        assert Path(result["local_yaml_path"]).exists()
        assert result["effective_config"]

    def test_full_fields_round_trip(self, _tmp_projects):
        from core.config import create_project_from_wizard
        result = create_project_from_wizard({
            "project_name": "wiz_full",
            "platform": "gen5_selena",
            "outer_repo_root": "D:/code/repo",
            "selena_branch": "develop_evo",
            "selena_build_script": "D:/code/repo/apl/byd/selena/jenkins_selena_build.bat",
            "build_config": "full_dsp",
            "build_output": "D:/code/repo/build/full_dsp",
            "runtime_xml": "D:/data/runtime.xml",
            "adapter_file": "D:/data/adapter.txt",
            "datasets": [{"name": "test_ds", "input_dir": "D:/data/mf4"}],
            "cluster_workspace_root": "\\\\server\\share\\Cluster",
            "cluster_group": "Radar",
            "cluster_subgroup": "PSS2",
        })
        cfg = result["effective_config"]
        assert cfg.get("project", {}).get("name") == "wiz_full"
        # Check profiles were generated.
        profiles = cfg.get("profiles", [])
        profile_names = [p.get("name") for p in profiles if isinstance(p, dict)]
        assert "local-build" in profile_names
        assert "cloud-build" in profile_names

    def test_validation_failure_raises(self, _tmp_projects):
        from core.config import create_project_from_wizard
        with pytest.raises(ValueError, match="项目名称"):
            create_project_from_wizard({"project_name": "", "outer_repo_root": "C:/src"})

    def test_generated_cloud_build_profile_has_cluster_settings(self, _tmp_projects):
        from core.config import create_project_from_wizard
        result = create_project_from_wizard({
            "project_name": "wiz_cluster",
            "outer_repo_root": "C:/src",
            "cluster_group": "Radar",
            "cluster_subgroup": "PSS1",
        })
        cfg = result["effective_config"]
        profiles = cfg.get("profiles", [])
        cloud = next((p for p in profiles if isinstance(p, dict) and p.get("name") == "cloud-build"), None)
        assert cloud is not None
        assert cloud.get("backend") == "cluster"
        assert cloud.get("selena", {}).get("source") == "build"


class TestValidateWizardFieldsScenario:
    """Scenario-aware validation (T3 no_code vs T1/T2 has_code)."""

    def test_no_code_skips_repo_requirement(self, _tmp_projects):
        from core.config import validate_wizard_fields
        r = validate_wizard_fields({
            "scenario": "no_code",
            "project_name": "t3_proj",
            "selena_exe": "\\\\share\\selena.exe",
        })
        assert r["ok"]

    def test_no_code_requires_selena_exe(self, _tmp_projects):
        from core.config import validate_wizard_fields
        r = validate_wizard_fields({
            "scenario": "no_code",
            "project_name": "t3_proj",
            "selena_exe": "",
        })
        assert not r["ok"]
        assert any("Selena" in e for e in r["errors"])

    def test_has_code_still_requires_repo(self, _tmp_projects):
        from core.config import validate_wizard_fields
        r = validate_wizard_fields({
            "scenario": "has_code",
            "project_name": "t1_proj",
            "outer_repo_root": "",
        })
        assert not r["ok"]
        assert any("源码仓" in e for e in r["errors"])


class TestCreateT3Project:
    """T3 project creation: single profile, no compile."""

    def test_t3_creates_single_profile(self, _tmp_projects):
        from core.config import create_project_from_wizard
        result = create_project_from_wizard({
            "scenario": "no_code",
            "project_name": "t3_test",
            "selena_exe": "\\\\share\\selena_bl03.exe",
            "cluster_group": "Radar",
            "cluster_subgroup": "PSS1",
        })
        cfg = result["effective_config"]
        profiles = cfg.get("profiles", [])
        profile_names = [p.get("name") for p in profiles if isinstance(p, dict)]
        assert "cloud-shared" in profile_names
        assert "local-build" not in profile_names

    def test_t3_no_local_build_profile(self, _tmp_projects):
        from core.config import create_project_from_wizard
        result = create_project_from_wizard({
            "scenario": "no_code",
            "project_name": "t3_nolocal",
            "selena_exe": "\\\\share\\selena.exe",
        })
        cfg = result["effective_config"]
        profiles = cfg.get("profiles", [])
        # Should only have cloud-shared, never local-build.
        assert len([p for p in profiles if isinstance(p, dict)]) >= 1
        for p in profiles:
            if isinstance(p, dict):
                assert p.get("name") != "local-build"
