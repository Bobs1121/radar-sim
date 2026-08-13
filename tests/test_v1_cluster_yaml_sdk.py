from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from core.artifact_store import ArtifactStore
from core.api_v1 import ApiV1Service
from core.api_v1_fastapi import create_app
from core.control_service import ControlService
from core.existing_selena import import_existing_selena
from core.runtime_bundle_catalog import RuntimeBundleCatalog
from core.runtime_bundle_upload_service import RuntimeBundleUploadService
from core.transfer_service import TransferService, TransferStore
from core.user_config import UserRunConfig
from radar_sim_sdk import RadarSimClient


def _inputs(tmp_path: Path):
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


def test_server_import_verifies_and_catalogues_existing_selena_archive(tmp_path: Path):
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
    assert catalog.get(bundle["id"]).internal_project.startswith("workspace-")
    assert {item["role"] for item in bundle["files"]} == {
        "entrypoint", "runtime_library", "runtime_config"
    }


def test_linux_run_keeps_existing_selena_as_direct_transfer_metadata(tmp_path: Path):
    _selena, _runtime, _data, _mat_filter, yaml_path = _inputs(tmp_path)
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    config = UserRunConfig.from_yaml(yaml_path).to_dict()

    job = api.submit_user_run("alice", config_payload=config)
    private = control.get_job(job["id"])
    stage = next(item for item in private["stages"] if item["stage_type"] == "prepare_data")

    # Linux persists only the user YAML and source-role metadata.  It must not
    # invoke RuntimeBundleUploadService/import_existing or archive Selena.
    assert "selena" not in job["resolved_spec"]["decisions"]
    assert stage["payload"]["dispatch_scope"] == "direct_transfer"
    assert {item["source_role"] for item in stage["payload"]["source_paths"]} == {
        "dataset", "runtime_bundle", "runtime_xml", "mat_filter"
    }


def test_linux_run_keeps_unc_selena_zero_copy_while_transferring_local_data(tmp_path: Path):
    _selena, _runtime, _data, _mat_filter, yaml_path = _inputs(tmp_path)
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    config = UserRunConfig.from_yaml(yaml_path).to_dict()
    config["selena"]["existing_path"] = r"\\server\share\ovrs25-selena"
    config["selena"]["runtime_xml"] = r"\\server\share\Runtime.xml"
    config["simulation"]["mat_filter"] = r"\\server\share\Mat.filter"

    job = api.submit_user_run("alice", config_payload=config)
    private = control.get_job(job["id"])
    stage = next(item for item in private["stages"] if item["stage_type"] == "prepare_data")

    assert stage["payload"]["dispatch_scope"] == "direct_transfer"
    assert [item["source_role"] for item in stage["payload"]["source_paths"]] == ["dataset"]
    assert "runtime_bundle" not in stage["payload"]["source_roles"]
    assert "runtime_xml" not in stage["payload"]["source_roles"]


def test_sdk_v2_one_call_completes_direct_manifests_without_linux_body_uploads(tmp_path: Path):
    selena, runtime, data, mat_filter, yaml_path = _inputs(tmp_path)
    target = tmp_path / "cluster-target"
    target.mkdir()
    transfer = TransferService(
        TransferStore(tmp_path / "transfers.db"),
        trusted_root=target,
        allow_local_test_root=True,
    )
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(
        control_service_factory=lambda _owner: control,
        transfer_service=transfer,
    )
    test_client = TestClient(create_app(api_service=api, transfer_service=transfer))
    sdk = RadarSimClient("http://testserver", client=test_client, user="alice")

    submitted = sdk.submit_yaml(
        yaml_path,
        idempotency_key="direct-v2",
        allow_local_test=True,
    )
    plans = sdk._request("GET", f"/api/v1/jobs/{submitted.id}/transfers")

    assert plans["status"] == "transfer_completed"
    assert {item["source_role"] for item in plans["plans"]} == {
        "dataset", "runtime_bundle", "runtime_xml", "mat_filter"
    }
    assert all(item["status"] == "completed" for item in plans["plans"])
    assert any(path.is_file() for path in target.rglob("*"))
    assert not (tmp_path / "catalog.db").exists()
    assert data.as_posix() in submitted.spec["data"]["path"]
    assert mat_filter.as_posix() in submitted.spec["simulation"]["mat_filter"]
    assert selena.as_posix() in submitted.spec["selena"]["existing_path"]
    assert runtime.as_posix() in submitted.spec["selena"]["runtime_xml"]
    sdk.close()


