"""TDD contract tests for the UserRunConfig 2.0 control/data-plane split.

These tests intentionally describe the product contract rather than preserving
legacy HTTP upload fallbacks.  A failing assertion here means that user file
bodies can still enter the Linux control plane, or that Web/SDK routing differs.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cli import agent as agent_cli
from core.api_v1 import ApiV1Error, ApiV1Service
from core.api_v1_fastapi import create_app
from core.agent_policy import LINUX_EXECUTOR_CAPABILITIES
from core.cluster_stage_executor import LINUX_STAGE_AGENT_ID
from core.control_service import ControlService
from core.transfer_service import (
    ClusterWorkspaceWhitelist,
    TransferService,
    TransferStore,
)
from core.user import USER_HEADER
from core.user_config import UserRunConfig
from radar_sim_sdk import RadarSimClient


def _local_existing_inputs(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    selena = tmp_path / "ovrs25-selena"
    selena.mkdir()
    (selena / "Selena.exe").write_bytes(b"exe")
    (selena / "core.dll").write_bytes(b"dll")
    runtime = tmp_path / "Runtime_For_byd_ovrs25.xml"
    runtime.write_text("<runtime project='BYD_OVS'/>", encoding="utf-8")
    data = tmp_path / "measurements"
    data.mkdir()
    (data / "one.MF4").write_bytes(b"mf4")
    mat_filter = tmp_path / "MatFilter.cfg"
    mat_filter.write_text("signal=*", encoding="utf-8")
    config = {
        "schema_version": "2.0",
        "selena": {
            "source": "existing",
            "existing_path": selena.as_posix(),
            "runtime_xml": runtime.as_posix(),
        },
        "data": {"path": data.as_posix()},
        "simulation": {
            "target": "cluster",
            "adapter_file": "",
            "mat_filter": mat_filter.as_posix(),
        },
    }
    return config, {
        "selena": selena,
        "runtime": runtime,
        "data": data,
        "mat_filter": mat_filter,
    }


def _shared_existing_config() -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "selena": {
            "source": "existing",
            "existing_path": "//cluster/share/selena/ovrs25",
            "runtime_xml": "//cluster/share/selena/Runtime.xml",
        },
        "data": {"path": "//cluster/share/data/run-001"},
        "simulation": {
            "target": "cluster",
            "adapter_file": "",
            "mat_filter": "//cluster/share/config/MatFilter.cfg",
        },
    }


def _bundle_record(bundle_id: str = "selena-bundle:sha256:" + "a" * 64) -> SimpleNamespace:
    return SimpleNamespace(
        internal_project="ovrs25",
        public_dict={
            "id": bundle_id,
            "storage_ref": "shared://selena-bundles/ovrs25/runtime-bundle.zip",
            "archive_checksum": "sha256:" + "b" * 64,
            "archive_size": 3,
            "files": [],
            "source": {"branch": "", "build_mode": "Release"},
        },
    )


class _TransferTrap:
    """A scheduler spy: zero-copy/local routes must never issue a transfer."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def issue_plan(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        raise AssertionError("this route must not create a TransferPlan")


def _stage_projection(job: dict[str, Any] | Any) -> tuple[Any, ...]:
    stages = job["stages"] if isinstance(job, dict) else job.stages
    resolved = job["resolved_spec"] if isinstance(job, dict) else job.resolved_spec
    waiting = job.get("waiting") if isinstance(job, dict) else job.waiting
    return (
        dict(resolved.get("decisions") or {}).get("execution"),
        [
            (stage["stage_type"], stage["status"], stage.get("skip_reason", ""))
            for stage in stages
        ],
        waiting or {},
    )


def test_web_user_run_never_uploads_task_file_bodies_to_linux() -> None:
    """The browser entry must use the same direct-transfer contract as SDK.

    A browser cannot safely turn a local folder picker into an absolute source
    path.  Local paths therefore stay in UserRunConfig and are read by the
    owner-bound Connector; the Web control plane must not retain its legacy
    dataset/config-asset body upload fallback.
    """

    root = Path(__file__).resolve().parents[1]
    javascript = (root / "radar_sim_web" / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "radar_sim_web" / "static" / "index.html").read_text(encoding="utf-8")

    assert 'api("/run-data-uploads"' not in javascript
    assert 'api("/config-assets"' not in javascript
    assert "body: file" not in javascript
    assert "body: blob" not in javascript
    assert "文件正文不经过本 Linux Web 服务" in html
    assert "与 SDK 使用相同路径和直传语义" in html


