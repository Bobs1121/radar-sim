import hashlib
import io
import json
import shutil
from types import SimpleNamespace

from cli.agent import _ControlClient, _execute_v5_runtime_bundle_cache
from core.agent_data_bindings import AgentDataBindingStore
from core.agent_data_lease import AgentDataLeaseStore
from core.agent_policy import DEFAULT_FULL_CAPABILITIES, DEFAULT_LIGHT_CAPABILITIES
from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseStore
from core.api_v1 import ApiV1Service
from core.artifact_store import ArtifactStore
from core.control_service import ControlService
from core.runtime_bundle import RuntimeSourceEvidence, discover_runtime_bundle
from core.runtime_bundle_archive import stage_runtime_bundle_archive
from core.runtime_bundle_catalog import RuntimeBundleCatalog, RuntimeBundleRecord
from core.runtime_bundle_upload_service import RuntimeBundleUploadService
from core.stage_binder import advance_after_stage_result
from tests.test_api_v1_service import run_config_dict


def _bundle(tmp_path):
    binary = tmp_path / "binary"
    binary.mkdir()
    (binary / "selena.exe").write_bytes(b"exe")
    (binary / "runtime.dll").write_bytes(b"dll")
    runtime = tmp_path / "Runtime.xml"
    runtime.write_text("<runtime/>", encoding="utf-8")
    bundle = discover_runtime_bundle(
        binary / "selena.exe",
        runtime,
        source=RuntimeSourceEvidence(
            "main", "a" * 40, False, "", "Release", "vs", "recipe:demo"
        ),
        created_at=1,
    )
    return bundle, stage_runtime_bundle_archive(bundle, tmp_path / "staging")


def _registered_service(tmp_path, bundle, archive):
    store = ArtifactStore(
        tmp_path / "store",
        object_filename="runtime-bundle.zip",
        storage_ref_prefix="shared://selena-bundles/",
    )
    session = store.create_upload_session(
        "alice", "demo", "bundles/shared", archive.size, archive.checksum
    )
    store.append_chunk(session.session_id, 0, archive.path.read_bytes(), owner="alice")
    published = store.finalize_upload(session.session_id, owner="alice")
    catalog = RuntimeBundleCatalog(tmp_path / "catalog.db")
    record = catalog.register(
        RuntimeBundleRecord(
            manifest=bundle.manifest,
            internal_project="demo",
            storage_ref=published["storage_ref"],
            archive_checksum=archive.checksum,
            archive_size=archive.size,
            owner="alice",
            created_by="builder",
        )
    )
    return RuntimeBundleUploadService(store, catalog, lambda _owner, _ref: None), record


def _local_config(bundle_id, data_path, adapter, mat_filter):
    config = run_config_dict()
    config["selena"] = {
        "source": "existing",
        "existing_path": bundle_id,
        "runtime_xml": "D:/existing/Selena/Runtime.xml",
    }
    config["data"]["path"] = str(data_path)
    config["simulation"].update(
        {
            "target": "local",
            "adapter_file": str(adapter),
            "mat_filter": str(mat_filter),
        }
    )
    return config


def test_shared_bundle_archive_can_be_resolved_by_another_authenticated_user(tmp_path):
    bundle, archive = _bundle(tmp_path)
    service, _ = _registered_service(tmp_path, bundle, archive)
    record, location = service.resolve_archive("bob", bundle.manifest.id)
    assert record.manifest.id == bundle.manifest.id
    assert location.read_bytes() == archive.path.read_bytes()


