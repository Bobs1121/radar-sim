"""Concurrency isolation tests — verify the four deadly race conditions are fixed.

1. _results_runtime_dir: concurrent load_config() calls get unique _run_id dirs
   (no more os.getpid() collision across threads).
2. assigned_agent_id: a task pre-bound to agent A cannot be claimed by agent B.
3. prepare_repo_worktree: creates an isolated worktree (smoke test; full git
   concurrency is environment-dependent).
4. create_job + claim: same-user two agents — unbound tasks are claimable by
   either, bound tasks only by the bound agent.
"""

import threading
import uuid
from pathlib import Path

import pytest

from core.config import load_config
from core.control_service import ControlService
from core.simulation import _results_runtime_dir


# --- 1. _run_id uniqueness across concurrent load_config calls ---

def test_run_id_unique_per_load_config():
    """Two load_config() calls produce different _run_id → isolated runtime dirs."""
    cfg1 = load_config("ovrs25")
    cfg2 = load_config("ovrs25")
    rid1 = cfg1.get("_meta", {}).get("_run_id")
    rid2 = cfg2.get("_meta", {}).get("_run_id")
    assert rid1 and rid2
    assert rid1 != rid2, "consecutive load_config calls must differ in _run_id"


def test_run_id_stable_within_one_config():
    """The same config object yields the same runtime dir on repeated calls."""
    cfg = load_config("ovrs25")
    d1 = _results_runtime_dir(cfg)
    d2 = _results_runtime_dir(cfg)
    assert d1 == d2, "same config object must keep a stable runtime dir"


def test_concurrent_load_config_distinct_dirs():
    """Threads in the same process get distinct _run_id (the pid-collision bug)."""
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        cfg = load_config("ovrs25")
        results.append(_results_runtime_dir(cfg))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 8
    assert len(set(str(p) for p in results)) == 8, \
        f"runtime dirs must be unique across threads, got {len(set(map(str, results)))} unique"


# --- 2. assigned_agent_id binding ---

@pytest.fixture
def svc(tmp_path):
    return ControlService(db_path=tmp_path / "control.db")


def _register(svc, agent_id, caps):
    svc.register_agent(
        name=agent_id, agent_id=agent_id, platform="test",
        hostname="h", capabilities=caps, metadata={},
    )


def test_assigned_agent_id_blocks_other_agent(svc):
    """A task bound to agent-A cannot be claimed by agent-B."""
    _register(svc, "agent-A", ["local.check"])
    _register(svc, "agent-B", ["local.check"])
    # Create a job with task bound to agent-A.
    svc.create_job("local.check", payload={"project": "x"}, assigned_agent_id="agent-A")
    # Agent-B polls — must NOT get the bound task.
    claimed_b = svc.claim_next_task("agent-B")
    assert claimed_b is None, "agent-B must not steal agent-A's bound task"
    # Agent-A polls — gets it.
    claimed_a = svc.claim_next_task("agent-A")
    assert claimed_a is not None
    assert claimed_a["assigned_agent_id"] == "agent-A"


def test_unbound_task_claimable_by_any_agent(svc):
    """No assigned_agent_id → backward compatible, any agent can claim."""
    _register(svc, "agent-A", ["local.check"])
    _register(svc, "agent-B", ["local.check"])
    svc.create_job("local.check", payload={"project": "x"})  # no binding
    # Whichever polls first wins (FIFO by created_at); the other gets nothing.
    claimed = svc.claim_next_task("agent-A")
    assert claimed is not None
    assert svc.claim_next_task("agent-B") is None


def test_per_task_spec_override_binding(svc):
    """tasks[].assigned_agent_id overrides the job-level binding."""
    _register(svc, "agent-A", ["local.check"])
    _register(svc, "agent-B", ["local.check"])
    svc.create_job(
        "local.check",
        tasks=[
            {"task_type": "local.check", "assigned_agent_id": "agent-B"},
        ],
    )
    # agent-A cannot claim (task is bound to agent-B).
    assert svc.claim_next_task("agent-A") is None
    assert svc.claim_next_task("agent-B") is not None


# --- 3. worktree smoke test ---

def test_prepare_repo_worktree_no_branch_returns_empty():
    """No target branch → no worktree needed, returns ('', '')."""
    from core.repo import prepare_repo_worktree, cleanup_repo_worktree
    cfg = {"repos": {"inner_repo_root": ""}, "build": {}}
    err, wt = prepare_repo_worktree(cfg)
    assert err == ""
    assert wt == ""
    cleanup_repo_worktree(wt)  # no-op on empty


def test_prepare_repo_worktree_missing_repo_errors():
    """Non-existent inner_repo → error, no worktree."""
    from core.repo import prepare_repo_worktree, cleanup_repo_worktree
    cfg = {"repos": {"inner_repo_root": "/nonexistent/path/xyz"}, "build": {"selena_branch": "main"}}
    err, wt = prepare_repo_worktree(cfg)
    assert err != ""
    assert wt == ""
    cleanup_repo_worktree(wt)


# --- 4. end-to-end: same-user two agents, mixed bound/unbound ---

def test_mixed_bound_and_unbound_tasks_two_agents(svc):
    """Same-user scenario: 2 agents, one job bound to A, one unbound.

    Bound task → only A. Unbound task → whoever polls first (A or B).
    """
    _register(svc, "agent-A", ["cluster.run"])
    _register(svc, "agent-B", ["cluster.run"])
    # Job 1 bound to A.
    svc.create_job("cluster.run", payload={"run_id": "bound"}, assigned_agent_id="agent-A")
    # Job 2 unbound.
    svc.create_job("cluster.run", payload={"run_id": "free"})

    # B polls first: gets the unbound job, NOT the bound one.
    claimed_b = svc.claim_next_task("agent-B")
    assert claimed_b is not None
    assert claimed_b["payload"]["run_id"] == "free", "B should only get the unbound task"

    # A polls: gets the bound job.
    claimed_a = svc.claim_next_task("agent-A")
    assert claimed_a is not None
    assert claimed_a["payload"]["run_id"] == "bound"
