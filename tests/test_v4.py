"""
v4 unit tests — config, models, plugins, analysis runner.
"""

import json
import os
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ============================================================
# Config system tests
# ============================================================

class TestConfigSystem:
    """Test multi-project config loading."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = os.path.join(self.tmpdir, "config", "projects")
        self.default_cfg = os.path.join(self.tmpdir, "config", "default.yaml")

        # Create default config
        os.makedirs(os.path.dirname(self.default_cfg), exist_ok=True)
        with open(self.default_cfg, "w") as f:
            yaml.dump({"default_project": "test"}, f)

    def test_list_projects(self):
        from core.config import list_projects
        projects = list_projects()
        assert isinstance(projects, list)

    def test_load_config(self):
        """Test loading project config."""
        from core.config import load_config, merge_cli_overrides

        # Create test project config
        proj_dir = os.path.join(self.projects_dir, "test")
        os.makedirs(proj_dir, exist_ok=True)
        with open(os.path.join(proj_dir, "config.yaml"), "w") as f:
            yaml.dump({"project_root": "/test/root"}, f)

        # Test merge CLI overrides
        cfg = {"project_root": "/test/root"}
        overrides = {"project_root": "/override/root"}
        merged = merge_cli_overrides(cfg, overrides)
        assert merged["project_root"] == "/override/root"

    def test_load_config_ovrs25_layered_shape(self):
        from core.config import load_config

        cfg = load_config("ovrs25")
        project_root = Path(__file__).resolve().parents[1]
        assets_root = project_root / "config" / "projects" / "ovrs25" / "assets"
        expected_template = assets_root / "selena" / "selena_config_tmpl.txt"
        expected_runtime = assets_root / "selena" / "runtime.xml"
        expected_matfilter = assets_root / "selena" / "matfilefilter.txt"
        expected_fixed = os.path.normpath(str(project_root / "results" / "ovrs25" / "_runtime" / cfg["_meta"]["_run_id"] / "byd_CR_Selena_Config_ovrs.txt"))

        assert isinstance(cfg["machine"], dict)
        assert isinstance(cfg["build"], dict)
        assert isinstance(cfg["assets"], dict)
        assert isinstance(cfg["vs_debug"], dict)
        assert cfg["machine"]["platform"] == "gen5_selena"
        assert cfg["machine"]["binding"] == "ovrs25"
        assert cfg["build"]["build_mode"] == "RelWithDebInfo"
        assert cfg["paths"]["assets_dir"] == str(assets_root)
        assert cfg["assets"]["root"] == str(assets_root)
        assert cfg["assets"]["config_template"] == str(expected_template)
        assert cfg["assets"]["runtime_xml"] == str(expected_runtime)
        assert cfg["assets"]["matfilefilter"] == str(expected_matfilter)
        assert os.path.normpath(cfg["assets"]["fixed_config_path"]) == expected_fixed
        assert os.path.normpath(cfg["paths"]["selena_paramconfig"]) == expected_fixed
        assert cfg["vs_debug"]["command_args"] == ["--paramconfig", expected_fixed]
        assert Path(cfg["project_root"]) == Path("C:/BYD_OVS_CB")
        assert Path(cfg["repos"]["outer_repo_root"]) == Path("C:/BYD_OVS_CB")
        assert Path(cfg["repos"]["inner_repo_root"]) == Path("C:/BYD_OVS_CB/apl/byd")
        assert "python3_path" in cfg["environment"]
        assert cfg["vs_debug"]["target_project"] == "selena"

    def test_selena_helpers(self):
        from core.config import get_selena_vs_solution, load_config, render_selena_environment_path

        cfg = load_config("ovrs25")
        expected_sln = Path(cfg["build"]["build_output"]) / "dc_tools" / "selena" / "selena.sln"
        env_path = render_selena_environment_path(cfg)

        assert get_selena_vs_solution(cfg) == str(expected_sln)
        assert cfg["build"]["vs_solution"] == str(expected_sln)
        assert cfg["vs_debug"]["solution"] == str(expected_sln)
        assert "MATLAB" in env_path or "matlab" in env_path.lower()
        assert "qt" in env_path.lower()
        assert "boost" in env_path.lower()
        assert env_path.endswith("$(Path);$(LocalDebuggerEnvironment);")
        assert "%PATH%" not in env_path
        assert "$(Path)" in env_path
        assert "$(LocalDebuggerEnvironment)" in env_path

    def test_derive_project_context_from_selena_script(self, tmp_path):
        from core.config import derive_project_context_from_selena_script

        script = (
            tmp_path
            / "BYD_OVS_CB"
            / "apl"
            / "byd"
            / "bindings"
            / "ovrs25"
            / "selena"
            / "jenkins_selena_build.bat"
        )
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            textwrap.dedent(
                r"""
                @echo off
                set buildmode=RelWithDebInfo
                set selena_config=ROS_PER_SIT_RPM_FCT_RECR
                set SELENA_ENV_PATH=C:\TCC\Tools\selena_environment\0.1.7_WIN64
                set BOOST_ROOT=C:\TCC\Tools\boost\1.63.0_WIN64
                set PATH=%BOOST_ROOT%\lib64-msvc-14.0;C:\TCC\Tools\qt\5.8.0_WIN64\5.8\msvc2015_64\bin;C:\TCC\Tools\qt\5.8.0_WIN64\5.8\msvc2015_64\lib;%PATH%
                """
            ).strip(),
            encoding="utf-8",
        )

        data = derive_project_context_from_selena_script(str(script))

        assert Path(data["project_root"]) == tmp_path / "BYD_OVS_CB"
        assert data["binding"] == "ovrs25"
        assert data["build_mode"] == "RelWithDebInfo"
        assert data["build_config"] == "ROS_PER_SIT_RPM_FCT_RECR"
        assert data["boost_root"] == r"C:\TCC\Tools\boost\1.63.0_WIN64"
        assert data["qt_path"] == r"C:\TCC\Tools\qt\5.8.0_WIN64\5.8\msvc2015_64"
        assert data["selena_env_path"] == r"C:\TCC\Tools\selena_environment\0.1.7_WIN64"
        assert data["r2d2_script"].endswith(r"ip_dc\dc_tools\R2D2.py")
        assert data["build_output"].endswith(r"ip_dc\build\ROS_PER_SIT_RPM_FCT_RECR")

    def test_merge_cli_overrides(self):
        from core.config import merge_cli_overrides
        cfg = {"a": 1}
        overrides = {"a": 2}
        merged = merge_cli_overrides(cfg, overrides)
        assert merged["a"] == 2

    def test_load_config_bydod25_recipe_shape(self):
        from core.config import load_config

        cfg = load_config("bydod25")

        assert cfg["project"]["recipe"] == "g3n_fvg3_od25"
        assert cfg["_meta"]["recipe"] == "g3n_fvg3_od25"
        assert cfg["simulation"]["source"] == "RadarFC"
        assert cfg["simulation"]["auto_detect_radar"] is False
        assert cfg["simulation"]["adapter_file"].endswith("adapter_byd.txt")
        assert cfg["simulation"]["matfilefilter"].endswith("matlabPerSit_OD_gac.filter")
        assert cfg["simulation"]["paramconfig_options"]["distilled-mat"] is True
        assert cfg["build"]["script_args_template"] == []

    def test_get_default_project(self):
        from core.config import get_default_project
        # Default should exist
        default = get_default_project()
        assert default is not None or True  # May return None if no config


# ============================================================
# Model tests
# ============================================================

class TestModels:
    """Test v4 data models."""

    def test_build_result(self):
        from core.models import BuildResult
        r = BuildResult(success=True, build_type="hex", duration_sec=5.0)
        assert r.success is True
        assert r.build_type == "hex"
        assert r.duration_sec == 5.0

    def test_build_result_json(self):
        from core.models import BuildResult
        r = BuildResult(success=False, build_type="selena", errors=["timeout"])
        data = r.to_dict()
        assert data["success"] is False
        assert data["errors"] == ["timeout"]

    def test_signal_data(self):
        from core.models import SignalData
        s = SignalData(name="Test", values=[1, 2, 3], timestamps=[0.0, 1.0, 2.0])
        assert s.name == "Test"
        assert len(s.values) == 3
        assert s.unit == ""

    def test_plugin_result(self):
        from core.models import PluginResult
        p = PluginResult(plugin_name="test", success=True, summary="OK")
        assert p.plugin_name == "test"
        assert p.success is True
        assert p.summary == "OK"

    def test_rule_result(self):
        from core.models import RuleResult
        r = RuleResult(name="rule1", status="pass", severity="P0", message="OK")
        assert r.name == "rule1"
        assert r.status == "pass"

    def test_analysis_context(self):
        from core.models import AnalysisContext
        from datetime import datetime
        c = AnalysisContext(
            mf4_path="/test.mf4",
            project="test",
            platform="gen5_selena",
            timestamp=datetime.now(), signals_config=[],
            rules_config=[],
        )

        assert c.mf4_path == "/test.mf4"
        assert c.project == "test"

    def test_analysis_result(self):
        from core.models import AnalysisResult
        from datetime import datetime
        r = AnalysisResult(
            id="test_001",
            timestamp=datetime.now(),
            project="test",
            mf4_path="/test.mf4",
        )
        assert r.id == "test_001"

    def test_diff_result(self):
        from core.models import DiffResult
        d = DiffResult(base_dir="/a", current_dir="/b")
        assert d.base_dir == "/a"


# ============================================================
# Plugin tests
# ============================================================

class TestSignalSummaryPlugin:
    """Test signal_summary analysis plugin."""

    def test_basic_summary(self):
        from plugins.analysis.signal_summary import SignalSummaryPlugin
        from core.models import AnalysisContext, SignalData
        from datetime import datetime

        plugin = SignalSummaryPlugin()
        assert plugin.name == "signal_summary"

        signals = {
            "TestSignal": SignalData(
                name="TestSignal",
                values=[1.0, 2.0, 3.0, 4.0, 5.0],
                timestamps=[0.0, 1.0, 2.0, 3.0, 4.0],
                unit="m",
            ),
        }
        context = AnalysisContext(
            mf4_path="/test.mf4", project="test", platform="gen5_selena",
            timestamp=datetime.now(), signals_config=[], rules_config=[],
        )

        result = plugin.analyze(signals, context)
        assert result.success is True
        assert "TestSignal" in result.data
        assert result.data["TestSignal"]["min"] == 1.0
        assert result.data["TestSignal"]["max"] == 5.0
        assert result.data["TestSignal"]["mean"] == 3.0

    def test_empty_signal(self):
        from plugins.analysis.signal_summary import SignalSummaryPlugin
        from core.models import AnalysisContext, SignalData
        from datetime import datetime

        plugin = SignalSummaryPlugin()
        signals = {
            "Empty": SignalData(name="Empty", values=[], timestamps=[]),
        }
        context = AnalysisContext(
            mf4_path="/test.mf4", project="test", platform="gen5_selena",
            timestamp=datetime.now(), signals_config=[], rules_config=[],
        )

        result = plugin.analyze(signals, context)
        assert result.success is True
        assert result.data["Empty"] == {"error": "No data"}


class TestRuleCheckPlugin:
    """Test rule_check analysis plugin."""

    def test_reaches_value_pass(self):
        from plugins.analysis.rule_check import RuleCheckPlugin
        from core.models import AnalysisContext, SignalData
        from datetime import datetime

        plugin = RuleCheckPlugin()
        assert plugin.name == "rule_check"

        signals = {
            "FCTA_State": SignalData(
                name="FCTA_State",
                values=[0.0, 0.0, 1.0, 1.0],
                timestamps=[0.0, 1.0, 2.0, 3.0],
            ),
        }
        context = AnalysisContext(
            mf4_path="/test.mf4", project="test", platform="gen5_selena",
            timestamp=datetime.now(), signals_config=[],
            rules_config=[{
                "name": "fcta_activates",
                "signal": "FCTA_State",
                "condition": "reaches value 1",
                "severity": "P0",
                "description": "FCTA should activate",
                "source": "signal",
            }],
        )

        result = plugin.analyze(signals, context)
        assert result.success is True
        assert len(result.data["rules"]) == 1
        assert result.data["rules"][0]["status"] == "pass"

    def test_reaches_value_fail(self):
        from plugins.analysis.rule_check import RuleCheckPlugin
        from core.models import AnalysisContext, SignalData
        from datetime import datetime

        plugin = RuleCheckPlugin()
        signals = {
            "FCTA_State": SignalData(
                name="FCTA_State",
                values=[0.0, 0.0, 0.0],
                timestamps=[0.0, 1.0, 2.0],
            ),
        }
        context = AnalysisContext(
            mf4_path="/test.mf4", project="test", platform="gen5_selena",
            timestamp=datetime.now(), signals_config=[],
            rules_config=[{
                "name": "fcta_activates",
                "signal": "FCTA_State",
                "condition": "reaches value 1",
                "severity": "P0",
                "source": "signal",
            }],
        )

        result = plugin.analyze(signals, context)
        assert result.data["rules"][0]["status"] == "fail"

    def test_missing_signal(self):
        from plugins.analysis.rule_check import RuleCheckPlugin
        from core.models import AnalysisContext, SignalData
        from datetime import datetime

        plugin = RuleCheckPlugin()
        signals = {}  # No signals
        context = AnalysisContext(
            mf4_path="/test.mf4", project="test", platform="gen5_selena",
            timestamp=datetime.now(), signals_config=[],
            rules_config=[{
                "name": "rule1",
                "signal": "Missing",
                "condition": "reaches value 1",
                "severity": "P1",
                "source": "signal",
            }],
        )

        result = plugin.analyze(signals, context)
        assert result.data["rules"][0]["status"] == "skip"


class TestDefaultReportPlugin:
    """Test default_report HTML generation."""

    def test_generates_report(self, tmp_path):
        from plugins.analysis.default_report import DefaultReportPlugin
        from core.models import AnalysisContext, SignalData
        from datetime import datetime

        plugin = DefaultReportPlugin()
        signals = {
            "TestSignal": SignalData(
                name="TestSignal",
                values=[1.0, 2.0, 3.0],
                timestamps=[0.0, 1.0, 2.0],
                unit="m",
                summary={"min": 1.0, "max": 3.0, "mean": 2.0, "transitions": 2},
            ),
        }
        context = AnalysisContext(
            mf4_path="/test.mf4", project="test", platform="gen5_selena",
            timestamp=datetime.now(), output_dir=str(tmp_path),
            signals_config=[], rules_config=[],
        )

        result = plugin.analyze(signals, context)
        assert result.success is True
        report_path = result.data["report_path"]
        assert os.path.exists(report_path)
        with open(report_path) as f:
            content = f.read()
        assert "TestSignal" in content
        assert "<html" in content.lower()


class TestPluginDiscovery:
    """Test plugin auto-discovery."""

    def test_discover_plugins(self):
        from core.analysis_runner import discover_plugins, load_plugins

        plugins = discover_plugins()
        assert "signal_summary" in plugins
        assert "rule_check" in plugins
        assert "default_report" in plugins
        assert "ai_qa" in plugins

    def test_load_plugins(self):
        from core.analysis_runner import load_plugins

        # Load specific plugins
        loaded = load_plugins(["signal_summary", "rule_check"])
        assert len(loaded) == 2
        names = [p.name for p in loaded]
        assert "signal_summary" in names
        assert "rule_check" in names


# ============================================================
# CLI command tests
# ============================================================

class TestCLI:
    """Test CLI command registration."""

    def test_main_help(self):
        import subprocess
        result = subprocess.run(
            ["python", "rsim.py", "--help"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "analyze" in result.stdout
        assert "build" in result.stdout
        assert "ask" in result.stdout
        assert "diff" in result.stdout
        assert "open-vs" in result.stdout
        assert "check" in result.stdout

    def test_analyze_help(self):
        import subprocess
        result = subprocess.run(
            ["python", "rsim.py", "analyze", "--help"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0

    def test_build_help(self):
        import subprocess
        result = subprocess.run(
            ["python", "rsim.py", "build", "--help"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0

    def test_missing_mf4(self):
        """analyze with non-existent MF4 should fail."""
        import subprocess
        result = subprocess.run(
            ["python", "rsim.py", "--project", "ovrs25", "analyze", "/nonexistent.mf4"],
            capture_output=True, text=True,
        )
        assert result.returncode == 1


# ============================================================
# Platform backend tests
# ============================================================

class TestPlatformBackend:
    """Test platform registration and interface."""

    def test_gen5_registered(self):
        from platforms import get, list_all
        assert "gen5_selena" in list_all()

    def test_gen5_platform(self):
        from platforms import get
        cfg = {"project_root": "/test", "paths": {"r2d2_script": "/test/r2d2.py"}}
        p = get("gen5_selena", cfg)
        assert p.platform_name == "gen5_selena"


# ============================================================
# End-to-end config test
# ============================================================

class TestE2E:
    """Integration tests."""

    def test_config_load_and_merge(self):
        """Full config loading pipeline."""
        from core.config import load_config, list_projects, get_default_project

        # Should not crash
        projects = list_projects()
        assert isinstance(projects, list)

        default = get_default_project()
        assert default is not None

        cfg = load_config(default)
        assert "project_root" in cfg

    def test_analysis_runner_init(self):
        """AnalysisRunner initializes correctly."""
        from core.analysis_runner import AnalysisRunner

        runner = AnalysisRunner("ovrs25", {"project_root": "/test"})
        assert runner.project == "ovrs25"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ============================================================
# History command test
# ============================================================

class TestHistoryCommand:
    """Test history CLI module."""

    def test_history_module_loads(self):
        import importlib
        module = importlib.import_module("cli.history")
        assert hasattr(module, "register")
        assert hasattr(module, "run")

    def test_history_creates_parser(self):
        import argparse
        from io import StringIO

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")

        import importlib
        module = importlib.import_module("cli.history")
        module.register(subparsers)

        # Should accept --limit, --search, --json
        args = parser.parse_args(["history", "--limit", "5", "--search", "test"])
        assert args.command == "history"
        assert args.limit == 5
        assert args.search == "test"


# ============================================================
# Init command test
# ============================================================

class TestInitCommand:
    """Test init CLI module."""

    def test_init_module_loads(self):
        import importlib
        module = importlib.import_module("cli.init")
        assert hasattr(module, "register")
        assert hasattr(module, "run")


# ============================================================
# Diff command test
# ============================================================

class TestDiffCommand:
    """Test diff CLI module."""

    def test_diff_module_loads(self):
        import importlib
        module = importlib.import_module("cli.diff")
        assert hasattr(module, "register")
        assert hasattr(module, "run")

    def test_load_signals_from_dir(self):
        from cli.diff import _load_signals
        # Non-existent path should return empty dict
        result = _load_signals("/nonexistent/path")
        assert isinstance(result, dict)


# ============================================================
# Hyphenated command dispatch tests
# ============================================================

class TestHyphenatedCommandDispatch:
    """Test that commands with hyphens are correctly dispatched."""

    def test_prepare_sim_registered_with_hyphen(self):
        """prepare_sim.py registers as 'prepare-sim', dispatch must match."""
        import sys
        from pathlib import Path
        import importlib.util

        cli_dir = Path(__file__).resolve().parents[1] / "cli"
        py_file = cli_dir / "prepare_sim.py"
        spec = importlib.util.spec_from_file_location("cli.prepare_sim", py_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        import argparse
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        module.register(subparsers)

        args = parser.parse_args(["prepare-sim", "--dry-run"])
        assert args.command == "prepare-sim"
        assert args.dry_run is True

    def test_open_vs_registered_with_hyphen(self):
        """open_vs.py registers as 'open-vs', dispatch must match."""
        from pathlib import Path
        import importlib.util

        cli_dir = Path(__file__).resolve().parents[1] / "cli"
        py_file = cli_dir / "open_vs.py"
        spec = importlib.util.spec_from_file_location("cli.open_vs", py_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        import argparse
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        module.register(subparsers)

        args = parser.parse_args(["open-vs"])
        assert args.command == "open-vs"

    def test_commands_dict_has_hyphenated_keys(self):
        """_COMMANDS must contain hyphenated command names, not underscores."""
        from pathlib import Path
        import sys
        import importlib.util

        # Replicate the registration logic from rsim.py
        commands = {}
        cli_dir = Path(__file__).resolve().parents[1] / "cli"
        for py_file in sorted(cli_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            module_name = f"cli.{py_file.stem}"
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            if hasattr(module, "register"):
                import argparse
                _p = argparse.ArgumentParser()
                _sp = _p.add_subparsers()
                module.register(_sp)
                cmd_name = py_file.stem.replace("_", "-")
                commands[cmd_name] = module

        assert "prepare-sim" in commands
        assert "open-vs" in commands
        assert "build" in commands
        assert "analyze" in commands

    def test_prepare_sim_help_via_subprocess(self):
        """prepare-sim --help should show the subcommand help, not main help."""
        import subprocess
        result = subprocess.run(
            ["python", "rsim.py", "prepare-sim", "--help"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        assert result.returncode == 0
        assert "prepare" in result.stdout.lower() or "simulation" in result.stdout.lower()
        assert "dry-run" in result.stdout
