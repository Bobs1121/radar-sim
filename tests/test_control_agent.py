"""Focused tests for control-plane agent command mapping."""

import sys
from types import SimpleNamespace

import pytest

from cli.agent import _build_progress_from_output, _build_task_command, _run_task


def test_selena_build_progress_output_is_normalized():
    assert _build_progress_from_output("[R2D2 (make)] [45/120] Compiling main.cpp") == (
        45 / 120,
        "Compiling main.cpp",
    )
    assert _build_progress_from_output("[R2D2 (make)] [ 72%] Linking") == (
        0.72,
        "Selena build in progress",
    )
    assert _build_progress_from_output("ordinary compiler warning") == (None, "")


def test_build_task_command_for_local_run_sim_matches_cli_flags():
    task = {
        "task_type": "local.run_sim",
        "payload": {
            "config_path": "D:/cfg/local.yaml",
            "input_path": "D:/data/case.MF4",
            "dataset": "smoke",
            "profile": "local-build",
            "select": True,
            "limit": 5,
            "required_signals": ["VehicleSpeed", "YawRate"],
            "output_mf4": "D:/out/case_out.MF4",
            "timeout": 120,
            "max_duration": 90,
            "stall_timeout": 15,
            "no_retry": True,
            "no_wait": True,
            "dry_run": True,
            "extra_args": ["--enable-doorkeeper", "--foo=1"],
        },
    }

    command = _build_task_command(task)

    # command[0] is sys.executable: "python.exe" on Windows, "python3" on Linux.
    exe = command[0].lower()
    assert exe.endswith("python.exe") or exe.endswith("python") or exe.endswith("python3"), (
        f"unexpected interpreter: {command[0]}"
    )
    assert command[1].endswith("rsim.py")
    assert command[2:5] == ["--config", "D:/cfg/local.yaml", "run"]
    assert "D:/data/case.MF4" in command
    assert "--dataset" in command
    assert "--profile" in command
    assert "--select" in command
    assert "--limit" in command
    assert command.count("--required-signal") == 2
    assert "--max-duration" in command
    assert "--stall-timeout" in command
    assert "--no-retry" in command
    assert "--no-wait" in command
    assert "--dry-run" in command
    assert "--extra-arg=--enable-doorkeeper" in command
    assert "--extra-arg=--foo=1" in command


def test_build_task_command_for_cluster_run_matches_cli_flags():
    task = {
        "task_type": "cluster.run",
        "payload": {
            "project": "ovrs25",
            "input_mf4": "D:/data/case.MF4",
            "dataset": "smoke",
            "profile": "cloud-build",
            "select": True,
            "limit": 3,
            "run_id": "run42",
            "copy_data": True,
            "copy_selena": True,
            "required_signals": ["VehicleSpeed"],
            "no_wait": True,
            "no_fetch": True,
            "max_minutes": 45,
            "execute": True,
        },
    }

    command = _build_task_command(task)

    assert command[2:6] == ["--project", "ovrs25", "cluster", "run"]
    assert "D:/data/case.MF4" in command
    assert "--select" in command
    assert "--limit" in command
    assert "--copy-data" in command
    assert "--copy-selena" in command
    assert "--required-signal" in command
    assert "--no-wait" in command
    assert "--no-fetch" in command
    assert "--max-minutes" in command
    assert "--execute" in command


def test_build_task_command_for_tcc_actions():
    base = ["--project", "ovrs25"]

    cmd = _build_task_command({"task_type": "tcc.bootstrap_itc2", "payload": {"project": "ovrs25"}})
    assert cmd[2:6] == [*base, "tcc", "bootstrap-itc2"]

    cmd = _build_task_command({
        "task_type": "tcc.install_toolcollection",
        "payload": {"project": "ovrs25", "toolcollection": "IF:BTC-7.0.0"},
    })
    assert cmd[2:6] == [*base, "tcc", "install"]
    assert "IF:BTC-7.0.0" in cmd

    # Missing toolcollection → omit positional (rsim tcc install reads config).
    cmd = _build_task_command({
        "task_type": "tcc.install_toolcollection",
        "payload": {"project": "ovrs25"},
    })
    assert cmd[2:6] == [*base, "tcc", "install"]
    assert all("BTC" not in c for c in cmd)

    cmd = _build_task_command({"task_type": "tcc.auto_repair_all", "payload": {"project": "ovrs25"}})
    assert cmd[2:6] == [*base, "tcc", "auto-repair"]


