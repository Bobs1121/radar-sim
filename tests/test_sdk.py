import pytest
import hashlib
import httpx
import time
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.api_v1_fastapi import create_app
from core.control_service import ControlService
from core.direct_transfer import (
    TransferCancelled,
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
from radar_sim_sdk.errors import RadarSimTransportError
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
from radar_sim_sdk import (
    ArtifactUpload,
    ArtifactUploadResult,
    Job,
    RadarSimApiError,
    RadarSimClient,
    RadarSimIntegrityError,
    RadarSimTransferCancelledError,
    PartialUserRunConfig,
    UserRunConfig,
)
from radar_sim_sdk.client import (
    _dataset_transfer_fingerprints,
    _is_retryable_direct_transfer_exception,
    _is_windows_physical_path,
    _resolved_direct_transfer_roles,
    _sdk_local_transfer_sources,
    _trust_environment_proxy,
)
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


def test_sdk_validate_and_submit_run_share_v2_hash_with_web_json(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    config = UserRunConfig.from_dict(run_config_dict())

    validation = sdk.validate_run(config)
    job = sdk.submit_run(config, dry_run=True, idempotency_key="sdk-key")

    assert validation.fingerprint == config.fingerprint()
    assert job.spec_hash == validation.fingerprint
    assert len(job.stages) == 10
    assert job.resolved_spec["status"] == "planned"
    assert job.spec == config.to_dict()
    submitted = sdk.submit_run(config, dry_run=True, idempotency_key="sdk-key")
    assert submitted.id == job.id
    assert submitted.job_id == job.id


def test_sdk_partial_yaml_import_export_is_separate_from_strict_submit_validation(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    partial_yaml = """
selena:
  source: build
  code_path: D:/workspace/byd
"""

    imported = sdk.import_yaml(partial_yaml)
    assert imported["valid"] is True
    assert imported["complete"] is False
    assert imported["config"]["selena"]["code_path"] == "D:/workspace/byd"
    assert "data.path" in imported["missing_fields"]

    exported = sdk.export_yaml(imported["config"])
    assert exported["complete"] is False
    assert "code_path: D:/workspace/byd" in exported["yaml_content"]
    exported_model = sdk.export_yaml(
        PartialUserRunConfig.from_dict(imported["config"])
    )
    assert exported_model["complete"] is False

    with pytest.raises(ValidationError):
        sdk.validate_run(imported["config"])


def test_sdk_downloads_one_time_windows_connector_for_current_scope(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    target = sdk.download_windows_connector(tmp_path)

    assert target.name == "RadarSim-Connect-Windows.cmd"
    content = target.read_text(encoding="utf-8")
    assert "install.ps1?mode=" in content
    assert "RSIM_CONNECTOR_MODE=unified" in content
    assert "__RSIM_SERVER_URL_BASE64__" not in content
    assert "__RSIM_OWNER_BASE64__" not in content

    with pytest.raises(ValueError, match="unified"):
        sdk.download_windows_connector(tmp_path, mode="light")


def test_sdk_exposes_web_readiness_and_public_run_config_schema(tmp_path):
    sdk, _ = make_sdk(tmp_path)

    readiness = sdk.cluster_readiness()
    schema = sdk.user_run_config_schema()

    assert readiness["ready"] is False
    assert readiness["code"] == "cluster_readiness_unavailable"
    assert schema["title"]
    assert "selena" in schema["properties"]
    assert sdk.run_config_schema() == schema


def test_sdk_reads_exact_windows_connector_status():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "agent_id": "agent-1",
                "configured": True,
                "available": True,
                "contract_current": True,
                "reason": "",
            },
        )

    sdk = RadarSimClient(
        "http://testserver",
        user="alice",
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )

    status = sdk.windows_connector_status("agent-1")

    assert status["available"] is True
    assert "agent_id=agent-1" in seen[0]


def test_sdk_downloads_and_verifies_agent_tools_bundle_and_installer(tmp_path):
    import json

    bundle = b"agent-tools-bundle"
    checksum = "sha256:" + hashlib.sha256(bundle).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/agent-tools/manifest":
            return httpx.Response(
                200,
                json={
                    "release_version": "4.0.0-agent.1",
                    "bundle": {"sha256": checksum, "size": len(bundle)},
                },
            )
        if request.url.path == "/api/v1/agent-tools/package.zip":
            return httpx.Response(200, content=bundle)
        if request.url.path == "/api/v1/agent-tools/install.ps1":
            return httpx.Response(200, text="Write-Output radar-sim")
        if request.url.path == "/api/v1/agent-tools/install.py":
            return httpx.Response(200, text="print('radar-sim')")
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    sdk = RadarSimClient(
        "http://testserver",
        user="alice",
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )

    installer = sdk.download_agent_tools_installer(tmp_path)
    bootstrap = sdk.download_agent_tools_bootstrap(tmp_path)
    downloaded = sdk.download_agent_tools_bundle(tmp_path)

    assert installer.read_text(encoding="utf-8") == "Write-Output radar-sim"
    assert bootstrap.read_text(encoding="utf-8") == "print('radar-sim')"
    assert downloaded.read_bytes() == bundle
    json.dumps(sdk.agent_tools_manifest())


def test_sdk_preserves_agent_tools_identity_for_bootstrap_process():
    sdk = RadarSimClient(
        "http://testserver",
        user="alice",
        token="secret-token",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
        trust_env=False,
    )

    environment = sdk._agent_tools_bootstrap_environment()

    assert environment == {
        "RADAR_SIM_TOKEN": "secret-token",
        "RADAR_SIM_USER": "user-alice",
    }


def test_sdk_rejects_corrupt_agent_tools_bundle(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/manifest"):
            return httpx.Response(
                200,
                json={"bundle": {"sha256": "sha256:" + "a" * 64, "size": 5}},
            )
        if request.url.path.endswith("/package.zip"):
            return httpx.Response(200, content=b"wrong")
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    sdk = RadarSimClient(
        "http://testserver",
        user="alice",
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )

    with pytest.raises(RadarSimIntegrityError) as caught:
        sdk.download_agent_tools_bundle(tmp_path)

    assert getattr(caught.value, "code", "") == "agent_tools_checksum_mismatch"
    assert not list(tmp_path.glob("*.part.*"))


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


def test_front_radar_acquisition_source_maps_to_mature_front_mounting(
    monkeypatch, tmp_path: Path
):
    mf4 = tmp_path / "front-recording.MF4"
    mf4.write_bytes(b"mf4")
    monkeypatch.setattr(
        "core.simulation.discover_radar_acquisition_sources",
        lambda _path: ["RadarFC"],
    )
    result = build_effective_simulation(
        {"_meta": {"project": "anonymous", "_run_id": "run-front"}, "simulation": {}},
        str(mf4),
    )
    assert result["source"] == "RadarFC"
    assert result["mounting_position"] == "front"
    assert result["radar_detection"]["method"] == "acquisition_source"
    assert detect_radar_transfer_metadata(str(mf4)) == {
        "radar_source": "RadarFC",
        "radar_mounting_position": "front",
    }


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


def test_sdk_submit_yaml_accepts_yaml_text_for_mcp_callers(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    config = UserRunConfig.from_dict(run_config_dict())

    job = sdk.submit_yaml(config.to_yaml(), dry_run=True, idempotency_key="yaml-text")

    assert job.spec == config.to_dict()
    assert job.spec_hash == config.fingerprint()


def test_sdk_submit_run_keeps_local_data_path_for_direct_transfer(tmp_path, monkeypatch):
    sdk, _ = make_sdk(tmp_path)
    data = tmp_path / "measurements"
    data.mkdir()
    (data / "one.MF4").write_bytes(b"mf4")
    config = run_config_dict()
    config["data"] = {"path": str(data)}
    config["simulation"]["target"] = "cluster"
    job = sdk.submit_run(config)

    assert job.spec["data"] == {"path": data.as_posix()}
    assert "project" not in job.spec


def test_sdk_local_mat_filter_discovery_does_not_change_web_config_fingerprint(tmp_path):
    sdk, services = make_sdk(tmp_path)
    web_client = TestClient(
        create_app(control_service_factory=lambda owner: services.setdefault(
            owner, ControlService(tmp_path / f"{owner}.db")
        ))
    )
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    filter_path = (
        repository
        / "tools"
        / "selena"
        / "matlab_transport_cfg"
        / "matlab_swx_plotreco.mdf.mat.filter"
    )
    filter_path.parent.mkdir(parents=True)
    filter_path.write_text("filter", encoding="utf-8")
    build_script = repository / "build_selena.bat"
    build_script.write_text("@echo off\n", encoding="utf-8")
    runtime_xml = repository / "Runtime.xml"
    runtime_xml.write_text("<Runtime />", encoding="utf-8")

    config_payload = run_config_dict()
    config_payload["selena"].update(
        {
            "code_path": str(repository),
            "selena_build_script": str(build_script),
            "runtime_xml": str(runtime_xml),
        }
    )
    config_payload["simulation"]["mat_filter"] = ""
    config = UserRunConfig.from_dict(config_payload)

    web_validation = web_client.post(
        "/api/v1/run-configs/validate",
        json=config.to_dict(),
        headers={"X-Rsim-User": "alice"},
    ).json()
    validation = sdk.validate_run(config)
    dry_run = sdk.submit_run(config, dry_run=True, idempotency_key="mat-filter-parity")

    # The Web request keeps the optional field empty, so the canonical hash
    # and Job spec must remain unchanged even though the SDK can read the
    # inferred file for a later direct-transfer plan.
    assert web_validation["config"] == config.to_dict()
    assert validation.config.to_dict() == web_validation["config"]
    assert validation.fingerprint == web_validation["fingerprint"]
    assert dry_run.spec == config.to_dict()
    assert dry_run.spec_hash == config.fingerprint()
    assert ("mat_filter", filter_path.resolve()) in _sdk_local_transfer_sources(config)


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
    job = sdk.submit_run(config)

    assert job.spec["data"]["path"] == readable_share.as_posix()


def test_sdk_readable_windows_unc_is_eligible_for_direct_transfer(tmp_path, monkeypatch):
    source = tmp_path / "unc-mounted-data"
    source.mkdir()
    (source / "one.MF4").write_bytes(b"mf4")
    config_payload = run_config_dict()
    config_payload["data"] = {"path": str(source)}
    config_payload["simulation"]["target"] = "cluster"
    config = UserRunConfig.from_dict(config_payload)
    monkeypatch.setattr(
        "radar_sim_sdk.client.classify_data_path", lambda _value: "shared"
    )
    monkeypatch.setattr(
        "radar_sim_sdk.client._is_windows_physical_path", lambda _value: True
    )

    sources = _sdk_local_transfer_sources(config)

    assert _is_windows_physical_path(r"\\server\share\data") is True
    assert [role for role, _path in sources] == ["dataset"]


def test_sdk_permanent_transfer_api_errors_are_not_waiting():
    assert not _is_retryable_direct_transfer_exception(
        RadarSimApiError("invalid_transfer_item", "bad item", status_code=422),
        code="invalid_transfer_item",
    )
    assert _is_retryable_direct_transfer_exception(
        RadarSimApiError("cluster_direct_transfer_unavailable", "temporary", status_code=503),
        code="cluster_direct_transfer_unavailable",
    )


def test_sdk_transfer_cancellation_is_not_converted_to_waiting(monkeypatch, tmp_path):
    plan = TransferPlan(
        transfer_id="transfer-cancel-test",
        owner_scope="owner-scope-cancel-test",
        job_id="job-cancel-test",
        stage_id="stage-cancel-test",
        mode="shared_copy",
        source_role="dataset",
        client_target_root=str(tmp_path / "target"),
        relative_root=build_isolated_relative_root(
            "owner-scope-cancel-test", "job-cancel-test", "transfer-cancel-test"
        ),
        expires_at=time.time() + 3600,
        items=(),
    )
    monkeypatch.setattr(
        "radar_sim_sdk.client.execute_transfer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TransferCancelled("user stopped the transfer", code="transfer_cancelled")
        ),
    )
    sdk = RadarSimClient("http://testserver", transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})))

    with pytest.raises(RadarSimTransferCancelledError) as caught:
        sdk.execute_transfer_plan(plan, tmp_path)

    assert caught.value.code == "transfer_cancelled"
    assert not _is_retryable_direct_transfer_exception(
        caught.value, code="transfer_cancelled"
    )


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


