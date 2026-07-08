"""Tests for core.task_store: SQLite task + log persistence."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

from core.task_store import TaskStore


@dataclass
class FakeTask:
    """BuildTask-compatible for store tests."""
    task_id: str
    project: str = "test"
    kind: str = "build"
    status: str = "running"
    started_at: float = 1000.0
    finished_at: float = 0.0
    stdout_lines: list[str] = field(default_factory=list)
    returncode: Optional[int] = None
    errors: list[str] = field(default_factory=list)
    exe_path: str = ""
    current_file: str = ""
    files_done: int = 0
    files_total: int = 0


@pytest.fixture
def store(tmp_path):
    return TaskStore(db_path=tmp_path / "tasks.db")


def test_save_and_load_task(store):
    t = FakeTask(task_id="t1", status="running", started_at=1000.0)
    t.stdout_lines = ["line1", "line2"]
    store.save_task(t, new_lines=["line1", "line2"])
    loaded = store.load_task("t1")
    assert loaded is not None
    assert loaded["status"] == "running"
    assert loaded["total_lines"] == 2


def test_tail_logs_incremental(store):
    t = FakeTask(task_id="t2", stdout_lines=["a", "b", "c"])
    store.save_task(t, new_lines=["a", "b", "c"])
    assert store.tail_logs("t2", 0) == ["a", "b", "c"]
    assert store.tail_logs("t2", 1) == ["b", "c"]
    assert store.tail_logs("t2", 3) == []


def test_append_new_lines_on_resave(store):
    t = FakeTask(task_id="t3", stdout_lines=["x"])
    store.save_task(t, new_lines=["x"])
    # Append more lines.
    t.stdout_lines = ["x", "y", "z"]
    t.status = "success"
    store.save_task(t, new_lines=["y", "z"])
    loaded = store.load_task("t3")
    assert loaded["status"] == "success"
    assert loaded["total_lines"] == 3
    assert store.tail_logs("t3", 0) == ["x", "y", "z"]


def test_load_missing_task(store):
    assert store.load_task("nope") is None


def test_list_tasks_newest_first(store):
    t1 = FakeTask(task_id="old", started_at=1000.0, status="success")
    t1.stdout_lines = ["done"]
    store.save_task(t1, new_lines=["done"])
    t2 = FakeTask(task_id="new", started_at=2000.0, status="running")
    t2.stdout_lines = ["run"]
    store.save_task(t2, new_lines=["run"])
    tasks = store.list_tasks(10)
    assert len(tasks) == 2
    assert tasks[0]["task_id"] == "new"  # newest first
    assert tasks[1]["task_id"] == "old"


def test_errors_persisted_as_json(store):
    t = FakeTask(task_id="t4", errors=["boom", "crash"])
    store.save_task(t, new_lines=[])
    loaded = store.load_task("t4")
    assert loaded["errors"] == ["boom", "crash"]


def test_tail_from_store_via_registry(store, monkeypatch):
    """TaskRegistry.tail falls back to SQLite when task not in memory."""
    from core.build_runner import TaskRegistry
    reg = TaskRegistry()
    monkeypatch.setattr("core.task_store.get_store", lambda: store)
    t = FakeTask(task_id="mem_gone", status="success", started_at=1000.0, finished_at=1010.0, returncode=0)
    t.stdout_lines = ["survived"]
    store.save_task(t, new_lines=["survived"])
    # Not in registry memory → should find via store.
    result = reg.tail("mem_gone", 0)
    assert result["found"] is True
    assert result["status"] == "success"
    assert result["lines"] == ["survived"]


def test_list_tasks_via_registry(store, monkeypatch):
    from core.build_runner import TaskRegistry
    reg = TaskRegistry()
    monkeypatch.setattr("core.task_store.get_store", lambda: store)
    t = FakeTask(task_id="hist", started_at=5000.0)
    store.save_task(t, new_lines=[])
    tasks = reg.list_tasks(10)
    assert any(t["task_id"] == "hist" for t in tasks)
