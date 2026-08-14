import inspect
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cli import server as server_cli
from core.spec import SimulationSpec
from core.api_v1 import ApiV1Error, ApiV1Service
from core.api_v1_fastapi import create_app
from core.control_service import ControlService
from core.agent_policy import WINDOWS_CONNECTOR_CONTRACT_VERSION
from core.config_assets import ConfigAssetStore
from core.http_auth import HttpTokenAuthenticator
from core.local_results import ResultCatalog
from tests.test_api_v1_service import run_config_dict, spec_dict


def make_client(tmp_path):
    services: dict[str, ControlService] = {}

    def factory(owner: str) -> ControlService:
        services.setdefault(owner, ControlService(tmp_path / f"{owner}.db"))
        return services[owner]

    return TestClient(create_app(control_service_factory=factory)), services


ALICE_TOKEN = "alice-token-0123456789_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
BOB_TOKEN = "bob-token-0123456789_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
AGENT_TOKEN = "agent-token-0123456789_ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def make_authenticator():
    return HttpTokenAuthenticator.from_mapping({
        "version": 1,
        "users": {"alice": ALICE_TOKEN, "bob": BOB_TOKEN},
        "agents": {"agent-1": {"owner": "alice", "token": AGENT_TOKEN}},
    })


def test_bearer_auth_derives_owner_and_ignores_spoofed_user_header(tmp_path):
    services: dict[str, ControlService] = {}

    def factory(owner: str) -> ControlService:
        services.setdefault(owner, ControlService(tmp_path / f"{owner}.db"))
        return services[owner]

    client = TestClient(create_app(
        control_service_factory=factory,
        authenticator=make_authenticator(),
    ))
    assert client.get("/api/v1/jobs").status_code == 401
    created = client.post(
        "/api/v1/run-jobs",
        headers={"Authorization": f"Bearer {ALICE_TOKEN}", "X-Rsim-User": "bob"},
        json={"config": run_config_dict(), "dry_run": True},
    )
    assert created.status_code == 201
    assert len(services["alice"].list_jobs()) == 1
    assert "bob" not in services
    assert client.get(
        "/api/v1/jobs",
        headers={"Authorization": f"Bearer {BOB_TOKEN}", "X-Rsim-User": "alice"},
    ).json()["jobs"] == []


def test_agent_bearer_auth_derives_identity_and_rejects_body_spoof(tmp_path):
    control = ControlService(tmp_path / "control.db")
    client = TestClient(create_app(
        api_service=ApiV1Service(control_service_factory=lambda _owner: control),
        authenticator=make_authenticator(),
    ))
    payload = {
        "name": "light", "agent_id": "agent-1", "hostname": "win",
        "platform": "win32", "capabilities": ["local.check"],
        "metadata": {"node_kind": "legacy"},
    }
    assert client.post(
        "/api/agents/register", json=payload,
        headers={"Authorization": f"Bearer {ALICE_TOKEN}"},
    ).status_code == 401
    spoofed = dict(payload, agent_id="agent-2")
    assert client.post(
        "/api/agents/register", json=spoofed,
        headers={"Authorization": f"Bearer {AGENT_TOKEN}", "X-Rsim-User": "bob"},
    ).status_code == 403
    registered = client.post(
        "/api/agents/register", json=payload,
        headers={"Authorization": f"Bearer {AGENT_TOKEN}", "X-Rsim-User": "bob"},
    )
    assert registered.status_code == 201
    job = control.create_job(
        "local.check", owner="alice",
        tasks=[{"task_type": "local.check", "assigned_agent_id": "agent-1"}],
    )
    claimed = client.post(
        "/api/agents/poll", json={"agent_id": "agent-1"},
        headers={"Authorization": f"Bearer {AGENT_TOKEN}", "X-Rsim-User": "bob"},
    )
    assert claimed.status_code == 200
    assert claimed.json()["task"]["job_id"] == job["job_id"]
    task_id = claimed.json()["task"]["task_id"]
    assert client.post(
        "/api/tasks/logs",
        json={"task_id": task_id, "agent_id": "agent-1", "lines": ["ok"]},
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
    ).status_code == 200
    progress = client.post(
        "/api/tasks/progress",
        json={"task_id": task_id, "agent_id": "agent-1", "progress": 0.25, "message": "Compiling"},
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
    )
    assert progress.status_code == 200
    assert control.get_task(task_id)["progress"] == 0.25
    assert client.post(
        "/api/tasks/progress",
        json={"task_id": task_id, "agent_id": "agent-2", "progress": 0.5},
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
    ).status_code == 403
    assert client.post(
        "/api/tasks/logs",
        json={"task_id": task_id, "agent_id": "agent-2", "lines": ["spoof"]},
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
    ).status_code == 403
    assert client.post(
        "/api/agents/poll", json={"agent_id": "agent-2"},
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
    ).status_code == 403


