import json
from types import SimpleNamespace

from cli.agent import (
    _execute_v5_local_collect,
    _execute_v5_local_finalize,
    _execute_v5_local_preflight,
    _execute_v5_local_simulation,
)
from core.agent_asset_bindings import AgentAssetBindingStore
from core.agent_data_bindings import AgentDataBindingStore
from core.agent_data_lease import AgentDataLeaseStore
from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseStore
from core.runtime_bundle import RuntimeSourceEvidence, discover_runtime_bundle
from core.runtime_bundle_archive import stage_runtime_bundle_archive


def test_windows_full_local_preflight_run_collect_finalize_path_free(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("RSIM_HOME", str(home))
    runtime_dir = tmp_path / "built"
    runtime_dir.mkdir()
    exe = runtime_dir / "selena.exe"
    dll = runtime_dir / "runtime.dll"
    runtime_xml = tmp_path / "Runtime.xml"
    exe.write_bytes(b"exe")
    dll.write_bytes(b"dll")
    runtime_xml.write_text("<runtime/>", encoding="utf-8")
    discovered = discover_runtime_bundle(
        exe,
        runtime_xml,
        source=RuntimeSourceEvidence(
            branch="feature/local", commit="a" * 40, dirty=False,
            dirty_fingerprint="", build_mode="release", toolchain_fingerprint="vs",
        ),
        created_at=1,
    )
    archive = stage_runtime_bundle_archive(discovered, tmp_path / "archives")
    runtime_lease = AgentRuntimeBundleLeaseStore().create(
        project="demo", workspace_binding_id="workspace:sha256:" + "1" * 24,
        build_stage_id="build-stage", build_attempt=1,
        manifest=discovered.manifest, archive=archive,
    )

    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "one.MF4").write_bytes(b"input-one")
    (data_root / "two.MF4").write_bytes(b"input-two")
    data_bindings = AgentDataBindingStore()
    data_binding = data_bindings.register(project="demo", root_path=data_root)
    data_lease = AgentDataLeaseStore().create(
        {
            "project": "demo", "data_binding_id": data_binding.binding_id,
            "data_path": str(data_root), "required_signals": [],
        },
        data_bindings,
        stage_id="data-stage",
        attempt=1,
    )

    assets_root = tmp_path / "assets"
    assets_root.mkdir()
    adapter = assets_root / "adapter.txt"
    mat_filter = assets_root / "signals.filter"
    template = assets_root / "template.txt"
    adapter.write_text("adapter", encoding="utf-8")
    mat_filter.write_text("filter", encoding="utf-8")
    template.write_text("input={{INPUT_MF4}}\noutput={{OUTPUT_MF4}}\n", encoding="utf-8")
    AgentAssetBindingStore().register(assets_root)
    base_config = {
        "_meta": {"project": "demo"},
        "project": {"name": "demo"},
        "assets": {"config_template": str(template), "fixed_config_path": str(tmp_path / "unused.txt")},
        "simulation": {"auto_detect_radar": False, "source": "RadarFC", "mounting_position": "front"},
        "environment": {"path_prefix": []},
    }
    monkeypatch.setattr("core.config.load_config", lambda project: base_config)
    monkeypatch.setattr(
        "core.preflight.run_preflight",
        lambda config: SimpleNamespace(
            ok=True, checks=[SimpleNamespace(name="compatibility", level="error", passed=True)]
        ),
    )
    payload = {
        "dispatch_scope": "local_simulation", "project": "demo",
        "runtime_bundle_lease_ref": runtime_lease.lease_id,
        "runtime_bundle_id": discovered.manifest.id,
        "data_lease_ref": data_lease.lease_id,
        "dataset_id": "dataset:sha256:" + "2" * 64,
        "adapter_file": str(adapter), "mat_filter": str(mat_filter),
        "limit": 1, "timeout_minutes": 1, "owner": "alice", "retain_days": 7,
        "config_fingerprint": "sha256:" + "3" * 64,
    }
    preflight = _execute_v5_local_preflight(
        {"task_id": "preflight-stage", "job_id": "job-local", "payload": payload}
    )
    lease_ref = preflight["local_run_lease_ref"]
    assert preflight["preflight"]["ok"] is True

    def fake_runner(request, cancel_requested):
        from core.agent_local_run import LocalRunOutcome

        assert not cancel_requested()
        request.output_mf4.write_bytes(b"simulated")
        return LocalRunOutcome(0)

    monkeypatch.setattr("core.local_selena_runner.run_local_selena", fake_runner)
    run_result, returncode = _execute_v5_local_simulation(
        {"payload": {"local_run_lease_ref": lease_ref}}, lambda: False
    )
    assert returncode == 0
    assert run_result["status"] == "succeeded"
    assert len(run_result["files"]) == 1

    successor = {
        **payload,
        "local_run_lease_ref": lease_ref,
        "job_id": "job-local",
    }
    collected = _execute_v5_local_collect({"payload": successor})
    successor["result_ref"] = collected["result_ref"]
    finalized = _execute_v5_local_finalize({"job_id": "job-local", "payload": successor})
    manifest = finalized["manifest"]
    serialized = json.dumps(manifest)
    assert manifest["status"] == "succeeded"
    assert manifest["runtime_bundle_id"] == discovered.manifest.id
    assert manifest["dataset_id"] == payload["dataset_id"]
    assert manifest["result_ref"] == collected["result_ref"]
    assert str(tmp_path) not in serialized
    assert "storage_ref" not in serialized
    assert all(not item["relative_path"].startswith(("/", "\\")) for item in manifest["files"])
