from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
import time

import pytest

from core.cluster_runs import ClusterRunStore, ClusterRunStoreError
from core.cluster_stage_executor import build_public_run_manifest, execute_cluster_collect, resolve_cluster_data
from core.cluster_stage_executor import ClusterStageContext, ClusterStageExecutor
from core.control_service import ControlService
from core.api_v1 import ApiV1Service
from core.artifact_store import ArtifactStore
from core.config_assets import ConfigAssetStore
from core.datasets import DatasetCatalog, DatasetFileRef
from core.runtime_bundle import RuntimeSourceEvidence, discover_runtime_bundle
from core.runtime_bundle_archive import stage_runtime_bundle_archive
from core.runtime_bundle_catalog import RuntimeBundleCatalog, RuntimeBundleRecord
from core.local_results import ResultCatalog


def _job():
    return {
        "job_id": "job-demo",
        "owner": "alice",
        "payload": {"spec_hash": "sha256:" + "1" * 64},
        "spec": {"simulation": {"timeout_minutes": 1}},
        "resolved_spec": {
            "decisions": {
                "selena": {"runtime_bundle": {"id": "selena-bundle:sha256:" + "2" * 64}},
                "data": {"dataset": {"id": "dataset:sha256:" + "3" * 64}},
            }
        },
    }


def test_collect_cancellation_creates_path_free_terminal_result(tmp_path: Path):
    store = ClusterRunStore(tmp_path / "runs.db", now_fn=lambda: 10.0)
    run = store.create_run(
        owner="alice", control_job_id="job-demo", project="bydod25",
        dataset_id="dataset:sha256:" + "3" * 64,
        artifact_id="selena-bundle:sha256:" + "2" * 64,
        artifact_storage_ref="shared://selena-bundles/bydod25/runtime-bundle.zip",
        profile="default", job_dir=str(tmp_path / "private-job"),
        config_path="//cluster/job/Config.cfg", output_location=str(tmp_path / "private-output"),
    )
    store.mark_submitted(run.ref, owner="alice", external_job_id="10321", submit_mode="xmlrpc")
    context = SimpleNamespace(
        run_store=store,
        config_loader=lambda _project: {"cluster": {"timeout_min": 120}},
        now_fn=lambda: 10.0,
    )

    output = execute_cluster_collect(context, _job(), run.ref, cancelled=lambda: True, sleep_fn=lambda _s: None)
    result = store.get_result(output["result_ref"], owner="alice")
    assert result.state == "cancelled"
    assert result.files == ()
    assert str(tmp_path) not in str(output)


def test_collect_uses_result_ini_when_official_page_has_no_tasks(tmp_path: Path, monkeypatch):
    store = ClusterRunStore(tmp_path / "runs.db", now_fn=lambda: 10.0)
    run = store.create_run(
        owner="alice", control_job_id="job-demo", project="ovrs25",
        dataset_id="dataset:sha256:" + "3" * 64,
        artifact_id="selena-bundle:sha256:" + "2" * 64,
        artifact_storage_ref="shared://selena-bundles/ovrs25/runtime-bundle.zip",
        profile="default", job_dir=str(tmp_path / "private-job"),
        config_path="//cluster/job/Config.cfg", output_location=str(tmp_path / "private-output"),
    )
    store.mark_submitted(run.ref, owner="alice", external_job_id="1", submit_mode="xmlrpc")
    context = SimpleNamespace(
        run_store=store,
        config_loader=lambda _project: {"cluster": {"timeout_min": 1}},
        now_fn=lambda: 10.0,
        result_catalog=None,
    )
    monkeypatch.setattr(
        "core.cluster.get_cluster_web_status",
        lambda *_args, **_kwargs: {"found": True, "job_id": "1", "tasks": []},
    )
    monkeypatch.setattr(
        "core.cluster.inspect_cluster_job",
        lambda *_args, **_kwargs: {
            "state": "finished-failed", "file_count": 2,
            "success_count": 0, "fail_count": 1,
            "error_summary": ["missing signal"],
            "result_files": [{"relative_path": "OUT/result.ini"}],
        },
    )

    output = execute_cluster_collect(
        context, _job(), run.ref,
        cancelled=lambda: False,
        sleep_fn=lambda _seconds: (_ for _ in ()).throw(AssertionError("must not wait")),
    )

    result = store.get_result(output["cluster_result_ref"], owner="alice")
    assert result.state == "failed"
    assert result.summary["failed_count"] == 1
    assert output["result_ref"] == ""


