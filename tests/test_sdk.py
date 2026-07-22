import pytest
import httpx
from types import SimpleNamespace
from fastapi.testclient import TestClient

from core.api_v1_fastapi import create_app
from core.control_service import ControlService
from core.api_v1 import ApiV1Service
from core.config_assets import ConfigAssetStore
from core.local_results import ResultCatalog
from core.http_auth import HttpTokenAuthenticator
from radar_sim_sdk import RadarSimApiError, RadarSimClient, SimulationSpec, UserRunConfig
from radar_sim_sdk.events import event_from_sse, parse_sse_lines
from tests.test_api_v1_service import run_config_dict, spec_dict


def make_sdk(tmp_path):
    services: dict[str, ControlService] = {}

    def factory(owner: str) -> ControlService:
        services.setdefault(owner, ControlService(tmp_path / f"{owner}.db"))
        return services[owner]

    test_client = TestClient(create_app(control_service_factory=factory))
    sdk = RadarSimClient("http://testserver", client=test_client, user="alice")
    return sdk, services


def test_sdk_validate_and_submit_share_spec_hash_with_web_json(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    spec = SimulationSpec.from_dict(spec_dict())

    validation = sdk.validate(spec)
    job = sdk.submit(spec, dry_run=True, idempotency_key="sdk-key")

    assert validation.fingerprint == spec.fingerprint()
    assert job.spec_hash == validation.fingerprint
    assert len(job.stages) == 10
    assert job.resolved_spec["status"] == "pending"
    assert job.spec == spec.to_dict()
    assert sdk.submit(spec, dry_run=True, idempotency_key="sdk-key").id == job.id


def test_sdk_and_web_share_project_free_run_config_contract(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    config = UserRunConfig.from_dict(run_config_dict())
    validation = sdk.validate_run(config)
    job = sdk.submit_run(config, idempotency_key="sdk-run-v2")
    assert validation.config == config
    assert len(validation.execution_plan) == 10
    assert job.spec_hash == config.fingerprint()
    assert job.type == "simulation.run_config.v2"
    assert "project" not in job.spec
    assert job.waiting["reason"] == "windows_connection_required"
    assert job.waiting["mode"] == "light"
    assert job.waiting["action"]["type"] == "connect_windows"


def test_sdk_submit_yaml_accepts_every_user_run_combination(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    config = UserRunConfig.from_dict(run_config_dict())
    yaml_path = tmp_path / "simulation.yaml"
    yaml_path.write_text(config.to_yaml(), encoding="utf-8")

    job = sdk.submit_yaml(yaml_path, dry_run=True, idempotency_key="generic-yaml")

    assert job.status == "succeeded"
    assert job.spec == config.to_dict()
    assert job.type == "simulation.run_config.v2.dry_run"


def test_sdk_submit_run_transparently_uploads_linux_local_data_path(tmp_path, monkeypatch):
    sdk, _ = make_sdk(tmp_path)
    data = tmp_path / "measurements"
    data.mkdir()
    (data / "one.MF4").write_bytes(b"mf4")
    config = run_config_dict()
    config["data"] = {"path": str(data)}
    config["simulation"]["target"] = "cluster"
    uploaded_path = "dataset://sha256/" + "a" * 64
    seen = []
    monkeypatch.setattr(
        sdk,
        "upload_run_data",
        lambda source: seen.append(str(source)) or SimpleNamespace(data_path=uploaded_path),
    )

    job = sdk.submit_run(config)

    assert seen == [str(data)]
    assert job.spec["data"] == {"path": uploaded_path}
    assert "project" not in job.spec


def test_sdk_submit_run_keeps_shared_data_even_when_caller_can_read_it(tmp_path, monkeypatch):
    sdk, _ = make_sdk(tmp_path)
    readable_share = tmp_path / "mounted-share"
    readable_share.mkdir()
    (readable_share / "one.MF4").write_bytes(b"mf4")
    config = run_config_dict()
    config["data"] = {"path": str(readable_share)}
    config["simulation"]["target"] = "cluster"
    monkeypatch.setattr("radar_sim_sdk.client.classify_data_path", lambda _path: "shared")
    monkeypatch.setattr(
        sdk,
        "upload_run_data",
        lambda _source: pytest.fail("shared data must remain a direct path"),
    )

    job = sdk.submit_run(config)

    assert job.spec["data"]["path"] == readable_share.as_posix()


def test_sdk_submit_run_prepares_local_data_and_configuration_assets(tmp_path, monkeypatch):
    sdk, _ = make_sdk(tmp_path)
    data = tmp_path / "measurements"
    data.mkdir()
    (data / "one.MF4").write_bytes(b"mf4")
    mat_filter = tmp_path / "signals.filter"
    mat_filter.write_text("signal=*\n", encoding="utf-8")
    adapter = tmp_path / "adapter.txt"
    adapter.write_text("adapter=1\n", encoding="utf-8")
    config = run_config_dict()
    config["data"] = {"path": str(data)}
    config["simulation"].update(
        {
            "target": "cluster",
            "mat_filter": str(mat_filter),
            "adapter_file": str(adapter),
        }
    )
    uploaded = []
    monkeypatch.setattr(
        sdk,
        "upload_run_data",
        lambda source: SimpleNamespace(data_path="dataset://sha256/" + "a" * 64),
    )
    monkeypatch.setattr(
        sdk,
        "upload_config_asset",
        lambda kind, source: uploaded.append((kind, str(source)))
        or {"uri": "config-asset://sha256/" + ("b" if kind == "mat_filter" else "c") * 64},
    )

    job = sdk.submit_run(config)

    assert uploaded == [("mat_filter", str(mat_filter)), ("adapter", str(adapter))]
    assert job.spec["data"]["path"].startswith("dataset://")
    assert job.spec["simulation"]["mat_filter"].startswith("config-asset://")
    assert job.spec["simulation"]["adapter_file"].startswith("config-asset://")


def test_sdk_submit_run_dry_run_never_uploads_local_inputs(tmp_path, monkeypatch):
    sdk, _ = make_sdk(tmp_path)
    data = tmp_path / "measurements"
    data.mkdir()
    (data / "one.MF4").write_bytes(b"mf4")
    mat_filter = tmp_path / "signals.filter"
    mat_filter.write_text("signal=*\n", encoding="utf-8")
    config = run_config_dict()
    config["data"] = {"path": str(data)}
    config["simulation"]["target"] = "cluster"
    config["simulation"]["mat_filter"] = str(mat_filter)
    monkeypatch.setattr(
        sdk, "upload_run_data", lambda _source: pytest.fail("dry-run uploaded data")
    )
    monkeypatch.setattr(
        sdk,
        "upload_config_asset",
        lambda _kind, _source: pytest.fail("dry-run uploaded a config asset"),
    )
    monkeypatch.setattr(
        sdk,
        "_upload_existing_selena",
        lambda _folder, _runtime: pytest.fail("dry-run uploaded Selena"),
    )

    job = sdk.submit_run(config, dry_run=True)

    assert job.status == "succeeded"
    assert job.spec["data"]["path"] == data.as_posix()
    assert job.spec["simulation"]["mat_filter"] == mat_filter.as_posix()


def test_sdk_submit_run_keeps_unreachable_paths_for_server_or_agent(tmp_path, monkeypatch):
    sdk, _ = make_sdk(tmp_path)
    config = run_config_dict()
    config["data"] = {"path": "D:/remote-machine/data"}
    config["simulation"].update(
        {
            "target": "cluster",
            "mat_filter": "D:/remote-machine/signals.filter",
            "adapter_file": "D:/remote-machine/adapter.txt",
        }
    )
    monkeypatch.setattr(
        sdk, "upload_run_data", lambda _source: pytest.fail("unreachable data uploaded")
    )
    monkeypatch.setattr(
        sdk,
        "upload_config_asset",
        lambda _kind, _source: pytest.fail("unreachable config asset uploaded"),
    )

    job = sdk.submit_run(config)

    assert job.spec["data"]["path"] == "D:/remote-machine/data"
    assert job.spec["simulation"]["mat_filter"] == "D:/remote-machine/signals.filter"
    assert job.spec["simulation"]["adapter_file"] == "D:/remote-machine/adapter.txt"


def test_sdk_uploads_and_lists_reusable_configuration_assets(tmp_path):
    api = ApiV1Service(
        control_service_factory=lambda _owner: ControlService(tmp_path / "control.db"),
        config_asset_store=ConfigAssetStore(tmp_path / "assets", tmp_path / "assets.db"),
    )
    test_client = TestClient(create_app(api_service=api))
    sdk = RadarSimClient("http://testserver", client=test_client, user="alice")
    source = tmp_path / "signals.filter"
    source.write_text("signal=*\n", encoding="utf-8")

    uploaded = sdk.upload_config_asset("mat_filter", source)
    assert uploaded["uri"].startswith("config-asset://sha256/")
    assert sdk.list_config_assets(kind="mat_filter") == [uploaded]
    assert sdk.get_config_asset(uploaded["id"], kind="mat_filter") == uploaded


def test_sdk_token_adds_bearer_authorization_header():
    token = "sdk-token-0123456789_ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {token}"
        return httpx.Response(200, json={"jobs": []})

    sdk = RadarSimClient("http://testserver", token=token, transport=httpx.MockTransport(handler))
    assert sdk.list_jobs() == []


def test_sdk_agent_downloads_config_asset_and_verifies_checksum(tmp_path):
    agent_token = "agent-token-0123456789_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    user_token = "alice-token-0123456789_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    store = ConfigAssetStore(tmp_path / "assets", tmp_path / "assets.db")
    record = store.put(
        owner="alice", kind="mat_filter", filename="signals.filter", content=b"signal=*\n"
    )
    api = ApiV1Service(
        control_service_factory=lambda _owner: ControlService(tmp_path / "control.db"),
        config_asset_store=store,
    )
    authenticator = HttpTokenAuthenticator.from_mapping({
        "version": 1,
        "users": {"alice": user_token},
        "agents": {"agent-1": {"owner": "alice", "token": agent_token}},
    })
    test_client = TestClient(create_app(api_service=api, authenticator=authenticator))
    sdk = RadarSimClient("http://testserver", client=test_client, token=agent_token)
    destination = sdk.download_config_asset(
        record.id, kind="mat_filter", destination=tmp_path / "signals.filter"
    )
    assert destination.read_bytes() == b"signal=*\n"


def test_sdk_config_asset_download_rejects_digest_mismatch(tmp_path):
    digest = "0" * 64

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"tampered")

    sdk = RadarSimClient("http://testserver", transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="checksum"):
        sdk.download_config_asset(
            "config-asset:sha256:" + digest,
            kind="adapter",
            destination=tmp_path / "adapter.txt",
        )
    assert not (tmp_path / "adapter.txt").exists()


def test_sdk_lists_gets_and_downloads_owner_scoped_local_result(tmp_path):
    controlled = tmp_path / "runs"
    source = controlled / "lease" / "outputs"
    source.mkdir(parents=True)
    (source / "result.MF4").write_bytes(b"result")
    catalog = ResultCatalog(
        tmp_path / "result-store", tmp_path / "results.db", allowed_source_root=controlled
    )
    published = catalog.publish(
        owner="alice", run_ref="local-run:one", source_root=source,
        files=["result.MF4"], retain_until=10_000_000_000,
    )
    api = ApiV1Service(
        control_service_factory=lambda _owner: ControlService(tmp_path / "control.db"),
        result_catalog=catalog,
    )
    test_client = TestClient(create_app(api_service=api))
    sdk = RadarSimClient("http://testserver", client=test_client, user="alice")

    assert sdk.list_results() == [published.public_dict]
    assert sdk.get_result(published.ref) == published.public_dict
    downloaded = sdk.download_result(published.ref, tmp_path / "downloads")
    assert downloaded.is_file()
    assert downloaded.read_bytes() == catalog.resolve_archive(published.ref, owner="alice").read_bytes()

    bob = RadarSimClient("http://testserver", client=test_client, user="bob")
    with pytest.raises(RadarSimApiError) as excinfo:
        bob.get_result(published.ref)
    assert excinfo.value.status_code == 404


def test_sdk_minimal_spec_and_task_center_list(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    job = sdk.submit({"project": "bydod25", "data": {"path": "D:/measurement/run"}})

    jobs = sdk.list_jobs(status="queued", limit=10)
    assert [item.id for item in jobs] == [job.id]
    assert jobs[0].current_stage == "resolve_spec"
    assert jobs[0].progress == 0.0
    assert jobs[0].available_actions[0]["type"] == "cancel_job"


def test_sdk_error_mapping_uses_api_envelope(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    with pytest.raises(RadarSimApiError) as excinfo:
        sdk.validate({"schema_version": "1.0"})
    assert excinfo.value.code == "invalid_spec"
    assert excinfo.value.status_code == 422
    assert excinfo.value.actions[0]["type"] == "fix_spec"


def test_sse_parser_comments_blank_multiline_id_event():
    messages = list(
        parse_sse_lines(
            [
                ": keepalive",
                "id: 7",
                "event: log",
                "data: {\"message\":\"hello\"",
                "data: ,\"extra\":true}",
                "",
            ]
        )
    )
    assert len(messages) == 1
    assert messages[0].id == "7"
    assert messages[0].event == "log"
    assert messages[0].data == '{"message":"hello"\n,"extra":true}'

    event = event_from_sse(messages[0])
    assert event.id == 7
    assert event.event == "log"


def test_sdk_stream_events_watch_wait_cancel_and_manifest(tmp_path):
    sdk, services = make_sdk(tmp_path)
    job = sdk.submit(spec_dict())
    task_id = job.tasks[0]["task_id"]
    services["alice"].append_logs(task_id, ["line-1"])

    streamed = list(sdk.stream_events(job.id))
    assert [event.message for event in streamed if event.event == "log"] == ["line-1"]
    cursor = max(event.id for event in streamed if event.id is not None)

    services["alice"].append_logs(task_id, ["line-2"])
    cancelled = sdk.cancel(job.id)
    assert cancelled.status == "cancelled"

    watched = list(sdk.watch(job.id, cursor=cursor, timeout=2.0, poll_interval=0.01))
    assert [event.message for event in watched if event.event == "log"] == ["line-2"]
    assert sdk.wait(job.id, timeout=2.0, poll_interval=0.01).status == "cancelled"

    manifest = sdk.manifest(job.id)
    assert manifest.available is False
    assert manifest.manifest is None


def test_sdk_structured_event_fields_and_retry_stage(tmp_path):
    sdk, services = make_sdk(tmp_path)
    job = sdk.submit(spec_dict())
    stage_id = job.stages[0]["stage_id"]
    services["alice"].report_stage_progress(stage_id, progress=0.5, message="half", code="P50")
    page = sdk.events(job.id)
    progress_event = next(event for event in page.events if event.event == "stage.progress")
    assert progress_event.stage_id == stage_id
    assert progress_event.status == "queued"
    assert progress_event.progress == 0.5
    assert progress_event.code == "P50"

    services["alice"].register_internal_agent("scheduler", agent_id="__v1_scheduler__", capabilities=["*"])
    claimed = services["alice"].claim_next_task("__v1_scheduler__")
    services["alice"].submit_task_result(claimed["stage_id"], agent_id="__v1_scheduler__", status="failed", returncode=1)
    retried = sdk.retry_stage(job.id, stage_id)
    assert retried.stages[0]["status"] == "queued"


def test_sdk_does_not_import_scheduler_dependencies():
    import radar_sim_sdk.client as client_module

    source = client_module.__loader__.get_source(client_module.__name__)
    for forbidden in ["core.profiles", "core.control_service", "cluster.run", "prepare_cluster_job"]:
        assert forbidden not in source


def test_sdk_watch_retries_initial_sse_transport_failure_with_cursor():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "stream=true" in str(request.url) and len(calls) == 1:
            raise httpx.ConnectError("sse down", request=request)
        return httpx.Response(
            200,
            json={
                "job_id": "job_1",
                "status": "cancelled",
                "events": [{"id": 1, "event": "log", "message": "recovered", "data": {"message": "recovered"}}],
                "next_cursor": 1,
                "terminal": True,
            },
        )

    sdk = RadarSimClient("http://testserver", transport=httpx.MockTransport(handler))
    events = list(sdk.watch("job_1", timeout=1.0, poll_interval=0.01))
    assert [event.message for event in events] == ["recovered"]
    assert any("since=0" in call for call in calls)


def test_sdk_watch_retries_polling_transport_failure_without_duplicate_events():
    state = {"stream_calls": 0, "poll_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "stream=true" in url:
            state["stream_calls"] += 1
            if state["stream_calls"] == 1:
                return httpx.Response(
                    200,
                    text='id: 1\nevent: log\ndata: {"id": 1, "event": "log", "message": "once", "data": {"message": "once"}}\n\n',
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(200, text="", headers={"content-type": "text/event-stream"})
        state["poll_calls"] += 1
        if state["poll_calls"] == 1:
            raise httpx.ReadError("poll down", request=request)
        return httpx.Response(
            200,
            json={"job_id": "job_1", "status": "cancelled", "events": [], "next_cursor": 1, "terminal": True},
        )

    sdk = RadarSimClient("http://testserver", transport=httpx.MockTransport(handler))
    events = list(sdk.watch("job_1", timeout=1.0, poll_interval=0.01))
    assert [event.message for event in events] == ["once"]
    assert state["poll_calls"] == 2


def test_sdk_watch_continuous_transport_failure_times_out():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    sdk = RadarSimClient("http://testserver", transport=httpx.MockTransport(handler))
    with pytest.raises(TimeoutError):
        list(sdk.watch("job_1", timeout=0.05, poll_interval=0.01))
