"""Tests for rsim check command."""

import tempfile
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from cli.check import (
    run,
    _check_build_consistency,
    _check_build_scripts,
    _check_config,
    _check_environment_vars,
    _check_repo_context,
    _check_simulation_config,
)


def test_check_config_valid():
    """Test config validation with valid configuration."""
    config = {
        "project": {
            "name": "test_project",
            "platform": "gen5_selena"
        },
        "paths": {
            "project_root": "/fake/path"
        }
    }
    
    with patch("cli.check.Path.exists", return_value=True):
        issues = _check_config(config)
    assert issues == []


def test_check_config_missing_sections():
    """Test config validation with missing sections."""
    config = {
        "project": {
            "name": "test_project"
        }
        # Missing paths section
    }
    
    issues = _check_config(config)
    assert any("Missing required section 'paths'" in issue for issue in issues)


def test_check_config_missing_project_name():
    """Test config validation with missing project name."""
    config = {
        "project": {},
        "paths": {
            "project_root": "/fake/path"
        }
    }
    
    with patch("cli.check.Path.exists", return_value=True):
        issues = _check_config(config)
    assert any("Missing project name" in issue for issue in issues)


def test_check_config_missing_required_paths():
    """Test config validation with missing required paths."""
    config = {
        "project": {
            "name": "test_project",
            "platform": "gen5_selena"
        },
        "paths": {}
    }
    
    issues = _check_config(config)
    assert len(issues) == 1
    assert "Missing required path 'project_root'" in issues[0]


def test_check_config_invalid_project_root():
    """Project root existence is not enforced at config-parse time."""
    config = {
        "project": {
            "name": "test_project",
            "platform": "gen5_selena"
        },
        "paths": {
            "project_root": "/nonexistent/path"
        }
    }

    issues = _check_config(config)
    assert issues == []


