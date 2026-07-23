import json

from cli.agent import _resolve_existing_v2_run_config, _run_task, _upload_resolution_config_assets
from core.agent_policy import DEFAULT_FULL_CAPABILITIES, DEFAULT_LIGHT_CAPABILITIES
from core.api_v1 import ApiV1Service
from core.control_service import ControlService
from core.stage_binder import bind_existing_runtime_resolution
from tests.test_api_v1_service import run_config_dict


def _existing_config(tmp_path, *, target):
    binary = tmp_path / "ovrs25-existing"
    binary.mkdir()
    (binary / "selena.exe").write_bytes(b"exe")
    (binary / "core.dll").write_bytes(b"core")
    (binary / "plugin.dll").write_bytes(b"plugin")
    runtime = tmp_path / "Runtime_For_byd_ovrs25.xml"
    runtime.write_text("<runtime project='BYD_OVS'/>", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "one.MF4").write_bytes(b"mf4")
    config = run_config_dict()
    config["selena"] = {
        "source": "existing",
        "existing_path": str(binary),
        "runtime_xml": str(runtime),
    }
    config["data"]["path"] = str(data)
    config["simulation"]["target"] = target
    return config, binary, runtime, data


def _register(control, *, agent_id, mode):
    full = mode == "full"
    control.register_agent(
        mode,
        agent_id=agent_id,
        capabilities=list(DEFAULT_FULL_CAPABILITIES if full else DEFAULT_LIGHT_CAPABILITIES),
        metadata={
            "node_kind": "windows_full" if full else "windows_agent",
            "windows_mode": mode,
            "auto_configure": True,
            "workspace_bindings": [],
            "asset_bindings": [],
            "data_bindings": [],
        },
    )


def test_existing_local_resolve_can_only_bind_windows_full(tmp_path):
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    config, _binary, _runtime, _data = _existing_config(tmp_path, target="local")
    _register(control, agent_id="light-1", mode="light")
    _register(control, agent_id="full-1", mode="full")
    api.submit_user_run("alice", config_payload=config)

    assert control.bind_pending_run_config_resolution("light-1") is None
    bound = control.bind_pending_run_config_resolution("full-1")
    assert bound is not None
    assert bound["payload"]["source"] == "existing"
    assert bound["payload"]["existing_path"] == config["selena"]["existing_path"].replace("\\", "/")
    assert "project" not in bound["payload"]
    assert "output_root" not in bound["payload"]


def test_existing_cluster_resolve_can_bind_light_agent(tmp_path):
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    config, _binary, _runtime, _data = _existing_config(tmp_path, target="cluster")
    config["selena"].update(
        {
            "code_path": "C:/BYD_OVS_CB",
            "selena_build_script": (
                "C:/BYD_OVS_CB/apl/byd/bindings/ovrs25/selena/"
                "jenkins_selena_build.bat"
            ),
            "package_build_script": (
                "C:/BYD_OVS_CB/apl/byd/bindings/ovrs25/buildscripts/package.bat"
            ),
        }
    )
    _register(control, agent_id="light-1", mode="light")
    api.submit_user_run("alice", config_payload=config)

    bound = control.bind_pending_run_config_resolution("light-1")
    assert bound is not None
    assert bound["payload"]["selected_target"] == "cluster"
    assert bound["payload"]["mat_filter"] == config["simulation"]["mat_filter"]
    assert bound["payload"]["adapter_file"] == config["simulation"]["adapter_file"]
    assert bound["payload"]["code_path"] == "C:/BYD_OVS_CB"
    assert bound["payload"]["selena_build_script"].endswith(
        "jenkins_selena_build.bat"
    )
    assert bound["payload"]["package_build_script"].endswith("package.bat")


def test_agent_uploads_local_simulation_assets_without_changing_user_config(tmp_path):
    mat_filter = tmp_path / "signals.filter"
    adapter = tmp_path / "adapter.txt"
    mat_filter.write_text("*\n", encoding="utf-8")
    adapter.write_text("adapter\n", encoding="utf-8")

    class Client:
        def __init__(self):
            self.calls = []

        def upload_config_asset(self, source, *, kind, owner=""):
            self.calls.append((source, kind, owner))
            digest = "a" if kind == "adapter" else "b"
            return {"uri": "config-asset://sha256/" + digest * 64}

    client = Client()
    result = _upload_resolution_config_assets(
        {"adapter_file": str(adapter), "mat_filter": str(mat_filter)},
        client=client,
        owner="alice",
    )

    assert result == {
        "adapter_file": "config-asset://sha256/" + "a" * 64,
        "mat_filter": "config-asset://sha256/" + "b" * 64,
    }
    assert [(kind, owner) for _path, kind, owner in client.calls] == [
        ("adapter", "alice"),
        ("mat_filter", "alice"),
    ]


def test_agent_existing_resolver_creates_path_free_complete_bundle_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("RSIM_HOME", str(tmp_path / "home"))
    config, binary, runtime, data = _existing_config(tmp_path, target="cluster")
    result = _resolve_existing_v2_run_config(
        {
            "task_id": "resolve-existing-1",
            "stage_id": "resolve-existing-1",
            "attempt_count": 1,
            "payload": {
                "source": "existing",
                "existing_path": str(binary),
                "runtime_xml": str(runtime),
                "data_path": str(data),
                "selected_target": "cluster",
                "auto_configure": True,
            },
        }
    )

    assert result["status"] == "resolved"
    assert result["source"] == "existing"
    assert result["internal_project"] == "ovrs25"
    assert result["runtime_bundle_lease_ref"].startswith("runtime-bundle-lease:sha256:")
    assert result["build_evidence_ref"] == "resolve-existing-1:1"
    roles = [item["role"] for item in result["runtime_bundle"]["files"]]
    assert roles.count("entrypoint") == 1
    assert roles.count("runtime_library") == 2
    assert roles.count("runtime_config") == 1
    assert result["data_binding_id"].startswith("data-root:sha256:")
    serialized = json.dumps(result)
    assert str(tmp_path) not in serialized
    assert str(binary) not in serialized
    assert "output_root" not in serialized


