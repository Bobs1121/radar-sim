"""Focused contract tests for the Windows Agent direct-transfer adapter."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from cli import agent as agent_module
from cli.agent import (
    _ControlClient,
    _dataset_transfer_fingerprints,
    _raw_sha256,
    _scan_direct_transfer_items,
)
from cli.agent import _direct_transfer_asset
from core.datasets import DatasetFileRef
from core.direct_transfer import (
    TransferPlan,
    TransferPlanItem,
    _TransferProgressReporter,
    build_isolated_relative_root,
    generate_opaque_id,
    generate_owner_scope,
)
from core.transfer_service import TransferProgress


class _FakeTransferClient(_ControlClient):
    def __init__(self, plan: TransferPlan):
        super().__init__("http://unused", timeout=5)
        self.plan = plan
        self.progress = []
        self.manifest = None

    def report_transfer_progress(self, progress, *, owner=""):
        self.progress.append(progress)
        return {"status": "in_progress"}

    def report_transfer_manifest(self, manifest, *, owner=""):
        self.manifest = manifest
        return {"status": "completed"}


class _RoleTransferClient(_FakeTransferClient):
    def __init__(self, target: Path):
        super().__init__(None)  # type: ignore[arg-type]
        self.target = target
        self.plans: list[tuple[str, list[dict]]] = []
        self.fingerprints: dict[str, dict] = {}

    def issue_transfer_plan(self, *, owner, job_id, stage_id, mode, source_role, items, source_fingerprints=None, ttl_seconds=86400.0):
        self.plans.append((source_role, list(items)))
        self.fingerprints[source_role] = dict(source_fingerprints or {})
        transfer_id = generate_opaque_id()
        scope = generate_owner_scope(owner, job_id)
        return TransferPlan(
            transfer_id=transfer_id,
            owner_scope=scope,
            job_id=job_id,
            stage_id=stage_id,
            mode="shared_copy",
            source_role=source_role,
            client_target_root=str(self.target),
            relative_root=build_isolated_relative_root(scope, job_id, transfer_id),
            items=tuple(TransferPlanItem.from_dict(item) for item in items),
            expires_at=10_000_000_000,
            owner=owner,
        )

    def execute_transfer_plan(self, plan, *, source_root, owner="", cancel_check=None, progress_callback=None, chunk_size=1024 * 1024, allow_local_test=False):
        return super().execute_transfer_plan(
            plan,
            source_root=source_root,
            owner=owner,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
            chunk_size=chunk_size,
            allow_local_test=True,
        )


def test_agent_scan_preserves_complete_directory_and_normalizes_checksums(tmp_path: Path):
    source = tmp_path / "Selena"
    (source / "bin").mkdir(parents=True)
    (source / "bin" / "Selena.exe").write_bytes(b"exe")
    (source / "bin" / "core.dll").write_bytes(b"dll")

    items = _scan_direct_transfer_items(source, source_role="runtime_bundle")

    assert [item["relative_path"] for item in items] == ["bin/core.dll", "bin/Selena.exe"]
    assert {item["size"] for item in items} == {3}
    assert all(item["checksum"] == "" for item in items)
    assert _raw_sha256("sha256:" + "a" * 64) == "a" * 64
    assert _raw_sha256("b" * 64) == "b" * 64


def test_agent_dataset_transfer_fingerprints_project_rl_to_radar_rl(monkeypatch, tmp_path: Path):
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


def test_agent_execute_plan_writes_bytes_and_only_reports_metadata(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    file = source / "one.MF4"
    file.write_bytes(b"mf4")
    target = tmp_path / "target"
    target.mkdir()
    transfer_id = generate_opaque_id()
    owner_scope = generate_owner_scope("alice", "job-1")
    item = TransferPlanItem(
        source_role="dataset",
        relative_path="one.MF4",
        size=3,
        checksum="" + __import__("hashlib").sha256(b"mf4").hexdigest(),
        mtime_ns=file.stat().st_mtime_ns,
    )
    plan = TransferPlan(
        transfer_id=transfer_id,
        owner_scope=owner_scope,
        job_id="job-1",
        stage_id="stage-1",
        mode="shared_copy",
        source_role="dataset",
        client_target_root=str(target),
        relative_root=build_isolated_relative_root(owner_scope, "job-1", transfer_id),
        items=(item,),
        expires_at=10_000_000_000,
        owner="alice",
    )
    client = _FakeTransferClient(plan)

    manifest = client.execute_transfer_plan(
        plan,
        source_root=source,
        owner="alice",
        allow_local_test=True,
    )

    assert (target / plan.relative_root / "one.MF4").read_bytes() == b"mf4"
    assert manifest.transfer_id == transfer_id
    assert client.progress
    assert client.manifest == manifest


def test_agent_execute_plan_throttles_http_but_keeps_local_chunk_callbacks(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    file = source / "large.MF4"
    file.write_bytes(b"x" * 100)
    target = tmp_path / "target"
    target.mkdir()
    transfer_id = generate_opaque_id()
    owner_scope = generate_owner_scope("alice", "job-throttle")
    item = TransferPlanItem(
        source_role="dataset",
        relative_path=file.name,
        size=100,
        checksum=__import__("hashlib").sha256(file.read_bytes()).hexdigest(),
        mtime_ns=file.stat().st_mtime_ns,
    )
    plan = TransferPlan(
        transfer_id=transfer_id,
        owner_scope=owner_scope,
        job_id="job-throttle",
        stage_id="stage-throttle",
        mode="shared_copy",
        source_role="dataset",
        client_target_root=str(target),
        relative_root=build_isolated_relative_root(owner_scope, "job-throttle", transfer_id),
        items=(item,),
        expires_at=10_000_000_000,
        owner="alice",
    )
    client = _FakeTransferClient(plan)
    local: list[TransferProgress] = []

    manifest = client.execute_transfer_plan(
        plan,
        source_root=source,
        owner="alice",
        progress_callback=local.append,
        chunk_size=1,
        allow_local_test=True,
    )

    assert manifest.total_bytes == 100
    assert len(local) == 100  # every one-byte kernel event remains local
    assert len(client.progress) < len(local)  # HTTP progress is throttled
    assert client.progress[0].bytes_transferred == 1
    assert client.progress[-1].bytes_transferred == 100


def test_transfer_progress_callback_is_per_event_while_http_updates_are_throttled():
    """Local rendering stays smooth while control-plane progress is sparse."""

    network: list[TransferProgress] = []
    local: list[TransferProgress] = []
    reporter = _TransferProgressReporter(
        network.append,
        local.append,
        # Keep this deterministic: only the first and forced terminal update
        # should publish for the small sequence below.
        min_interval_sec=10_000.0,
        min_fraction=0.5,
        min_bytes=10_000,
        clock=lambda: 0.0,
    )
    for completed in (1, 2, 3, 4):
        reporter.emit(
            TransferProgress("transfer:test", completed, 100, "one.MF4")
        )
    reporter.finish(TransferProgress("transfer:test", 100, 100, "one.MF4"))

    assert [progress.bytes_transferred for progress in local] == [1, 2, 3, 4, 100]
    assert [progress.bytes_transferred for progress in network] == [1, 100]


def test_agent_transfers_selena_and_each_config_asset_as_independent_role(tmp_path: Path):
    selena = tmp_path / "Selena"
    selena.mkdir()
    (selena / "Selena.exe").write_bytes(b"exe")
    (selena / "core.dll").write_bytes(b"dll")
    runtime = tmp_path / "Runtime.xml"
    runtime.write_bytes(b"<runtime/>")
    mat_filter = tmp_path / "MatFilter.cfg"
    mat_filter.write_bytes(b"signals=*")
    adapter = tmp_path / "adapter.ini"
    adapter.write_bytes(b"adapter=1")
    target = tmp_path / "cluster"
    target.mkdir()
    client = _RoleTransferClient(target)
    task = {"job_id": "job-assets", "task_id": "stage-assets", "payload": {}}

    for role, source in (
        ("runtime_bundle", selena),
        ("runtime_xml", runtime),
        ("mat_filter", mat_filter),
        ("adapter", adapter),
    ):
        result = _direct_transfer_asset(
            client,
            task,
            owner="alice",
            source_role=role,
            source_path=str(source),
            cancel_check=lambda: False,
        )
        assert result["source_role"] == role
        assert result["transfer_status"] == "transfer_completed"

    assert [role for role, _ in client.plans] == [
        "runtime_bundle",
        "runtime_xml",
        "mat_filter",
        "adapter",
    ]
    runtime_items = next(items for role, items in client.plans if role == "runtime_bundle")
    assert {item["relative_path"] for item in runtime_items} == {"Selena.exe", "core.dll"}


def test_agent_dataset_transfer_plan_carries_radar_fingerprints(monkeypatch, tmp_path: Path):
    dataset = tmp_path / "dataset.MF4"
    dataset.write_bytes(b"mf4")
    target = tmp_path / "cluster"
    target.mkdir()
    client = _RoleTransferClient(target)
    monkeypatch.setattr(
        "core.simulation.detect_radar_transfer_metadata_safe",
        lambda _path: {"radar_source": "RadarRL", "radar_mounting_position": "CRL"},
    )

    _direct_transfer_asset(
        client,
        {"job_id": "job-dataset", "task_id": "stage-dataset", "payload": {}},
        owner="alice",
        source_role="dataset",
        source_path=str(dataset),
        cancel_check=lambda: False,
    )

    assert client.fingerprints["dataset"]["radar_source"] == "RadarRL"
    assert client.fingerprints["dataset"]["radar_mounting_position"] == "CRL"


def test_agent_mixed_shared_dataset_skips_lease_and_transfers_only_local_assets(monkeypatch, tmp_path: Path):
    runtime_xml = tmp_path / "Runtime.xml"
    runtime_xml.write_bytes(b"<runtime/>")
    mat_filter = tmp_path / "MatFilter.cfg"
    mat_filter.write_bytes(b"signals=*")
    calls: list[str] = []

    class ForbiddenLeaseStore:
        def __init__(self):
            raise AssertionError("shared dataset must not create an AgentDataLease")

    class Client:
        def __init__(self):
            self.results = []

        def append_logs(self, _task_id, _lines):
            pass

        def heartbeat(self, _agent_id, **_kwargs):
            return {"cancel_requested": False}

        def submit_result(self, _task_id, **kwargs):
            self.results.append(kwargs)

    def transfer_asset(_client, _task, *, owner, source_role, source_path, cancel_check):
        calls.append(source_role)
        return {
            "source_role": source_role,
            "transfer_status": "transfer_completed",
            "transfer_id": f"transfer:{source_role}",
            "storage_refs": [],
        }

    monkeypatch.setattr("core.agent_data_lease.AgentDataLeaseStore", ForbiddenLeaseStore)
    monkeypatch.setattr("core.agent_data_bindings.AgentDataBindingStore", lambda: object())
    monkeypatch.setattr(agent_module, "_direct_transfer_asset", transfer_asset)
    client = Client()
    task = {
        "task_id": "stage-mixed-shared",
        "task_type": "prepare_data",
        "stage_type": "prepare_data",
        "attempt_count": 1,
        "owner": "alice",
        "payload": {
            "dispatch_scope": "direct_transfer",
            "data_path": "//cluster/share/measurements",
            "source_paths": [
                {"source_role": "runtime_xml", "path": str(runtime_xml)},
                {"source_role": "mat_filter", "path": str(mat_filter)},
            ],
        },
    }

    assert agent_module._run_v5_prepare_data(
        client, "agent-a", task, heartbeat_interval=1
    ) == 0
    assert client.results[0]["status"] == "succeeded"
    assert calls == ["runtime_xml", "mat_filter"]
    assert "dataset_id" not in client.results[0]["result"]


def test_agent_mixed_local_dataset_skips_shared_assets_and_uses_metadata_lease(monkeypatch, tmp_path: Path):
    data = tmp_path / "measurements"
    data.mkdir()
    input_file = data / "one.MF4"
    input_file.write_bytes(b"mf4")
    mtime_ns = input_file.stat().st_mtime_ns
    issue_items: list[dict] = []

    class LeaseStore:
        def create(self, payload, bindings, *, stage_id, attempt, checksum, cancel_requested):
            assert checksum is False
            return SimpleNamespace(
                lease_id="data-lease:sha256:" + "a" * 32,
                files=(DatasetFileRef("one.MF4", 3, mtime_ns=mtime_ns),),
                project="project-a",
                binding_id="data-root:sha256:" + "b" * 24,
                source_path=data,
            )

    class Entry:
        relative_path = "one.MF4"
        size = 3
        checksum = "c" * 64
        storage_ref = "cluster-staging://v1/one"

        def to_dict(self):
            return {
                "relative_path": self.relative_path,
                "size": self.size,
                "checksum": self.checksum,
                "storage_ref": self.storage_ref,
                "mtime_ns": mtime_ns,
            }

    class Manifest:
        transfer_id = "transfer:sha256:" + "d" * 64
        total_bytes = 3
        entries = (Entry(),)

        def to_dict(self):
            return {
                "transfer_id": self.transfer_id,
                "entries": [item.to_dict() for item in self.entries],
                "total_bytes": self.total_bytes,
            }

    class Client:
        def __init__(self):
            self.results = []

        def append_logs(self, _task_id, _lines):
            pass

        def heartbeat(self, _agent_id, **_kwargs):
            return {"cancel_requested": False}

        def issue_transfer_plan(self, **kwargs):
            issue_items.extend(kwargs["items"])
            return {"transfer_id": "plan"}

        def execute_transfer_plan(self, plan, **kwargs):
            return Manifest()

        def submit_result(self, _task_id, **kwargs):
            self.results.append(kwargs)

    monkeypatch.setattr("core.agent_data_lease.AgentDataLeaseStore", LeaseStore)
    monkeypatch.setattr("core.agent_data_bindings.AgentDataBindingStore", lambda: object())
    monkeypatch.setattr(
        agent_module,
        "_direct_transfer_asset",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("shared assets must not be transferred")
        ),
    )
    client = Client()
    task = {
        "task_id": "stage-mixed-local",
        "task_type": "prepare_data",
        "stage_type": "prepare_data",
        "attempt_count": 1,
        "owner": "alice",
        "job_id": "job-mixed-local",
        "payload": {
            "dispatch_scope": "direct_transfer",
            "project": "project-a",
            "data_path": str(data),
            "data_binding_id": "data-root:sha256:" + "b" * 24,
            "source_paths": [{"source_role": "dataset", "path": str(data)}],
        },
    }

    assert agent_module._run_v5_prepare_data(
        client, "agent-a", task, heartbeat_interval=1
    ) == 0
    assert client.results[0]["status"] == "succeeded"
    assert client.results[0]["result"]["dataset"]["file_count"] == 1
    assert issue_items[0]["checksum"] == ""