def test_sdk_existing_cluster_local_paths_never_use_linux_body_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cluster preparation is a control request; SDK file bytes go data-plane."""
    config, _ = _local_existing_inputs(tmp_path)
    sdk = RadarSimClient("http://control.invalid")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("existing+cluster called a legacy Linux body-upload helper")

    for name in (
        "_upload_existing_selena",
        "import_existing_runtime_bundle",
        "upload_runtime_bundle",
        "upload_artifact",
        "upload_config_asset",
    ):
        monkeypatch.setattr(sdk, name, forbidden)

    requests: list[dict[str, Any]] = []

    def request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        assert (method, path) == ("POST", "/api/v1/run-jobs")
        requests.append(dict(kwargs))
        return {
            "id": "job-control-only",
            "type": "simulation.run_config.v2",
            "status": "queued",
            "spec": kwargs["json"]["config"],
            "stages": [],
        }

    monkeypatch.setattr(sdk, "_request", request)
    job = sdk.submit_run(config)

    assert len(requests) == 1
    assert requests[0]["json"]["prepared_runtime_bundle_id"] == ""
    assert set(requests[0]["json"]["client_transfer_roles"]) == {
        "dataset", "runtime_bundle", "runtime_xml", "mat_filter"
    }
    assert job.spec == UserRunConfig.from_dict(config).to_dict()


def test_linux_sdk_posix_sources_use_direct_transfer_hint_not_linux_body_route(
    tmp_path: Path,
) -> None:
    """A remote Linux SDK's `/home/...` paths belong to that caller, not server disk."""

    config = {
        "schema_version": "2.0",
        "selena": {
            "source": "existing",
            "existing_path": "/home/alice/Selena",
            "runtime_xml": "/home/alice/Runtime.xml",
        },
        "data": {"path": "/home/alice/data"},
        "simulation": {
            "target": "cluster",
            "adapter_file": "",
            "mat_filter": "/home/alice/MatFilter.cfg",
        },
    }
    control = ControlService(tmp_path / "linux-sdk-control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)

    job = api.submit_user_run(
        "alice",
        config_payload=config,
        client_transfer_roles=("dataset", "runtime_bundle", "runtime_xml", "mat_filter"),
    )
    private = control.get_job(job["id"])
    stages = {item["stage_type"]: item for item in private["stages"]}

    assert stages["resolve_spec"]["status"] == "skipped"
    assert stages["resolve_spec"]["skip_reason"] == "runtime_bundle_direct_transfer"
    assert stages["prepare_data"]["payload"]["dispatch_scope"] == "direct_transfer"
    assert set(stages["prepare_data"]["payload"]["source_roles"]) == {
        "dataset", "runtime_bundle", "runtime_xml", "mat_filter"
    }
    assert stages["prepare_data"].get("required_agent_id", "") != "linux-v2-stage-executor"


def test_linux_control_plane_does_not_import_server_visible_existing_cluster_bodies(
    tmp_path: Path,
) -> None:
    """Even a readable path is referenced/transferred; Linux must not archive it."""
    config, _ = _local_existing_inputs(tmp_path)
    calls: list[int] = []
    record = _bundle_record()

    class LegacyBodySink:
        def import_existing(self, _owner: str, *, archive_bytes: bytes, **_kwargs: Any) -> dict[str, Any]:
            calls.append(len(archive_bytes))
            return {"runtime_bundle": {"id": record.public_dict["id"]}}

        def resolve_bundle(self, _owner: str, _selected: str) -> SimpleNamespace:
            return record

    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(
        control_service_factory=lambda _owner: control,
        runtime_bundle_upload_service_factory=lambda _owner: LegacyBodySink(),
    )

    api.submit_user_run("alice", config_payload=config)

    # Expected by PRODUCT_CONTRACT section 1.7: Linux may inspect metadata or
    # issue a plan, but must never read/archive the Selena directory as bytes.
    assert calls == []


def test_existing_cluster_agent_resolution_never_calls_linux_body_upload_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Connector may direct-copy bytes to Cluster, never to Linux upload APIs."""
    body_upload_calls: list[str] = []

    monkeypatch.setattr(
        agent_cli,
        "_resolve_existing_v2_run_config",
        lambda _task: {"runtime_bundle_lease_ref": "lease-local"},
    )

    def upload_assets(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        body_upload_calls.append("config_asset")
        return {}

    monkeypatch.setattr(agent_cli, "_upload_resolution_config_assets", upload_assets)

    class Client:
        def append_logs(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

        def heartbeat(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

        def import_existing_runtime_bundle(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            body_upload_calls.append("runtime_bundle")
            return {"runtime_bundle": _bundle_record().public_dict}

        def submit_result(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

    task = {
        "task_id": "task-resolve-existing",
        "task_type": "resolve_spec",
        "owner": "alice",
        "payload": {
            "contract": "user-run-config/2.0",
            "source": "existing",
            "target": "cluster",
            "selected_target": "cluster",
        },
    }
    agent_cli._run_task(
        Client(),
        "alice-windows",
        task,
        heartbeat_interval=0.01,
        node_kind="windows_agent",
    )

    assert body_upload_calls == []


def test_web_and_sdk_share_local_zero_transfer_scheduling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reachable local inputs stay in place and both entries hit one scheduler."""
    config, _ = _local_existing_inputs(tmp_path)
    config["simulation"]["target"] = "local"
    control = ControlService(tmp_path / "local-control.db")
    control.register_agent(
        "alice-full",
        agent_id="alice-full",
        capabilities=["simulation.local"],
        metadata={
            "node_kind": "windows_full",
            "user": "user-alice",
            "auto_configure": True,
        },
    )
    transfers = _TransferTrap()
    api = ApiV1Service(
        control_service_factory=lambda _owner: control,
        transfer_service=transfers,  # type: ignore[arg-type]
    )
    http = TestClient(create_app(api_service=api))
    web_response = http.post(
        "/api/v1/run-jobs",
        json={"config": config},
        headers={USER_HEADER: "user-alice"},
    )
    assert web_response.status_code == 201
    web_job = web_response.json()

    sdk = RadarSimClient("http://testserver", client=http, user="alice")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("local execution attempted to upload an already reachable input")

    for name in ("_upload_existing_selena", "upload_config_asset"):
        monkeypatch.setattr(sdk, name, forbidden)

    sdk_job = sdk.submit_run(config)

    assert sdk_job.spec == web_job["spec"] == UserRunConfig.from_dict(config).to_dict()
    assert _stage_projection(sdk_job) == _stage_projection(web_job)
    assert transfers.calls == []
    assert all(
        stage.get("payload", {}).get("dispatch_scope")
        not in {"data_upload", "direct_transfer"}
        for stage in control.get_job(web_job["id"])["stages"]
    )


def test_shared_cluster_inputs_are_zero_copy_and_need_no_windows_connector(tmp_path: Path) -> None:
    control = ControlService(tmp_path / "shared-control.db")
    transfers = _TransferTrap()
    api = ApiV1Service(
        control_service_factory=lambda _owner: control,
        transfer_service=transfers,  # type: ignore[arg-type]
    )
    config = _shared_existing_config()

    job = api.submit_user_run("alice", config_payload=config)
    private_stages = control.get_job(job["id"])["stages"]

    assert job["spec"] == UserRunConfig.from_dict(config).to_dict()
    assert job["waiting"] is None
    assert transfers.calls == []
    assert all(
        stage.get("payload", {}).get("dispatch_scope")
        not in {"data_upload", "direct_transfer"}
        for stage in private_stages
    )


def test_shared_existing_cluster_skips_windows_resolution_and_registration(tmp_path: Path) -> None:
    """A fully Cluster-visible existing Selena run starts on Linux only."""

    control = ControlService(tmp_path / "shared-existing-control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)
    config = _shared_existing_config()

    job = api.submit_user_run("alice", config_payload=config)
    private = control.get_job(job["id"])
    stages = {item["stage_type"]: item for item in private["stages"]}

    assert job["current_stage"] == "environment_check"
    assert job["waiting"] is None
    assert stages["resolve_spec"]["status"] == "skipped"
    assert stages["register_artifact"]["status"] == "skipped"
    assert stages["environment_check"]["required_agent_id"] == "linux-v2-stage-executor"
    assert stages["environment_check"]["payload"]["dispatch_scope"] == "shared_existing_environment"
    assert stages["prepare_data"]["required_agent_id"] == "linux-v2-stage-executor"


def test_shared_unc_inputs_win_over_conservative_client_transfer_hints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A readable deployment UNC stays zero-copy even if Web/SDK hints it."""

    mount = tmp_path / "mnt"
    selena_root = mount / "loc" / "szh" / "Isilon2" / "Selena"
    data_root = mount / "loc" / "szh" / "Isilon2" / "data"
    config_root = mount / "loc" / "szh" / "Isilon2" / "config"
    selena_root.mkdir(parents=True)
    data_root.mkdir(parents=True)
    config_root.mkdir(parents=True)
    (selena_root / "selena.exe").write_bytes(b"exe")
    (selena_root / "runtime.dll").write_bytes(b"dll")
    (selena_root / "Runtime.xml").write_text("<runtime/>", encoding="utf-8")
    (data_root / "one.MF4").write_bytes(b"mf4")
    (config_root / "MatFilter.cfg").write_text("signal=*", encoding="utf-8")
    monkeypatch.setattr(
        "core.config.load_cluster_execution_config",
        lambda _project: {
            "cluster": {
                "linux_mount_map": {
                    r"\\abtvdfs2.de.bosch.com\ismdfs": str(mount),
                }
            }
        },
    )
    prefix = r"\\abtvdfs2.de.bosch.com\ismdfs"
    config = {
        "schema_version": "2.0",
        "selena": {
            "source": "existing",
            "existing_path": prefix + r"\loc\szh\Isilon2\Selena",
            "runtime_xml": prefix + r"\loc\szh\Isilon2\Selena\Runtime.xml",
        },
        "data": {"path": prefix + r"\loc\szh\Isilon2\data\one.MF4"},
        "simulation": {
            "target": "cluster",
            "adapter_file": "",
            "mat_filter": prefix + r"\loc\szh\Isilon2\config\MatFilter.cfg",
        },
    }
    control = ControlService(tmp_path / "shared-hinted-control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)

    job = api.submit_user_run(
        "alice",
        config_payload=config,
        client_transfer_roles=("dataset", "runtime_bundle", "runtime_xml", "mat_filter"),
    )
    private = control.get_job(job["id"])
    stages = {item["stage_type"]: item for item in private["stages"]}

    assert stages["resolve_spec"]["skip_reason"] == "existing_selena_is_cluster_visible"
    assert stages["register_artifact"]["skip_reason"] == "existing_selena_is_cluster_visible"
    assert stages["environment_check"]["required_agent_id"] == LINUX_STAGE_AGENT_ID
    assert stages["prepare_data"]["required_agent_id"] == LINUX_STAGE_AGENT_ID
    assert stages["prepare_data"]["payload"]["dispatch_scope"] == "shared_reference"
    assert stages["prepare_data"]["payload"]["transfer_required"] is False


def test_mixed_shared_dataset_keeps_only_local_runtime_resources_on_connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mixed route remains source-bound only for resources still local."""

    mount = tmp_path / "mnt"
    shared_data = mount / "loc" / "szh" / "Isilon2" / "data" / "one.MF4"
    shared_data.parent.mkdir(parents=True)
    shared_data.write_bytes(b"mf4")
    monkeypatch.setattr(
        "core.config.load_cluster_execution_config",
        lambda _project: {
            "cluster": {
                "linux_mount_map": {
                    r"\\abtvdfs2.de.bosch.com\ismdfs": str(mount),
                }
            }
        },
    )
    prefix = r"\\abtvdfs2.de.bosch.com\ismdfs"
    config = {
        "schema_version": "2.0",
        "selena": {
            "source": "existing",
            "existing_path": "D:/alice/Selena",
            "runtime_xml": "D:/alice/Runtime.xml",
        },
        "data": {"path": prefix + r"\loc\szh\Isilon2\data\one.MF4"},
        "simulation": {
            "target": "cluster",
            "adapter_file": "",
            "mat_filter": "D:/alice/MatFilter.cfg",
        },
    }
    control = ControlService(tmp_path / "mixed-hinted-control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)

    job = api.submit_user_run(
        "alice",
        config_payload=config,
        client_transfer_roles=("dataset", "runtime_bundle", "runtime_xml", "mat_filter"),
    )
    private = control.get_job(job["id"])
    stages = {item["stage_type"]: item for item in private["stages"]}
    prepare = stages["prepare_data"]

    assert prepare["required_agent_id"] == ""
    assert prepare["payload"]["dispatch_scope"] == "direct_transfer"
    assert "dataset" not in prepare["payload"]["source_roles"]
    assert {item["source_role"] for item in prepare["payload"]["source_paths"]} == {
        "runtime_bundle",
        "runtime_xml",
        "mat_filter",
    }


def test_existing_cluster_direct_transfer_uses_prepare_data_barrier_without_agent_wait(
    tmp_path: Path,
) -> None:
    """Linux checks Cluster readiness before requesting source-side transfer."""
    config, _ = _local_existing_inputs(tmp_path)
    # This case specifically exercises Web/Connector-owned Windows paths.
    # Use platform-independent Windows syntax instead of relying on tmp_path's
    # host OS classification; Linux SDK caller-local POSIX paths are covered
    # separately by test_linux_sdk_posix_sources_use_direct_transfer_hint_not_linux_body_route.
    config["selena"]["existing_path"] = "D:/alice/Selena"
    config["selena"]["runtime_xml"] = "D:/alice/Runtime.xml"
    config["data"]["path"] = "D:/alice/data"
    config["simulation"]["mat_filter"] = "D:/alice/MatFilter.cfg"
    control = ControlService(tmp_path / "existing-direct-control.db")
    api = ApiV1Service(control_service_factory=lambda _owner: control)

    job = api.submit_user_run("alice", config_payload=config)
    private = control.get_job(job["id"])
    stages = {item["stage_type"]: item for item in private["stages"]}

    # Do the cheap Linux/Cluster readiness check before asking a user device
    # to move large files. Resolver/registration are not Windows tasks.
    assert job["current_stage"] == "environment_check"
    assert job["waiting"] is None
    assert stages["resolve_spec"]["status"] == "skipped"
    assert stages["register_artifact"]["status"] == "skipped"
    assert stages["environment_check"]["dependencies"] == [stages["resolve_spec"]["task_id"]]
    assert stages["environment_check"]["required_agent_id"] == "linux-v2-stage-executor"
    assert stages["prepare_data"]["dependencies"] == [stages["environment_check"]["task_id"]]

    control.register_cluster_worker(
        "linux executor",
        role_id=LINUX_STAGE_AGENT_ID,
        worker_id=LINUX_STAGE_AGENT_ID,
        worker_index=0,
        worker_count=1,
        platform="Linux",
        capabilities=list(LINUX_EXECUTOR_CAPABILITIES),
        node_kind="linux_executor",
    )
    claimed = control.claim_next_task(LINUX_STAGE_AGENT_ID)
    assert claimed is not None
    assert claimed["task_id"] == stages["environment_check"]["task_id"]
    control.submit_task_result(
        claimed["task_id"],
        agent_id=LINUX_STAGE_AGENT_ID,
        status="succeeded",
        returncode=0,
        result={"status": "ready"},
    )
    waiting = api.get_job("alice", job["id"])
    assert waiting["current_stage"] == "prepare_data"
    assert waiting["waiting"] == {
        "reason": "windows_connection_required",
        "mode": "unified",
        "stage": "prepare_data",
        "missing_capability": "windows_connector",
        "connection_state": "not_configured",
        "message": "This task is waiting for a connected Windows computer that can access local files.",
        "action": {
            "type": "connect_windows",
            "label": "Connect this Windows computer",
            "mode": "unified",
        },
    }

    # Completing every role on the same prepare_data Stage releases Cluster
    # preflight. The API receives only path-free transfer metadata.
    for index, role in enumerate(("dataset", "runtime_bundle", "runtime_xml", "mat_filter"), start=1):
        control.complete_transfer_stage(
            job["id"],
            stages["prepare_data"]["stage_id"],
            owner="alice",
            source_role=role,
            transfer={
                "transfer_id": f"transfer-{index}",
                "entries": [
                    {
                        "relative_path": "payload.bin",
                        "size": 1,
                        "sha256": "",
                        "storage_ref": f"cluster-staging://job/{index}/payload.bin",
                    }
                ],
            },
        )

    after = api.get_job("alice", job["id"])
    assert after["current_stage"] == "preflight"
    assert after["waiting"] is None
    after_stages = {
        item["stage_type"]: item for item in control.get_job(job["id"])["stages"]
    }
    assert after_stages["prepare_data"]["status"] == "succeeded"


def test_transfer_progress_and_plan_access_are_owner_isolated(tmp_path: Path) -> None:
    target = tmp_path / "cluster-staging"
    target.mkdir()
    transfer_service = TransferService(
        TransferStore(tmp_path / "transfers.db"),
        ClusterWorkspaceWhitelist((str(target),), allow_local_test_roots=True),
        now_fn=lambda: 100.0,
    )
    control = ControlService(tmp_path / "control.db")
    api = ApiV1Service(
        control_service_factory=lambda _owner: control,
        transfer_service=transfer_service,
    )
    job = control.create_job(
        "simulation.run_config.v2",
        owner="alice",
        tasks=[
            {
                "task_id": "stage-data",
                "task_type": "prepare_data",
                "stage_type": "prepare_data",
                "payload": {
                    "dispatch_scope": "direct_transfer",
                    "source_roles": ["dataset"],
                },
            }
        ],
    )
    plan = api.issue_transfer_plan(
        "alice",
        job_id=job["job_id"],
        stage_id="stage-data",
        source_role="dataset",
        items=[
            {
                "source_role": "dataset",
                "relative_path": "one.MF4",
                "size": 3,
                "checksum": "",
            }
        ],
    )

    with pytest.raises(ApiV1Error) as read_error:
        api.get_transfer_plan("bob", plan["transfer_id"])
    assert read_error.value.code == "transfer_owner_mismatch"

    with pytest.raises(ApiV1Error) as progress_error:
        api.report_transfer_progress(
            "bob",
            {
                "transfer_id": plan["transfer_id"],
                "bytes_transferred": 1,
                "bytes_total": 3,
                "status": "in_progress",
            },
        )
    assert progress_error.value.code == "transfer_owner_mismatch"


def test_missing_direct_transfer_capability_blocks_with_stable_status_not_http_upload(
    tmp_path: Path,
) -> None:
    bundle_id = "selena-bundle:sha256:" + "a" * 64
    record = _bundle_record(bundle_id)
    control = ControlService(tmp_path / "blocked-control.db")
    api = ApiV1Service(
        control_service_factory=lambda _owner: control,
        runtime_bundle_upload_service_factory=lambda _owner: SimpleNamespace(
            resolve_bundle=lambda _scope, _selected: record
        ),
        transfer_service=None,
    )
    config = {
        "schema_version": "2.0",
        "selena": {
            "source": "existing",
            "existing_path": "Z:/owner-local/Selena",
            "runtime_xml": "Z:/owner-local/Runtime.xml",
        },
        "data": {"path": "Z:/owner-local/data"},
        "simulation": {
            "target": "cluster",
            "adapter_file": "",
            "mat_filter": "Z:/owner-local/MatFilter.cfg",
        },
    }

    job = api.submit_user_run(
        "alice",
        config_payload=config,
        prepared_runtime_bundle_id=bundle_id,
    )
    prepare_data = next(
        stage
        for stage in control.get_job(job["id"])["stages"]
        if stage["stage_type"] == "prepare_data"
    )

    # CONTROL_DATA_PLANE_PLAN section 6 defines this as a durable product
    # status. It must block before transfer, never select the legacy upload DAG.
    assert job["status"] == "needs_input"
    assert prepare_data["status"] == "blocked"
    assert prepare_data["error"]["code"] == "cluster_direct_transfer_unavailable"
    assert prepare_data["payload"]["transfer_status"] == "cluster_direct_transfer_unavailable"
    assert prepare_data["payload"].get("dispatch_scope") != "data_upload"
