"""Tests for server-owned stale-agent recovery maintenance."""

from __future__ import annotations

import logging
import threading

from cli.server import _MaintenanceLoop, _maintenance_settings


def test_maintenance_loop_runs_once_and_stops_cleanly():
    calls: list[int] = []
    first_pass = threading.Event()
    second_pass = threading.Event()

    def reclaim() -> list[dict[str, object]]:
        calls.append(1)
        (first_pass if len(calls) == 1 else second_pass).set()
        return []

    loop = _MaintenanceLoop(reclaim, interval_seconds=60.0)
    loop.start()
    assert first_pass.wait(1.0)
    thread = loop.thread
    loop.stop(timeout_seconds=1.0)

    assert len(calls) == 1
    assert thread is not None
    assert not thread.is_alive()
    # Calling start twice must still represent one loop, not duplicate passes.
    loop.start()
    assert second_pass.wait(1.0)
    loop.stop(timeout_seconds=1.0)
    assert loop.thread is None


def test_maintenance_exception_is_logged_and_loop_survives(caplog):
    calls = 0
    second_pass = threading.Event()

    def reclaim() -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database failure")
        second_pass.set()
        return []

    with caplog.at_level(logging.ERROR):
        loop = _MaintenanceLoop(reclaim, interval_seconds=0.01)
        loop.start()
        assert second_pass.wait(1.0)
        loop.stop(timeout_seconds=1.0)

    assert calls >= 2
    assert "Stale-task maintenance pass failed" in caplog.text


def test_maintenance_settings_are_deployment_controls(monkeypatch):
    monkeypatch.setenv("RSIM_MAINTENANCE_INTERVAL_SECONDS", "7")
    monkeypatch.setenv("RSIM_MAINTENANCE_STALE_AFTER_SECONDS", "91")
    monkeypatch.setenv("RSIM_MAINTENANCE_MAX_ATTEMPTS", "0")

    assert _maintenance_settings() == (7.0, 91.0, None, 30.0)


def test_invalid_maintenance_settings_fall_back_to_safe_defaults(monkeypatch):
    monkeypatch.setenv("RSIM_MAINTENANCE_INTERVAL_SECONDS", "-1")
    monkeypatch.setenv("RSIM_MAINTENANCE_STALE_AFTER_SECONDS", "not-a-number")
    monkeypatch.setenv("RSIM_MAINTENANCE_MAX_ATTEMPTS", "-3")

    interval, stale_after, max_attempts, assignment_grace = _maintenance_settings()

    assert interval == 30.0
    assert stale_after == 300.0
    assert max_attempts is None
    assert assignment_grace == 30.0


def test_serve_v1_starts_and_stops_one_maintenance_loop(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import uvicorn
    from cli import server as server_cli

    lifecycle: list[str] = []

    class FakeLoop:
        def __init__(self, reclaim, *, interval_seconds):
            assert callable(reclaim)
            assert interval_seconds > 0

        def start(self):
            lifecycle.append("start")

        def stop(self):
            lifecycle.append("stop")

    monkeypatch.setattr(server_cli, "_MaintenanceLoop", FakeLoop)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: lifecycle.append("serve"))
    args = SimpleNamespace(
        host="127.0.0.1",
        port=8878,
        db_path=str(tmp_path / "server.db"),
        auth_file="",
        insecure_no_auth=False,
        no_cluster_executor=True,
    )

    assert server_cli._run_serve_v1(args) == 0
    assert lifecycle == ["start", "serve", "stop"]