def test_sdk_artifact_upload_reconciles_a_committed_chunk_after_transport_loss(tmp_path):
    source = tmp_path / "selena.exe"
    source.write_bytes(b"abcdef")
    initial = ArtifactUpload(
        session_id="artifact-session",
        status="pending",
        project="demo",
        publish_path="selena.exe",
        storage_ref="shared://artifact/demo/selena.exe",
        build_evidence_ref="evidence-1",
        expected_size=6,
        expected_checksum="sha256:" + "a" * 64,
        received_bytes=0,
        chunk_size=3,
    )
    observed = {"value": initial}
    append_offsets = []

    sdk = RadarSimClient("http://testserver", transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})))
    sdk.create_artifact_upload = lambda *_args, **_kwargs: initial

    def append(_session_id, offset, data):
        append_offsets.append((offset, data))
        if len(append_offsets) == 1:
            # The server committed the first chunk but the response was lost.
            observed["value"] = ArtifactUpload(**{**initial.__dict__, "received_bytes": 3})
            raise RadarSimTransportError("connection dropped after commit")
        return ArtifactUpload(**{**initial.__dict__, "received_bytes": offset + len(data)})

    sdk.append_artifact_upload = append
    sdk.get_artifact_upload = lambda _session_id: observed["value"]
    sdk.finalize_artifact_upload = lambda session_id: ArtifactUploadResult(
        session=observed["value"], artifact={"id": "artifact-1"}, reused=False
    )

    result = sdk.upload_artifact("evidence-1", source)

    assert result.artifact["id"] == "artifact-1"
    assert append_offsets == [(0, b"abc"), (3, b"def")]


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
        owner="user-alice", run_ref="local-run:one", source_root=source,
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
    assert downloaded.read_bytes() == catalog.resolve_archive(published.ref, owner="user-alice").read_bytes()

    bob = RadarSimClient("http://testserver", client=test_client, user="bob")
    with pytest.raises(RadarSimApiError) as excinfo:
        bob.get_result(published.ref)
    assert excinfo.value.status_code == 404


