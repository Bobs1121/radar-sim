import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def _load_build_module():
    build_path = Path(__file__).resolve().parents[1] / "cli" / "build.py"
    spec = importlib.util.spec_from_file_location("cli_build_test", build_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_hex_respects_no_progress_and_writes_state(tmp_path, monkeypatch):
    module = _load_build_module()
    monkeypatch.chdir(tmp_path)

    script = tmp_path / "build_hex.bat"
    script.write_text("@echo off\n")

    class _Stdout:
        def __init__(self, lines):
            self._lines = iter(lines)

        def readline(self):
            return next(self._lines, "")

    class _Proc:
        def __init__(self):
            self.stdout = _Stdout(["copy complete\n"])
            self.returncode = 1

        def poll(self):
            return self.returncode

    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: _Proc())

    result = module._build_hex({"hex_build_script": str(script)}, clean=False, no_progress=True)

    assert result.success is False
    state_file = tmp_path / ".build_state"
    assert state_file.exists()
    assert json.loads(state_file.read_text())["status"] == "copy_done"


def test_prepare_repo_context_rejects_mismatch_without_checkout(tmp_path, monkeypatch):
    module = _load_build_module()
    repo = tmp_path / "apl" / "byd"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()

    calls = []

    def _run(cmd, capture_output=True, text=True, timeout=10):
        calls.append(cmd)
        if "branch" in cmd:
            return SimpleNamespace(returncode=0, stdout="feature_a\n", stderr="")
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout="abc\n", stderr="")
        if any(part in ("checkout", "stash", "reset") for part in cmd):
            raise AssertionError(f"mutating git command was called: {cmd}")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(module.subprocess, "run", _run)
    issue = module._prepare_repo_context({
        "repos": {"inner_repo_root": str(repo), "inner_repo_branch": "selena_branch_x"},
        "build": {"selena_branch": "selena_branch_x"},
    })

    assert "Automatic branch switching is disabled" in issue
    assert not any(any(part in ("checkout", "stash", "reset") for part in cmd) for cmd in calls)


def test_prepare_repo_context_allows_current_dirty_branch(tmp_path, monkeypatch):
    module = _load_build_module()
    repo = tmp_path / "apl" / "byd"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()

    calls = []

    def _run(cmd, capture_output=True, text=True, timeout=10):
        calls.append(cmd)
        if "branch" in cmd:
            return SimpleNamespace(returncode=0, stdout="selena_branch_x\n", stderr="")
        if any(part in ("checkout", "stash", "reset") for part in cmd):
            raise AssertionError(f"mutating git command was called: {cmd}")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(module.subprocess, "run", _run)
    issue = module._prepare_repo_context({
        "repos": {"inner_repo_root": str(repo), "inner_repo_branch": "selena_branch_x"},
        "build": {"selena_branch": "selena_branch_x"},
    })

    assert issue == ""
    assert not any(any(part in ("checkout", "stash", "reset") for part in cmd) for cmd in calls)


def test_build_selena_uses_script_when_present(tmp_path, monkeypatch):
    module = _load_build_module()

    script = tmp_path / "jenkins_selena_build.bat"
    script.write_text("@echo off\n", encoding="utf-8")
    build_output = tmp_path / "build" / "dc_tools" / "selena" / "core" / "RelWithDebInfo"
    build_output.mkdir(parents=True)
    (build_output / "selena.exe").write_text("", encoding="utf-8")

    class _Stdout:
        def readline(self):
            return ""

        def close(self):
            return None

    class _Proc:
        def __init__(self):
            self.stdout = _Stdout()

        def wait(self):
            return 0

    popen_calls = {}

    def _popen(cmd, **kwargs):
        popen_calls["cmd"] = cmd
        popen_calls["cwd"] = kwargs.get("cwd")
        return _Proc()

    monkeypatch.setattr(module.subprocess, "Popen", _popen)
    monkeypatch.setattr(module, "_prepare_repo_context", lambda config: "")

    config = {
        "build": {
            "selena_build_script": str(script),
            "build_config": "ROS_PER_SIT_RPM_FCT_RECR",
        },
        "binding": "ovrs25",
        "paths": {
            "build_output": str(tmp_path / "build"),
        },
        "environment": {},
        "project_root": str(tmp_path),
        "selena": {
            "exe_pattern": "dc_tools/selena/core/{build_mode}",
        },
    }

    result = module._build_selena(config, clean=False, mode="RelWithDebInfo")

    assert result.success is True
    assert result.executable_path == str(build_output / "selena.exe")
    assert popen_calls["cmd"][:3] == ["cmd", "/c", str(script)]
    assert "ROS_PER_SIT_RPM_FCT_RECR" in popen_calls["cmd"]
    assert popen_calls["cwd"] == str(script.parent)


def test_build_selena_resolves_executable_path_from_configured_pattern(tmp_path, monkeypatch):
    module = _load_build_module()

    script = tmp_path / "jenkins_selena_build.bat"
    script.write_text("@echo off\n", encoding="utf-8")
    executable = tmp_path / "build" / "bin" / "custom" / "Release" / "my_selena.exe"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")

    class _Stdout:
        def readline(self):
            return ""

        def close(self):
            return None

    class _Proc:
        def __init__(self):
            self.stdout = _Stdout()

        def wait(self):
            return 0

    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: _Proc())

    config = {
        "build": {
            "selena_build_script": str(script),
            "build_config": "full_dsp",
            "build_mode": "Release",
        },
        "paths": {
            "build_output": str(tmp_path / "build"),
        },
        "environment": {},
        "project_root": str(tmp_path),
        "selena": {
            "exe_pattern": "bin/custom/{build_mode}",
            "executable_name": "my_selena.exe",
        },
    }

    result = module._build_selena(config, clean=False, mode="Release")

    assert result.success is True
    assert result.executable_path == str(executable)


def test_build_selena_script_command_respects_template(tmp_path):
    module = _load_build_module()
    script = tmp_path / "jenkins_selena_build.bat"
    config = {
        "project_root": str(tmp_path),
        "repos": {
            "outer_repo_root": str(tmp_path),
            "inner_repo_root": str(tmp_path / "apl" / "byd"),
        },
        "build": {
            "selena_build_script": str(script),
            "build_config": "full_dsp",
            "script_args_template": ["--mode", "{build_mode}", "--cfg", "{build_config_name}"],
            "script_workdir": str(tmp_path / "custom_workdir"),
        },
    }

    cmd, cwd = module._build_selena_script_command(config, "Release")

    assert cmd == ["cmd", "/c", str(script), "--mode", "Release", "--cfg", "full_dsp"]
    assert cwd == str(tmp_path / "custom_workdir")


def test_build_selena_script_command_uses_recipe_handler_defaults(tmp_path):
    module = _load_build_module()
    script = tmp_path / "jenkins_selena_build.bat"
    config = {
        "_meta": {
            "recipe": "g3n_fvg3_od25",
        },
        "build": {
            "selena_build_script": str(script),
            "build_config": "full_dsp",
        },
        "binding": "ovrs25",
    }

    cmd, cwd = module._build_selena_script_command(config, "Release")

    assert cmd == ["cmd", "/c", str(script)]
    assert cwd == str(tmp_path)
