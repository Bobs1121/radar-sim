from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.cluster_stage_executor import (
    ClusterStageExecutionError,
    ClusterStageContext,
    _dataset_worker_root,
    _validate_transfer_resources,
    execute_cluster_preflight,
    execute_cluster_environment,
    resolve_cluster_data,
)


def _resource(role: str, entries: list[dict], *, root: str = "//cluster/share", relative_root: str = "iso") -> dict:
    return {
        "transfer_id": "transfer:sha256:" + role,
        "source_role": role,
        "status": "resolved",
        "relative_root": relative_root,
        "worker_root": root,
        "entries": entries,
    }


def _entry(path: str, size: int, token: str) -> dict:
    return {
        "relative_path": path,
        "size": size,
        "sha256": "a" * 64,
        "storage_ref": f"cluster-staging://v1/{token}/{path}",
    }


def test_direct_resource_probe_checks_size_without_file_body(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    (probe / "iso" / "data").mkdir(parents=True)
    (probe / "iso" / "data" / "one.MF4").write_bytes(b"payload")
    job = {
        "owner": "alice",
        "resolved_spec": {
            "decisions": {
                "transfers": {
                    "status": "resolved",
                    "resources": {
                        "dataset": _resource("dataset", [_entry("data/one.MF4", 7, "dataset")]),
                    },
                }
            }
        },
    }
    context = SimpleNamespace(server_probe_root=probe, storage_ref_resolver=None)
    paths = _validate_transfer_resources(context, job, {"cluster": {}})
    assert paths["dataset"][0][2] == "//cluster/share/iso/data/one.MF4"

    (probe / "iso" / "data" / "one.MF4").write_bytes(b"x")
    with pytest.raises(ClusterStageExecutionError) as excinfo:
        _validate_transfer_resources(context, job, {"cluster": {}})
    assert excinfo.value.code == "CLUSTER_STORAGE_UNAVAILABLE"


def test_probe_accepts_transfer_service_style_owner_resolver(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    probe.mkdir()
    payload = probe / "payload.MF4"
    payload.write_bytes(b"payload")
    calls: list[tuple] = []

    class TransferServiceStyleResolver:
        def resolve_storage_ref(self, storage_ref: str, *, owner: str, require_exists: bool = True) -> Path:
            calls.append((storage_ref, owner, require_exists))
            return payload

    job = {
        "owner": "alice",
        "resolved_spec": {
            "decisions": {
                "transfers": {
                    "status": "resolved",
                    "resources": {
                        "dataset": _resource("dataset", [_entry("data/payload.MF4", 7, "dataset")]),
                    },
                }
            }
        },
    }
    context = SimpleNamespace(
        server_probe_root="",
        storage_ref_resolver=TransferServiceStyleResolver().resolve_storage_ref,
    )
    paths = _validate_transfer_resources(context, job, {"cluster": {}})
    assert paths["dataset"][0][2] == "//cluster/share/iso/data/payload.MF4"
    assert calls == [(paths["dataset"][0][1]["storage_ref"], "alice", True)]


def test_cluster_stage_context_derives_transfer_service_callbacks(tmp_path: Path) -> None:
    class Service:
        server_probe_root = str(tmp_path / "probe")

        def resolve_storage_ref(self, _ref: str, *, owner: str, require_exists: bool = True) -> Path:
            return Path(self.server_probe_root)

    service = Service()
    context = ClusterStageContext(
        runtime_catalog=None,
        runtime_store=None,
        dataset_catalog=None,
        config_assets=None,
        run_store=None,
        work_root=tmp_path / "work",
        config_loader=lambda _project: {},
        transfer_service=service,
    )
    assert context.storage_ref_resolver == service.resolve_storage_ref
    assert context.server_probe_root == service.server_probe_root


def test_direct_runtime_environment_uses_global_config_without_bundle_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[str] = []
    context = SimpleNamespace(
        config_loader=lambda project: loaded.append(project) or {"cluster": {}},
        runtime_catalog=SimpleNamespace(
            get=lambda _bundle_id: (_ for _ in ()).throw(AssertionError("direct environment must not read bundle catalog"))
        ),
    )
    job = {
        "resolved_spec": {
            "decisions": {
                "transfers": {
                    "status": "resolved",
                    "resources": {"runtime_bundle": _resource("runtime_bundle", [_entry("Selena.exe", 1, "exe")])},
                }
            }
        }
    }
    monkeypatch.setattr(
        "core.cluster.check_cluster_environment",
        lambda _config: [SimpleNamespace(name="manager", ok=True, severity="error")],
    )
    result = execute_cluster_environment(context, job)
    assert loaded == ["run-config-v2"]
    assert result["environment_snapshot"]["status"] == "ready"


def test_dataset_uri_direct_transfer_can_resolve_without_legacy_catalog(tmp_path: Path) -> None:
    digest = "c" * 64
    job = {
        "owner": "alice",
        "spec": {"data": {"path": f"dataset://sha256/{digest}"}},
        "resolved_spec": {
            "decisions": {
                "transfers": {
                    "status": "resolved",
                    "resources": {
                        "dataset": _resource("dataset", [_entry("data/one.MF4", 7, "dataset")]),
                    },
                }
            }
        },
    }
    context = SimpleNamespace(
        dataset_catalog=SimpleNamespace(),
        config_loader=lambda _project: {"shared_namespaces": []},
    )
    result = resolve_cluster_data(context, job)
    assert result["dataset"]["file_count"] == 1
    assert result["dataset"]["files"][0]["storage_ref"].startswith("cluster-staging://")


def test_dataset_worker_root_preserves_all_manifest_entries() -> None:
    entries = [
        (_resource("dataset", []), _entry("data/a.MF4", 1, "a"), "//cluster/share/iso/data/a.MF4"),
        (_resource("dataset", []), _entry("data/b.MF4", 2, "b"), "//cluster/share/iso/data/b.MF4"),
    ]
    assert _dataset_worker_root(entries) == "//cluster/share/iso"


def test_cluster_preflight_direct_refs_skips_linux_archive_and_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = tmp_path / "probe"
    for path, content in (
        ("iso/data/one.MF4", b"payload"),
        ("iso/selena/Selena.exe", b"exe-body"),
        ("iso/selena/Runtime.xml", b"xml-body"),
        ("iso/config/MatFilter.cfg", b"filter"),
    ):
        target = probe / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    manifest = SimpleNamespace(
        id="selena-bundle:sha256:" + "b" * 64,
        source=SimpleNamespace(branch="main"),
        files=(
            SimpleNamespace(role="entrypoint", relative_path="selena/Selena.exe"),
            SimpleNamespace(role="runtime_config", relative_path="selena/Runtime.xml"),
        ),
    )
    bundle = SimpleNamespace(
        manifest=manifest,
        internal_project="demo",
        storage_ref="shared://selena-bundles/demo/runtime.zip",
    )
    dataset = SimpleNamespace(
        id="dataset:sha256:" + "c" * 64,
        source_kind="agent_upload",
    )
    resources = {
        "dataset": _resource("dataset", [_entry("data/one.MF4", 7, "dataset")]),
        "runtime_bundle": _resource(
            "runtime_bundle",
            [_entry("selena/Selena.exe", 8, "exe"), _entry("selena/Runtime.xml", 8, "xml")],
        ),
        "mat_filter": _resource("mat_filter", [_entry("config/MatFilter.cfg", 6, "filter")]),
    }
    job = {
        "job_id": "job-direct",
        "owner": "alice",
        "spec": {"simulation": {"target": "cluster", "mat_filter": ""}},
        "resolved_spec": {"decisions": {"transfers": {"status": "resolved", "resources": resources}}},
    }
    captured: dict = {}

    class ForbiddenRuntimeStore:
        def resolve_location(self, _ref):
            raise AssertionError("direct Cluster preflight must not read the Runtime archive")

    def fake_prepare(config, **kwargs):
        captured["config"] = config
        captured["kwargs"] = kwargs
        manifest_path = tmp_path / "package" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(manifest_path=str(manifest_path), config_path="//cluster/job/Config.cfg", profile="default")

    monkeypatch.setattr("core.cluster.prepare_cluster_job", fake_prepare)
    monkeypatch.setattr("core.cluster_stage_executor._bundle", lambda _context, _job: bundle)
    monkeypatch.setattr("core.preflight.run_preflight", lambda _config: (_ for _ in ()).throw(AssertionError("MF4 preflight must be skipped")))
    context = SimpleNamespace(
        server_probe_root=probe,
        storage_ref_resolver=None,
        config_loader=lambda _project: {"simulation": {}, "cluster": {}},
        runtime_store=ForbiddenRuntimeStore(),
        dataset_catalog=SimpleNamespace(resolve_location=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dataset body must not be read"))),
        run_store=SimpleNamespace(
            create_run=lambda **_kwargs: SimpleNamespace(
                ref="cluster-run:sha256:" + "d" * 64,
                to_dict=lambda: {"ref": "cluster-run:sha256:" + "d" * 64},
            )
        ),
        work_root=tmp_path / "work",
    )

    result = execute_cluster_preflight(context, job)
    assert result["preflight"]["ok"] is True
    assert captured["kwargs"]["copy_data"] is False
    assert captured["kwargs"]["copy_selena"] is False
    assert captured["config"]["_cluster_zero_copy"] is True
    assert captured["config"]["_cluster_skip_mf4_probe"] is True


def _preflight_capture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, bundle, dataset, config, context):
    captured: dict = {}

    def fake_prepare(run_config, **kwargs):
        captured["config"] = run_config
        captured["kwargs"] = kwargs
        manifest_path = tmp_path / "package" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(
            manifest_path=str(manifest_path),
            config_path="//cluster/job/Config.cfg",
            profile="default",
        )

    monkeypatch.setattr("core.cluster.prepare_cluster_job", fake_prepare)
    monkeypatch.setattr("core.cluster_stage_executor._bundle", lambda _context, _job: bundle)
    monkeypatch.setattr("core.cluster_stage_executor._dataset", lambda _context, _job, owner: dataset)
    monkeypatch.setattr(
        "core.preflight.run_preflight",
        lambda _config: (_ for _ in ()).throw(AssertionError("direct role must skip MF4 preflight")),
    )
    return captured


def test_direct_dataset_can_use_shared_selena_and_independent_runtime_xml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = tmp_path / "probe"
    (probe / "iso" / "data").mkdir(parents=True)
    (probe / "iso" / "data" / "one.MF4").write_bytes(b"payload")
    bundle = SimpleNamespace(
        manifest=SimpleNamespace(
            id="selena-bundle:sha256:" + "b" * 64,
            source=SimpleNamespace(branch="main"), files=()
        ),
        internal_project="demo",
        storage_ref="shared://selena-bundles/demo/runtime.zip",
    )
    dataset = SimpleNamespace(id="dataset:sha256:" + "c" * 64, source_kind="agent_upload")
    config = {
        "simulation": {},
        "cluster": {"selena_exe": "//cluster/share/selena/Selena.exe"},
    }
    resources = {
        "dataset": _resource("dataset", [_entry("data/one.MF4", 7, "dataset")]),
    }
    job = {
        "job_id": "job-mixed-dataset",
        "owner": "alice",
        "spec": {
            "selena": {"runtime_xml": "//cluster/share/selena/Runtime.xml"},
            "simulation": {"target": "cluster"},
        },
        "resolved_spec": {"decisions": {"transfers": {"status": "partial", "resources": resources}}},
    }

    class ForbiddenRuntimeStore:
        def resolve_location(self, _ref):
            raise AssertionError("shared Selena must not be archived on Linux")

    context = SimpleNamespace(
        server_probe_root=probe,
        storage_ref_resolver=None,
        config_loader=lambda _project: config,
        runtime_store=ForbiddenRuntimeStore(),
        dataset_catalog=SimpleNamespace(
            resolve_location=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("direct dataset must not resolve a catalog body")
            )
        ),
        run_store=SimpleNamespace(
            create_run=lambda **_kwargs: SimpleNamespace(
                ref="cluster-run:sha256:" + "d" * 64,
                to_dict=lambda: {"ref": "cluster-run:sha256:" + "d" * 64},
            )
        ),
        work_root=tmp_path / "work",
    )
    captured = _preflight_capture(monkeypatch, tmp_path, bundle=bundle, dataset=dataset, config=config, context=context)

    execute_cluster_preflight(context, job)
    assert captured["kwargs"]["copy_data"] is False
    assert captured["kwargs"]["copy_selena"] is False
    assert captured["config"]["cluster"]["selena_exe"].replace("\\", "/") == "//cluster/share/selena/Selena.exe"
    assert captured["config"]["simulation"]["runtime_xml"].replace("\\", "/") == "//cluster/share/selena/Runtime.xml"


def test_direct_selena_and_runtime_xml_can_use_shared_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = tmp_path / "probe"
    for relative, content in (
        ("iso/selena/Selena.exe", b"exe-body"),
        ("iso/selena/required.dll", b"dll-body"),
        ("iso/runtime/Runtime.xml", b"xml-body"),
    ):
        path = probe / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    bundle = SimpleNamespace(
        manifest=SimpleNamespace(
            id="selena-bundle:sha256:" + "b" * 64,
            source=SimpleNamespace(branch="main"),
            files=(
                SimpleNamespace(role="entrypoint", relative_path="selena/Selena.exe"),
                SimpleNamespace(role="runtime_library", relative_path="selena/required.dll"),
            ),
        ),
        internal_project="demo",
        storage_ref="shared://selena-bundles/demo/runtime.zip",
    )
    dataset = SimpleNamespace(id="dataset:sha256:" + "c" * 64, source_kind="shared_path")
    config = {"simulation": {}, "cluster": {}}
    resources = {
        "runtime_bundle": _resource(
            "runtime_bundle",
            [
                _entry("selena/Selena.exe", 8, "exe"),
                _entry("selena/required.dll", 8, "dll"),
            ],
            root="//cluster/share",
        ),
        "runtime_xml": _resource(
            "runtime_xml", [_entry("runtime/Runtime.xml", 8, "xml")], root="//cluster/share"
        ),
    }
    # Runtime Bundle has no runtime_config entry on purpose: Runtime XML is
    # an independent transfer role and must be selected from that resource.
    resources["runtime_bundle"]["entries"][0]["role"] = "entrypoint"
    resources["runtime_bundle"]["entries"][1]["role"] = "runtime_library"
    job = {
        "job_id": "job-mixed-runtime",
        "owner": "alice",
        "spec": {"simulation": {"target": "cluster"}},
        "resolved_spec": {"decisions": {"transfers": {"status": "resolved", "resources": resources}}},
    }
    dataset_location_calls: list[tuple] = []
    context = SimpleNamespace(
        server_probe_root=probe,
        storage_ref_resolver=None,
        config_loader=lambda _project: config,
        runtime_store=SimpleNamespace(resolve_location=lambda _ref: (_ for _ in ()).throw(AssertionError("direct runtime must not archive"))),
        dataset_catalog=SimpleNamespace(
            resolve_location=lambda *args, **kwargs: dataset_location_calls.append((args, kwargs)) or "//cluster/share/dataset"
        ),
        run_store=SimpleNamespace(
            create_run=lambda **_kwargs: SimpleNamespace(
                ref="cluster-run:sha256:" + "d" * 64,
                to_dict=lambda: {"ref": "cluster-run:sha256:" + "d" * 64},
            )
        ),
        work_root=tmp_path / "work",
    )
    captured = _preflight_capture(monkeypatch, tmp_path, bundle=bundle, dataset=dataset, config=config, context=context)

    execute_cluster_preflight(context, job)
    assert dataset_location_calls, "shared dataset should use its registered worker reference"
    assert captured["kwargs"]["copy_data"] is False
    assert captured["kwargs"]["copy_selena"] is False
    assert captured["config"]["cluster"]["selena_exe"].replace("\\", "/") == "//cluster/share/iso/selena/Selena.exe"
    assert captured["config"]["simulation"]["runtime_xml"].replace("\\", "/") == "//cluster/share/iso/runtime/Runtime.xml"
