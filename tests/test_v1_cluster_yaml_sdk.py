import base64
import json
from types import SimpleNamespace
import time

from fastapi.testclient import TestClient

from core.artifact_store import ArtifactStore
from core.api_v1 import ApiV1Service
from core.api_v1_fastapi import create_app
from core.cluster_runs import ClusterRunStore
from core.cluster_stage_executor import ClusterStageContext, ClusterStageExecutor
from core.config_assets import ConfigAssetStore
from core.control_service import ControlService
from core.dataset_store import DatasetStore
from core.dataset_upload_service import DatasetUploadService
from core.datasets import DatasetCatalog
from core.existing_selena import import_existing_selena
from core.runtime_bundle_catalog import RuntimeBundleCatalog
from core.runtime_bundle_upload_service import RuntimeBundleUploadService
from core.user_config import UserRunConfig
from radar_sim_sdk import RadarSimClient


def _inputs(tmp_path):
    selena = tmp_path / "ovrs25-selena"
    selena.mkdir()
    (selena / "selena.exe").write_bytes(b"exe")
    (selena / "core.dll").write_bytes(b"core")
    (selena / "plugin.dll").write_bytes(b"plugin")
    runtime = tmp_path / "Runtime_For_byd_ovrs25.xml"
    runtime.write_text("<runtime project='BYD_OVS'/>", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "one.MF4").write_bytes(b"mf4")
    mat_filter = tmp_path / "mat.filter"
    mat_filter.write_text("signal=*", encoding="utf-8")
    yaml_path = tmp_path / "simulation.yaml"
    yaml_path.write_text(
        "\n".join(
            [
                "schema_version: '2.0'",
                "selena:",
                "  source: existing",
                f"  existing_path: '{selena.as_posix()}'",
                f"  runtime_xml: '{runtime.as_posix()}'",
                "data:",
                f"  path: '{data.as_posix()}'",
                "simulation:",
                "  target: cluster",
                "  adapter_file: ''",
                f"  mat_filter: '{mat_filter.as_posix()}'",
            ]
        ),
        encoding="utf-8",
    )
    return selena, runtime, data, mat_filter, yaml_path


def test_server_import_verifies_and_catalogues_existing_selena_archive(tmp_path):
    selena, runtime, _data, _mat_filter, _yaml = _inputs(tmp_path)
    imported = import_existing_selena(
        selena, runtime, staging_root=tmp_path / "staging", created_at=100
    )
    store = ArtifactStore(
        tmp_path / "store",
        object_filename="runtime-bundle.zip",
        storage_ref_prefix="shared://selena-bundles/",
    )
    catalog = RuntimeBundleCatalog(tmp_path / "catalog.db")
    service = RuntimeBundleUploadService(store, catalog, lambda _owner, _ref: None)
    result = service.import_existing(
        "alice",
        metadata={
            "internal_project": imported.internal_project,
            "adapter_key": imported.adapter_key,
            "manifest": imported.bundle.manifest.to_dict(),
            "archive_checksum": imported.archive.checksum,
            "archive_size": imported.archive.size,
        },
        archive_bytes=imported.archive.path.read_bytes(),
    )
    bundle = result["runtime_bundle"]
    assert bundle["id"] == imported.bundle.manifest.id
    assert catalog.get(bundle["id"]).internal_project == "ovrs25"
    assert {item["role"] for item in bundle["files"]} == {
        "entrypoint", "runtime_library", "runtime_config"
    }


def test_linux_service_imports_a_server_visible_shared_selena_path(tmp_path):
    _selena, _runtime, _data, _mat_filter, yaml_path = _inputs(tmp_path)
    store = ArtifactStore(
        tmp_path / "store",
        object_filename="runtime-bundle.zip",
        storage_ref_prefix="shared://selena-bundles/",
    )
    catalog = RuntimeBundleCatalog(tmp_path / "catalog.db")
    uploads = RuntimeBundleUploadService(store, catalog, lambda _owner, _ref: None)
    api = ApiV1Service(
        control_service_factory=lambda _owner: ControlService(tmp_path / "control.db"),
        runtime_bundle_upload_service_factory=lambda _owner: uploads,
    )
    config = UserRunConfig.from_yaml(yaml_path).to_dict()

    first = api.submit_user_run("alice", config_payload=config)
    second = api.submit_user_run("alice", config_payload=config)

    first_bundle = first["resolved_spec"]["decisions"]["selena"]["runtime_bundle"]["id"]
    second_bundle = second["resolved_spec"]["decisions"]["selena"]["runtime_bundle"]["id"]
    assert first_bundle == second_bundle
    assert catalog.get(first_bundle).internal_project == "ovrs25"
    assert next(stage for stage in first["stages"] if stage["stage_type"] == "resolve_spec")["status"] == "skipped"


