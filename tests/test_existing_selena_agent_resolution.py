import json

from cli.agent import _resolve_existing_v2_run_config
from core.agent_policy import DEFAULT_FULL_CAPABILITIES, DEFAULT_LIGHT_CAPABILITIES
from core.api_v1 import ApiV1Service
from core.control_service import ControlService
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
    _register(control, agent_id="light-1", mode="light")
    api.submit_user_run("alice", config_payload=config)

    bound = control.bind_pending_run_config_resolution("light-1")
    assert bound is not None
    assert bound["payload"]["selected_target"] == "cluster"


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