def test_agent_existing_resolution_imports_bundle_for_task_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("RSIM_HOME", str(tmp_path / "home"))
    config, binary, runtime, data = _existing_config(tmp_path, target="cluster")

    class Client:
        def __init__(self):
            self.results = []
            self.imports = []

        def heartbeat(self, *_args, **_kwargs):
            return {"cancel_requested": False}

        def append_logs(self, *_args, **_kwargs):
            return {}

        def import_existing_runtime_bundle(self, recognition, *, owner=""):
            self.imports.append((owner, recognition["runtime_bundle_lease_ref"]))
            return {
                "runtime_bundle": {
                    **recognition["runtime_bundle"],
                    "storage_ref": "shared://selena-bundles/ovrs25/imported",
                    "archive_checksum": recognition["archive"]["checksum"],
                    "archive_size": recognition["archive"]["size"],
                }
            }

        def submit_result(self, _task_id, **kwargs):
            self.results.append(kwargs)
            return {}

    client = Client()
    task = {
        "task_id": "resolve-existing-2",
        "stage_id": "resolve-existing-2",
        "job_id": "job-existing-2",
        "task_type": "resolve_spec",
        "stage_type": "resolve_spec",
        "attempt_count": 1,
        "owner": "alice",
        "payload": {
            "source": "existing",
            "existing_path": str(binary),
            "runtime_xml": str(runtime),
            "data_path": str(data),
            "selected_target": "cluster",
            "auto_configure": True,
        },
    }

    assert _run_task(
        client, "light-1", task, heartbeat_interval=1, node_kind="windows_agent"
    ) == 0
    recognition = client.results[0]["result"]["recognition"]
    assert client.imports[0][0] == "alice"
    assert recognition["registered_runtime_bundle"]["storage_ref"].startswith(
        "shared://selena-bundles/"
    )


def _complete_existing_resolution(control, api, config, *, agent_id, mode):
    _register(control, agent_id=agent_id, mode=mode)
    job = api.submit_user_run("alice", config_payload=config)
    task = api.poll_agent("alice", agent_id)["task"]
    bundle_id = "selena-bundle:sha256:" + "a" * 64
    completed = control.submit_task_result(
        task["stage_id"],
        agent_id=agent_id,
        status="succeeded",
        returncode=0,
        result={
            "recognition": {
                "status": "resolved",
                "source": "existing",
                "internal_project": "ovrs25",
                "adapter_key": "project:ovrs25",
                "runtime_bundle_lease_ref": "runtime-bundle-lease:sha256:" + "b" * 64,
                "registered_runtime_bundle": {
                    "id": bundle_id,
                    "storage_ref": "shared://selena-bundles/ovrs25/opaque",
                    "archive_checksum": "sha256:" + "c" * 64,
                    "archive_size": 123,
                },
                "data_binding_id": "data-root:sha256:" + "d" * 24,
                "confidence": 1.0,
                "evidence": ["existing_folder_validated"],
                "config_assets": {
                    "mat_filter": "config-asset://sha256/" + "e" * 64,
                },
            }
        },
    )
    stage = next(item for item in completed["stages"] if item["stage_type"] == "resolve_spec")
    bound = bind_existing_runtime_resolution(control, job["id"], stage["stage_id"])
    return control.get_job(job["id"]), bound


def test_existing_folder_cluster_handoff_registers_bundle_and_uploads_local_data(tmp_path):
    control = ControlService(tmp_path / "control.db")
    control.register_agent(
        "linux",
        agent_id="linux-v2-stage-executor",
        capabilities=["environment.cluster.check", "data.resolve", "preflight", "result.collect", "manifest.finalize"],
        metadata={"node_kind": "linux_executor"},
    )
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    config, *_ = _existing_config(tmp_path, target="cluster")
    config["data"]["path"] = "D:/agent-local/data"

    job, bound = _complete_existing_resolution(
        control, api, config, agent_id="light-1", mode="light"
    )
    stages = {item["stage_type"]: item for item in job["stages"]}

    assert bound["stage_type"] == "environment_check"
    assert bound["assigned_agent_id"] == "linux-v2-stage-executor"
    assert stages["register_artifact"]["assigned_agent_id"] == "light-1"
    assert stages["register_artifact"]["payload"]["already_registered"] is True
    assert stages["prepare_data"]["assigned_agent_id"] == "light-1"
    assert stages["prepare_data"]["payload"]["dispatch_scope"] == "data_upload"
    assert job["resolved_spec"]["decisions"]["selena"]["status"] == "resolved"
    assert job["resolved_spec"]["decisions"]["simulation_assets"]["mat_filter"].startswith(
        "config-asset://sha256/"
    )


def test_existing_folder_local_handoff_reuses_bundle_on_full_agent(tmp_path):
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    config, *_ = _existing_config(tmp_path, target="local")

    job, bound = _complete_existing_resolution(
        control, api, config, agent_id="full-1", mode="full"
    )
    stages = {item["stage_type"]: item for item in job["stages"]}

    assert bound["stage_type"] == "environment_check"
    assert bound["assigned_agent_id"] == "full-1"
    assert bound["payload"]["dispatch_scope"] == "existing_runtime"
    assert bound["payload"]["data_binding_id"].startswith("data-root:sha256:")
    assert stages["register_artifact"]["status"] == "skipped"