def test_linux_service_maps_authorized_unc_selena_to_its_mount(tmp_path, monkeypatch):
    _selena, _runtime, _data, _mat_filter, yaml_path = _inputs(tmp_path)
    store = ArtifactStore(
        tmp_path / "store",
        object_filename="runtime-bundle.zip",
        storage_ref_prefix="shared://selena-bundles/",
    )
    catalog = RuntimeBundleCatalog(tmp_path / "catalog.db")
    uploads = RuntimeBundleUploadService(store, catalog, lambda _owner, _ref: None)
    monkeypatch.setattr(
        "core.config.load_config",
        lambda _project: {
            "cluster": {"linux_mount_map": {r"\\server\share": tmp_path.as_posix()}}
        },
    )
    api = ApiV1Service(
        control_service_factory=lambda _owner: ControlService(tmp_path / "control.db"),
        runtime_bundle_upload_service_factory=lambda _owner: uploads,
        project_names_provider=lambda: ["ovrs25"],
    )
    config = UserRunConfig.from_yaml(yaml_path).to_dict()
    config["selena"]["existing_path"] = r"\\server\share\ovrs25-selena"
    config["selena"]["runtime_xml"] = r"\\server\share\Runtime_For_byd_ovrs25.xml"

    job = api.submit_user_run("alice", config_payload=config)

    bundle_id = job["resolved_spec"]["decisions"]["selena"]["runtime_bundle"]["id"]
    assert catalog.get(bundle_id).internal_project == "ovrs25"


def test_submit_cluster_yaml_is_one_call_and_prepares_all_local_inputs(tmp_path, monkeypatch):
    _selena, _runtime, data, mat_filter, yaml_path = _inputs(tmp_path)
    sdk = RadarSimClient("http://testserver")
    calls = []
    bundle_id = "selena-bundle:sha256:" + "a" * 64

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/v1/existing-selena-imports":
            metadata_text = kwargs["headers"]["X-Rsim-Existing-Metadata"]
            metadata = json.loads(
                base64.urlsafe_b64decode(metadata_text + "=" * (-len(metadata_text) % 4))
            )
            assert metadata["internal_project"] == "ovrs25"
            assert len(kwargs["content"]) == metadata["archive_size"]
            return {"runtime_bundle": {"id": bundle_id}, "reused": False}
        if path == "/api/v1/run-jobs":
            return {"id": "job-v1", "status": "queued", "spec": kwargs["json"]["config"]}
        raise AssertionError(path)

    monkeypatch.setattr(sdk, "_request", request)
    monkeypatch.setattr(
        sdk,
        "upload_run_data",
        lambda source: SimpleNamespace(data_path="dataset://sha256/" + "b" * 64),
    )
    monkeypatch.setattr(
        sdk,
        "upload_config_asset",
        lambda kind, source: {
            "uri": "config-asset://sha256/" + ("c" if kind == "mat_filter" else "d") * 64
        },
    )

    job = sdk.submit_cluster_yaml(yaml_path)

    assert job.id == "job-v1"
    final = next(item for item in calls if item[1] == "/api/v1/run-jobs")[2]["json"]
    assert final["prepared_runtime_bundle_id"] == bundle_id
    assert final["config"]["data"]["path"].startswith("dataset://")
    assert final["config"]["simulation"]["mat_filter"].startswith("config-asset://")
    assert final["config"]["simulation"]["adapter_file"] == ""
    assert final["config"]["selena"]["source"] == "existing"
    assert data.as_posix() not in final["config"]["data"]["path"]
    assert mat_filter.as_posix() not in final["config"]["simulation"]["mat_filter"]


