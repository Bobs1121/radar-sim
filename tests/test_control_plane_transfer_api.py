"""Focused owner/stage/manifest checks for the P0 control-plane routes."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from core.api_v1 import ApiV1Service
from core.api_v1_fastapi import create_app
from core.control_service import ControlService
from core.direct_transfer import make_storage_ref
from core.transfer_service import ClusterWorkspaceWhitelist, TransferService, TransferStore
from core.user import USER_HEADER


def _config(root: Path, *, shared: bool = False) -> dict[str, Any]:
    if shared:
        selena = "//cluster/share/selena"
        runtime = "//cluster/share/Runtime.xml"
        data = "//cluster/share/data"
        mat_filter = "//cluster/share/MatFilter.cfg"
    else:
        selena = str(root / "Selena")
        runtime = str(root / "Runtime.xml")
        data = str(root / "data")
        mat_filter = str(root / "MatFilter.cfg")
    return {
        "schema_version": "2.0",
        "selena": {"source": "existing", "existing_path": selena, "runtime_xml": runtime},
        "data": {"path": data},
        "simulation": {"target": "cluster", "adapter_file": "", "mat_filter": mat_filter},
    }


def _api(tmp_path: Path) -> tuple[TestClient, ControlService]:
    control = ControlService(tmp_path / "control.db")
    target = tmp_path / "transfer-target"
    target.mkdir()
    transfer = TransferService(
        TransferStore(tmp_path / "transfer.db"),
        ClusterWorkspaceWhitelist((str(target),), allow_local_test_roots=True),
        now_fn=lambda: 100.0,
    )
    service = ApiV1Service(
        control_service_factory=lambda _owner: control,
        transfer_service=transfer,
    )
    return TestClient(create_app(api_service=service)), control


def test_transfer_plan_requires_direct_stage_and_owner(tmp_path: Path) -> None:
    client, control = _api(tmp_path)
    config = _config(tmp_path)
    response = client.post("/api/v1/run-jobs", json={"config": config}, headers={USER_HEADER: "alice"})
    assert response.status_code == 201
    job_id = response.json()["job_id"]
    stage = next(item for item in control.get_job(job_id)["stages"] if item["stage_type"] == "prepare_data")
    assert stage["payload"]["dispatch_scope"] == "direct_transfer"

    denied = client.post(
        f"/api/v1/jobs/{job_id}/stages/{stage['stage_id']}/transfers",
        json={"source_role": "dataset", "items":[{"relative_path":"one.mf4", "size": 1}]},
        headers={USER_HEADER: "bob"},
    )
    assert denied.status_code == 404

    wrong_role = client.post(
        f"/api/v1/jobs/{job_id}/stages/{stage['stage_id']}/transfers",
        json={"source_role": "not-a-stage-role", "items":[{"relative_path":"one.mf4", "size": 1}]},
        headers={USER_HEADER: "alice"},
    )
    assert wrong_role.status_code == 422
    assert wrong_role.json()["code"] == "transfer_source_role_not_allowed"

    plan = client.post(
        f"/api/v1/jobs/{job_id}/stages/{stage['stage_id']}/transfers",
        json={"source_role": "dataset", "items":[{"relative_path":"one.mf4", "size": 1}]},
        headers={USER_HEADER: "alice"},
    )
    assert plan.status_code == 201
    assert "server_probe_root" not in plan.json()


def test_manifest_roles_complete_stage_only_after_all_resources(tmp_path: Path) -> None:
    client, control = _api(tmp_path)
    response = client.post("/api/v1/run-jobs", json={"config": _config(tmp_path)}, headers={USER_HEADER: "alice"})
    job_id = response.json()["job_id"]
    stage = next(item for item in control.get_job(job_id)["stages"] if item["stage_type"] == "prepare_data")

    plans: dict[str, dict[str, Any]] = {}
    for role in ("dataset", "mat_filter", "runtime_bundle", "runtime_xml"):
        plan_response = client.post(
            f"/api/v1/jobs/{job_id}/stages/{stage['stage_id']}/transfers",
            json={"source_role": role, "items":[{"relative_path":f"{role}.bin", "size": 1}]},
            headers={USER_HEADER: "alice"},
        )
        assert plan_response.status_code == 201
        plans[role] = plan_response.json()

    first = plans["dataset"]
    digest = hashlib.sha256(b"x").hexdigest()
    first_manifest = client.post(
        f"/api/v1/transfers/{first['transfer_id']}/manifest",
        json={
            "entries": [{
                "relative_path": "dataset.bin",
                "size": 1,
                "checksum": digest,
                "target_logical_ref": make_storage_ref(digest, transfer_id=first["transfer_id"], relative_path="dataset.bin"),
            }],
        },
        headers={USER_HEADER: "alice"},
    )
    assert first_manifest.status_code == 200
    assert first_manifest.json()["remaining_roles"]
    current = control.get_job(job_id)
    current_stage = next(item for item in current["stages"] if item["stage_id"] == stage["stage_id"])
    assert current_stage["status"] == "running"
    direct_dataset = current["resolved_spec"]["decisions"]["data"]
    assert direct_dataset["status"] == "resolved"
    assert direct_dataset["route"] == "direct_transfer"
    assert direct_dataset["dataset"] == {
        "id": "dataset:sha256:" + hashlib.sha256(first["transfer_id"].encode()).hexdigest(),
        "source_kind": "direct_transfer",
        "file_count": 1,
        "total_size": 1,
        "storage_refs": [make_storage_ref(digest, transfer_id=first["transfer_id"], relative_path="dataset.bin")],
    }

    second = plans["mat_filter"]
    second_manifest = client.post(
        f"/api/v1/transfers/{second['transfer_id']}/manifest",
        json={
            "entries": [{
                "relative_path": "mat_filter.bin",
                "size": 1,
                "checksum": digest,
                "target_logical_ref": make_storage_ref(digest, transfer_id=second["transfer_id"], relative_path="mat_filter.bin"),
            }],
        },
        headers={USER_HEADER: "alice"},
    )
    assert second_manifest.status_code == 200
    assert set(second_manifest.json()["remaining_roles"]) == {"runtime_bundle", "runtime_xml"}
    for role in ("runtime_bundle", "runtime_xml"):
        plan = plans[role]
        completed = client.post(
            f"/api/v1/transfers/{plan['transfer_id']}/manifest",
            json={
                "entries": [{
                    "relative_path": f"{role}.bin",
                    "size": 1,
                    "checksum": digest,
                    "target_logical_ref": make_storage_ref(digest, transfer_id=plan["transfer_id"], relative_path=f"{role}.bin"),
                }],
            },
            headers={USER_HEADER: "alice"},
        )
        assert completed.status_code == 200
    current = control.get_job(job_id)
    current_stage = next(item for item in current["stages"] if item["stage_id"] == stage["stage_id"])
    assert current_stage["status"] == "succeeded"
    resources = current["resolved_spec"]["decisions"]["transfers"]["resources"]
    assert {resources["dataset"]["source_role"], resources["mat_filter"]["source_role"]} == {"dataset", "mat_filter"}
    direct_bundle = current["resolved_spec"]["decisions"]["selena"]
    assert direct_bundle["status"] == "resolved"
    assert direct_bundle["action"] == "use_direct_transfer"
    assert direct_bundle["runtime_bundle"] == {
        "id": "selena-bundle:sha256:" + hashlib.sha256(plans["runtime_bundle"]["transfer_id"].encode()).hexdigest(),
        "source_kind": "direct_transfer",
        "file_count": 1,
        "total_size": 1,
        "storage_refs": [make_storage_ref(digest, transfer_id=plans["runtime_bundle"]["transfer_id"], relative_path="runtime_bundle.bin")],
    }


def test_build_direct_stage_never_treats_source_workspace_as_runtime_bundle(tmp_path: Path) -> None:
    client, control = _api(tmp_path)
    config = _config(tmp_path)
    config["selena"] = {
        "source": "build",
        "code_path": "D:/workspace/project",
        "branch": "",
        "selena_build_script": "D:/workspace/project/build.bat",
        "package_build_script": "",
        "runtime_xml": "D:/workspace/project/Runtime.xml",
    }
    response = client.post("/api/v1/run-jobs", json={"config": config}, headers={USER_HEADER: "alice"})
    assert response.status_code == 201
    job = control.get_job(response.json()["job_id"])
    prepare = next(item for item in job["stages"] if item["stage_type"] == "prepare_data")
    assert all(str(item.get("path") or "") != config["selena"]["code_path"] for item in prepare["payload"].get("source_paths") or [])
    assert "runtime_bundle" not in set(prepare["payload"].get("source_roles") or [])
