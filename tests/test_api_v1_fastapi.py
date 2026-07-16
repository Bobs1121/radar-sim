import inspect
from types import SimpleNamespace

from fastapi.testclient import TestClient

from cli import server as server_cli
from core.spec import SimulationSpec
from core.api_v1 import ApiV1Service
from core.api_v1_fastapi import create_app
from core.control_service import ControlService
from core.config_assets import ConfigAssetStore
from core.http_auth import HttpTokenAuthenticator
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
        "/api/v1/jobs",
        headers={"Authorization": f"Bearer {ALICE_TOKEN}", "X-Rsim-User": "bob"},
        json={"spec": spec_dict(), "dry_run": True},
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


def test_health_schema_validate_submit_get_cancel_manifest(tmp_path):
    client, _ = make_client(tmp_path)

    assert client.get("/api/v1/health").json()["api_version"] == "v1"
    schema = client.get("/api/v1/schema/simulation-spec").json()
    assert "project" in schema["properties"]

    validation = client.post("/api/v1/validate", json=spec_dict()).json()
    job = client.post("/api/v1/jobs", json={"spec": spec_dict(), "dry_run": True}).json()
    assert job["spec_hash"] == validation["fingerprint"]
    assert job["type"] == "simulation.v1.dry_run"

    fetched = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert fetched["id"] == job["id"]
    assert client.get(f"/api/v1/jobs/{job['id']}/manifest").json()["available"] is False

    cancelled = client.post(f"/api/v1/jobs/{job['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"


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


def test_project_free_run_config_routes_share_one_contract(tmp_path):
    client, _ = make_client(tmp_path)
    schema = client.get("/api/v1/schema/run-config").json()
    assert "project" not in schema["properties"]
    config = run_config_dict()
    validated = client.post("/api/v1/run-configs/validate", json=config)
    assert validated.status_code == 200
    assert len(validated.json()["execution_plan"]) == 10
    assert validated.json()["execution"]["selected_target"] in {"local", "cluster"}
    created = client.post(
        "/api/v1/run-jobs",
        json={"config": config},
        headers={"Idempotency-Key": "run-config-1", "X-Rsim-User": "alice"},
    )
    assert created.status_code == 201
    assert created.json()["spec_hash"] == validated.json()["fingerprint"]
    assert "project" not in created.json()["spec"]

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
        metadata={"node_kind": "windows_full", "workspace": "D:/private/workspace"},
    )
    body = client.get("/api/v1/capabilities", headers={"X-Rsim-User": "alice"}).json()
    assert body["capabilities"]["windows_full"]["available"] is True
    assert "full-a" not in str(body)
    assert "private" not in str(body).lower()