def test_one_sdk_call_reaches_cluster_submission_with_existing_selena(tmp_path, monkeypatch):
    """V1 release gate: YAML -> SDK -> Linux API -> Cluster submit."""
    _selena, _runtime, _data, _mat_filter, yaml_path = _inputs(tmp_path)
    control = ControlService(tmp_path / "control.db")

    runtime_store = ArtifactStore(
        tmp_path / "runtime-store",
        object_filename="runtime-bundle.zip",
        storage_ref_prefix="shared://selena-bundles/",
    )
    runtime_catalog = RuntimeBundleCatalog(tmp_path / "runtime-catalog.db")
    runtime_uploads = RuntimeBundleUploadService(
        runtime_store, runtime_catalog, lambda _owner, _ref: None
    )
    dataset_catalog = DatasetCatalog(tmp_path / "dataset-catalog.db")
    dataset_uploads = DatasetUploadService(
        DatasetStore(tmp_path / "dataset-store"), dataset_catalog
    )
    config_assets = ConfigAssetStore(tmp_path / "config-assets", tmp_path / "config-assets.db")
    run_store = ClusterRunStore(tmp_path / "cluster-runs.db")

    api = ApiV1Service(
        control_service_factory=lambda _owner: control,
        dataset_upload_service_factory=lambda _owner: dataset_uploads,
        runtime_bundle_upload_service_factory=lambda _owner: runtime_uploads,
        config_asset_store=config_assets,
    )
    test_client = TestClient(create_app(api_service=api))
    sdk = RadarSimClient("http://testserver", client=test_client, user="alice")

    private_job = tmp_path / "cluster-job"
    (private_job / "output").mkdir(parents=True)
    (private_job / "output" / "result.ini").write_text("successfull=1", encoding="utf-8")
    submitted_configs = []
    monkeypatch.setattr(
        "core.cluster.check_cluster_environment",
        lambda _cfg: [SimpleNamespace(name="manager", ok=True)],
    )
    monkeypatch.setattr(
        "core.preflight.run_preflight",
        lambda _cfg: SimpleNamespace(ok=True, checks=[]),
    )
    monkeypatch.setattr(
        "core.cluster.prepare_cluster_job",
        lambda *_args, **_kwargs: SimpleNamespace(
            manifest_path=str(private_job / "manifest.json"),
            config_path=str(private_job / "Config.cfg"),
            profile="default",
        ),
    )

    def submit(config_path, _config, *, dry_run):
        submitted_configs.append((config_path, dry_run))
        return SimpleNamespace(returncode=0, stdout="value=10321", mode="xmlrpc")

    monkeypatch.setattr("core.cluster.submit_cluster_job", submit)
    monkeypatch.setattr(
        "core.cluster.get_cluster_web_status",
        lambda *_args, **_kwargs: {
            "found": True,
            "state": "finished",
            "tasks": [{"simulation_state": "finished"}],
        },
    )
    monkeypatch.setattr(
        "core.cluster.inspect_cluster_job",
        lambda *_args, **_kwargs: {
            "file_count": 1,
            "success_count": 1,
            "fail_count": 0,
            "error_summary": [],
            "result_files": [{"relative_path": "output/result.ini"}],
        },
    )
    cluster_config = {
        "_meta": {"project": "ovrs25"},
        "paths": {},
        "selena": {},
        "build": {},
        "simulation": {},
        "cluster": {
            "timeout_min": 1,
            "workspace_root": "//cluster/work",
            "project_folder": "radar-sim",
        },
    }
    executor = ClusterStageExecutor(
        control,
        ClusterStageContext(
            runtime_catalog=runtime_catalog,
            runtime_store=runtime_store,
            dataset_catalog=dataset_catalog,
            config_assets=config_assets,
            run_store=run_store,
            work_root=tmp_path / "work",
            config_loader=lambda _project: cluster_config,
        ),
        poll_interval=0.02,
    )
    executor.start()
    try:
        submitted = sdk.submit_cluster_yaml(yaml_path)
        deadline = time.time() + 10
        current = submitted
        while time.time() < deadline:
            current = sdk.get_job(submitted.id)
            if current.status in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)

        assert current.status == "succeeded", [
            (stage.type, stage.status, stage.error) for stage in current.stages
        ]
        assert submitted_configs == [(str(private_job / "Config.cfg"), False)]
        manifest = sdk.manifest(submitted.id)
        assert manifest.available is True
        assert manifest.manifest["runtime_bundle_id"].startswith("selena-bundle:sha256:")
    finally:
        executor.stop()
        sdk.close()