def test_direct_transfer_refs_reach_cluster_preflight_without_archive(tmp_path: Path, monkeypatch):
    """Direct manifests are consumed by the Cluster preflight handoff."""
    from core.cluster_stage_executor import execute_cluster_preflight

    probe = tmp_path / "probe"
    (probe / "iso" / "data").mkdir(parents=True)
    (probe / "iso" / "selena").mkdir(parents=True)
    (probe / "iso" / "data" / "one.MF4").write_bytes(b"mf4")
    (probe / "iso" / "selena" / "Selena.exe").write_bytes(b"exe")
    (probe / "iso" / "selena" / "Runtime.xml").write_bytes(b"<runtime/>")
    captured: dict = {}
    resources = {
        "dataset": {
            "transfer_id": "transfer:sha256:" + "a" * 64,
            "source_role": "dataset",
            "status": "resolved",
            "relative_root": "iso",
            "worker_root": "//cluster/share",
            "entries": [{
                "relative_path": "data/one.MF4", "size": 3,
                "sha256": "a" * 64,
                "storage_ref": "cluster-staging://v1/data/one.MF4",
            }],
        },
        "runtime_bundle": {
            "transfer_id": "transfer:sha256:" + "b" * 64,
            "source_role": "runtime_bundle",
            "status": "resolved",
            "relative_root": "iso",
            "worker_root": "//cluster/share",
            "entries": [{
                "relative_path": "selena/Selena.exe", "size": 3,
                "sha256": "b" * 64,
                "storage_ref": "cluster-staging://v1/selena/Selena.exe",
            }],
        },
        "runtime_xml": {
            "transfer_id": "transfer:sha256:" + "f" * 64,
            "source_role": "runtime_xml",
            "status": "resolved",
            "relative_root": "iso",
            "worker_root": "//cluster/share",
            "entries": [{
                "relative_path": "selena/Runtime.xml", "size": 10,
                "sha256": "f" * 64,
                "storage_ref": "cluster-staging://v1/selena/Runtime.xml",
            }],
        },
    }

    def fake_prepare(_config, **kwargs):
        captured["kwargs"] = kwargs
        path = tmp_path / "manifest.json"
        path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(manifest_path=str(path), config_path="//cluster/job/Config.cfg", profile="default")

    monkeypatch.setattr("core.cluster.prepare_cluster_job", fake_prepare)
    monkeypatch.setattr(
        "core.cluster_stage_executor._bundle",
        lambda _context, _job: SimpleNamespace(
            manifest=SimpleNamespace(files=(), id="selena-bundle:sha256:" + "c" * 64),
            internal_project="demo",
            storage_ref="shared://selena-bundles/demo/runtime.zip",
        ),
    )
    monkeypatch.setattr(
        "core.cluster_stage_executor._dataset",
        lambda _context, _job, owner="": SimpleNamespace(
            id="dataset:sha256:" + "d" * 64, source_kind="agent_upload"
        ),
    )
    context = SimpleNamespace(
        server_probe_root=probe,
        storage_ref_resolver=None,
        config_loader=lambda _project: {"cluster": {}, "simulation": {}},
        runtime_store=SimpleNamespace(),
        dataset_catalog=SimpleNamespace(),
        run_store=SimpleNamespace(
            create_run=lambda **_kwargs: SimpleNamespace(
                ref="cluster-run:sha256:" + "e" * 64,
                to_dict=lambda: {"ref": "cluster-run:sha256:" + "e" * 64},
            )
        ),
        work_root=tmp_path / "work",
    )
    job = {
        "job_id": "job-direct",
        "owner": "alice",
        "spec": {
            "selena": {"runtime_xml": "//cluster/share/iso/selena/Runtime.xml"},
            "simulation": {"target": "cluster"},
        },
        "resolved_spec": {"decisions": {"transfers": {"status": "resolved", "resources": resources}}},
    }

    result = execute_cluster_preflight(context, job)
    assert result["preflight"]["ok"] is True
    assert captured["kwargs"]["copy_data"] is False
    assert captured["kwargs"]["copy_selena"] is False