def test_sdk_download_result_rejects_checksum_mismatch_with_stable_error(tmp_path):
    digest = "0" * 64
    result_ref = "result:sha256:" + digest

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/results/{result_ref}":
            return httpx.Response(200, json={"archive_checksum": "sha256:" + "f" * 64})
        if request.url.path == f"/api/v1/results/{result_ref}/download":
            return httpx.Response(200, content=b"tampered")
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    from radar_sim_sdk.errors import RadarSimIntegrityError

    sdk = RadarSimClient(
        "http://testserver", transport=httpx.MockTransport(handler), user="alice"
    )
    target = tmp_path / "downloads"
    with pytest.raises(RadarSimIntegrityError) as excinfo:
        sdk.download_result(result_ref, target)
    assert excinfo.value.code == "result_checksum_mismatch"
    assert excinfo.value.resource == result_ref
    # The corrupted stream must not leave a usable destination file behind.
    assert not target.exists()
    assert not list(target.parent.glob("*.part.*"))


def test_sdk_download_job_result_follows_manifest_and_explicit_zip_destination(tmp_path):
    archive = b"result-zip"
    checksum = "sha256:" + hashlib.sha256(archive).hexdigest()
    configured = tmp_path / "configured-results"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/jobs/job-result-1":
            return httpx.Response(
                200,
                json={
                    "id": "job-result-1",
                    "status": "succeeded",
                    "spec": {"result": {"path": str(configured)}},
                },
            )
        if request.url.path == "/api/v1/jobs/job-result-1/manifest":
            return httpx.Response(
                200,
                json={
                    "job_id": "job-result-1",
                    "available": True,
                    "manifest": {"result_ref": "result:sha256:abc"},
                },
            )
        if request.url.path == "/api/v1/results/result:sha256:abc":
            return httpx.Response(200, json={"archive_checksum": checksum})
        if request.url.path == "/api/v1/results/result:sha256:abc/download":
            return httpx.Response(200, content=archive)
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    sdk = RadarSimClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
        user="alice",
    )

    downloaded = sdk.download_job_result("job-result-1")

    assert downloaded.parent == configured / "job-result-1"
    assert downloaded.name == "radar-sim-result-" + checksum.removeprefix("sha256:")[:12] + ".zip"
    assert downloaded.read_bytes() == archive

    explicit_destination = tmp_path / "zip-downloads"
    explicit = sdk.download_job_result("job-result-1", explicit_destination)
    assert explicit.parent == explicit_destination
    assert explicit.read_bytes() == archive