def test_project_catalog_and_yaml_import_export_for_web(tmp_path):
    services: dict[str, ControlService] = {}

    def factory(owner: str) -> ControlService:
        services.setdefault(owner, ControlService(tmp_path / f"{owner}.db"))
        return services[owner]

    api = ApiV1Service(
        control_service_factory=factory,
        project_names_provider=lambda: ["ovrs25", "bydod25", "ovrs25", ""],
    )
    client = TestClient(create_app(api_service=api))
    assert client.get("/api/v1/projects").json() == {
        "projects": ["bydod25", "ovrs25"],
        "count": 2,
    }

    imported = client.post(
        "/api/v1/specs/import",
        json={"yaml_content": "project: ovrs25\ndata:\n  path: //server/share/run\n"},
    )
    assert imported.status_code == 200
    spec = imported.json()["spec"]
    assert spec["selena"]["mode"] == "auto"
    exported = client.post("/api/v1/specs/export", json={"spec": spec})
    assert exported.status_code == 200
    assert "project: ovrs25" in exported.json()["yaml_content"]

    invalid = client.post(
        "/api/v1/specs/import",
        json={"yaml_content": "project: ovrs25\ndata: []\n"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_spec"


def test_v1_web_console_is_same_origin_and_legacy_routes_are_not_shadowed(tmp_path):
    client, _ = make_client(tmp_path)
    index = client.get("/")
    assert index.status_code == 200
    assert "Radar Sim 控制台" in index.text
    app_js = client.get("/console/app.js")
    assert app_js.status_code == 200
    assert 'stage.status === "running"' in app_js.text
    assert '["failed", "cancelled", "succeeded"].includes(job.status)' in app_js.text
    assert client.get("/console/styles.css").status_code == 200
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/config").status_code == 404


def test_task_center_list_route_supports_owner_status_and_minimal_spec(tmp_path):
    client, _ = make_client(tmp_path)
    minimal = {"project": "bydod25", "data": {"path": "D:/measurement/run"}}
    created = client.post(
        "/api/v1/jobs",
        json={"spec": minimal},
        headers={"X-Rsim-User": "alice"},
    )
    assert created.status_code == 201
    job = created.json()
    assert job["spec"]["selena"]["mode"] == "auto"

    client.post("/api/v1/jobs", json={"spec": minimal}, headers={"X-Rsim-User": "bob"})
    page = client.get("/api/v1/jobs?status=queued&limit=10", headers={"X-Rsim-User": "alice"}).json()
    assert page["count"] == 1
    assert page["jobs"][0]["id"] == job["id"]
    assert page["jobs"][0]["current_stage"] == "resolve_spec"
    assert page["jobs"][0]["progress"] == 0.0


def test_invalid_spec_and_request_errors_share_envelope(tmp_path):
    client, _ = make_client(tmp_path)
    invalid = spec_dict(project="")
    response = client.post("/api/v1/validate", json=invalid, headers={"X-Request-ID": "req-test"})
    body = response.json()
    assert response.status_code == 422
    assert response.headers["X-Request-ID"] == "req-test"
    assert set(body) == {"code", "message", "detail", "actions", "request_id"}
    assert body["code"] == "invalid_spec"
    assert body["request_id"] == "req-test"
    assert body["detail"]["errors"][0]["loc"] == ["body", "project"]
    assert "traceback" not in str(body).lower()
    assert "ValueError(" not in str(body)

    missing_spec = client.post("/api/v1/jobs", json={})
    assert missing_spec.status_code == 422
    assert missing_spec.json()["code"] == "invalid_request"

    not_found = client.get("/api/v1/jobs/job_missing")
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "not_found"


def test_openapi_validate_and_submit_reference_same_simulation_spec_schema(tmp_path):
    client, _ = make_client(tmp_path)
    openapi = client.get("/openapi.json").json()

    validate_schema = openapi["paths"]["/api/v1/validate"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    submit_spec_schema = openapi["components"]["schemas"]["SubmitJobRequest"]["properties"]["spec"]

    assert validate_schema == {"$ref": "#/components/schemas/SimulationSpec"}
    assert submit_spec_schema == {"$ref": "#/components/schemas/SimulationSpec"}


def test_durable_idempotency_conflict_over_http(tmp_path):
    client, _ = make_client(tmp_path)
    first = client.post("/api/v1/jobs", json={"spec": spec_dict()}, headers={"Idempotency-Key": "k"}).json()
    second = client.post("/api/v1/jobs", json={"spec": spec_dict()}, headers={"Idempotency-Key": "k"}).json()
    assert second["id"] == first["id"]

    changed = spec_dict(data={"path": "D:/different", "limit": 0, "required_signals": []})
    conflict = client.post("/api/v1/jobs", json={"spec": changed}, headers={"Idempotency-Key": "k"})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"


def test_user_isolation_reuses_x_rsim_user(tmp_path):
    client, services = make_client(tmp_path)
    alice = client.post("/api/v1/jobs", json={"spec": spec_dict()}, headers={"X-Rsim-User": "alice"}).json()
    assert client.get(f"/api/v1/jobs/{alice['id']}", headers={"X-Rsim-User": "alice"}).status_code == 200
    assert client.get(f"/api/v1/jobs/{alice['id']}", headers={"X-Rsim-User": "bob"}).status_code == 404

    unsafe = client.post("/api/v1/jobs", json={"spec": spec_dict()}, headers={"X-Rsim-User": "../../../escape"}).json()
    assert unsafe["metadata"]["owner"] != "../../../escape"
    assert unsafe["metadata"]["owner"] in services
    assert "../../../escape" not in services


def test_events_json_and_sse_reconnect_cursor(tmp_path):
    client, services = make_client(tmp_path)
    job = client.post("/api/v1/jobs", json={"spec": spec_dict()}, headers={"X-Rsim-User": "alice"}).json()
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
    job = client.post("/api/v1/jobs", json={"spec": spec_dict()}, headers={"X-Rsim-User": "alice"}).json()
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
