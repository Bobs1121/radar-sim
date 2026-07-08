"""Focused tests for control-plane agent command mapping."""

import pytest

from cli.agent import _build_task_command, _run_task


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