def test_collect_queries_by_generated_job_directory_and_waits_for_every_dataset_file(
    tmp_path: Path, monkeypatch
):
    store = ClusterRunStore(tmp_path / "runs.db", now_fn=lambda: 10.0)
    run = store.create_run(
        owner="alice", control_job_id="job-demo", project="bydod25",
        dataset_id="dataset:sha256:" + "3" * 64,
        artifact_id="selena-bundle:sha256:" + "2" * 64,
        artifact_storage_ref="shared://selena-bundles/bydod25/runtime-bundle.zip",
        profile="default", job_dir=str(tmp_path / "private-job"),
        config_path=r"\\cluster\jobs\job-demo\Config.cfg",
        output_location=str(tmp_path / "private-output"),
    )
    # This deployment returns the created task count, not the durable Cluster
    # job id. Collection must therefore resolve the job by its generated path.
    store.mark_submitted(run.ref, owner="alice", external_job_id="2", submit_mode="xmlrpc")
    context = SimpleNamespace(
        run_store=store,
        config_loader=lambda _project: {"cluster": {"timeout_min": 1}},
        now_fn=lambda: 10.0,
        result_catalog=None,
    )
    job = _job()
    job["resolved_spec"]["decisions"]["data"]["dataset"]["file_count"] = 2
    queries = []
    monkeypatch.setattr(
        "core.cluster.get_cluster_web_status",
        lambda _config, query: queries.append(query) or {
            "found": True, "job_id": "10357", "state": "running", "tasks": []
        },
    )
    inspections = iter([
        {
            "state": "finished-success", "file_count": 2,
            "success_count": 1, "fail_count": 0, "error_summary": [],
            "output_mf4": [{"relative_path": "output/aout.MF4", "size": 10}],
            "result_files": [{"relative_path": "output/a/result.ini"}],
        },
        {
            "state": "finished-success", "file_count": 4,
            "success_count": 2, "fail_count": 0, "error_summary": [],
            "output_mf4": [
                {"relative_path": "output/aout.MF4", "size": 10},
                {"relative_path": "output/bout.MF4", "size": 20},
            ],
            "result_files": [
                {"relative_path": "output/a/result.ini"},
                {"relative_path": "output/b/result.ini"},
            ],
        },
        {
            "state": "finished-success", "file_count": 4,
            "success_count": 2, "fail_count": 0, "error_summary": [],
            "output_mf4": [
                {"relative_path": "output/aout.MF4", "size": 10},
                {"relative_path": "output/bout.MF4", "size": 20},
            ],
            "result_files": [
                {"relative_path": "output/a/result.ini"},
                {"relative_path": "output/b/result.ini"},
            ],
        },
    ])
    monkeypatch.setattr("core.cluster.inspect_cluster_job", lambda *_args: next(inspections))
    sleeps = []

    output = execute_cluster_collect(
        context, job, run.ref,
        cancelled=lambda: False,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    expected_query = str(PureWindowsPath(r"\\cluster\jobs\job-demo\Config.cfg").parent)
    assert queries == [expected_query, expected_query]
    assert sleeps == [15.0]
    assert output["result"]["state"] == "succeeded"
    assert output["result"]["summary"]["success_count"] == 2


def test_collect_archive_failure_does_not_make_cluster_run_terminal(tmp_path: Path, monkeypatch):
    store = ClusterRunStore(tmp_path / "runs.db", now_fn=lambda: 10.0)
    run = store.create_run(
        owner="alice", control_job_id="job-demo", project="bydod25",
        dataset_id="dataset:sha256:" + "3" * 64,
        artifact_id="selena-bundle:sha256:" + "2" * 64,
        artifact_storage_ref="shared://selena-bundles/bydod25/runtime-bundle.zip",
        profile="default", job_dir=str(tmp_path / "private-job"),
        config_path=r"\\cluster\jobs\job-demo\Config.cfg",
        output_location=str(tmp_path / "private-output"),
    )
    store.mark_submitted(run.ref, owner="alice", external_job_id="1", submit_mode="xmlrpc")
    context = SimpleNamespace(
        run_store=store,
        config_loader=lambda _project: {"cluster": {"timeout_min": 1}},
        now_fn=lambda: 10.0,
        result_catalog=SimpleNamespace(
            publish=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("source changed"))
        ),
    )
    monkeypatch.setattr(
        "core.cluster.get_cluster_web_status",
        lambda *_args, **_kwargs: {
            "found": True, "state": "finished",
            "tasks": [{"simulation_state": "finished"}],
        },
    )
    monkeypatch.setattr(
        "core.cluster.inspect_cluster_job",
        lambda *_args, **_kwargs: {
            "state": "finished-success", "file_count": 2,
            "success_count": 1, "fail_count": 0, "error_summary": [],
            "output_mf4": [{"relative_path": "output/aout.MF4", "size": 10}],
            "result_files": [{"relative_path": "output/result.ini"}],
        },
    )

    with pytest.raises(RuntimeError, match="source changed"):
        execute_cluster_collect(context, _job(), run.ref, sleep_fn=lambda _seconds: None)

    assert store.get(run.ref, owner="alice").state == "running"