def test_sdk_download_job_result_uses_receiver_default_when_result_path_is_empty(tmp_path, monkeypatch):
    archive = b"default-result-zip"
    checksum = "sha256:" + hashlib.sha256(archive).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/jobs/job-default-result":
            return httpx.Response(
                200,
                json={"id": "job-default-result", "status": "succeeded", "spec": {"result": {"path": ""}}},
            )
        if request.url.path == "/api/v1/jobs/job-default-result/manifest":
            return httpx.Response(
                200,
                json={
                    "job_id": "job-default-result",
                    "available": True,
                    "manifest": {"result_ref": "result:sha256:def"},
                },
            )
        if request.url.path == "/api/v1/results/result:sha256:def":
            return httpx.Response(200, json={"archive_checksum": checksum})
        if request.url.path == "/api/v1/results/result:sha256:def/download":
            return httpx.Response(200, content=archive)
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    sdk = RadarSimClient("http://testserver", transport=httpx.MockTransport(handler), user="alice")

    downloaded = sdk.download_job_result("job-default-result")

    assert downloaded.parent == tmp_path / "RadarSim" / "results" / "job-default-result"
    assert downloaded.read_bytes() == archive


@pytest.mark.parametrize("status", ["queued", "running", "failed"])
def test_sdk_download_job_result_classifies_missing_archive(status):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/jobs/job-no-result":
            return httpx.Response(200, json={"id": "job-no-result", "status": status, "spec": {}})
        if request.url.path == "/api/v1/jobs/job-no-result/manifest":
            return httpx.Response(
                200,
                json={"job_id": "job-no-result", "available": False, "manifest": None},
            )
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    sdk = RadarSimClient("http://testserver", transport=httpx.MockTransport(handler), user="alice")
    with pytest.raises(ValueError, match=r"^result_unavailable:"):
        sdk.download_job_result("job-no-result")


