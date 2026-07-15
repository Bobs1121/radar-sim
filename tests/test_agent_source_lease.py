import subprocess

import pytest

from core.agent_bindings import AgentBindingStore
from core.agent_source_lease import AgentSourceLeaseError, AgentSourceLeaseStore


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_source_lease_pins_branch_without_touching_dirty_workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "source.txt").write_text("main", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "main")
    _git(repo, "branch", "feature/demo")
    (repo / "source.txt").write_text("dirty", encoding="utf-8")
    output = repo / "build"
    output.mkdir()
    bindings = AgentBindingStore(tmp_path / "bindings.db")
    binding = bindings.register("demo", repo, (output,))
    store = AgentSourceLeaseStore(tmp_path / "source.db", now_fn=lambda: 10.0)

    lease = store.create(
        project="demo", workspace_binding_id=binding.binding_id, requested_ref="feature/demo",
        prepare_stage_id="source-1", prepare_attempt=1, job_id="job-1", binding_store=bindings,
    )
    assert lease.public_dict["branch"] == "feature/demo"
    assert str(repo) not in str(lease.public_dict)
    assert (lease.worktree_path / "source.txt").read_text(encoding="utf-8") == "main"
    assert (repo / "source.txt").read_text(encoding="utf-8") == "dirty"
    same = store.create(
        project="demo", workspace_binding_id=binding.binding_id, requested_ref="feature/demo",
        prepare_stage_id="source-1", prepare_attempt=1, job_id="job-1", binding_store=bindings,
    )
    assert same.lease_id == lease.lease_id
    worktree = lease.worktree_path
    store.release(lease.lease_id)
    assert not worktree.exists()


def test_source_lease_rejects_dangerous_ref(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "source.txt").write_text("main", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "main")
    output = repo / "build"
    output.mkdir()
    bindings = AgentBindingStore(tmp_path / "bindings.db")
    binding = bindings.register("demo", repo, (output,))
    store = AgentSourceLeaseStore(tmp_path / "source.db")
    with pytest.raises(AgentSourceLeaseError, match="preparation failed"):
        store.create(
            project="demo", workspace_binding_id=binding.binding_id, requested_ref="HEAD~1",
            prepare_stage_id="source-1", prepare_attempt=1, job_id="job-1", binding_store=bindings,
        )