def test_public_manifest_contains_refs_but_no_physical_locations(tmp_path: Path):
    store = ClusterRunStore(tmp_path / "runs.db", now_fn=lambda: 10.0)
    run = store.create_run(
        owner="alice", control_job_id="job-demo", project="bydod25",
        dataset_id="dataset:sha256:" + "3" * 64,
        artifact_id="selena-bundle:sha256:" + "2" * 64,
        artifact_storage_ref="shared://selena-bundles/bydod25/runtime-bundle.zip",
        profile="default", job_dir="//private/job", config_path="//private/job/Config.cfg",
        output_location="//private/job/output",
    )
    with pytest.raises(ClusterRunStoreError, match="private fields"):
        store.finalize_result(
            run.ref, owner="alice", state="failed", files=("output/result.ini",),
            summary={"status": "failed", "private_path": "D:/secret/output"},
            physical_root="D:/secret/output",
        )
    result = store.finalize_result(
        run.ref, owner="alice", state="failed", files=("output/result.ini",),
        summary={"status": "failed"}, physical_root="D:/secret/output",
    )
    manifest = build_public_run_manifest(_job(), result)

    assert manifest["runtime_bundle_id"].startswith("selena-bundle:sha256:")
    assert manifest["dataset_id"].startswith("dataset:sha256:")
    assert manifest["result_ref"].startswith("result:sha256:")
    assert "D:/secret" not in str(manifest)
    assert "private_path" not in str(manifest)


def test_shared_data_uses_trusted_recognition_while_runtime_is_still_building(monkeypatch):
    dataset = SimpleNamespace(
        id="dataset:sha256:" + "3" * 64,
        to_dict=lambda: {"id": "dataset:sha256:" + "3" * 64},
    )
    loaded = []
    context = SimpleNamespace(
        config_loader=lambda project: loaded.append(project) or {"shared_namespaces": []},
        dataset_catalog=object(),
    )
    monkeypatch.setattr(
        "core.cluster_stage_executor.resolve_data_reference",
        lambda *_args, **_kwargs: SimpleNamespace(status="resolved", dataset=dataset, action=""),
    )
    job = {
        "owner": "alice",
        "spec": {"data": {"path": "//shared/data/input.MF4"}},
        "resolved_spec": {"decisions": {}},
        "stages": [{
            "stage_type": "resolve_spec",
            "status": "succeeded",
            "result": {"recognition": {"internal_project": "ovrs25"}},
        }],
    }

    result = resolve_cluster_data(context, job)

    assert result["dataset_id"] == dataset.id
    assert loaded == ["ovrs25"]


