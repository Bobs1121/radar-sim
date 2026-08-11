import pytest
import httpx
import time
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient

from core.api_v1_fastapi import create_app
from core.control_service import ControlService
from core.direct_transfer import (
    TransferPlan,
    TransferPlanItem,
    build_isolated_relative_root,
    generate_opaque_id,
    generate_owner_scope,
)
from core.api_v1 import ApiV1Service
from core.config_assets import ConfigAssetStore
from core.local_results import ResultCatalog
from core.http_auth import HttpTokenAuthenticator
from core.simulation import (
    _MF4_CHANNEL_GROUP,
    _MF4_COMMON,
    _MF4_DATA_GROUP,
    _MF4_HEADER,
    _MF4_SOURCE_INFORMATION,
    _discover_mf4_acquisition_sources_stdlib,
    build_effective_simulation,
    detect_radar_transfer_metadata,
    detect_radar_transfer_metadata_safe,
    discover_radar_acquisition_sources,
)
from radar_sim_sdk import Job, RadarSimApiError, RadarSimClient, SimulationSpec, UserRunConfig
from radar_sim_sdk.client import _dataset_transfer_fingerprints, _trust_environment_proxy
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


def test_sdk_downloads_one_time_windows_connector_for_current_scope(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    target = sdk.download_windows_connector(tmp_path, mode="light")

    assert target.name == "RadarSim-Connect-Windows.cmd"
    content = target.read_text(encoding="utf-8")
    assert "install.ps1?mode=" in content
    assert "RSIM_CONNECTOR_MODE=light" in content
    assert "__RSIM_SERVER_URL_BASE64__" not in content
    assert "__RSIM_OWNER_BASE64__" not in content


def test_sdk_without_explicit_user_gets_stable_os_login_identity(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["X-Rsim-User"])
        return httpx.Response(200, json={"ok": True, "api_version": "v1"})

    monkeypatch.setattr("radar_sim_sdk.client.getpass.getuser", lambda: "new-user")
    with RadarSimClient("http://testserver", transport=httpx.MockTransport(handler)) as first:
        first.health()
    with RadarSimClient("http://testserver", transport=httpx.MockTransport(handler)) as second:
        second.health()

    assert seen[0] == seen[1]
    assert seen[0] == "user-new-user"


def test_sdk_dataset_transfer_fingerprints_project_rl_to_radar_rl(monkeypatch, tmp_path: Path):
    source = tmp_path / "dataset"
    source.mkdir()
    (source / "recording.MF4").write_bytes(b"mf4")
    monkeypatch.setattr(
        "core.simulation.detect_radar_transfer_metadata_safe",
        lambda _path: {"radar_source": "RadarRL", "radar_mounting_position": "CRL"},
    )

    fingerprints = _dataset_transfer_fingerprints(
        source,
        [{"relative_path": "recording.MF4"}],
    )

    assert fingerprints == {"radar_source": "RadarRL", "radar_mounting_position": "CRL"}


def test_radar_transfer_metadata_prefers_first_acquisition_source(monkeypatch, tmp_path: Path):
    mf4 = tmp_path / "recording.MF4"
    mf4.write_bytes(b"mf4")
    monkeypatch.setattr(
        "core.simulation.discover_radar_acquisition_sources",
        lambda _path: ["RadarRL", "RadarRR"],
    )
    monkeypatch.setattr(
        "core.simulation.detect_radar_orientation",
        lambda _path: pytest.fail("orientation fallback must not run when acquisition sources exist"),
    )

    assert detect_radar_transfer_metadata(str(mf4)) == {
        "radar_source": "RadarRL",
        "radar_mounting_position": "CRL",
    }


def test_safe_radar_transfer_metadata_uses_stdlib_without_optional_parser(monkeypatch, tmp_path: Path):
    mf4 = tmp_path / "recording.MF4"
    mf4.write_bytes(b"mf4")
    monkeypatch.setattr(
        "core.simulation._discover_mf4_acquisition_sources_stdlib",
        lambda _path: ["RadarRR"],
    )
    monkeypatch.setattr(
        "core.simulation.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("stdlib metadata must avoid the optional parser subprocess"),
    )

    assert detect_radar_transfer_metadata_safe(str(mf4)) == {
        "radar_source": "RadarRR",
        "radar_mounting_position": "CRR",
    }


def test_acquisition_source_discovery_uses_fast_metadata_before_asammdf(
    monkeypatch, tmp_path: Path
):
    mf4 = tmp_path / "recording.MF4"
    mf4.write_bytes(b"mf4")
    monkeypatch.setattr(
        "core.simulation._discover_mf4_acquisition_sources_stdlib",
        lambda _path: ["RadarRL", "RadarRR"],
    )

    # Importing the optional parser would prove that the slow full-MDF path
    # was entered even though authoritative acquisition metadata was present.
    import builtins

    original_import = builtins.__import__

    def reject_asammdf(name, *args, **kwargs):
        if name == "asammdf":
            pytest.fail("metadata discovery must not import asammdf")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_asammdf)
    assert discover_radar_acquisition_sources(str(mf4)) == ["RadarRL", "RadarRR"]


