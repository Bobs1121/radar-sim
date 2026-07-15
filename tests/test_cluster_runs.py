from pathlib import Path

import pytest

from core.cluster_runs import ClusterRunStore, ClusterRunStoreError


DATASET_ID = "dataset:sha256:" + "d" * 64


def _store(tmp_path):
    return ClusterRunStore(tmp_path / "cluster-runs.db", now_fn=lambda: 100)


def _create(store, **patch):
    values = {
        "owner": "alice",
        "control_job_id": "job-1",
        "project": "ovrs25",
        "dataset_id": DATASET_ID,
        "artifact_id": "artifact-1",
        "artifact_storage_ref": "shared://selena/ovrs25/build/selena.exe",
        "profile": "default",
        "job_dir": "//private/share/job-1",
        "config_path": "//private/share/job-1/Config.cfg",
        "output_location": "//private/share/job-1/output",
    }
    values.update(patch)
    return store.create_run(**values)


def test_public_run_ref_is_path_free_but_private_lease_resolves(tmp_path):
    store = _store(tmp_path)
    run = _create(store)

    assert run.ref.startswith("cluster-run:")
    assert "private" not in str(run.to_dict()).lower()
    lease = store.resolve_private(run.ref, owner="alice")
    assert lease.config_path.endswith("Config.cfg")
    assert lease.public == run


def test_run_is_owner_isolated_and_idempotent_per_control_job(tmp_path):
    store = _store(tmp_path)
    first = _create(store)
    assert _create(store).ref == first.ref
    with pytest.raises(ClusterRunStoreError, match="unavailable"):
        store.get(first.ref, owner="bob")
    with pytest.raises(ClusterRunStoreError, match="different resolved inputs"):
        _create(store, dataset_id="dataset:sha256:" + "e" * 64)


def test_submit_requires_external_id_and_terminal_state_is_immutable(tmp_path):
    store = _store(tmp_path)
    run = _create(store)
    with pytest.raises(ClusterRunStoreError, match="external job id"):
        store.mark_submitted(run.ref, owner="alice", external_job_id="", submit_mode="xmlrpc")
    submitted = store.mark_submitted(
        run.ref, owner="alice", external_job_id="10321", submit_mode="xmlrpc"
    )
    assert submitted.state == "submitted"
    assert submitted.external_job_id == "10321"
    assert store.update_state(run.ref, owner="alice", state="running").state == "running"
    assert store.update_state(run.ref, owner="alice", state="failed").state == "failed"
    with pytest.raises(ClusterRunStoreError, match="immutable"):
        store.update_state(run.ref, owner="alice", state="running")


def test_result_ref_is_path_free_idempotent_and_private_root_is_server_only(tmp_path):
    store = _store(tmp_path)
    run = _create(store)
    private_root = tmp_path / "results" / "job-1"
    result = store.finalize_result(
        run.ref,
        owner="alice",
        state="succeeded",
        files=["output/a.MF4", "logs/CRlog.log"],
        summary={"file_count": 2, "failed_count": 0},
        physical_root=str(private_root),
    )
    assert result.ref.startswith("result:sha256:")
    assert str(tmp_path) not in str(result.to_dict())
    assert store.resolve_result_location(result.ref, owner="alice") == Path(private_root)
    again = store.finalize_result(
        run.ref,
        owner="alice",
        state="succeeded",
        files=["logs/CRlog.log", "output/a.MF4"],
        summary={"file_count": 2, "failed_count": 0},
        physical_root=str(private_root),
    )
    assert again == result


@pytest.mark.parametrize(
    "files,summary",
    [(["../escape.MF4"], {"count": 1}), (["output/a.MF4"], {"output_path": "//secret"})],
)
def test_result_rejects_path_leakage(tmp_path, files, summary):
    store = _store(tmp_path)
    run = _create(store)
    with pytest.raises(ClusterRunStoreError):
        store.finalize_result(
            run.ref,
            owner="alice",
            state="succeeded",
            files=files,
            summary=summary,
            physical_root=str(tmp_path / "private"),
        )