def test_agent_token_downloads_only_owners_config_asset(tmp_path):
    store = ConfigAssetStore(tmp_path / "assets", tmp_path / "assets.db")
    record = store.put(owner="alice", kind="adapter", filename="adapter.txt", content=b"adapter=1\n")
    api = ApiV1Service(
        control_service_factory=lambda _owner: ControlService(tmp_path / "control.db"),
        config_asset_store=store,
    )
    client = TestClient(create_app(api_service=api, authenticator=make_authenticator()))
    response = client.get(
        f"/api/agents/config-assets/{record.id}/download",
        params={"kind": "adapter"},
        headers={"Authorization": f"Bearer {AGENT_TOKEN}", "X-Rsim-User": "bob"},
    )
    assert response.status_code == 200
    assert response.content == b"adapter=1\n"
    assert response.headers["X-Content-SHA256"] == record.checksum


def test_authenticated_agent_can_download_shared_runtime_bundle(tmp_path):
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(b"bundle")
    bundle_id = "selena-bundle:sha256:" + "a" * 64

    class BundleApi:
        def get_runtime_bundle(self, owner, requested):
            assert owner == "alice"
            assert requested == bundle_id
            return {
                "id": bundle_id,
                "archive_checksum": "sha256:" + "b" * 64,
                "archive_size": archive.stat().st_size,
            }

        def runtime_bundle_archive(self, owner, requested):
            assert owner == "alice"
            assert requested == bundle_id
            return archive

    client = TestClient(create_app(api_service=BundleApi(), authenticator=make_authenticator()))
    response = client.get(
        f"/api/v1/runtime-bundles/{bundle_id}/download",
        headers={"Authorization": f"Bearer {AGENT_TOKEN}", "X-Rsim-User": "bob"},
    )
    assert response.status_code == 200
    assert response.content == b"bundle"
    assert response.headers["X-Content-SHA256"] == "sha256:" + "b" * 64
    assert client.get(f"/api/v1/runtime-bundles/{bundle_id}/download").status_code == 401


def test_health_v2_schema_validate_submit_get_cancel_manifest(tmp_path):
    client, _ = make_client(tmp_path)

    assert client.get("/api/v1/health").json()["api_version"] == "v1"
    schema = client.get("/api/v1/schema/run-config").json()
    assert "project" not in schema["properties"]

    validation = client.post("/api/v1/run-configs/validate", json=run_config_dict()).json()
    job = client.post(
        "/api/v1/run-jobs", json={"config": run_config_dict(), "dry_run": True}
    ).json()
    assert job["spec_hash"] == validation["fingerprint"]
    assert job["type"] == "simulation.run_config.v2.dry_run"

    fetched = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert fetched["id"] == job["id"]
    assert client.get(f"/api/v1/jobs/{job['id']}/manifest").json()["available"] is False
    diagnosis = client.get(f"/api/v1/jobs/{job['id']}/diagnosis").json()
    assert diagnosis["job_id"] == job["id"]
    assert diagnosis["outcome"] == "succeeded"

    cancelled = client.post(f"/api/v1/jobs/{job['id']}/cancel").json()
    assert cancelled["status"] == "succeeded"


