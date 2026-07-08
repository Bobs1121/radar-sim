"""Tests for prepare_sim command."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cli.prepare_sim import _validate_configuration, run
from core.config import render_selena_environment_path


def _make_layered_config(tmp_path: Path) -> dict:
    assets_root = tmp_path / "assets"
    assets_root.mkdir()
    source = assets_root / "template_paramconfig.txt"
    source.write_text(
        "assets={{ASSETS_DIR}}\n"
        "project={{PROJECT_ROOT}}\n"
        "tools={{TOOLS_DIR}}\n"
        "input={{INPUT_MF4}}\n"
        "output={{OUTPUT_MF4}}\n",
        encoding="utf-8",
    )
    fixed = assets_root / "generated" / "fixed_paramconfig.txt"

    return {
        "project": {"name": "ovrs25", "platform": "gen5_selena"},
        "project_root": r"C:\BYD_OVS_CB",
        "build": {"vs_solution": r"C:\work\selena\selena.sln"},
        "paths": {
            "input_mf4": str(tmp_path / "in.mf4"),
            "output_mf4": str(tmp_path / "out.mf4"),
        },
        "assets": {
            "root": str(assets_root),
            "config_template": str(source),
            "fixed_config_path": str(fixed),
        },
        "environment": {
            "matlab_root": r"C:\MATLAB\R2022a",
            "qt_path": r"C:\Qt\5.8.0_WIN64\5.8\msvc2015_64",
            "boost_root": r"C:\local\boost_1_74_0",
            "python3_path": r"C:\Python312\python.exe",
        },
        "vs_debug": {},
    }


def test_validate_configuration_accepts_layered_shape_without_sim_input_dir(tmp_path):
    config = _make_layered_config(tmp_path)
    config["paths"]["build_output"] = str(tmp_path / "build")  # no sim_input_dir

    errors = _validate_configuration(config)

    assert errors == []


def test_validate_configuration_missing_layered_keys(tmp_path):
    config = {
        "project": {"name": "ovrs25"},
        "build": {},
        "assets": {},
    }

    errors = _validate_configuration(config)

    assert any("Platform not specified" in error for error in errors)
    assert any("assets.root" in error for error in errors)
    assert any("assets.config_template" in error for error in errors)
    assert any("assets.fixed_config_path" in error for error in errors)
    assert any("build.vs_solution" in error for error in errors)


def test_run_syncs_fixed_paramconfig_and_prints_vs_guidance(tmp_path, capsys):
    config = _make_layered_config(tmp_path)
    args = SimpleNamespace(project="ovrs25", dry_run=False, force=False)

    code = run(args, config)
    out = capsys.readouterr().out
    fixed_path = Path(config["assets"]["fixed_config_path"])

    assert code == 0
    assert fixed_path.exists()
    assert fixed_path.read_text(encoding="utf-8") == (
        f"assets={config['assets']['root']}\n"
        f"project={config['project_root']}\n"
        f"tools={fixed_path.parent}\n"
        f"input={config['paths']['input_mf4']}\n"
        f"output={config['paths']['output_mf4']}\n"
    )
    assert "Visual Studio guidance:" in out
    assert "Solution: C:\\work\\selena\\selena.sln" in out
    assert "Target project: selena" in out
    assert f"Args: --paramconfig {fixed_path}" in out
    assert "PATH:" in out
    expected_path = render_selena_environment_path(config)
    assert expected_path.endswith("$(Path);$(LocalDebuggerEnvironment);")
    assert "%PATH%" not in expected_path
    assert "$(Path)" in expected_path
    assert "$(LocalDebuggerEnvironment)" in expected_path
    assert f"PATH: {expected_path}" in out
    assert "C:\\Windows\\System32" not in out


def test_run_fails_when_selena_config_source_missing(tmp_path, capsys):
    config = _make_layered_config(tmp_path)
    config["assets"]["config_template"] = str(tmp_path / "assets" / "missing_template.txt")
    args = SimpleNamespace(project="ovrs25", dry_run=False, force=False)

    code = run(args, config)
    out = capsys.readouterr().out

    assert code == 1
    assert "Selena config source not found" in out
    assert "assets.config_template" in out


def test_prepare_sim_uses_recipe_hook_to_shape_guidance(tmp_path, capsys):
    config = _make_layered_config(tmp_path)
    args = SimpleNamespace(project="ovrs25", dry_run=False, force=False)

    class _RecipeHandler:
        def __init__(self):
            self.stages = []

        def prepare_simulation(self, config, sim, *, stage):
            self.stages.append(stage)
            shaped = dict(sim)
            shaped["source"] = "RecipeSource"
            return shaped

    handler = _RecipeHandler()

    with patch("cli.prepare_sim.get_for_config", return_value=handler):
        code = run(args, config)

    out = capsys.readouterr().out
    assert code == 0
    assert "Source: RecipeSource" in out
    assert handler.stages == ["prepare"]