def test_build_task_command_rejects_unknown_tcc():
    with pytest.raises(ValueError):
        _build_task_command({"task_type": "tcc.bogus", "payload": {}})


def test_run_task_reports_popen_failure(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.logs = []
            self.results = []

        def append_logs(self, task_id, lines):
            self.logs.extend(lines)
            return {"appended": len(lines)}

        def heartbeat(self, *args, **kwargs):
            return {"cancel_requested": False}

        def submit_result(self, task_id, *, agent_id, status, returncode, result):
            self.results.append(
                {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "status": status,
                    "returncode": returncode,
                    "result": result,
                }
            )
            return {}

    def fail_popen(*args, **kwargs):
        raise OSError("cannot start process")

    monkeypatch.setattr("cli.agent.subprocess.Popen", fail_popen)
    client = FakeClient()
    task = {"task_id": "task_1", "task_type": "local.check", "payload": {"project": "ovrs25"}}

    assert _run_task(client, "agent_1", task, heartbeat_interval=1) == 1
    assert any("cannot start process" in line for line in client.logs)
    assert client.results[-1]["status"] == "failed"
    assert client.results[-1]["returncode"] == -1


class _V5Client:
    def __init__(self):
        self.logs = []
        self.results = []

    def append_logs(self, _task_id, lines):
        self.logs.extend(lines)

    def heartbeat(self, *_args, **_kwargs):
        return {"cancel_requested": False}

    def submit_result(self, _task_id, **kwargs):
        self.results.append(kwargs)


def test_run_v5_build_stage_uses_adapter_and_returns_redacted_evidence(monkeypatch, tmp_path):
    prepared = SimpleNamespace(
        command=(sys.executable, "-c", "print('build ok')"),
        cwd=tmp_path,
    )
    calls = []
    monkeypatch.setattr("cli.agent._prepare_v5_selena_build", lambda payload: calls.append(("prepare", payload)) or prepared)
    monkeypatch.setattr("cli.agent._verify_v5_selena_build", lambda value: calls.append(("verify", value)))
    evidence = {
        "project": "demo",
        "workspace_binding_id": "workspace:sha256:" + "a" * 24,
        "artifact": {"logical_path": "selena.exe", "checksum": "sha256:" + "b" * 64, "size": 6},
    }
    monkeypatch.setattr(
        "cli.agent._finish_v5_selena_build",
        lambda value, **_kwargs: calls.append(("finish", value)) or evidence,
    )
    monkeypatch.setattr(
        "cli.agent._create_v5_artifact_lease",
        lambda prepared, result, **kwargs: {
            "lease_id": "artifact-lease:sha256:" + "c" * 64,
            "build_evidence_ref": f"{kwargs['build_stage_id']}:{kwargs['build_attempt']}",
        },
    )
    client = _V5Client()
    task = {
        "task_id": "stage-build",
        "task_type": "build_selena",
        "stage_type": "build_selena",
        "attempt_count": 1,
        "payload": {
            "project": "demo",
            "workspace_binding_id": "workspace:sha256:" + "a" * 24,
            "build_mode": "Release",
        },
    }
    assert _run_task(
        client,
        "light-a",
        task,
        heartbeat_interval=1,
        node_kind="windows_agent",
    ) == 0
    assert [item[0] for item in calls] == ["prepare", "verify", "finish"]
    assert client.results[-1]["status"] == "succeeded"
    assert client.results[-1]["result"] == evidence
    assert evidence["artifact_lease_ref"].startswith("artifact-lease:sha256:")
    assert "command" not in client.results[-1]["result"]
    assert str(tmp_path) not in str(client.results[-1]["result"])


def test_run_v5_build_setup_failure_does_not_spawn_or_return_local_cwd(monkeypatch):
    from core.agent_build_stage import AgentBuildStageError

    monkeypatch.setattr(
        "cli.agent._prepare_v5_selena_build",
        lambda _payload: (_ for _ in ()).throw(AgentBuildStageError("binding not found")),
    )
    monkeypatch.setattr(
        "cli.agent.subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    client = _V5Client()
    task = {"task_id": "stage-build", "task_type": "build_selena", "payload": {}}
    assert _run_task(
        client,
        "light-a",
        task,
        heartbeat_interval=1,
        node_kind="windows_agent",
    ) == 1
    result = client.results[-1]["result"]
    assert result == {"error": "binding not found"}
    assert "cwd" not in result