def test_check_build_scripts_valid():
    """Test build script checking with valid setup."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a fake project root
        project_root = Path(temp_dir) / "project"
        project_root.mkdir()
        
        # Create required build scripts
        for script_name in ("jenkins_selena_build.bat", "testbuild_BaseC0S_SINGLE.bat", "R2D2.py"):
            (project_root / script_name).touch()
        
        config = {
            "paths": {
                "project_root": str(project_root),
                "build_output": str(project_root / "build")
            }
        }
        
        issues = _check_build_scripts(config)
        assert issues == []


def test_check_build_scripts_missing_scripts():
    """Test build script checking with missing scripts."""
    with tempfile.TemporaryDirectory() as temp_dir:
        project_root = Path(temp_dir) / "project"
        project_root.mkdir()
        
        config = {
            "paths": {
                "project_root": str(project_root),
                "build_output": str(project_root / "build")
            }
        }
        
        issues = _check_build_scripts(config)
        # Should find at least one missing script
        assert len(issues) >= 1
        assert "Script not found" in issues[0]


def test_check_environment_vars_valid():
    """Test environment variable checking with valid config."""
    config = {
        "environment": {
            "BOOST_ROOT": "/fake/path"
        }
    }
    
    issues = _check_environment_vars(config)
    assert len(issues) == 0


def test_check_environment_vars_missing_required():
    """Test environment variable checking with missing required vars."""
    config = {
        "environment": {}
    }
    
    issues = _check_environment_vars(config)
    assert len(issues) == 1
    assert "Required environment variable 'BOOST_ROOT'" in issues[0]


def test_check_environment_vars_empty_var():
    """Test environment variable checking with empty variable."""
    config = {
        "environment": {
            "BOOST_ROOT": ""
        }
    }
    
    issues = _check_environment_vars(config)
    assert len(issues) == 1
    assert "Environment variable 'BOOST_ROOT' is empty" in issues[0]


def test_check_environment_vars_invalid_python_path():
    """Test environment variable checking with invalid python path."""
    config = {
        "environment": {
            "BOOST_ROOT": "/fake/path",
            "python3_path": "/nonexistent/python"
        }
    }
    
    issues = _check_environment_vars(config)
    assert len(issues) == 1
    assert "python3 path not found" in issues[0]


def test_check_build_consistency_no_build_output():
    """Missing build_output is allowed before the first build."""
    config = {
        "paths": {
            "project_root": "/fake/project",
            "build_output": ""
        }
    }

    issues = _check_build_consistency(config)
    assert issues == []


def test_check_repo_context_missing_inner_repo():
    config = {
        "repos": {
            "outer_repo_root": "C:/BYD_OVS_CB",
            "inner_repo_root": "C:/BYD_OVS_CB/apl/byd_missing",
            "inner_repo_branch": "selena_branch_x",
        },
        "build": {"selena_branch": "selena_branch_x"},
        "project_root": "C:/BYD_OVS_CB",
    }
    issues = _check_repo_context(config)
    assert any("inner repo not found" in issue.lower() for issue in issues)


def test_check_simulation_config_missing_runtime_and_dataset():
    config = {
        "assets": {
            "runtime_xml": "/missing/runtime.xml",
            "matfilefilter": "/missing/filter.txt",
        },
        "simulation": {
            "datasets": [
                {"name": "case_a", "input_dir": "/missing/data_dir"},
                {"name": "case_b", "input_mf4": "/missing/input.MF4"},
            ]
        },
    }
    issues = _check_simulation_config(config)
    assert any("runtime_xml not found" in issue for issue in issues)
    assert any("matfilefilter not found" in issue for issue in issues)
    assert any("case_a" in issue for issue in issues)
    assert any("case_b" in issue for issue in issues)


def test_check_run_with_mocked_platform():
    """Test the full run function with mocked platform."""
    # Mock config
    config = {
        "project": {
            "name": "test_project",
            "platform": "gen5_selena"
        },
        "paths": {
            "project_root": "/fake/project"
        },
        "environment": {
            "BOOST_ROOT": "/fake/boost"
        }
    }
    
    # Mock platform.check_environment to return empty list (no issues)
    with patch('cli.check.platforms.get') as mock_get_platform:
        mock_platform = Mock()
        mock_platform.check_environment.return_value = []
        mock_get_platform.return_value = mock_platform
        
        # Mock the other check functions to return empty lists
        with patch('cli.check._check_config', return_value=[]), \
             patch('cli.check._check_build_scripts', return_value=[]), \
             patch('cli.check._check_environment_vars', return_value=[]), \
             patch('cli.check._check_repo_context', return_value=[]), \
             patch('cli.check._check_simulation_config', return_value=[]), \
             patch('cli.check._check_build_consistency', return_value=[]):
            
            # Run the check command
            result = run(Mock(), config)
            
            # Should return 0 (success)
            assert result == 0


def test_check_run_with_issues():
    """Test the full run function with issues found."""
    # Mock config
    config = {
        "project": {
            "name": "test_project",
            "platform": "gen5_selena"
        },
        "paths": {
            "project_root": "/fake/project"
        },
        "environment": {
            "BOOST_ROOT": "/fake/boost"
        }
    }
    
    # Mock platform.check_environment to return issues
    with patch('cli.check.platforms.get') as mock_get_platform:
        mock_platform = Mock()
        mock_platform.check_environment.return_value = ["Platform issue found"]
        mock_get_platform.return_value = mock_platform
        
        # Mock the other check functions to return empty lists
        with patch('cli.check._check_config', return_value=[]), \
             patch('cli.check._check_build_scripts', return_value=[]), \
             patch('cli.check._check_environment_vars', return_value=[]), \
             patch('cli.check._check_repo_context', return_value=[]), \
             patch('cli.check._check_simulation_config', return_value=[]), \
             patch('cli.check._check_build_consistency', return_value=[]):
            
            # Run the check command
            result = run(Mock(), config)
            
            # Should return 1 (failure due to issues)
            assert result == 1


def test_check_run_includes_recipe_validation():
    config = {
        "_meta": {
            "recipe": "g3n_fvg3_od25",
        },
        "project": {
            "name": "test_project",
            "platform": "gen5_selena",
        },
        "paths": {
            "project_root": "/fake/project",
        },
        "environment": {
            "BOOST_ROOT": "/fake/boost",
        },
        "simulation": {},
    }

    with patch('cli.check.platforms.get') as mock_get_platform:
        mock_platform = Mock()
        mock_platform.check_environment.return_value = []
        mock_get_platform.return_value = mock_platform

        with patch('cli.check._check_config', return_value=[]), \
             patch('cli.check._check_build_scripts', return_value=[]), \
             patch('cli.check._check_environment_vars', return_value=[]), \
             patch('cli.check._check_repo_context', return_value=[]), \
             patch('cli.check._check_simulation_config', return_value=[]), \
             patch('cli.check._check_build_consistency', return_value=[]):

            result = run(Mock(), config)

            assert result == 1