def test_sdk_result_download_wraps_stream_transport_error(tmp_path):
    archive = b"result-zip"
    checksum = "sha256:" + hashlib.sha256(archive).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/results/result:sha256:transport":
            return httpx.Response(200, json={"archive_checksum": checksum})
        if request.url.path == "/api/v1/results/result:sha256:transport/download":
            def broken_stream() -> object:
                yield archive[:3]
                raise httpx.ReadError("connection dropped")

            return httpx.Response(200, content=broken_stream())
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    sdk = RadarSimClient("http://testserver", transport=httpx.MockTransport(handler), user="alice")
    with pytest.raises(RadarSimTransportError, match="connection dropped"):
        sdk.download_result("result:sha256:transport", tmp_path / "result.zip")
    assert not (tmp_path / "result.zip").exists()


def test_sdk_result_download_restarts_a_broken_stream(tmp_path, monkeypatch):
    archive = b"retryable-result-zip"
    checksum = "sha256:" + hashlib.sha256(archive).hexdigest()
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/download"):
            attempts["count"] += 1
            if attempts["count"] == 1:
                def broken_stream() -> object:
                    yield archive[:4]
                    raise httpx.ReadError("temporary disconnect")

                return httpx.Response(200, content=broken_stream())
            return httpx.Response(200, content=archive)
        return httpx.Response(200, json={"archive_checksum": checksum})

    monkeypatch.setattr("radar_sim_sdk.client.time.sleep", lambda _seconds: None)
    sdk = RadarSimClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
        user="alice",
    )

    downloaded = sdk.download_result(
        "result:sha256:" + "e" * 64,
        tmp_path / "result.zip",
    )

    assert attempts["count"] == 2
    assert downloaded.read_bytes() == archive


