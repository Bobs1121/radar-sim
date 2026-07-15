import hashlib

from core.artifact_store import ArtifactStore
from core.runtime_bundle import RuntimeSourceEvidence, discover_runtime_bundle
from core.runtime_bundle_archive import stage_runtime_bundle_archive
from core.runtime_bundle_catalog import RuntimeBundleCatalog
from core.runtime_bundle_upload_service import RuntimeBundleUploadService, TrustedRuntimeBundleEvidence
from core.runtime_bundle_upload_service import trusted_runtime_bundle_evidence_from_control
from core.control_service import ControlService
from core.api_v1 import ApiV1Service
from core.api_v1_fastapi import create_app
from fastapi.testclient import TestClient
from radar_sim_sdk import RadarSimClient
from tests.test_api_v1_service import run_config_dict


def test_runtime_bundle_archive_resumable_upload_and_shared_registration(tmp_path):
    output = tmp_path / "build"
    output.mkdir()
    (output / "selena.exe").write_bytes(b"exe")
    (output / "a.dll").write_bytes(b"dll")
    runtime = tmp_path / "Runtime.xml"
    runtime.write_text("<runtime/>", encoding="utf-8")
    bundle = discover_runtime_bundle(
        output / "selena.exe", runtime,
        source=RuntimeSourceEvidence("main", "a" * 40, False, "", "Release", "tool"),
        created_at=1,
    )
    archive = stage_runtime_bundle_archive(bundle, tmp_path / "staging")
    evidence = TrustedRuntimeBundleEvidence(
        "stage-1:1", "alice", "demo", "agent-1", bundle.manifest,
        archive.checksum, archive.size, "runtime-bundle-lease:sha256:" + "a" * 64,
    )
    store = ArtifactStore(
        tmp_path / "store", object_filename="runtime-bundle.zip",
        storage_ref_prefix="shared://selena-bundles/", chunk_size=7,
    )
    service = RuntimeBundleUploadService(
        store, RuntimeBundleCatalog(tmp_path / "catalog.db"),
        lambda owner, ref: evidence,
    )
    session = service.create("alice", evidence_ref="stage-1:1")
    content = archive.path.read_bytes()
    offset = 0
    while offset < len(content):
        chunk = content[offset:offset + 7]
        service.append("alice", session["session_id"], offset=offset, data=chunk)
        offset += len(chunk)
    result = service.finalize("alice", session["session_id"])
    assert result["runtime_bundle"]["id"] == bundle.manifest.id
    assert result["runtime_bundle"]["storage_ref"].startswith("shared://selena-bundles/demo/")
    assert result["runtime_bundle"]["visibility"] == "shared"
    location = store.resolve_location(result["runtime_bundle"]["storage_ref"])
    assert hashlib.sha256(location.read_bytes()).hexdigest() == archive.checksum.removeprefix("sha256:")


def test_runtime_bundle_http_sdk_and_trusted_control_evidence(tmp_path):
    output = tmp_path / "build"
    output.mkdir()
    (output / "selena.exe").write_bytes(b"exe")
    runtime = tmp_path / "Runtime.xml"
    runtime.write_text("<runtime/>", encoding="utf-8")
    bundle = discover_runtime_bundle(
        output / "selena.exe", runtime,
        source=RuntimeSourceEvidence("main", "a" * 40, False, "", "Release", "tool", "recipe:demo"),
        created_at=1,
    )
    archive = stage_runtime_bundle_archive(bundle, tmp_path / "staging")
    control = ControlService(tmp_path / "control.db")
    job = control.create_job(
        "simulation.run_config.v2", owner="alice",
        spec={"schema_version": "2.0", "selena": {"source": "build", "build_mode": "Release"}},
        tasks=[{"task_type": "build_selena", "stage_type": "build_selena"}],
    )
    control.register_agent(
        "agent", agent_id="agent-1", node_kind="windows_agent", capabilities=["build.selena"]
    )
    claimed = control.claim_next_task("agent-1")
    control.submit_task_result(
        claimed["stage_id"], agent_id="agent-1", status="succeeded", returncode=0,
        result={
            "project": "demo", "source_changed_during_build": False,
            "runtime_bundle": bundle.public_dict,
            "runtime_bundle_identity": {"adapter_key": "recipe:demo"},
            "runtime_bundle_archive": archive.public_dict,
            "runtime_bundle_lease_ref": "runtime-bundle-lease:sha256:" + "c" * 64,
        },
    )
    evidence_ref = f"{claimed['stage_id']}:1"
    trusted = trusted_runtime_bundle_evidence_from_control(control, "alice", evidence_ref)
    assert trusted.manifest.source.adapter_key == "recipe:demo"
    store = ArtifactStore(
        tmp_path / "store", object_filename="runtime-bundle.zip",
        storage_ref_prefix="shared://selena-bundles/", chunk_size=9,
    )
    service = RuntimeBundleUploadService(
        store, RuntimeBundleCatalog(tmp_path / "catalog.db"),
        lambda owner, ref: trusted_runtime_bundle_evidence_from_control(control, owner, ref),
    )
    api = ApiV1Service(runtime_bundle_upload_service_factory=lambda _owner: service)
    sdk = RadarSimClient("http://testserver", client=TestClient(create_app(api_service=api)), user="alice")
    uploaded = sdk.upload_runtime_bundle(evidence_ref, archive.path)
    assert uploaded.runtime_bundle["id"] == bundle.manifest.id
    assert uploaded.runtime_bundle["source"].get("adapter_key") is None
    assert sdk.list_runtime_bundles()[0]["id"] == bundle.manifest.id
    assert sdk.get_runtime_bundle(bundle.manifest.id)["storage_ref"].startswith("shared://selena-bundles/")
    config = run_config_dict()
    config["selena"] = {
        "source": "existing",
        "existing_path": bundle.manifest.id,
        "runtime_xml": "D:/existing/Selena/Runtime.xml",
    }
    run_api = ApiV1Service(
        control_service_factory=lambda _owner: control,
        runtime_bundle_upload_service_factory=lambda _owner: service,
    )
    selected = run_api.submit_user_run("alice", config_payload=config)
    assert selected["resolved_spec"]["decisions"]["selena"]["runtime_bundle"]["id"] == bundle.manifest.id
    stages = {stage["stage_type"]: stage for stage in selected["stages"]}
    assert stages["resolve_spec"]["status"] == "skipped"
    assert stages["build_selena"]["status"] == "skipped"
    assert stages["register_artifact"]["status"] == "skipped"
