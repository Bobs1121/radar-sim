from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.agent_asset_bindings import AgentAssetBindingStore
from core.agent_data_lease import AgentDataLease
from core.agent_local_run import (
    AgentLocalRunError,
    AgentLocalRunLeaseStore,
    LocalRunOutcome,
    execute_local_run,
)
from core.datasets import DatasetFileRef
from core.runtime_bundle import RuntimeBundleManifest, RuntimeFile, RuntimeSourceEvidence


def _checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path, *, now: float = 10.0):
    extracted = tmp_path / "extracted"
    binary = extracted / "bin" / "selena.exe"
    library = extracted / "bin" / "runtime.dll"
    runtime = extracted / "runtime" / "Runtime.xml"
    for path, content in ((binary, b"exe"), (library, b"dll"), (runtime, b"<runtime/>")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    files = (
        RuntimeFile("entrypoint", "bin/selena.exe", binary.stat().st_size, _checksum(binary)),
        RuntimeFile("runtime_library", "bin/runtime.dll", library.stat().st_size, _checksum(library)),
        RuntimeFile("runtime_config", "runtime/Runtime.xml", runtime.stat().st_size, _checksum(runtime)),
    )
    manifest = RuntimeBundleManifest(
        id="selena-bundle:sha256:" + "1" * 64,
        files=files,
        source=RuntimeSourceEvidence(
            branch="feature/test", commit="a" * 40, dirty=False,
            dirty_fingerprint="", build_mode="release", toolchain_fingerprint="vs",
        ),
        created_at=now,
    )
    locations = {item.relative_path: extracted / Path(item.relative_path) for item in files}

    data_root = tmp_path / "data"
    data_root.mkdir()
    first = data_root / "one.MF4"
    second = data_root / "nested" / "two.mf4"
    second.parent.mkdir()
    first.write_bytes(b"mf4-one")
    second.write_bytes(b"mf4-two")
    refs = tuple(
        DatasetFileRef(
            path.relative_to(data_root).as_posix(), path.stat().st_size,
            _checksum(path), mtime_ns=path.stat().st_mtime_ns,
        )
        for path in (first, second)
    )
    data_lease = AgentDataLease(
        lease_id="data-lease:sha256:" + "2" * 32,
        project="runtime-project",
        binding_id="data-root:sha256:" + "3" * 24,
        source_path=data_root,
        files=refs,
        evidence_ref="stage:1",
        status="ready",
        dataset_id="",
        created_at=now,
        updated_at=now,
    )

    assets_root = tmp_path / "assets"
    assets_root.mkdir()
    adapter = assets_root / "adapter.txt"
    mat_filter = assets_root / "mat.filter"
    adapter.write_text("adapter", encoding="utf-8")
    mat_filter.write_text("filter", encoding="utf-8")
    asset_store = AgentAssetBindingStore(tmp_path / "bindings.db", now_fn=lambda: now)
    binding = asset_store.register(assets_root)

    store = AgentLocalRunLeaseStore(
        tmp_path / "local-runs.db", runs_root=tmp_path / "rsim" / "agent" / "runs",
        now_fn=lambda: now,
    )
    kwargs = {
        "job_id": "job-local-1",
        "project": "runtime-project",
        "base_config": {"environment": {"path_prefix": ["runtime"]}},
        "runtime_manifest": manifest,
        "runtime_locations": locations,
        "data_lease": data_lease,
        "asset_bindings": asset_store,
        "adapter_binding_id": binding.binding_id,
        "adapter_path": str(adapter),
        "mat_filter_binding_id": binding.binding_id,
        "mat_filter_path": str(mat_filter),
        "timeout_seconds": 90,
    }
    return store, kwargs


def test_default_runs_root_is_below_rsim_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RSIM_HOME", str(tmp_path / "home"))
    store = AgentLocalRunLeaseStore(tmp_path / "runs.db")
    assert store.runs_root == (tmp_path / "home" / "agent" / "runs").resolve()


def test_create_builds_private_config_and_path_free_public_lease(tmp_path):
    store, kwargs = _fixture(tmp_path)
    public = store.create_from_authorized_inputs(**kwargs)
    private = store.get_private(public["lease_id"])

    assert public["status"] == "ready"
    assert public["runtime_bundle_id"] == kwargs["runtime_manifest"].id
    assert public["input_count"] == 2
    assert "path" not in json.dumps(public).lower()
    assert str(tmp_path) not in json.dumps(public)

    simulation = private["config"]["simulation"]
    assert Path(simulation["runtime_xml"]).name == "Runtime.xml"
    assert Path(simulation["adapter_file"]).name == "adapter.txt"
    assert Path(simulation["matfilefilter"]).name == "mat.filter"
    assert private["config"]["build"]["selena_branch"] == "feature/test"
    assert private["run_root"].is_relative_to(store.runs_root)
    assert all(item["output_relative_path"].startswith("outputs/") for item in private["inputs"])

    # Same job and immutable evidence is idempotent.
    assert store.create_from_authorized_inputs(**kwargs)["lease_id"] == public["lease_id"]


def test_create_and_execute_without_optional_adapter(tmp_path):
    store, kwargs = _fixture(tmp_path)
    kwargs["adapter_binding_id"] = ""
    kwargs["adapter_path"] = ""
    lease = store.create_from_authorized_inputs(**kwargs)
    private = store.get_private(lease["lease_id"])

    assert private["config"]["simulation"]["adapter_file"] == ""
    assert private["evidence"]["adapter_checksum"] == ""

    def runner(request, _cancel_requested):
        assert request.adapter_file is None
        request.output_mf4.write_bytes(b"output")
        return LocalRunOutcome(0)

    assert execute_local_run(lease["lease_id"], store, runner=runner) == 0
    assert store.result(lease["lease_id"])["status"] == "succeeded"


def test_create_rejects_changed_runtime_and_unauthorized_assets(tmp_path):
    store, kwargs = _fixture(tmp_path)
    kwargs["runtime_locations"]["bin/runtime.dll"].write_bytes(b"changed")
    with pytest.raises(AgentLocalRunError, match="Runtime Bundle"):
        store.create_from_authorized_inputs(**kwargs)

    store, kwargs = _fixture(tmp_path / "second")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    kwargs["adapter_path"] = str(outside)
    with pytest.raises(AgentLocalRunError, match="not authorized"):
        store.create_from_authorized_inputs(**kwargs)


def test_injected_runner_writes_only_controlled_deterministic_outputs(tmp_path):
    store, kwargs = _fixture(tmp_path)
    lease = store.create_from_authorized_inputs(**kwargs)
    seen = []

    def runner(request, cancel_requested):
        assert not cancel_requested()
        assert request.output_mf4.is_relative_to(store.runs_root)
        assert request.output_mf4.parent.name == "outputs"
        assert request.output_mf4.parent != request.input_mf4.parent
        assert request.executable.name == "selena.exe"
        assert request.runtime_xml.name == "Runtime.xml"
        assert request.adapter_file.name == "adapter.txt"
        assert request.mat_filter.name == "mat.filter"
        request.output_mf4.write_bytes(b"output-" + str(request.item_index).encode())
        seen.append(request.output_mf4.name)
        return LocalRunOutcome(0)

    assert execute_local_run(lease["lease_id"], store, runner=runner) == 0
    result = store.result(lease["lease_id"])
    assert result["status"] == "succeeded"
    assert result["summary"] == {"file_count": 2, "error_count": 0, "error_code": ""}
    assert len(result["files"]) == 2
    assert all(item["relative_path"].startswith("outputs/") for item in result["files"])
    assert all(item["checksum"].startswith("sha256:") for item in result["files"])
    assert str(tmp_path) not in json.dumps(result)
    assert seen == [path.split("/", 1)[1] for path in (item["relative_path"] for item in result["files"])]


def test_cancellation_is_terminal_and_does_not_call_runner(tmp_path):
    store, kwargs = _fixture(tmp_path)
    lease = store.create_from_authorized_inputs(**kwargs)

    def runner(request, cancel_requested):  # pragma: no cover - must not run
        raise AssertionError("runner called after cancellation")

    assert execute_local_run(
        lease["lease_id"], store, runner=runner, cancel_requested=lambda: True
    ) == 130
    result = store.result(lease["lease_id"])
    assert result["status"] == "cancelled"
    assert result["files"] == []
    assert result["summary"]["error_code"] == "cancelled"


def test_missing_native_runner_fails_with_stable_path_free_code(tmp_path):
    store, kwargs = _fixture(tmp_path)
    lease = store.create_from_authorized_inputs(**kwargs)
    assert execute_local_run(lease["lease_id"], store) == 1
    result = store.result(lease["lease_id"])
    assert result["status"] == "failed"
    assert result["summary"]["error_code"] == "runner_unavailable"
    assert str(tmp_path) not in json.dumps(result)


def test_runner_cannot_claim_success_without_expected_output(tmp_path):
    store, kwargs = _fixture(tmp_path)
    lease = store.create_from_authorized_inputs(**kwargs)

    assert execute_local_run(
        lease["lease_id"], store, runner=lambda request, cancel: LocalRunOutcome(0)
    ) == 1
    result = store.result(lease["lease_id"])
    assert result["status"] == "failed"
    assert result["files"] == []
    assert result["summary"]["error_code"] == "runner_contract_failed"


def test_runner_exception_becomes_terminal_without_leaking_message(tmp_path):
    store, kwargs = _fixture(tmp_path)
    lease = store.create_from_authorized_inputs(**kwargs)

    def runner(request, cancel):
        raise RuntimeError(f"secret path: {request.input_mf4}")

    assert execute_local_run(lease["lease_id"], store, runner=runner) == 1
    result = store.result(lease["lease_id"])
    assert result["status"] == "failed"
    assert result["summary"]["error_code"] == "runner_contract_failed"
    assert str(tmp_path) not in json.dumps(result)