def test_safe_radar_transfer_metadata_tolerates_native_parser_exit(monkeypatch, tmp_path: Path):
    mf4 = tmp_path / "recording.MF4"
    mf4.write_bytes(b"mf4")
    monkeypatch.setattr(
        "core.simulation._discover_mf4_acquisition_sources_stdlib",
        lambda _path: [],
    )
    monkeypatch.setattr(
        "core.simulation.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=-1073741819, stdout="", stderr="native crash"),
    )

    assert detect_radar_transfer_metadata_safe(str(mf4)) == {}


def test_local_effective_simulation_uses_acquisition_source_before_orientation(
    monkeypatch, tmp_path: Path
):
    """Local and Cluster runs must select the same source from an MF4."""

    mf4 = tmp_path / "recording.MF4"
    mf4.write_bytes(b"mf4")
    monkeypatch.setattr(
        "core.simulation.discover_radar_acquisition_sources",
        lambda _path: ["RadarRL", "RadarRR"],
    )
    monkeypatch.setattr(
        "core.simulation.detect_radar_orientation",
        lambda _path: pytest.fail("orientation is only a fallback when acquisition metadata is absent"),
    )

    result = build_effective_simulation(
        {"_meta": {"project": "anonymous", "_run_id": "run-1"}, "simulation": {}},
        str(mf4),
    )

    assert result["source"] == "RadarRL"
    assert result["mounting_position"] == "CRL"
    assert result["radar_detection"]["method"] == "acquisition_source"
    assert result["radar_detection"]["evidence"]["available_sources"] == [
        "RadarRL",
        "RadarRR",
    ]


def test_explicit_source_wins_over_multi_source_metadata(monkeypatch, tmp_path: Path):
    mf4 = tmp_path / "recording.MF4"
    mf4.write_bytes(b"mf4")
    monkeypatch.setattr(
        "core.simulation.discover_radar_acquisition_sources",
        lambda _path: pytest.fail("explicit source must skip automatic metadata selection"),
    )
    result = build_effective_simulation(
        {
            "_meta": {"project": "anonymous", "_run_id": "run-explicit"},
            "simulation": {
                "source": "RadarRR",
                "mounting_position": "CRR",
                "auto_detect_radar": False,
            },
        },
        str(mf4),
    )
    assert result["source"] == "RadarRR"
    assert result["mounting_position"] == "CRR"