def test_serve_v1_exposes_agent_control_endpoints_on_same_database(tmp_path):
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    client = TestClient(create_app(api_service=api))
    headers = {"X-Rsim-User": "alice"}
    registered = client.post(
        "/api/agents/register",
        headers=headers,
        json={
            "name": "light", "agent_id": "agent-1", "hostname": "win",
            "platform": "win32", "capabilities": ["local.check"],
            "metadata": {"node_kind": "legacy"},
        },
    )
    assert registered.status_code == 201
    job = control.create_job(
        "local.check", owner="alice",
        tasks=[{"task_type": "local.check", "assigned_agent_id": "agent-1"}],
    )
    claimed = client.post("/api/agents/poll", headers=headers, json={"agent_id": "agent-1"})
    assert claimed.status_code == 200
    assert claimed.json()["task"]["job_id"] == job["job_id"]
    completed = client.post(
        "/api/tasks/result", headers=headers,
        json={
            "task_id": claimed.json()["task"]["task_id"], "agent_id": "agent-1",
            "status": "succeeded", "returncode": 0, "result": {"ok": True},
        },
    )
    assert completed.status_code == 200
    assert control.get_job(job["job_id"])["status"] == "succeeded"

    # Direct-transfer completion may persist the Stage from its final
    # manifest before the Agent's ordinary result callback arrives.  The
    # callback is idempotent for the assigned Agent and must not surface a
    # misleading HTTP 500 or make the connector restart.
    duplicate = client.post(
        "/api/tasks/result", headers=headers,
        json={
            "task_id": claimed.json()["task"]["task_id"], "agent_id": "agent-1",
            "status": "succeeded", "returncode": 0, "result": {"ok": True},
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["job_id"] == job["job_id"]


def test_one_click_windows_connector_is_bound_to_current_linux_service(tmp_path):
    bundle = tmp_path / "rsim-windows-connector.zip"
    bundle.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    client = TestClient(create_app(windows_connector_bundle=bundle))

    installer = client.get("/api/v1/windows-connector/install.ps1?mode=unified")
    assert installer.status_code == 200
    assert "attachment" in installer.headers["content-disposition"]
    assert "__RSIM_SERVER_URL_BASE64__" not in installer.text
    assert "__RSIM_WINDOWS_MODE__" not in installer.text
    assert "aHR0cDovL3Rlc3RzZXJ2ZXI=" in installer.text
    assert '$Mode = "unified"' in installer.text
    assert "/api/v1/windows-connector/package.zip" in installer.text
    assert "AgentToken" not in installer.text
    assert "ApiToken" not in installer.text

    launcher = client.get("/api/v1/windows-connector/connect.cmd?mode=unified")
    assert launcher.status_code == 200
    assert "RadarSim-%E8%BF%9E%E6%8E%A5%E6%9C%AC%E6%9C%BA.cmd" in launcher.headers["content-disposition"]
    assert "__RSIM_SERVER_URL_BASE64__" not in launcher.text
    assert "__RSIM_WINDOWS_MODE__" not in launcher.text
    assert "aHR0cDovL3Rlc3RzZXJ2ZXI=" in launcher.text
    assert "install.ps1?mode=" in launcher.text
    assert "AgentToken" not in launcher.text
    assert "ApiToken" not in launcher.text
    assert client.get("/api/v1/windows-connector/connect.cmd?mode=light").status_code == 422
    assert client.get("/api/v1/windows-connector/install.ps1?mode=full").status_code == 422

    package = client.get("/api/v1/windows-connector/package.zip")
    assert package.status_code == 200
    assert package.content == bundle.read_bytes()
    assert package.headers["X-Content-SHA256"].startswith("sha256:")
    assert len(package.headers["X-Content-SHA256"]) == 71
    assert package.headers["X-Rsim-Connector-Version"] == str(
        WINDOWS_CONNECTOR_CONTRACT_VERSION
    )


def test_one_click_windows_connector_never_embeds_long_lived_auth_tokens(tmp_path):
    client = TestClient(create_app(authenticator=make_authenticator()))
    installer = client.get("/api/v1/windows-connector/install.ps1?mode=unified")
    assert installer.status_code == 409
    assert installer.json()["code"] == "connector_pairing_required"
    assert client.get("/api/v1/windows-connector/connect.cmd?mode=unified").status_code == 409


def test_project_free_run_config_routes_share_one_contract(tmp_path):
    client, _ = make_client(tmp_path)
    schema = client.get("/api/v1/schema/run-config").json()
    assert "project" not in schema["properties"]
    config = run_config_dict()
    validated = client.post("/api/v1/run-configs/validate", json=config)
    assert validated.status_code == 200
    assert len(validated.json()["execution_plan"]) == 10
    assert validated.json()["execution"]["selected_target"] in {"local", "cluster"}
    assert validated.json()["config"]["result"] == {"path": ""}
    created = client.post(
        "/api/v1/run-jobs",
        json={"config": config},
        headers={"Idempotency-Key": "run-config-1", "X-Rsim-User": "alice"},
    )
    assert created.status_code == 201
    assert created.json()["spec_hash"] == validated.json()["fingerprint"]
    assert "project" not in created.json()["spec"]
    assert created.json()["waiting"]["reason"] == "windows_connection_required"
    assert created.json()["waiting"]["mode"] == "unified"
    assert created.json()["waiting"]["action"]["type"] == "connect_windows"
    assert "D:/" not in str(created.json()["waiting"])

    exported = client.post("/api/v1/run-configs/export", json={"config": config})
    imported = client.post(
        "/api/v1/run-configs/import",
        json={"yaml_content": exported.json()["yaml_content"]},
    )
    assert imported.status_code == 200
    assert imported.json()["config"] == validated.json()["config"]


def test_adapter_and_matfilter_uploads_return_reusable_private_refs(tmp_path):
    api = ApiV1Service(
        control_service_factory=lambda _owner: ControlService(tmp_path / "control.db"),
        config_asset_store=ConfigAssetStore(tmp_path / "assets", tmp_path / "assets.db"),
    )
    client = TestClient(create_app(api_service=api))
    headers = {
        "X-Rsim-User": "alice",
        "X-Asset-Kind": "adapter",
        "X-Asset-Filename": "adapter.txt",
    }
    created = client.post("/api/v1/config-assets", headers=headers, content=b"adapter=1\n")
    assert created.status_code == 201
    asset = created.json()
    assert asset["uri"].startswith("config-asset://sha256/")
    assert "path" not in str(asset).lower()
    assert client.get(
        f"/api/v1/config-assets/{asset['id']}",
        params={"kind": "adapter"},
        headers={"X-Rsim-User": "alice"},
    ).status_code == 200
    assert client.get(
        f"/api/v1/config-assets/{asset['id']}",
        params={"kind": "adapter"},
        headers={"X-Rsim-User": "bob"},
    ).status_code == 404


def test_capability_route_is_path_free_and_owner_scoped(tmp_path):
    client, services = make_client(tmp_path)
    services.setdefault("alice", ControlService(tmp_path / "alice.db")).register_agent(
        "full-a",
        agent_id="full-a",
        capabilities=["simulation.local"],
        metadata={
            "node_kind": "windows_full",
            "workspace": "D:/private/workspace",
            "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
        },
    )
    body = client.get("/api/v1/capabilities", headers={"X-Rsim-User": "alice"}).json()
    assert body["capabilities"]["windows"]["available"] is True
    assert "windows_full" not in body["capabilities"]
    assert "windows_light" not in body["capabilities"]
    assert "full-a" not in str(body)
    assert "private" not in str(body).lower()


def test_connector_install_status_is_exact_device_and_owner_scoped(tmp_path):
    control = ControlService(tmp_path / "shared.db")
    client = TestClient(create_app(api_service=ApiV1Service(control_service_factory=lambda _owner: control)))
    body = {
        "name": "pc-a",
        "agent_id": "agent-alice-pc-a",
        "hostname": "pc-a",
        "platform": "Windows",
        "capabilities": ["local.check"],
        "metadata": {
            "node_kind": "windows_full",
            "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
        },
    }
    assert client.post(
        "/api/agents/register", json=body, headers={"X-Rsim-User": "user-alice"}
    ).status_code == 201

    exact = client.get(
        "/api/v1/windows-connector/status",
        params={"agent_id": body["agent_id"]},
        headers={"X-Rsim-User": "user-alice"},
    ).json()
    assert exact == {
        "agent_id": "agent-alice-pc-a",
        "configured": True,
        "available": True,
        "contract_current": True,
        "reason": "",
    }
    missing = client.get(
        "/api/v1/windows-connector/status",
        params={"agent_id": "agent-alice-other-pc"},
        headers={"X-Rsim-User": "user-alice"},
    ).json()
    assert missing["reason"] == "not_registered"
    other_owner = client.get(
        "/api/v1/windows-connector/status",
        params={"agent_id": body["agent_id"]},
        headers={"X-Rsim-User": "user-bob"},
    ).json()
    assert other_owner["reason"] == "connector_owner_mismatch"


def test_connector_agent_id_cannot_be_silently_rebound_to_another_owner(tmp_path):
    control = ControlService(tmp_path / "shared.db")
    client = TestClient(create_app(api_service=ApiV1Service(control_service_factory=lambda _owner: control)))
    body = {
        "name": "pc-a",
        "agent_id": "agent-shared-id",
        "hostname": "pc-a",
        "platform": "Windows",
        "capabilities": ["local.check"],
        "metadata": {
            "node_kind": "windows_full",
            "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
        },
    }
    assert client.post(
        "/api/agents/register", json=body, headers={"X-Rsim-User": "user-alice"}
    ).status_code == 201
    conflict = client.post(
        "/api/agents/register", json=body, headers={"X-Rsim-User": "user-bob"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "connector_owner_mismatch"


def test_connector_poll_heartbeat_and_task_callbacks_are_owner_scoped(tmp_path):
    control = ControlService(tmp_path / "shared.db")
    client = TestClient(
        create_app(api_service=ApiV1Service(control_service_factory=lambda _owner: control))
    )
    register = {
        "name": "alice-pc",
        "agent_id": "agent-alice-pc",
        "hostname": "alice-pc",
        "platform": "Windows",
        "capabilities": ["local.check"],
        "metadata": {
            "node_kind": "windows_full",
            "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
        },
    }
    assert client.post(
        "/api/agents/register",
        json=register,
        headers={"X-Rsim-User": "user-alice"},
    ).status_code == 201

    for path, body in (
        ("/api/agents/poll", {"agent_id": "agent-alice-pc"}),
        (
            "/api/agents/heartbeat",
            {"agent_id": "agent-alice-pc", "status": "idle", "current_task_id": "", "metadata": {}},
        ),
    ):
        response = client.post(path, json=body, headers={"X-Rsim-User": "user-bob"})
        assert response.status_code == 409
        assert response.json()["code"] == "connector_owner_mismatch"

    job = control.create_job(
        "simulation.run_config.v2",
        owner="user-alice",
        tasks=[
            {
                "task_type": "local.check",
                "stage_type": "environment_check",
                "assigned_agent_id": "agent-alice-pc",
            }
        ],
    )
    task_id = job["tasks"][0]["task_id"]
    hidden = client.post(
        "/api/tasks/logs",
        json={"task_id": task_id, "lines": ["should-not-append"], "stream": "stdout"},
        headers={"X-Rsim-User": "user-bob"},
    )
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "task_not_found"
    assert control.get_logs(job_id=job["job_id"])["entries"] == []


def test_v8_double_namespaced_legacy_owner_can_repair_in_place(tmp_path):
    control = ControlService(tmp_path / "shared.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    api.register_agent(
        "user-web-0123456789abcdef01234567",
        name="legacy-pc",
        agent_id="agent-legacy-pc",
        hostname="legacy-pc",
        platform="Windows",
        capabilities=["local.check"],
        metadata={
            "node_kind": "windows_full",
            "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION - 1,
        },
    )
    repaired = api.register_agent(
        "web-0123456789abcdef01234567",
        name="legacy-pc",
        agent_id="agent-legacy-pc",
        hostname="legacy-pc",
        platform="Windows",
        capabilities=["local.check"],
        metadata={
            "node_kind": "windows_full",
            "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
        },
    )
    assert repaired["metadata"]["user"] == "web-0123456789abcdef01234567"


def test_pre_v9_generated_owner_can_migrate_once_to_stable_human_owner(tmp_path):
    control = ControlService(tmp_path / "shared.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    api.register_agent(
        "user-web-0123456789abcdef01234567",
        name="legacy-pc",
        agent_id="agent-legacy-stable-migration",
        hostname="legacy-pc",
        platform="Windows",
        capabilities=["local.check"],
        metadata={
            "node_kind": "windows_full",
            "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION - 1,
        },
    )

    migrated = api.register_agent(
        "user-hjn3wx",
        name="legacy-pc",
        agent_id="agent-legacy-stable-migration",
        hostname="legacy-pc",
        platform="Windows",
        capabilities=["local.check"],
        metadata={
            "node_kind": "windows_full",
            "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
        },
    )
    assert migrated["metadata"]["user"] == "user-hjn3wx"

    with pytest.raises(ApiV1Error) as conflict:
        api.register_agent(
            "user-bob",
            name="legacy-pc",
            agent_id="agent-legacy-stable-migration",
            hostname="legacy-pc",
            platform="Windows",
            capabilities=["local.check"],
            metadata={
                "node_kind": "windows_full",
                "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
            },
        )
    assert conflict.value.code == "connector_owner_mismatch"


def test_v2_removes_project_catalog_and_legacy_spec_routes(tmp_path):
    services: dict[str, ControlService] = {}

    def factory(owner: str) -> ControlService:
        services.setdefault(owner, ControlService(tmp_path / f"{owner}.db"))
        return services[owner]

    api = ApiV1Service(control_service_factory=factory)
    client = TestClient(create_app(api_service=api))
    for method, path in (
        ("get", "/api/v1/projects"),
        ("get", "/api/v1/schema/simulation-spec"),
        ("post", "/api/v1/specs/import"),
        ("post", "/api/v1/specs/export"),
        ("post", "/api/v1/validate"),
    ):
        assert getattr(client, method)(path).status_code == 404

    exported = client.post(
        "/api/v1/run-configs/export", json={"config": run_config_dict()}
    )
    assert exported.status_code == 200
    assert "schema_version: '2.0'" in exported.json()["yaml_content"]


def test_v2_openapi_hides_connector_and_compatibility_routes():
    paths = set(create_app().openapi()["paths"])

    assert "/api/v1/run-jobs" in paths
    assert "/api/v1/schema/run-config" in paths
    assert not any(path.startswith("/api/agents") for path in paths)
    assert not any("upload" in path for path in paths)
    assert not any("runtime-bundle" in path for path in paths)
    assert not any("existing-selena" in path for path in paths)


def test_v1_web_console_is_same_origin_and_legacy_routes_are_not_shadowed(tmp_path):
    client, _ = make_client(tmp_path)
    index = client.get("/")
    assert index.status_code == 200
    assert "Radar Sim 控制台" in index.text
    assert "期望 Selena 分支" in index.text
    assert "不会自动切换或清理代码仓" in index.text
    assert "隔离工作区切换" not in index.text
    app_js = client.get("/console/app.js")
    assert app_js.status_code == 200
    assert 'stage.status === "running"' in app_js.text
    assert '["failed", "cancelled", "partial", "succeeded"].includes(job.status)' in app_js.text
    assert 'partial: "部分成功"' in app_js.text
    assert 'input_relative_path' in app_js.text
    assert "jobsRequestInFlight" in app_js.text
    assert "followedLogTail" in app_js.text
    assert "仿真失败原因" in app_js.text
    assert "stage.error?.diagnostic?.action" in app_js.text
    assert "校验期望分支并编译当前工作区" in app_js.text
    assert "隔离切换分支并编译 Selena" not in app_js.text
    assert "windowsWaitState" in app_js.text
    assert "等待连接本机" in app_js.text
    assert "一键连接本机" in app_js.text
    assert "/windows-connector/connect.cmd?mode=" in app_js.text
    assert "RadarSim-连接本机.cmd" in app_js.text
    assert "请双击运行已下载的文件" in app_js.text
    assert "当前账号已有 Windows 电脑上线，等待中的任务将自动继续" in app_js.text
    request_headers_block = app_js.text.split("function requestHeaders", 1)[1].split(
        "async function", 1
    )[0]
    assert 'headers.set("X-Rsim-User", state.userId)' in request_headers_block
    download_result_block = app_js.text.split("async function downloadResult", 1)[1].split(
        "async function", 1
    )[0]
    assert "const headers = requestHeaders()" in download_result_block
    assert 'triggerBlobDownload(blob, "radar-sim-result.zip")' in download_result_block
    assert 'showToast("结果 ZIP 已开始下载")' in download_result_block
    connector_download_block = app_js.text.split(
        "async function downloadWindowsConnector", 1
    )[1].split("async function", 1)[0]
    assert "const headers = requestHeaders()" in connector_download_block
    assert 'triggerBlobDownload(blob, "RadarSim-连接本机.cmd")' in connector_download_block
    blob_download_block = app_js.text.split("function triggerBlobDownload", 1)[1].split(
        "async function", 1
    )[0]
    assert "document.body.append(link)" in blob_download_block
    assert "window.setTimeout" in blob_download_block
    assert "Linux 服务已连接" in app_js.text
    assert "当前账号尚未连接 Windows 电脑" in app_js.text
    assert "createFormWindowsRequirement" in app_js.text
    assert "createWindowsCallout" in index.text
    assert "首次使用准备" in index.text
    assert "connectorUpdateBanner" in index.text
    assert "一键更新本机组件" in index.text
    assert 'id="resultPath"' in index.text
    assert 'result: { path: resultPath }' in app_js.text
    assert "本机连接暂时中断，正在自动重连" in app_js.text
    assert "无需重新安装或重新提交" in app_js.text
    assert "通常会自动恢复；长时间未连接时可重新连接本机" in app_js.text
    assert 'actionButton("重新连接本机", "secondary"' in app_js.text
    styles = client.get("/console/styles.css")
    assert styles.status_code == 200
    assert ".windows-connect-callout" in styles.text
    assert ".connector-update-banner" in styles.text
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/config").status_code == 404


def test_result_download_requires_the_same_owner_as_web_job_requests(tmp_path):
    source_root = tmp_path / "controlled-results"
    source = source_root / "run"
    source.mkdir(parents=True)
    (source / "output.MF4").write_bytes(b"simulated-result")
    catalog = ResultCatalog(
        tmp_path / "result-store",
        tmp_path / "results.db",
        allowed_source_root=source_root,
    )
    result = catalog.publish(
        owner="user-alice",
        run_ref="local-run:web-download-owner",
        source_root=source,
        files=["output.MF4"],
    )
    service = ApiV1Service(
        control_service_factory=lambda _owner: ControlService(tmp_path / "control.db"),
        result_catalog=catalog,
    )
    client = TestClient(create_app(api_service=service))
    path = f"/api/v1/results/{result.ref}/download"

    assert client.get(path, headers={"X-Rsim-User": "user-bob"}).status_code == 404
    downloaded = client.get(path, headers={"X-Rsim-User": "user-alice"})
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/zip"
    assert len(downloaded.content) == result.archive_size


def test_task_center_list_route_supports_owner_status_and_v2_config(tmp_path):
    client, _ = make_client(tmp_path)
    config = run_config_dict()
    created = client.post(
        "/api/v1/run-jobs",
        json={"config": config},
        headers={"X-Rsim-User": "alice"},
    )
    assert created.status_code == 201
    job = created.json()
    assert job["spec"]["selena"]["source"] == "build"

    client.post("/api/v1/run-jobs", json={"config": config}, headers={"X-Rsim-User": "bob"})
    page = client.get("/api/v1/jobs?status=queued&limit=10", headers={"X-Rsim-User": "alice"}).json()
    assert page["count"] == 1
    assert page["jobs"][0]["id"] == job["id"]
    assert page["jobs"][0]["current_stage"] == "resolve_spec"
    assert page["jobs"][0]["progress"] == 0.1


def test_invalid_v2_config_and_request_errors_share_envelope(tmp_path):
    client, _ = make_client(tmp_path)
    invalid = run_config_dict()
    invalid["selena"]["runtime_xml"] = ""
    response = client.post(
        "/api/v1/run-configs/validate", json=invalid, headers={"X-Request-ID": "req-test"}
    )
    body = response.json()
    assert response.status_code == 422
    assert response.headers["X-Request-ID"] == "req-test"
    assert set(body) == {"code", "message", "detail", "actions", "request_id"}
    assert body["code"] == "invalid_run_config"
    assert body["request_id"] == "req-test"
    assert body["detail"]["errors"][0]["loc"] == ["body", "selena"]
    assert "traceback" not in str(body).lower()
    assert "ValueError(" not in str(body)

    missing_spec = client.post("/api/v1/run-jobs", json={})
    assert missing_spec.status_code == 422
    assert missing_spec.json()["code"] == "invalid_run_config"

    not_found = client.get("/api/v1/jobs/job_missing")
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "not_found"


def test_openapi_exposes_only_v2_submit_and_validation_contract(tmp_path):
    client, _ = make_client(tmp_path)
    openapi = client.get("/openapi.json").json()

    validate_schema = openapi["paths"]["/api/v1/run-configs/validate"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    submit_config_schema = openapi["components"]["schemas"]["SubmitUserRunRequest"]["properties"]["config"]

    assert validate_schema == {"$ref": "#/components/schemas/UserRunConfig"}
    assert submit_config_schema == {"$ref": "#/components/schemas/UserRunConfig"}
    assert "/api/v1/validate" not in openapi["paths"]
    assert "/api/v1/schema/simulation-spec" not in openapi["paths"]


def test_durable_idempotency_conflict_over_http(tmp_path):
    client, _ = make_client(tmp_path)
    first = client.post("/api/v1/run-jobs", json={"config": run_config_dict()}, headers={"Idempotency-Key": "k"}).json()
    second = client.post("/api/v1/run-jobs", json={"config": run_config_dict()}, headers={"Idempotency-Key": "k"}).json()
    assert second["id"] == first["id"]

    changed = run_config_dict()
    changed["data"]["path"] = "D:/different"
    conflict = client.post("/api/v1/run-jobs", json={"config": changed}, headers={"Idempotency-Key": "k"})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"


def test_user_isolation_reuses_x_rsim_user(tmp_path):
    client, services = make_client(tmp_path)
    alice = client.post("/api/v1/run-jobs", json={"config": run_config_dict()}, headers={"X-Rsim-User": "alice"}).json()
    assert client.get(f"/api/v1/jobs/{alice['id']}", headers={"X-Rsim-User": "alice"}).status_code == 200
    assert client.get(f"/api/v1/jobs/{alice['id']}", headers={"X-Rsim-User": "bob"}).status_code == 404

    unsafe = client.post("/api/v1/run-jobs", json={"config": run_config_dict()}, headers={"X-Rsim-User": "../../../escape"}).json()
    assert unsafe["metadata"]["owner"] != "../../../escape"
    assert unsafe["metadata"]["owner"] in services
    assert "../../../escape" not in services


def test_events_json_and_sse_reconnect_cursor(tmp_path):
    client, services = make_client(tmp_path)
    job = client.post("/api/v1/run-jobs", json={"config": run_config_dict()}, headers={"X-Rsim-User": "alice"}).json()
    task_id = job["tasks"][0]["task_id"]
    services["alice"].append_logs(task_id, ["hello", "world"])

    page = client.get(f"/api/v1/jobs/{job['id']}/events?since=0&limit=50", headers={"X-Rsim-User": "alice"}).json()
    log_events = [event for event in page["events"] if event["event"] == "log"]
    assert [event["message"] for event in log_events] == ["hello", "world"]

    with client.stream(
        "GET",
        f"/api/v1/jobs/{job['id']}/events?stream=true",
        headers={"X-Rsim-User": "alice", "Last-Event-ID": str(log_events[0]["id"])},
    ) as response:
        body = response.read().decode("utf-8")
    assert "text/event-stream" in response.headers["content-type"]
    assert f"id: {log_events[1]['id']}" in body
    assert "event: log" in body
    assert '"message": "world"' in body


def test_retry_stage_route_error_and_owner_isolation(tmp_path):
    client, services = make_client(tmp_path)
    job = client.post("/api/v1/run-jobs", json={"config": run_config_dict()}, headers={"X-Rsim-User": "alice"}).json()
    stage_id = job["stages"][0]["stage_id"]

    invalid = client.post(
        f"/api/v1/jobs/{job['id']}/stages/{stage_id}/retry",
        headers={"X-Rsim-User": "alice", "X-Request-ID": "req-retry"},
    )
    assert invalid.status_code == 409
    assert invalid.json()["code"] == "invalid_stage_retry"
    assert invalid.json()["request_id"] == "req-retry"

    assert client.post(f"/api/v1/jobs/{job['id']}/stages/{stage_id}/retry", headers={"X-Rsim-User": "bob"}).status_code == 404

    services["alice"].register_internal_agent("scheduler", agent_id="__v1_scheduler__", capabilities=["*"])
    claimed = services["alice"].claim_next_task("__v1_scheduler__")
    services["alice"].submit_task_result(claimed["stage_id"], agent_id="__v1_scheduler__", status="failed", returncode=1)
    ok = client.post(f"/api/v1/jobs/{job['id']}/stages/{stage_id}/retry", headers={"X-Rsim-User": "alice"})
    assert ok.status_code == 200
    assert ok.json()["stages"][0]["status"] == "queued"


def test_fastapi_routes_do_not_contain_scheduler_rules():
    source = inspect.getsource(__import__("core.api_v1_fastapi", fromlist=[""]))
    for forbidden in ["cluster.run", "local.run_sim", "prepare_cluster_job", "subprocess", "git worktree"]:
        assert forbidden not in source


def test_serve_v1_uses_uvicorn_single_worker(monkeypatch, tmp_path):
    calls = {}

    def fake_run(app, **kwargs):
        calls["kwargs"] = kwargs

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    args = SimpleNamespace(host="127.0.0.1", port=8878, db_path=str(tmp_path / "v1.db"))
    assert server_cli._run_serve_v1(args) == 0
    assert calls["kwargs"]["host"] == "127.0.0.1"
    assert calls["kwargs"]["port"] == 8878
    assert calls["kwargs"]["workers"] == 1


def test_serve_v1_refuses_unauthenticated_non_loopback_bind():
    args = SimpleNamespace(
        host="0.0.0.0", port=8878, db_path="", auth_file="",
        insecure_no_auth=False,
    )
    assert server_cli._run_serve_v1(args) == 2


def test_serve_v1_loads_auth_file_for_non_loopback_bind(monkeypatch, tmp_path):
    calls = {}
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        __import__("json").dumps({
            "version": 1,
            "users": {"alice": ALICE_TOKEN},
            "agents": {"agent-1": {"owner": "alice", "token": AGENT_TOKEN}},
        }),
        encoding="utf-8",
    )

    def fake_create_app(*, api_service=None, authenticator=None):
        calls["authenticator"] = authenticator
        return object()

    import uvicorn
    import core.api_v1_fastapi as fastapi_module

    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fastapi_module, "create_app", fake_create_app)
    args = SimpleNamespace(
        host="0.0.0.0", port=8878, db_path=str(tmp_path / "v1.db"),
        auth_file=str(auth_file), insecure_no_auth=False, no_cluster_executor=True,
    )
    assert server_cli._run_serve_v1(args) == 0
    assert calls["authenticator"].authenticate_user(f"Bearer {ALICE_TOKEN}").owner == "alice"


def test_serve_v1_wires_source_resolution_to_same_explicit_artifact_db(monkeypatch, tmp_path):
    calls = {"control_db": [], "artifact_db": [], "inspect": []}
    app_sentinel = object()

    def fake_run(app, **kwargs):
        calls["app"] = app
        calls["kwargs"] = kwargs

    def fake_create_app(*, api_service=None, control_service_factory=None):
        assert control_service_factory is None
        service = api_service.control_service_factory("alice")
        calls["control_db"].append(service._db_path)
        api_service.source_resolution_provider("alice", SimulationSpec.from_dict(spec_dict()))
        return app_sentinel

    def fake_build(owner, spec, *, catalog_factory, config_loader, now_fn, inspect_local_workspace):
        calls["artifact_db"].append(catalog_factory(owner)._db_path)
        calls["inspect"].append(inspect_local_workspace)
        return object()

    import uvicorn
    import core.api_v1_fastapi as fastapi_module
    import core.source_resolution_runtime as runtime_module

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(fastapi_module, "create_app", fake_create_app)
    monkeypatch.setattr(runtime_module, "build_legacy_source_resolution_inputs", fake_build)

    db_path = tmp_path / "explicit.db"
    args = SimpleNamespace(host="127.0.0.1", port=8878, db_path=str(db_path))
    assert server_cli._run_serve_v1(args) == 0

    assert calls["app"] is app_sentinel
    assert calls["control_db"] == [str(db_path)]
    assert calls["artifact_db"] == [str(db_path)]
    assert calls["inspect"] == [False]


def test_serve_v1_wires_central_owner_scoped_control_and_artifact_db(monkeypatch, tmp_path):
    calls = {"control_db": [], "artifact_db": [], "inspect": []}

    def user_db(user: str = ""):
        return tmp_path / f"{user or 'default'}.db"

    def fake_run(app, **kwargs):
        calls["app"] = app

    def fake_create_app(*, api_service=None, control_service_factory=None):
        service = api_service.control_service_factory("alice")
        calls["control_db"].append(service._db_path)
        api_service.source_resolution_provider("alice", SimulationSpec.from_dict(spec_dict()))
        return object()

    def fake_build(owner, spec, *, catalog_factory, config_loader, now_fn, inspect_local_workspace):
        calls["artifact_db"].append(catalog_factory(owner)._db_path)
        calls["inspect"].append(inspect_local_workspace)
        return object()

    import uvicorn
    import core.api_v1_fastapi as fastapi_module
    import core.source_resolution_runtime as runtime_module
    import core.user as user_module

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(fastapi_module, "create_app", fake_create_app)
    monkeypatch.setattr(runtime_module, "build_legacy_source_resolution_inputs", fake_build)
    monkeypatch.setattr(user_module, "control_db_path_for_user", user_db)
    artifact_root = tmp_path / "artifact-root"
    monkeypatch.setenv("RSIM_ARTIFACT_ROOT", str(artifact_root))

    args = SimpleNamespace(host="127.0.0.1", port=8878, db_path="")
    assert server_cli._run_serve_v1(args) == 0

    assert calls["control_db"] == [str(artifact_root / ".store" / "control_v1.db")]
    assert calls["artifact_db"] == [str(artifact_root / ".store" / "catalog.db")]
    assert calls["inspect"] == [False]
