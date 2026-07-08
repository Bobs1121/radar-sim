"""Tests for the server-side cluster executor (Mode A in-process execution).

Validates that cluster.run tasks are executed via prepare_cluster_job +
submit_cluster_job directly (no subprocess), with logs and results flowing
back through callbacks. Uses monkeypatch so no real cluster access is needed.
"""

from types import SimpleNamespace

import pytest

from core.server_cluster_executor import execute_cluster_run


@pytest.fixture
def captured_logs():
    logs: dict[str, list[str]] = {}

    def log(task_id, lines):
        logs.setdefault(task_id, []).extend(lines)

    return logs, log


def _fake_prepared(job_dir="/tmp/job", config_path="/tmp/job/Config.cfg", warnings=None):
    return SimpleNamespace(
        job_dir=job_dir,
        config_path=config_path,
        warnings=warnings or [],
    )


def _fake_submit_result(rc=0, mode="xmlrpc", dry_run=True, stdout="", stderr=""):
    return SimpleNamespace(
        mode=mode,
        dry_run=dry_run,
        command=["xmlrpc", "SZHRADAR01:8123", "addSimulation", "/tmp/job/Config.cfg"],
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
    )


def test_execute_cluster_run_dry_run_success(monkeypatch, captured_logs):
    """Default (no payload.execute) → dry-run submit, status succeeded."""
    monkeypatch.setattr("core.cluster.prepare_cluster_job", lambda *a, **kw: _fake_prepared())
    monkeypatch.setattr("core.cluster.submit_cluster_job", lambda *a, **kw: _fake_submit_result(dry_run=True))

    logs, log = captured_logs
    task = {"task_id": "t1", "task_type": "cluster.run",
            "payload": {"project": "ovrs25", "dataset": "BYD_SR"}}
    status, rc, result = execute_cluster_run(task, {"project": {"name": "ovrs25"}}, log=log)

    assert status == "succeeded"
    assert rc == 0
    assert result["dry_run"] is True
    assert result["job_dir"] == "/tmp/job"
    assert result["submit_mode"] == "xmlrpc"
    # Logs include prepare + dry-run notice.
    assert any("preparing cluster job" in line for line in logs["t1"])
    assert any("DRY-RUN" in line for line in logs["t1"])


def test_execute_cluster_run_executes_when_payload_execute_true(monkeypatch, captured_logs):
    """payload.execute=true → real submit (dry_run=False)."""
    monkeypatch.setattr("core.cluster.prepare_cluster_job", lambda *a, **kw: _fake_prepared())
    submit_calls = []
    def fake_submit(cfg, config, *, dry_run):
        submit_calls.append(dry_run)
        return _fake_submit_result(rc=0, dry_run=dry_run)
    monkeypatch.setattr("core.cluster.submit_cluster_job", fake_submit)

    _, log = captured_logs
    task = {"task_id": "t2", "task_type": "cluster.run",
            "payload": {"project": "ovrs25", "execute": True}}
    status, rc, result = execute_cluster_run(task, {"project": {"name": "ovrs25"}}, log=log)

    assert submit_calls == [False]  # called with dry_run=False
    assert status == "succeeded"
    assert result["dry_run"] is False


def test_execute_cluster_run_prepare_failure(monkeypatch, captured_logs):
    """prepare_cluster_job raising → status failed, error captured."""
    def boom(*a, **kw):
        raise RuntimeError("workspace not writable")
    monkeypatch.setattr("core.cluster.prepare_cluster_job", boom)

    logs, log = captured_logs
    task = {"task_id": "t3", "task_type": "cluster.run", "payload": {"project": "ovrs25"}}
    status, rc, result = execute_cluster_run(task, {}, log=log)

    assert status == "failed"
    assert rc == -1
    assert "workspace not writable" in result["error"]
    assert any("prepare failed" in line for line in logs["t3"])


def test_execute_cluster_run_submit_failure(monkeypatch, captured_logs):
    """submit returning nonzero rc → status failed."""
    monkeypatch.setattr("core.cluster.prepare_cluster_job", lambda *a, **kw: _fake_prepared())
    monkeypatch.setattr("core.cluster.submit_cluster_job",
                        lambda *a, **kw: _fake_submit_result(rc=1, stderr="manager offline"))

    _, log = captured_logs
    task = {"task_id": "t4", "task_type": "cluster.run",
            "payload": {"project": "ovrs25", "execute": True}}
    status, rc, result = execute_cluster_run(task, {"project": {"name": "ovrs25"}}, log=log)

    assert status == "failed"
    assert rc == 1
    assert "manager offline" in result["stderr"]


def test_execute_cluster_run_surfaces_warnings(monkeypatch, captured_logs):
    """Warnings from prepare_cluster_job are logged."""
    monkeypatch.setattr(
        "core.cluster.prepare_cluster_job",
        lambda *a, **kw: _fake_prepared(warnings=["data path local", "selena stale"]),
    )
    monkeypatch.setattr("core.cluster.submit_cluster_job", lambda *a, **kw: _fake_submit_result())

    logs, log = captured_logs
    task = {"task_id": "t5", "task_type": "cluster.run", "payload": {}}
    execute_cluster_run(task, {"project": {"name": "x"}}, log=log)

    joined = " ".join(logs["t5"])
    assert "data path local" in joined
    assert "selena stale" in joined


def test_execute_cluster_run_passes_profile_to_prepare(monkeypatch, captured_logs):
    """The profile from payload is forwarded to prepare_cluster_job."""
    captured = {}
    def fake_prepare(config, *, input_path=None, dataset=None, run_id=None,
                     profile=None, copy_data=None, copy_selena=None):
        captured["profile"] = profile
        captured["dataset"] = dataset
        captured["input_path"] = input_path
        return _fake_prepared()
    monkeypatch.setattr("core.cluster.prepare_cluster_job", fake_prepare)
    monkeypatch.setattr("core.cluster.submit_cluster_job", lambda *a, **kw: _fake_submit_result())

    _, log = captured_logs
    task = {"task_id": "t6", "task_type": "cluster.run",
            "payload": {"project": "ovrs25", "profile": "cloud-shared", "dataset": "BYD_SR"}}
    execute_cluster_run(task, {"project": {"name": "ovrs25"}}, log=log)

    assert captured["profile"] == "cloud-shared"
    assert captured["dataset"] == "BYD_SR"