def test_existing_bundle_cluster_pipeline_finishes_without_windows_or_adapter(tmp_path: Path, monkeypatch):
    control = ControlService(tmp_path / "control.db")
    runtime_output = tmp_path / "build"
    runtime_output.mkdir()
    (runtime_output / "selena.exe").write_bytes(b"selena")
    (runtime_output / "required.dll").write_bytes(b"dll")
    runtime_xml = tmp_path / "Runtime.xml"
    runtime_xml.write_text("<runtime />", encoding="utf-8")
    lease = discover_runtime_bundle(
        runtime_output / "selena.exe", runtime_xml,
        source=RuntimeSourceEvidence(
            branch="main", commit="a" * 40, dirty=False, dirty_fingerprint="",
            build_mode="Release", toolchain_fingerprint="msvc", adapter_key="recipe:bydod25",
        ), created_at=1.0,
    )
    archive = stage_runtime_bundle_archive(lease, tmp_path / "bundle-stage")
    runtime_catalog = RuntimeBundleCatalog(tmp_path / "runtime.db")
    record = RuntimeBundleRecord(
        manifest=lease.manifest, internal_project="bydod25",
        storage_ref="shared://selena-bundles/bydod25/runtime-bundle.zip",
        archive_checksum=archive.checksum, archive_size=archive.size,
        owner="alice", created_by="test-agent",
    )
    runtime_catalog.register(record)
    runtime_store = SimpleNamespace(resolve_location=lambda _ref: archive.path)

    datasets = DatasetCatalog(tmp_path / "datasets.db")
    data_root = tmp_path / "dataset"
    data_root.mkdir()
    (data_root / "input.MF4").write_bytes(b"mf4")
    dataset = datasets.register_uploaded(
        project="run-config-v2", owner="alice", source_kind="central_upload",
        source_path=str(data_root), storage_ref="shared://datasets/alice/demo",
        files=(DatasetFileRef("input.MF4", 3, "sha256:" + "1" * 64),),
    )
    assets = ConfigAssetStore(tmp_path / "assets", tmp_path / "assets.db")
    adapter = assets.put(owner="alice", kind="adapter", filename="adapter.txt", content=b"adapter\n")
    mat_filter = assets.put(owner="alice", kind="mat_filter", filename="signals.filter", content=b"*\n")
    runs = ClusterRunStore(tmp_path / "runs.db")
    results = ResultCatalog(
        tmp_path / "result-archives",
        tmp_path / "results.db",
        allowed_source_root=tmp_path,
    )
    private_job = tmp_path / "cluster-job"
    (private_job / "output").mkdir(parents=True)
    (private_job / "output" / "result.ini").write_text("successfull=1", encoding="utf-8")
    (private_job / "output" / "inputout.MF4").write_bytes(b"simulated-output")

    monkeypatch.setattr("core.cluster.check_cluster_environment", lambda _cfg: [SimpleNamespace(name="manager", ok=True)])
    monkeypatch.setattr("core.preflight.run_preflight", lambda _cfg: SimpleNamespace(ok=True, checks=[]))
    monkeypatch.setattr(
        "core.cluster.prepare_cluster_job",
        lambda *_args, **_kwargs: SimpleNamespace(
            manifest_path=str(private_job / "manifest.json"), config_path="//cluster/job/Config.cfg",
            profile="default",
        ),
    )
    monkeypatch.setattr(
        "core.cluster.submit_cluster_job",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="value=10321", mode="xmlrpc"),
    )
    monkeypatch.setattr(
        "core.cluster.get_cluster_web_status",
        lambda *_args, **_kwargs: {"found": True, "state": "finished", "tasks": [{"simulation_state": "finished"}]},
    )
    monkeypatch.setattr(
        "core.cluster.inspect_cluster_job",
        lambda *_args, **_kwargs: {
            "file_count": 2, "success_count": 1, "fail_count": 0, "error_summary": [],
            "output_mf4": [{"relative_path": "output/inputout.MF4", "size": 16}],
            "result_files": [{"relative_path": "output/result.ini"}],
        },
    )
    config = {
        "_meta": {"project": "bydod25"}, "paths": {}, "selena": {}, "build": {}, "simulation": {},
        "cluster": {"timeout_min": 1, "workspace_root": "//cluster/work", "project_folder": "radar-sim"},
    }
    context = ClusterStageContext(
        runtime_catalog=runtime_catalog, runtime_store=runtime_store,
        dataset_catalog=datasets, config_assets=assets, run_store=runs,
        work_root=tmp_path / "work", config_loader=lambda _project: config,
        result_catalog=results,
    )
    executor = ClusterStageExecutor(control, context, poll_interval=0.02)
    executor.start()
    try:
        upload_service = SimpleNamespace(resolve_bundle=lambda _owner, _bundle: record)
        api = ApiV1Service(
            control_service_factory=lambda _owner: control,
            runtime_bundle_upload_service_factory=lambda _owner: upload_service,
            result_catalog=results,
        )
        config_payload = {
            "schema_version": "2.0",
            "selena": {
                "source": "existing",
                "existing_path": record.manifest.id,
                "runtime_xml": "D:/existing/Selena/Runtime.xml",
            },
            "data": {"path": "dataset://sha256/" + dataset.id.rsplit(":", 1)[-1]},
            "simulation": {
                "target": "cluster", "adapter_file": "",
                "mat_filter": mat_filter.uri,
            },
        }
        submitted = api.submit_user_run("alice", config_payload=config_payload)
        deadline = time.time() + 10
        while time.time() < deadline:
            current = api.get_job("alice", submitted["id"])
            if current["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        diagnostics = [
            (stage["stage_type"], stage["status"], stage.get("result"), stage.get("error"))
            for stage in current["stages"]
        ]
        assert current["status"] == "succeeded", diagnostics
        manifest = api.manifest("alice", submitted["id"])
        assert manifest["available"] is True
        assert manifest["manifest"]["runtime_bundle_id"] == record.manifest.id
        result_ref = manifest["manifest"]["result_ref"]
        assert api.get_result("alice", result_ref)["file_count"] == 2
        assert results.resolve_archive(result_ref, owner="alice").is_file()
        assert str(tmp_path) not in str(manifest)
        assert all(
            stage["status"] in {"succeeded", "skipped"}
            for stage in current["stages"]
        )
    finally:
        executor.stop()