def test_existing_bundle_local_cache_and_data_are_bound_to_same_full_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("RSIM_HOME", str(tmp_path / "home"))
    bundle, archive = _bundle(tmp_path)
    service, record = _registered_service(tmp_path, bundle, archive)
    data = tmp_path / "data"
    data.mkdir()
    (data / "one.MF4").write_bytes(b"input")
    assets = tmp_path / "assets"
    assets.mkdir()
    adapter = assets / "adapter.txt"
    mat_filter = assets / "signals.filter"
    adapter.write_text("adapter", encoding="utf-8")
    mat_filter.write_text("filter", encoding="utf-8")
    data_binding = AgentDataBindingStore().register(project="demo", root_path=data)

    control = ControlService(tmp_path / "control.db")
    control.register_agent(
        "light",
        agent_id="light-1",
        capabilities=list(DEFAULT_LIGHT_CAPABILITIES),
        metadata={
            "node_kind": "windows_agent",
            "windows_mode": "light",
            "data_bindings": [data_binding.public_dict],
        },
    )
    control.register_agent(
        "full",
        agent_id="full-1",
        capabilities=list(DEFAULT_FULL_CAPABILITIES),
        metadata={
            "node_kind": "windows_full",
            "windows_mode": "full",
            "data_bindings": [data_binding.public_dict],
        },
    )
    api = ApiV1Service(
        control_service_factory=lambda _owner: control,
        runtime_bundle_upload_service_factory=lambda _owner: service,
    )
    job = api.submit_user_run(
        "bob",
        config_payload=_local_config(bundle.manifest.id, data, adapter, mat_filter),
    )
    stages = {item["stage_type"]: item for item in control.get_job(job["id"])["stages"]}
    assert stages["environment_check"]["payload"]["dispatch_scope"] == "runtime_bundle_cache"
    assert stages["register_artifact"]["status"] == "skipped"
    assert control.bind_pending_runtime_bundle_cache("light-1") is None

    task = api.poll_agent("bob", "full-1")["task"]
    assert task["stage_type"] == "environment_check"
    assert task["required_agent_id"] == "full-1"
    assert task["owner"] == "bob"
    assert "storage_ref" in task["payload"]["runtime_bundle"]

    class DownloadClient:
        def download_runtime_bundle(self, bundle_id, *, expected_checksum, expected_size):
            assert bundle_id == record.manifest.id
            assert expected_checksum == record.archive_checksum
            assert expected_size == record.archive_size
            destination = tmp_path / "home" / "agent" / "runtime-downloads" / "bundle.zip"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive.path, destination)
            return destination

    cached = _execute_v5_runtime_bundle_cache(task, client=DownloadClient())
    assert cached["runtime_bundle_lease_ref"].startswith("runtime-bundle-lease:sha256:")
    assert str(tmp_path) not in json.dumps(cached)
    AgentRuntimeBundleLeaseStore().get(cached["runtime_bundle_lease_ref"])

    completed = control.submit_task_result(
        task["stage_id"],
        agent_id="full-1",
        status="succeeded",
        returncode=0,
        result=cached,
    )
    environment = next(
        item for item in completed["stages"] if item["stage_type"] == "environment_check"
    )
    prepared = advance_after_stage_result(control, environment)
    assert prepared["stage_type"] == "prepare_data"
    assert prepared["required_agent_id"] == "full-1"
    assert prepared["payload"]["dispatch_scope"] == "local_data"

    data_lease = AgentDataLeaseStore().create(
        prepared["payload"],
        AgentDataBindingStore(),
        stage_id=prepared["stage_id"],
        attempt=1,
    )
    dataset_id = "dataset:sha256:" + "d" * 64
    after_data = control.submit_task_result(
        prepared["stage_id"],
        agent_id="full-1",
        status="succeeded",
        returncode=0,
        result={
            "dataset": {"id": dataset_id, "source_kind": "agent_local"},
            "data_lease_ref": data_lease.lease_id,
            "evidence_ref": f"{prepared['stage_id']}:1",
        },
    )
    prepare_data = next(
        item for item in after_data["stages"] if item["stage_type"] == "prepare_data"
    )
    preflight = advance_after_stage_result(control, prepare_data)
    assert preflight["stage_type"] == "preflight"
    assert preflight["required_agent_id"] == "full-1"
    assert preflight["payload"]["runtime_bundle_lease_ref"] == cached["runtime_bundle_lease_ref"]
    assert preflight["payload"]["dataset_id"] == dataset_id
    assert str(tmp_path) not in json.dumps(control.get_job(job["id"])["resolved_spec"])


def test_agent_runtime_bundle_download_is_atomic_authenticated_and_verified(tmp_path, monkeypatch):
    content = b"immutable runtime archive"
    checksum = "sha256:" + hashlib.sha256(content).hexdigest()
    bundle_id = "selena-bundle:sha256:" + "a" * 64
    monkeypatch.setenv("RSIM_HOME", str(tmp_path / "home"))
    observed = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["authorization"] = request.headers.get("Authorization")
        observed["timeout"] = timeout
        return Response(content)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = _ControlClient("https://control.example", timeout=17, token="agent-secret")
    downloaded = client.download_runtime_bundle(
        bundle_id,
        expected_checksum=checksum,
        expected_size=len(content),
    )
    assert downloaded.read_bytes() == content
    assert observed["authorization"] == "Bearer agent-secret"
    assert observed["timeout"] == 17
    assert "%3A" in observed["url"]
    assert not list(downloaded.parent.glob("*.part"))