def test_sdk_v2_run_and_task_center_list(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    job = sdk.submit_run(run_config_dict())

    jobs = sdk.list_jobs(status="queued", limit=10)
    assert [item.id for item in jobs] == [job.id]
    assert jobs[0].current_stage == "resolve_spec"
    assert jobs[0].progress == 0.1
    assert jobs[0].available_actions[0]["type"] == "cancel_job"
    diagnosis = sdk.diagnosis(job.id)
    assert diagnosis.job_id == job.id
    assert diagnosis.outcome == "pending"
    assert diagnosis.code == "job_queued"
    assert diagnosis.action["type"] == "wait_job"


def test_sdk_has_no_legacy_simulation_spec_entrypoints(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    assert not hasattr(sdk, "validate")
    assert not hasattr(sdk, "submit")


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
    job = sdk.submit_run(run_config_dict())
    task_id = job.tasks[0]["task_id"]
    services["user-alice"].append_logs(task_id, ["line-1"])

    streamed = list(sdk.stream_events(job.id))
    assert [event.message for event in streamed if event.event == "log"] == ["line-1"]
    cursor = max(event.id for event in streamed if event.id is not None)

    services["user-alice"].append_logs(task_id, ["line-2"])
    cancelled = sdk.cancel(job.id)
    assert cancelled.status == "cancelled"

    watched = list(sdk.watch(job.id, cursor=cursor, timeout=2.0, poll_interval=0.01))
    assert [event.message for event in watched if event.event == "log"] == ["line-2"]
    assert sdk.wait(job.id, timeout=2.0, poll_interval=0.01).status == "cancelled"

    manifest = sdk.manifest(job.id)
    assert manifest.available is False
    assert manifest.manifest is None


def test_sdk_wait_job_is_the_documented_adaptive_wait_entry_point(tmp_path):
    sdk, services = make_sdk(tmp_path)
    job = sdk.submit_run(run_config_dict())
    services["user-alice"].cancel_job(job.id)

    terminal = sdk.wait_job(job.id, timeout=2.0, poll_interval=0.01)
    assert terminal.status == "cancelled"


def test_sdk_wait_until_actionable_returns_connector_wait_without_hanging(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    job = sdk.submit_run(run_config_dict())

    actionable = sdk.wait_until_actionable(job.id, timeout=2.0, poll_interval=0.01)

    assert actionable.id == job.id
    assert actionable.needs_input is True
    assert actionable.waiting["reason"] == "windows_connection_required"
    assert actionable.waiting["action"]["type"] == "connect_windows"


def test_sdk_typed_models_are_json_safe_for_agent_adapters(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    job = sdk.submit_run(run_config_dict(), dry_run=True, idempotency_key="json-safe")
    validation = sdk.validate_run(run_config_dict())

    import json

    json.dumps(job.to_dict())
    json.dumps(validation.to_dict())
    assert job.to_dict()["job_id"] == job.id
    assert validation.to_dict()["config"] == validation.config.to_dict()


def test_sdk_watch_backoff_grows_delay_on_repeated_transport_errors(monkeypatch):
    from radar_sim_sdk.client import RadarSimClient

    sdk = RadarSimClient(
        "http://testserver",
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(httpx.ConnectError("down"))
        ),
        user="alice",
    )
    sleeps: list[float] = []
    monkeypatch.setattr(sdk.watch.__globals__["time"], "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(TimeoutError):
        list(sdk.watch("job-x", timeout=0.2, poll_interval=0.01, backoff_factor=2.0, max_poll_interval=0.08))

    # Backoff grows the transport-error delay across repeated failures.
    assert len(sleeps) >= 2
    assert sleeps[-1] > sleeps[0]


def test_sdk_watch_backoff_delay_is_bounded_by_max_poll_interval():
    from radar_sim_sdk.client import RadarSimClient

    delay = RadarSimClient._next_poll_delay(
        poll_interval=1.0, cap=8.0, factor=2.0, consecutive_errors=10, deadline=float("inf")
    )
    # 1.0 * 2**9 = 512, capped at 8.0.
    assert delay == 8.0
    no_backoff = RadarSimClient._next_poll_delay(
        poll_interval=1.0, cap=8.0, factor=0.0, consecutive_errors=10, deadline=float("inf")
    )
    # Default backoff_factor=0 keeps the original fixed poll interval.
    assert no_backoff == 1.0


def test_sdk_structured_event_fields_and_retry_stage(tmp_path):
    sdk, services = make_sdk(tmp_path)
    job = sdk.submit_run(run_config_dict())
    stage_id = job.stages[0]["stage_id"]
    services["user-alice"].report_stage_progress(stage_id, progress=0.5, message="half", code="P50")
    page = sdk.events(job.id)
    progress_event = next(event for event in page.events if event.event == "stage.progress")
    assert progress_event.stage_id == stage_id
    assert progress_event.status == "queued"
    assert progress_event.progress == 0.5
    assert progress_event.code == "P50"

    services["user-alice"].register_internal_agent("scheduler", agent_id="__v1_scheduler__", capabilities=["*"])
    claimed = services["user-alice"].claim_next_task("__v1_scheduler__")
    services["user-alice"].submit_task_result(claimed["stage_id"], agent_id="__v1_scheduler__", status="failed", returncode=1)
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


def test_sdk_watch_retries_transient_api_poll_errors(monkeypatch):
    state = {"stream": 0, "events": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "stream=true" in url:
            state["stream"] += 1
            if state["stream"] == 1:
                return httpx.Response(503, json={"code": "service_unavailable", "message": "retry"})
            return httpx.Response(200, text="", headers={"content-type": "text/event-stream"})
        state["events"] += 1
        if state["events"] == 1:
            return httpx.Response(503, json={"code": "service_unavailable", "message": "retry"})
        return httpx.Response(
            200,
            json={"job_id": "job-api-retry", "status": "cancelled", "events": [], "next_cursor": 0, "terminal": True},
        )

    monkeypatch.setattr("radar_sim_sdk.client.time.sleep", lambda _seconds: None)
    sdk = RadarSimClient(
        "http://testserver",
        transport=httpx.MockTransport(handler),
        user="alice",
        trust_env=False,
    )

    assert list(sdk.watch("job-api-retry", timeout=1.0, poll_interval=0.01)) == []
    assert state == {"stream": 2, "events": 2}


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


def test_sdk_retry_skips_roles_with_durable_transfer_manifests(tmp_path):
    sdk, _ = make_sdk(tmp_path)
    job = Job(
        id="job-resume",
        status="running",
        resolved_spec={
            "decisions": {
                "transfers": {
                    "resources": {
                        "dataset": {
                            "status": "resolved",
                            "transfer_id": "transfer-dataset",
                        },
                        "runtime_xml": [
                            {"status": "pending", "transfer_id": "transfer-old"},
                            {"status": "resolved", "transfer_id": "transfer-runtime"},
                        ],
                        "mat_filter": {"status": "failed"},
                    }
                }
            }
        },
    )

    assert _resolved_direct_transfer_roles(job) == {"dataset", "runtime_xml"}


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


def test_sdk_retries_transient_transport_errors_for_idempotent_reads(monkeypatch):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.RemoteProtocolError("server disconnected", request=request)
        return httpx.Response(
            200,
            json={"id": "job-read-retry", "status": "running", "stages": []},
        )

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    sdk = RadarSimClient(
        "http://testserver",
        user="alice",
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )

    assert sdk.get_job("job-read-retry").status == "running"
    assert attempts == 3
    assert sleeps == [0.2, 0.4]


def test_sdk_retries_submit_transport_errors_with_generated_idempotency_key(monkeypatch):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.RemoteProtocolError("server disconnected", request=request)

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    sdk = RadarSimClient(
        "http://testserver",
        user="alice",
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )

    with pytest.raises(RadarSimTransportError, match="server disconnected"):
        sdk.submit_run(run_config_dict())

    assert attempts == 3
    assert sleeps == [0.2, 0.4]


def test_sdk_retries_idempotent_upload_session_creation(monkeypatch):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.RemoteProtocolError("server disconnected", request=request)
        return httpx.Response(200, json={"session_id": "session-1"})

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    sdk = RadarSimClient(
        "http://testserver",
        user="alice",
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )

    assert sdk._request("POST", "/api/v1/artifact-uploads", json={})["session_id"] == "session-1"
    assert attempts == 3
    assert sleeps == [0.2, 0.4]
