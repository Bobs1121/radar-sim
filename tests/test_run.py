"""Tests for run command - Selena simulation launcher."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock


def test_run_module_loads():
    """run.py must register and be importable."""
    from cli.run import register, run
    assert callable(register)
    assert callable(run)


def test_run_registered_with_parser():
    """Run command parses correctly."""
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    from cli.run import register
    register(subparsers)

    args = parser.parse_args(["run", "input.mf4", "--timeout", "60"])
    assert args.command == "run"
    assert args.input_mf4 == "input.mf4"
    assert args.timeout == 60


def test_run_dataset_parser():
    """Run --dataset parses correctly."""
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    from cli.run import register
    register(subparsers)

    args = parser.parse_args(["run", "--dataset", "CBNA_23-4-26"])
    assert args.command == "run"
    assert args.dataset == "CBNA_23-4-26"
    assert args.input_mf4 is None


def test_run_repeatable_extra_arg_parser():
    """Run --extra-arg should append option-like Selena flags safely."""
    import argparse

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    from cli.run import register
    register(subparsers)

    args = parser.parse_args(["run", "input.mf4", "--extra-arg=--enable-doorkeeper", "--extra-arg=--foo=1"])
    assert args.command == "run"
    assert args.extra_arg == ["--enable-doorkeeper", "--foo=1"]


def test_run_missing_input_mf4_fails(tmp_path, capsys):
    """Run without input MF4 should show usage."""
    from cli.run import run

    config = {
        "project": {"name": "test"},
        "paths": {"simulation": {"datasets": []}},
    }
    args = SimpleNamespace(
        project="test",
        input_mf4=None,
        output_mf4=None,
        dataset=None,
        timeout=3600,
        no_wait=False,
        dry_run=False,
        extra_args=[],
    )

    code = run(args, config)
    out = capsys.readouterr().out
    assert code == 1
    assert "Input MF4 path required" in out


def test_run_input_mf4_not_found(tmp_path, capsys):
    """Run with non-existent input MF4 should fail."""
    from cli.run import run

    config = {"project": {"name": "test"}}
    args = SimpleNamespace(
        project="test",
        input_mf4=str(tmp_path / "nonexistent.mf4"),
        output_mf4=None,
        dataset=None,
        timeout=3600,
        no_wait=False,
        dry_run=False,
        extra_args=[],
    )

    code = run(args, config)
    out = capsys.readouterr().out
    assert code == 1
    assert "not found" in out.lower()


def test_run_selena_exe_not_found(tmp_path, capsys):
    """Run should fail when selena.exe is not compiled."""
    from cli.run import run

    input_mf4 = tmp_path / "input.mf4"
    input_mf4.touch()

    config = {
        "project": {"name": "test"},
        "paths": {
            "build_output": str(tmp_path / "build"),
            "simulation": {
                "runtime_xml": str(tmp_path / "runtime.xml"),
            },
        },
        "selena": {
            "exe_pattern": "dc_tools/selena/core/{build_mode}/selena.exe",
            "build_mode": "RelWithDebInfo",
            "executable_name": "selena.exe",
        },
    }

    args = SimpleNamespace(
        project="test",
        input_mf4=str(input_mf4),
        output_mf4=None,
        dataset=None,
        timeout=3600,
        no_wait=False,
        dry_run=False,
        extra_args=[],
    )

    code = run(args, config)
    out = capsys.readouterr().out
    assert code == 1
    assert "selena.exe not found" in out.lower()


def test_find_selena_exe_uses_configured_pattern_and_build_mode(tmp_path):
    from cli.run import _find_selena_exe

    build_output = tmp_path / "build"
    exe_path = build_output / "bin" / "custom" / "Release" / "my_selena.exe"
    exe_path.parent.mkdir(parents=True)
    exe_path.touch()

    config = {
        "paths": {
            "build_output": str(build_output),
        },
        "build": {
            "build_mode": "Release",
        },
        "selena": {
            "exe_pattern": "bin/custom/{build_mode}",
            "executable_name": "my_selena.exe",
        },
    }

    assert _find_selena_exe(config) == str(exe_path)


def test_run_dataset_not_found(tmp_path, capsys):
    """Run with unknown dataset name should fail."""
    from cli.run import run

    config = {
        "project": {"name": "test"},
        "paths": {
            "simulation": {
                "runtime_xml": str(tmp_path / "runtime.xml"),
                "datasets": [
                    {"name": "other_ds", "input_dir": str(tmp_path / "other")}
                ]
            }
        },
    }

    args = SimpleNamespace(
        project="test",
        input_mf4=None,
        output_mf4=None,
        dataset="unknown_ds",
        timeout=3600,
        no_wait=False,
        dry_run=False,
        extra_args=[],
    )

    code = run(args, config)
    out = capsys.readouterr().out
    assert code == 1
    assert "not found" in out.lower()


def test_resolves_dataset_mf4_files(tmp_path):
    """_resolve_input_files should return MF4 files from dataset directory."""
    from cli.run import _resolve_input_files

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "file1.MF4").touch()
    (data_dir / "file2.MF4").touch()
    (data_dir / "file1out.MF4").touch()  # should be excluded
    (data_dir / "file1out (2).MF4").touch()  # historical rerun output should be excluded

    sim = {
        "runtime_xml": "dummy.xml",
        "datasets": [
            {"name": "test_ds", "input_dir": str(data_dir)}
        ]
    }
    args = SimpleNamespace(
        input_mf4=None,
        output_mf4=None,
        dataset="test_ds",
    )

    result = _resolve_input_files(args, {"paths": {"simulation": sim}}, sim)
    assert result is not None
    assert len(result) == 2  # excludes out.MF4
    assert all("file" in f for f in result)
    assert not any("out.MF4" in f for f in result)


def test_gen_output_path():
    """_gen_output_path should produce <input>out.MF4 in same directory."""
    from cli.run import _gen_output_path

    inp = "D:/data/byd/test/Gen5_2009-01-01_05-56_0114.MF4"
    out = _gen_output_path(inp)
    assert out.endswith("Gen5_2009-01-01_05-56_0114out.MF4")


def test_build_command_basic():
    """_build_command should produce a paramconfig-based launch command."""
    from cli.run import _build_command

    sim = {
        "tolerant": True,
        "extra_args": ["--enable-doorkeeper"],
    }

    cmd = _build_command(
        sim,
        "C:/selena.exe",
        "C:/generated/test_paramconfig.txt",
        ["--enable-multibuffer-border"],
    )

    assert cmd[0] == "C:/selena.exe"
    assert "--paramconfig" in cmd
    assert "C:/generated/test_paramconfig.txt" in cmd
    assert "--tolerant" in cmd
    assert "--enable-multibuffer-border" in cmd
    assert "--enable-doorkeeper" in cmd


def test_build_command_flags_match_working_command():
    """Verify the built command preserves merged extra CLI flags."""
    from cli.run import _build_command

    sim = {
        "tolerant": True,
        "extra_args": ["--enable-doorkeeper"],
    }

    cmd = _build_command(
        sim,
        "C:/selena.exe",
        "C:/generated/test_paramconfig.txt",
        ["--enable-multibuffer-border"],
    )

    flags = set(cmd)
    assert "--paramconfig" in flags
    assert "--tolerant" in flags
    assert "--enable-multibuffer-border" in flags
    assert "--enable-doorkeeper" in flags


def test_get_effective_runtime_limit_uses_smaller_configured_cap():
    from cli.run import _get_effective_runtime_limit

    assert _get_effective_runtime_limit(3600, {"max_duration_per_file_sec": 600}) == 600
    assert _get_effective_runtime_limit(300, {"max_duration_per_file_sec": 600}) == 300
    assert _get_effective_runtime_limit(300, {}) == 300


def test_extract_errors():
    """_extract_errors should filter error lines."""
    from cli.run import _extract_errors

    lines = ["INFO: starting", "ERROR: something failed", "warning: minor", "FATAL crash"]
    errors = _extract_errors(lines)
    assert len(errors) >= 2
    assert any("failed" in e for e in errors)
    assert any("crash" in e for e in errors)


def test_run_dry_run_succeeds_with_valid_setup(tmp_path, capsys):
    """Run --dry-run should show the command without launching."""
    from cli.run import run

    # Create selena.exe
    build_dir = tmp_path / "build" / "dc_tools" / "selena" / "core" / "RelWithDebInfo"
    build_dir.mkdir(parents=True)
    (build_dir / "selena.exe").touch()

    # Create input MF4
    input_mf4 = tmp_path / "input.mf4"
    input_mf4.touch()

    # Create runtime XML + paramconfig template assets
    runtime_xml = tmp_path / "runtime.xml"
    runtime_xml.touch()
    template = tmp_path / "selena_config_tmpl.txt"
    template.write_text(
        "config={{RUNTIME_XML}}\n"
        "input={{INPUT_MF4}}\n"
        "output={{OUTPUT_MF4}}\n"
        "source={{SOURCE}}\n"
        "userparam=mountingPosition={{MOUNTING_POSITION}}\n",
        encoding="utf-8",
    )

    config = {
        "project": {"name": "test"},
        "paths": {
            "build_output": str(tmp_path / "build"),
            "simulation": {
                "runtime_xml": str(runtime_xml),
                "source": "RadarFL",
                "mounting_position": "CFL",
                "log_file": str(tmp_path / "log.log"),
                "enable_multibuffer_border": True,
                "enable_doorkeeper": True,
            },
        },
        "assets": {
            "root": str(tmp_path),
            "config_template": str(template),
            "fixed_config_path": str(tmp_path / "generated" / "paramconfig.txt"),
            "runtime_xml": str(runtime_xml),
        },
        "selena": {
            "exe_pattern": "dc_tools/selena/core/{build_mode}/selena.exe",
            "build_mode": "RelWithDebInfo",
            "executable_name": "selena.exe",
        },
    }

    args = SimpleNamespace(
        project="test",
        input_mf4=str(input_mf4),
        output_mf4=str(tmp_path / "output.mf4"),
        dataset=None,
        timeout=3600,
        no_wait=False,
        dry_run=True,
        extra_args=[],
    )

    code = run(args, config)
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY-RUN" in out
    assert "--paramconfig" in out
    assert "RadarFL" in out


def test_run_dry_run_uses_recipe_hook_to_shape_simulation(tmp_path, capsys):
    from cli.run import run

    build_dir = tmp_path / "build" / "dc_tools" / "selena" / "core" / "RelWithDebInfo"
    build_dir.mkdir(parents=True)
    (build_dir / "selena.exe").touch()

    input_mf4 = tmp_path / "input.mf4"
    input_mf4.touch()

    runtime_xml = tmp_path / "runtime.xml"
    runtime_xml.touch()
    template = tmp_path / "selena_config_tmpl.txt"
    template.write_text(
        "config={{RUNTIME_XML}}\n"
        "input={{INPUT_MF4}}\n"
        "output={{OUTPUT_MF4}}\n"
        "source={{SOURCE}}\n",
        encoding="utf-8",
    )

    config = {
        "project": {"name": "test", "recipe": "g3n_fvg3_od25"},
        "_meta": {"recipe": "g3n_fvg3_od25"},
        "paths": {
            "build_output": str(tmp_path / "build"),
            "simulation": {
                "runtime_xml": str(runtime_xml),
                "source": "RadarFL",
                "mounting_position": "CFL",
                "log_file": str(tmp_path / "log.log"),
            },
        },
        "assets": {
            "root": str(tmp_path),
            "config_template": str(template),
            "fixed_config_path": str(tmp_path / "generated" / "paramconfig.txt"),
            "runtime_xml": str(runtime_xml),
        },
        "selena": {
            "exe_pattern": "dc_tools/selena/core/{build_mode}/selena.exe",
            "build_mode": "RelWithDebInfo",
            "executable_name": "selena.exe",
        },
    }

    args = SimpleNamespace(
        project="test",
        input_mf4=str(input_mf4),
        output_mf4=str(tmp_path / "output.mf4"),
        dataset=None,
        timeout=3600,
        no_wait=False,
        dry_run=True,
        extra_args=[],
    )

    class _RecipeHandler:
        def __init__(self):
            self.stages = []

        def prepare_simulation(self, config, sim, *, stage):
            self.stages.append(stage)
            shaped = dict(sim)
            shaped["source"] = "RecipeSource"
            return shaped

    handler = _RecipeHandler()

    with patch("cli.run.get_for_config", return_value=handler):
        code = run(args, config)

    out = capsys.readouterr().out
    assert code == 0
    assert "RecipeSource" in out
    assert handler.stages == ["base", "run"]


def test_run_help_via_subprocess():
    """Run --help should succeed."""
    import subprocess
    result = subprocess.run(
        ["python", "rsim.py", "run", "--help"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0
    assert "simulation" in result.stdout.lower() or "selena" in result.stdout.lower()


def test_run_continues_and_retries_failed_items(tmp_path, monkeypatch, capsys):
    from cli.run import run

    build_dir = tmp_path / "build" / "dc_tools" / "selena" / "core" / "RelWithDebInfo"
    build_dir.mkdir(parents=True)
    (build_dir / "selena.exe").touch()

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    file_a = data_dir / "a.MF4"
    file_b = data_dir / "b.MF4"
    file_a.touch()
    file_b.touch()

    config = {
        "project": {"name": "test"},
        "paths": {
            "build_output": str(tmp_path / "build"),
            "simulation": {
                "datasets": [{"name": "ds", "input_dir": str(data_dir)}],
                "retry_failed_at_end": True,
                "continue_on_failure": True,
                "max_retries_per_file": 1,
                "auto_detect_radar": False,
                "source": "RadarFC",
            },
        },
    }

    args = SimpleNamespace(
        project="test",
        input_mf4=None,
        output_mf4=None,
        dataset="ds",
        timeout=3600,
        no_wait=False,
        dry_run=False,
        extra_args=[],
    )

    calls = []

    def _fake_run_single(**kwargs):
        calls.append((kwargs["input_mf4"], kwargs["sim"].get("output_mf4")))
        current = kwargs["input_mf4"]
        if current.endswith("a.MF4") and len([c for c in calls if c[0].endswith("a.MF4")]) == 1:
            return 1
        return 0

    monkeypatch.setattr("cli.run._run_single", _fake_run_single)

    code = run(args, config)
    out = capsys.readouterr().out

    assert code == 1
    assert "Queued retry for failed input" in out
    assert "Starting retry pass" in out
    assert len([c for c in calls if c[0].endswith("a.MF4")]) == 2
    assert len([c for c in calls if c[0].endswith("b.MF4")]) == 1


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
