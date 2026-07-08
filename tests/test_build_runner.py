"""Tests for core.build_runner: _build_env passthrough, TCC preflight, tcc task."""

from unittest.mock import patch

import pytest

from core.build_runner import BuildTask, TaskRegistry, _build_env, get_registry


def test_build_env_returns_os_environ_copy():
    import os
    env = _build_env({"environment": {"boost_root": "C:/fake"}})
    # No BOOST_ROOT injection — passed through unchanged.
    assert env == os.environ.copy()
    assert env.get("BOOST_ROOT") == os.environ.get("BOOST_ROOT")


def test_build_env_does_not_mutate_os_environ():
    import os
    before = dict(os.environ)
    _build_env({"environment": {"boost_root": "C:/fake", "qt_path": "C:/qt"}})
    assert dict(os.environ) == before


def test_start_tcc_task_unknown_action():
    reg = TaskRegistry()
    task_id = reg.start_tcc_task("test", "bogus_action")
    task = reg.get(task_id)
    # The task runs in a thread; give it a moment.
    import time
    for _ in range(50):
        if task.status not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert task.status == "failed"
    assert any("unknown tcc action" in e for e in task.errors)


def test_start_tcc_task_install_no_toolcollection(tmp_path):
    # No toolcollection configured and none passed → failed with clear message.
    reg = TaskRegistry()
    with patch("core.build_runner.load_config", return_value={"repos": {}}):
        task_id = reg.start_tcc_task("test", "install_toolcollection")
    task = reg.get(task_id)
    import time
    for _ in range(50):
        if task.status not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert task.status == "failed"
    assert any("no toolcollection" in e for e in task.errors)


def test_build_task_tail_structure():
    reg = TaskRegistry()
    task_id = reg.start_tcc_task("test", "bogus")
    import time
    time.sleep(0.3)
    snap = reg.tail(task_id, since=0)
    assert snap["found"] is True
    assert snap["status"] in ("failed", "running", "success")
    assert "task_id" in snap


def test_get_registry_singleton():
    assert get_registry() is get_registry()