def test_light_agent_mf4_reader_preserves_acquisition_group_order(tmp_path: Path):
    """The light Agent can infer RadarRL without installing asammdf."""

    import struct

    mf4 = tmp_path / "recording.MF4"
    content = bytearray(0x500)

    def put_text(address: int, text: str) -> None:
        raw = text.encode("utf-8") + b"\0"
        _MF4_COMMON.pack_into(content, address, b"##TX", 0, 24 + len(raw), 0)
        content[address + 24 : address + 24 + len(raw)] = raw

    # Header -> one data group -> two channel groups.  The source blocks are
    # deliberately placed in the opposite byte order to prove that the
    # linked-list order, not a raw byte scan, selects the first source.
    _MF4_HEADER.pack_into(
        content,
        0x40,
        b"##HD",
        0,
        104,
        6,
        0x100,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    _MF4_DATA_GROUP.pack_into(content, 0x100, b"##DG", 0, 64, 4, 0, 0x180, 0, 0, 0, b"\0" * 7)
    def put_channel_group(address: int, next_address: int, source_address: int) -> None:
        _MF4_CHANNEL_GROUP.pack_into(
            content,
            address,
            b"##CG",
            0,
            104,
            6,
            next_address,
            0,
            0,
            source_address,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )

    put_channel_group(0x180, 0x220, 0x280)
    put_channel_group(0x220, 0, 0x2C0)
    _MF4_SOURCE_INFORMATION.pack_into(content, 0x280, b"##SI", 0, 56, 3, 0, 0x3C0, 0, 1, 7, 0, b"\0" * 5)
    _MF4_SOURCE_INFORMATION.pack_into(content, 0x2C0, b"##SI", 0, 56, 3, 0, 0x380, 0, 1, 7, 0, b"\0" * 5)
    put_text(0x380, "RadarRR")
    put_text(0x3C0, "RadarRL")
    mf4.write_bytes(content)

    assert _discover_mf4_acquisition_sources_stdlib(str(mf4)) == ["RadarRL", "RadarRR"]


def test_sdk_bypasses_environment_proxy_for_private_control_plane():
    assert _trust_environment_proxy("http://10.190.171.44:8877") is False
    assert _trust_environment_proxy("http://127.0.0.1:8877") is False
    assert _trust_environment_proxy("http://[::1]:8877") is False
    assert _trust_environment_proxy("https://public.example.com") is True


def test_sdk_selects_unified_connector_for_local_simulation(tmp_path, monkeypatch):
    sdk, _ = make_sdk(tmp_path)
    config = run_config_dict()
    config["simulation"]["target"] = "local"
    seen = []
    monkeypatch.setattr(
        sdk,
        "download_windows_connector",
        lambda destination, *, mode="light": seen.append((Path(destination), mode))
        or Path(destination) / "RadarSim-Connect-Windows.cmd",
    )

    target = sdk.download_windows_connector_for_run(config, tmp_path)

    assert target.name == "RadarSim-Connect-Windows.cmd"
    assert seen == [(tmp_path, "unified")]


def test_sdk_selects_unified_connector_for_cluster_simulation(tmp_path, monkeypatch):
    sdk, _ = make_sdk(tmp_path)
    config = run_config_dict()
    config["simulation"]["target"] = "cluster"
    seen = []
    monkeypatch.setattr(
        sdk,
        "download_windows_connector",
        lambda destination, *, mode="light": seen.append((Path(destination), mode))
        or Path(destination) / "RadarSim-Connect-Windows.cmd",
    )

    sdk.download_windows_connector_for_run(config, tmp_path)

    assert seen == [(tmp_path, "unified")]


def test_sdk_auto_connector_mode_uses_same_resolved_target_as_web(tmp_path, monkeypatch):
    sdk, _ = make_sdk(tmp_path)
    config = run_config_dict()
    config["simulation"]["target"] = "auto"
    seen = []
    monkeypatch.setattr(
        sdk,
        "validate_run",
        lambda _config: SimpleNamespace(execution={"selected_target": "local"}),
    )
    monkeypatch.setattr(
        sdk,
        "download_windows_connector",
        lambda destination, *, mode="light": seen.append((Path(destination), mode))
        or Path(destination) / "RadarSim-Connect-Windows.cmd",
    )

    sdk.download_windows_connector_for_run(config, tmp_path)

    assert seen == [(tmp_path, "unified")]


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
    # Waiting/route details are owned by the control-plane resolver.  The SDK
    # must preserve the same user YAML and never manufacture an upload session.
    assert job.spec["data"] == config.to_dict()["data"]


def test_sdk_submit_yaml_accepts_every_user_run_combination(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    config = UserRunConfig.from_dict(run_config_dict())
    yaml_path = tmp_path / "simulation.yaml"
    yaml_path.write_text(config.to_yaml(), encoding="utf-8")

    job = sdk.submit_yaml(yaml_path, dry_run=True, idempotency_key="generic-yaml")

    assert job.status == "succeeded"
    assert job.spec == config.to_dict()
    assert job.type == "simulation.run_config.v2.dry_run"


def test_sdk_submit_run_keeps_local_data_path_for_direct_transfer(tmp_path, monkeypatch):
    sdk, _ = make_sdk(tmp_path)
    data = tmp_path / "measurements"
    data.mkdir()
    (data / "one.MF4").write_bytes(b"mf4")
    config = run_config_dict()
    config["data"] = {"path": str(data)}
    config["simulation"]["target"] = "cluster"
    monkeypatch.setattr(sdk, "upload_run_data", lambda *_: pytest.fail("legacy data upload"))

    job = sdk.submit_run(config)

    assert job.spec["data"] == {"path": data.as_posix()}
    assert "project" not in job.spec


def test_sdk_keeps_readable_local_path_even_when_posix_syntax_is_central(tmp_path, monkeypatch):
    sdk, _ = make_sdk(tmp_path)
    data = tmp_path / "linux-local"
    data.mkdir()
    (data / "one.MF4").write_bytes(b"mf4")
    config = run_config_dict()
    config["data"] = {"path": data.as_posix()}
    config["simulation"]["target"] = "cluster"
    monkeypatch.setattr(
        "radar_sim_sdk.client.classify_data_path",
        lambda _path: "central",
    )
    monkeypatch.setattr(
        "radar_sim_sdk.client._is_separate_mount",
        lambda _path: False,
    )
    monkeypatch.setattr(sdk, "upload_run_data", lambda *_: pytest.fail("legacy data upload"))

    job = sdk.submit_run(config)

    assert job.spec["data"]["path"] == data.as_posix()


def test_sdk_keeps_readable_cluster_mount_without_upload(tmp_path, monkeypatch):
    sdk, _ = make_sdk(tmp_path)
    mounted_share = tmp_path / "cluster-mount"
    mounted_share.mkdir()
    (mounted_share / "one.MF4").write_bytes(b"mf4")
    config = run_config_dict()
    config["data"] = {"path": mounted_share.as_posix()}
    config["simulation"]["target"] = "cluster"
    monkeypatch.setattr(
        "radar_sim_sdk.client.classify_data_path",
        lambda _path: "central",
    )
    monkeypatch.setattr(
        "radar_sim_sdk.client._is_separate_mount",
        lambda _path: True,
    )
    monkeypatch.setattr(
        sdk,
        "upload_run_data",
        lambda _source: pytest.fail("Cluster mount must remain a direct path"),
    )

    job = sdk.submit_run(config)

    assert job.spec["data"]["path"] == mounted_share.as_posix()


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


def test_sdk_submit_run_preserves_local_data_and_configuration_assets_for_direct_transfer(tmp_path, monkeypatch):
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
    monkeypatch.setattr(sdk, "upload_run_data", lambda *_: pytest.fail("legacy data upload"))
    monkeypatch.setattr(sdk, "upload_config_asset", lambda *_: pytest.fail("legacy asset upload"))

    job = sdk.submit_run(config)

    assert job.spec["data"]["path"] == data.as_posix()
    assert job.spec["simulation"]["mat_filter"] == mat_filter.as_posix()
    assert job.spec["simulation"]["adapter_file"] == adapter.as_posix()


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
    diagnosis = sdk.diagnosis(job.id)
    assert diagnosis.job_id == job.id
    assert diagnosis.outcome == "pending"
    assert diagnosis.code == "job_queued"
    assert diagnosis.action["type"] == "wait_job"


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


def test_sdk_direct_transfer_adapter_uses_metadata_only_control_requests(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.MF4").write_bytes(b"mf4")
    target = tmp_path / "cluster-target"
    target.mkdir()
    transfer_id = generate_opaque_id()
    owner_scope = generate_owner_scope("alice", "job-direct")
    item = TransferPlanItem(
        source_role="dataset",
        relative_path="one.MF4",
        size=3,
        checksum="" + __import__("hashlib").sha256(b"mf4").hexdigest(),
        mtime_ns=(source / "one.MF4").stat().st_mtime_ns,
    )
    plan = TransferPlan(
        transfer_id=transfer_id,
        owner_scope=owner_scope,
        job_id="job-direct",
        stage_id="stage-data",
        mode="shared_copy",
        source_role="dataset",
        client_target_root=str(target),
        relative_root=build_isolated_relative_root(owner_scope, "job-direct", transfer_id),
        items=(item,),
        expires_at=10_000_000_000,
        owner="alice",
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        if request.url.path.endswith("/transfers"):
            payload = __import__("json").loads(request.content.decode("utf-8"))
            assert set(payload) == {"source_role", "items", "source_fingerprints"}
            assert "source_root" not in payload and "client_target_root" not in payload
            return httpx.Response(200, json={"plan": plan.to_dict()})
        if request.url.path.endswith("/manifest"):
            # The target bytes must never be present in the control request.
            assert len(request.content) < 4096
            return httpx.Response(200, json={"status": "completed"})
        if request.url.path.endswith("/progress"):
            return httpx.Response(200, json={"status": "in_progress"})
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    sdk = RadarSimClient(
        "http://testserver",
        user="alice",
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )
    issued = sdk.issue_transfer_plan(
        job_id="job-direct",
        stage_id="stage-data",
        mode="shared_copy",
        source_role="dataset",
        items=[item.to_dict()],
    )
    manifest = sdk.execute_transfer_plan(issued, source, allow_local_test=True)

    destination = target / plan.relative_root / "one.MF4"
    assert destination.read_bytes() == b"mf4"
    assert manifest.entries[0].storage_ref.startswith("cluster-staging://v1/")
    assert all(b"mf4" not in body for _, _, body in seen)


def test_sdk_direct_transfer_throttles_http_but_not_local_callback(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    payload = b"x" * 100
    path = source / "large.MF4"
    path.write_bytes(payload)
    target = tmp_path / "cluster-target"
    target.mkdir()
    transfer_id = generate_opaque_id()
    owner_scope = generate_owner_scope("alice", "job-throttle-sdk")
    item = TransferPlanItem(
        source_role="dataset",
        relative_path=path.name,
        size=len(payload),
        checksum=__import__("hashlib").sha256(payload).hexdigest(),
        mtime_ns=path.stat().st_mtime_ns,
    )
    plan = TransferPlan(
        transfer_id=transfer_id,
        owner_scope=owner_scope,
        job_id="job-throttle-sdk",
        stage_id="stage-throttle-sdk",
        mode="shared_copy",
        source_role="dataset",
        client_target_root=str(target),
        relative_root=build_isolated_relative_root(owner_scope, "job-throttle-sdk", transfer_id),
        items=(item,),
        expires_at=10_000_000_000,
        owner="alice",
    )
    seen: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        if request.url.path.endswith("/progress"):
            return httpx.Response(200, json={"status": "in_progress"})
        if request.url.path.endswith("/manifest"):
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    sdk = RadarSimClient(
        "http://testserver",
        user="alice",
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )
    local = []
    manifest = sdk.execute_transfer_plan(
        plan,
        source,
        progress_callback=local.append,
        chunk_size=1,
        allow_local_test=True,
    )

    progress = [
        __import__("json").loads(body.decode("utf-8"))
        for _method, path_name, body in seen
        if path_name.endswith("/progress")
    ]
    assert manifest.total_bytes == len(payload)
    assert len(local) == len(payload)
    assert len(progress) < len(local)
    assert progress[0]["bytes_transferred"] == 1
    assert progress[-1]["bytes_transferred"] == len(payload)


def test_sdk_run_preparation_does_not_create_legacy_linux_uploads(tmp_path, monkeypatch):
    sdk, _ = make_sdk(tmp_path)
    config = UserRunConfig.from_dict(run_config_dict())
    monkeypatch.setattr(sdk, "upload_run_data", lambda *_: pytest.fail("legacy data upload"))
    monkeypatch.setattr(sdk, "upload_config_asset", lambda *_: pytest.fail("legacy asset upload"))
    monkeypatch.setattr(sdk, "_upload_existing_selena", lambda *_args, **_kwargs: pytest.fail("legacy Selena upload"))

    payload, prepared = sdk._prepare_user_run(config, dry_run=False)

    assert prepared == ""
    assert payload == config.to_dict()


def test_sdk_submit_run_auto_transfers_existing_selena_dataset_and_assets(tmp_path, monkeypatch):
    """One SDK call executes every local role through metadata-only routes."""

    data = tmp_path / "measurements"
    data.mkdir()
    sentinel = b"SDK_DIRECT_TRANSFER_SENTINEL_" * 32
    (data / "one.MF4").write_bytes(sentinel)
    selena = tmp_path / "selena"
    (selena / "nested").mkdir(parents=True)
    (selena / "Selena.exe").write_bytes(b"SELENA_EXE_" + sentinel)
    (selena / "nested" / "Selena.dll").write_bytes(b"SELENA_DLL_" + sentinel)
    runtime_xml = tmp_path / "Runtime.xml"
    runtime_xml.write_bytes(b"<Runtime>SDK_DIRECT_TRANSFER</Runtime>")
    mat_filter = tmp_path / "signals.filter"
    mat_filter.write_bytes(b"mat_filter=SDK_DIRECT_TRANSFER")
    adapter = tmp_path / "adapter.txt"
    adapter.write_bytes(b"adapter=SDK_DIRECT_TRANSFER")
    target = tmp_path / "cluster-target"
    target.mkdir()
    monkeypatch.setattr(
        "core.simulation.detect_radar_transfer_metadata_safe",
        lambda _path: {"radar_source": "RadarRL", "radar_mounting_position": "CRL"},
    )

    config = run_config_dict()
    config["selena"] = {
        "source": "existing",
        "existing_path": str(selena),
        "runtime_xml": str(runtime_xml),
    }
    config["data"] = {"path": str(data)}
    config["simulation"] = {
        "target": "cluster",
        "adapter_file": str(adapter),
        "mat_filter": str(mat_filter),
    }
    plans: dict[str, TransferPlan] = {}
    submitted_fingerprints: dict[str, dict] = {}
    progress: list[tuple[str, dict]] = []
    manifests: list[dict] = []
    forbidden_body = sentinel

    def job_payload(*, terminal: bool = False) -> dict:
        return {
            "id": "job-sdk-direct",
            "job_id": "job-sdk-direct",
            "type": "simulation.run_config.v2",
            "status": "succeeded" if terminal else "queued",
            "spec": config,
            "resolved_spec": {"decisions": {"execution": {"selected_target": "cluster"}}},
            "stages": [
                {
                    "stage_id": "stage-sdk-transfer",
                    "task_id": "stage-sdk-transfer",
                    "stage_type": "prepare_data",
                    "status": "succeeded" if terminal else "queued",
                }
            ],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        assert forbidden_body not in body
        path = request.url.path
        if path == "/api/v1/run-jobs":
            return httpx.Response(201, json=job_payload())
        if path.endswith("/transfers") and "/stages/" in path:
            payload = __import__("json").loads(body.decode("utf-8"))
            assert set(payload) == {"source_role", "items", "source_fingerprints"}
            role = str(payload["source_role"])
            submitted_fingerprints[role] = dict(payload["source_fingerprints"])
            transfer_id = generate_opaque_id(prefix=role)
            owner_scope = generate_owner_scope("alice", "job-sdk-direct")
            items = tuple(TransferPlanItem.from_dict(item) for item in payload["items"])
            plan = TransferPlan(
                transfer_id=transfer_id,
                owner_scope=owner_scope,
                job_id="job-sdk-direct",
                stage_id="stage-sdk-transfer",
                mode="shared_copy",
                source_role=role,
                client_target_root=str(target),
                relative_root=build_isolated_relative_root(owner_scope, "job-sdk-direct", transfer_id),
                items=items,
                expires_at=time.time() + 3600,
                owner="alice",
            )
            plans[role] = plan
            return httpx.Response(201, json={"plan": plan.to_dict()})
        if path.endswith("/progress"):
            progress.append((path.rsplit("/", 2)[-2], __import__("json").loads(body.decode("utf-8"))))
            return httpx.Response(200, json={"status": "in_progress"})
        if path.endswith("/manifest"):
            manifests.append(__import__("json").loads(body.decode("utf-8")))
            return httpx.Response(200, json={"status": "completed"})
        if path == "/api/v1/jobs/job-sdk-direct":
            return httpx.Response(200, json=job_payload(terminal=True))
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    sdk = RadarSimClient(
        "http://testserver",
        user="alice",
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )
    job = sdk.submit_run(config, allow_local_test=True)

    assert job.status == "succeeded"
    assert set(plans) == {"dataset", "runtime_bundle", "runtime_xml", "mat_filter", "adapter"}
    assert submitted_fingerprints["dataset"]["radar_source"] == "RadarRL"
    assert submitted_fingerprints["dataset"]["radar_mounting_position"] == "CRL"
    assert {item[0] for item in progress} == {
        plan.transfer_id for plan in plans.values()
    }
    assert {item["owner_scope"] for item in manifests} == {
        plan.owner_scope for plan in plans.values()
    }
    assert len(manifests) == len(plans)
    assert (target / plans["dataset"].relative_root / "one.MF4").read_bytes() == sentinel
    assert {
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.is_file()
    } >= {
        (plans["runtime_bundle"].relative_root + "/Selena.exe"),
        (plans["runtime_bundle"].relative_root + "/nested/Selena.dll"),
    }


def test_sdk_capabilities_and_job_transfer_status_are_public():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/v1/capabilities":
            return httpx.Response(
                200,
                json={
                    "capabilities": {"cluster": {"available": True}},
                    "observed_at": 123.0,
                },
            )
        if request.url.path == "/api/v1/jobs/job-status/transfers":
            return httpx.Response(
                200,
                json={
                    "job_id": "job-status",
                    "status": "transfer_completed",
                    "plan_count": 1,
                    "plans": [{"transfer_id": "transfer-1", "status": "completed"}],
                },
            )
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    sdk = RadarSimClient(
        "http://testserver",
        user="alice",
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )

    assert sdk.capabilities()["capabilities"]["cluster"]["available"] is True
    status = sdk.get_job_transfer_status("job-status")
    assert status["status"] == "transfer_completed"
    assert status["plans"][0]["status"] == "completed"
    assert seen == ["/api/v1/capabilities", "/api/v1/jobs/job-status/transfers"]


def test_sdk_prepare_direct_transfers_retries_until_prepare_stage_is_visible(monkeypatch):
    config = run_config_dict()
    config["data"] = {"path": "C:/local/measurements"}
    pending = Job.from_dict(
        {
            "id": "job-stage-retry",
            "status": "queued",
            "resolved_spec": {"decisions": {"execution": {"selected_target": "cluster"}}},
            "stages": [],
        }
    )
    ready = Job.from_dict(
        {
            "id": "job-stage-retry",
            "status": "queued",
            "resolved_spec": {"decisions": {"execution": {"selected_target": "cluster"}}},
            "stages": [{"stage_id": "stage-data", "stage_type": "prepare_data", "status": "queued"}],
        }
    )
    responses = iter([pending, ready])
    fetched: list[str] = []
    sdk = RadarSimClient("http://testserver", transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
    monkeypatch.setattr(sdk, "get_job", lambda job_id: fetched.append(job_id) or next(responses))
    monkeypatch.setattr(sdk, "_auto_prepare_direct_transfers", lambda job, *_args, **_kwargs: job)

    resumed = sdk.prepare_direct_transfers(
        "job-stage-retry", config, retries=2, retry_interval=0
    )

    assert resumed == ready
    assert fetched == ["job-stage-retry", "job-stage-retry"]


def test_sdk_prepare_direct_transfers_shared_inputs_are_a_noop():
    config = run_config_dict()
    job = Job.from_dict(
        {
            "id": "job-shared-noop",
            "status": "queued",
            "resolved_spec": {"decisions": {"execution": {"selected_target": "cluster"}}},
            "stages": [
                {
                    "stage_id": "stage-shared-noop",
                    "stage_type": "prepare_data",
                    "status": "queued",
                }
            ],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"shared-only direct transfer made HTTP call: {request.url}")

    sdk = RadarSimClient(
        "http://testserver",
        user="alice",
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )
    assert sdk.prepare_direct_transfers(job, config) == job
