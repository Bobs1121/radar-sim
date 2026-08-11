"""Focused tests for trusted target signing and metadata-only persistence."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from core.direct_transfer import build_isolated_relative_root, generate_owner_scope
from core.transfer_service import (
    ClusterWorkspaceWhitelist,
    TransferError,
    TransferManifest,
    TransferManifestEntry,
    TransferPlan,
    TransferPlanItem,
    TransferProgress,
    TransferService,
    TransferStore,
    cleanup_partial_files,
    execute_shared_copy,
)


NOW = 1_000_000.0


def _service(tmp_path: Path) -> TransferService:
    return TransferService(
        TransferStore(tmp_path / "transfer.sqlite3"),
        trusted_root=tmp_path / "trusted-staging",
        allow_local_test_root=True,
        now_fn=lambda: NOW,
    )


def _item(
    relative_path: str = "data/sample.mf4",
    content: bytes = b"sample-content",
    *,
    checksum: bool = True,
    mtime_ns: int | None = None,
) -> TransferPlanItem:
    return TransferPlanItem(
        source_role="dataset",
        relative_path=relative_path,
        size=len(content),
        checksum=hashlib.sha256(content).hexdigest() if checksum else "",
        mtime_ns=mtime_ns,
    )


def _issue(
    service: TransferService,
    *,
    owner: str = "alice",
    job_id: str = "job_001",
    stage_id: str = "stage_prepare",
    items: tuple[TransferPlanItem, ...] | None = None,
) -> TransferPlan:
    return service.issue_plan(
        owner=owner,
        job_id=job_id,
        stage_id=stage_id,
        mode="shared_copy",
        source_role="dataset",
        items=items if items is not None else (_item(),),
        ttl_seconds=600,
    )


def _manifest(plan: TransferPlan, *, checksum: str | None = None) -> TransferManifest:
    entries = []
    for item in plan.items:
        digest = checksum or item.checksum or hashlib.sha256(b"unknown").hexdigest()
        from core.direct_transfer import make_storage_ref

        entries.append(
            TransferManifestEntry(
                relative_path=item.relative_path,
                size=item.size,
                checksum=digest,
                target_logical_ref=make_storage_ref(
                    digest,
                    transfer_id=plan.transfer_id,
                    relative_path=item.relative_path,
                ),
                mtime_ns=item.mtime_ns or 0,
                started_at=NOW + 1,
                completed_at=NOW + 2,
            )
        )
    return TransferManifest(
        transfer_id=plan.transfer_id,
        owner=plan.owner,
        owner_scope=plan.owner_scope,
        job_id=plan.job_id,
        entries=tuple(entries),
        total_bytes=sum(entry.size for entry in entries),
        started_at=NOW + 1,
        completed_at=NOW + 2,
    )


def test_service_signs_only_the_injected_trusted_root(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plan = _issue(service)
    assert plan.target_root == str(tmp_path / "trusted-staging")
    assert plan.relative_root == build_isolated_relative_root(
        plan.owner_scope, plan.job_id, plan.transfer_id
    )


def test_dual_namespace_plan_dispatches_client_root_and_resolves_probe_root(
    tmp_path: Path,
) -> None:
    client_root = r"\\windows-host\cluster-share\staging"
    probe_root = tmp_path / "linux-cluster-mount"
    service = TransferService(
        TransferStore(tmp_path / "dual.db"),
        client_target_root=client_root,
        server_probe_root=probe_root,
        allow_local_test_root=True,
        now_fn=lambda: NOW,
    )
    content = b"direct-to-cluster"
    item = _item("data/sample.mf4", content, checksum=True)
    plan = _issue(service, items=(item,))
    assert plan.client_target_root == client_root
    assert plan.target_root == client_root  # compatibility read alias only
    assert plan.to_dict()["client_target_root"] == client_root
    assert "server_probe_root" not in plan.to_dict()

    destination = probe_root / plan.relative_root / item.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_bytes(content)
    manifest = _manifest(plan)
    service.receive_manifest(manifest, owner="alice")
    assert service.resolve_storage_ref(
        manifest.entries[0].storage_ref, owner="alice", require_exists=True
    ) == destination


def test_client_cannot_report_either_namespace_root(tmp_path: Path) -> None:
    service = TransferService(
        TransferStore(tmp_path / "dual-reject.db"),
        client_target_root=r"\\windows-host\cluster-share\staging",
        server_probe_root=tmp_path / "probe",
        allow_local_test_root=True,
        now_fn=lambda: NOW,
    )
    for kwargs in (
        {"client_target_root": r"\\attacker\share"},
        {"server_probe_root": str(tmp_path / "attacker-probe")},
    ):
        with pytest.raises(TransferError) as caught:
            service.issue_plan(
                owner="alice",
                job_id="job_001",
                stage_id="stage_prepare",
                mode="shared_copy",
                source_role="dataset",
                items=[_item()],
                **kwargs,
            )
        assert caught.value.code == "client_target_root_rejected"


def test_api_caller_cannot_self_report_even_an_allowlisted_target(tmp_path: Path) -> None:
    root = tmp_path / "trusted"
    whitelist = ClusterWorkspaceWhitelist((str(root),), allow_local_test_roots=True)
    service = TransferService(TransferStore(tmp_path / "db.sqlite3"), whitelist, now_fn=lambda: NOW)
    with pytest.raises(TransferError) as caught:
        service.issue_plan(
            owner="alice",
            job_id="job_001",
            stage_id="stage_prepare",
            mode="shared_copy",
            source_role="dataset",
            items=[_item()],
            target_root=str(root),
        )
    assert caught.value.code == "client_target_root_rejected"
    assert caught.value.status_code == 403


def test_descendant_is_not_a_signable_root(tmp_path: Path) -> None:
    root = tmp_path / "trusted"
    whitelist = ClusterWorkspaceWhitelist((str(root),), allow_local_test_roots=True)
    with pytest.raises(TransferError) as caught:
        whitelist.validate_target(str(root / "caller-subdir"))
    assert caught.value.code == "transfer_target_not_allowed"


def test_no_trusted_root_returns_actionable_unavailable(tmp_path: Path) -> None:
    service = TransferService(TransferStore(tmp_path / "db.sqlite3"), now_fn=lambda: NOW)
    with pytest.raises(TransferError) as caught:
        _issue(service)
    assert caught.value.code == "cluster_direct_transfer_unavailable"
    assert caught.value.status_code == 503


def test_gateway_upload_is_explicitly_unavailable_before_persistence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(TransferError) as caught:
        service.issue_plan(
            owner="alice",
            job_id="job_001",
            stage_id="stage_prepare",
            mode="gateway_upload",
            source_role="dataset",
            items=[_item()],
        )
    assert caught.value.code == "cluster_direct_transfer_unavailable"
    assert service.get_job_transfer_status("alice", "job_001")["plans"] == []


def test_source_to_local_is_rejected_without_target_specific_windows_cache(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(TransferError) as caught:
        service.issue_plan(
            owner="alice",
            job_id="job_local",
            stage_id="stage_local",
            mode="source_to_local",
            source_role="dataset",
            items=[_item()],
        )
    assert caught.value.code == "source_to_local_unavailable"
    assert caught.value.status_code == 503
    assert service.get_job_transfer_status("alice", "job_local")["plans"] == []


def test_owner_job_transfer_isolation_is_opaque_and_unique(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plans = (
        _issue(service, owner="alice", job_id="job_1"),
        _issue(service, owner="bob", job_id="job_1"),
        _issue(service, owner="alice", job_id="job_2"),
        _issue(service, owner="alice", job_id="job_1", stage_id="stage_2"),
    )
    assert len({plan.relative_root for plan in plans}) == 4
    for plan in plans:
        assert plan.owner not in plan.relative_root
        assert plan.job_id not in plan.relative_root
        assert plan.transfer_id not in plan.relative_root


def test_plan_metadata_round_trips_through_sqlite_restart(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite3"
    service = TransferService(
        TransferStore(db),
        trusted_root=tmp_path / "trusted",
        allow_local_test_root=True,
        now_fn=lambda: NOW,
    )
    plan = service.issue_plan(
        owner="alice",
        job_id="job_001",
        stage_id="stage_prepare",
        mode="shared_copy",
        source_role="dataset",
        items=[_item()],
        source_fingerprints={"scan_id": "scan-1", "file_count": 1},
    )
    restarted = TransferService(
        TransferStore(db),
        trusted_root=tmp_path / "trusted",
        allow_local_test_root=True,
        now_fn=lambda: NOW,
    )
    assert restarted.get_plan(plan.transfer_id, owner="alice").to_dict() == plan.to_dict()


def test_file_body_like_metadata_is_rejected(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(TransferError) as caught:
        service.issue_plan(
            owner="alice",
            job_id="job_001",
            stage_id="stage_prepare",
            mode="shared_copy",
            source_role="dataset",
            items=[_item()],
            source_fingerprints={"body": b"file bytes"},
        )
    assert caught.value.code == "file_body_rejected"


def test_owner_is_required_for_plan_reads_and_cross_owner_access_is_denied(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plan = _issue(service)
    with pytest.raises(TransferError) as missing:
        service.get_plan(plan.transfer_id)
    assert missing.value.code == "transfer_identity_required"
    with pytest.raises(TransferError) as cross_owner:
        service.get_plan(plan.transfer_id, owner="bob")
    assert cross_owner.value.code == "transfer_owner_mismatch"


def test_progress_requires_owner_or_bound_owner_scope(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plan = _issue(service)
    progress = TransferProgress(
        transfer_id=plan.transfer_id,
        bytes_transferred=1,
        bytes_total=plan.items[0].size,
        current_file=plan.items[0].relative_path,
        updated_at=NOW + 1,
    )
    with pytest.raises(TransferError) as missing:
        service.report_progress(progress)
    assert missing.value.code == "transfer_identity_required"
    service.report_progress(progress, owner="alice")
    assert service.get_plan(plan.transfer_id, owner="alice").status == "in_progress"


def test_progress_cannot_update_another_owner_or_wrong_total(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plan = _issue(service)
    progress = TransferProgress(
        transfer_id=plan.transfer_id,
        owner_scope=plan.owner_scope,
        bytes_transferred=1,
        bytes_total=plan.items[0].size,
    )
    with pytest.raises(TransferError) as wrong_owner:
        service.report_progress(progress, owner="bob")
    assert wrong_owner.value.code == "transfer_owner_mismatch"
    wrong_total = TransferProgress(
        transfer_id=plan.transfer_id,
        owner_scope=plan.owner_scope,
        bytes_transferred=1,
        bytes_total=999,
    )
    with pytest.raises(TransferError) as mismatch:
        service.report_progress(wrong_total)
    assert mismatch.value.code == "progress_total_mismatch"


def test_receive_manifest_persists_metadata_without_opening_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    plan = _issue(service)
    manifest = _manifest(plan)

    def forbidden_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("receive_manifest must not open transferred files")

    monkeypatch.setattr("builtins.open", forbidden_open)
    result = service.receive_manifest(manifest, owner="alice")
    assert result["status"] == "completed"
    assert result["total_bytes"] == plan.items[0].size


def test_manifest_must_exactly_match_planned_paths_size_checksum_and_ref(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plan = _issue(service)
    good = _manifest(plan)
    entry = good.entries[0]

    cases = (
        TransferManifest(
            transfer_id=plan.transfer_id,
            owner=plan.owner,
            owner_scope=plan.owner_scope,
            job_id=plan.job_id,
            entries=(
                TransferManifestEntry(
                    relative_path="other.mf4",
                    size=entry.size,
                    checksum=entry.checksum,
                    target_logical_ref=entry.target_logical_ref,
                    completed_at=NOW + 2,
                ),
            ),
            total_bytes=entry.size,
            completed_at=NOW + 2,
        ),
        TransferManifest(
            transfer_id=plan.transfer_id,
            owner=plan.owner,
            owner_scope=plan.owner_scope,
            job_id=plan.job_id,
            entries=(
                TransferManifestEntry(
                    relative_path=entry.relative_path,
                    size=entry.size + 1,
                    checksum=entry.checksum,
                    target_logical_ref=entry.target_logical_ref,
                    completed_at=NOW + 2,
                ),
            ),
            total_bytes=entry.size + 1,
            completed_at=NOW + 2,
        ),
        TransferManifest(
            transfer_id=plan.transfer_id,
            owner=plan.owner,
            owner_scope=plan.owner_scope,
            job_id=plan.job_id,
            entries=(
                TransferManifestEntry(
                    relative_path=entry.relative_path,
                    size=entry.size,
                    checksum="0" * 64,
                    target_logical_ref=entry.target_logical_ref,
                    completed_at=NOW + 2,
                ),
            ),
            total_bytes=entry.size,
            completed_at=NOW + 2,
        ),
    )
    expected_codes = ("manifest_items_mismatch", "manifest_size_mismatch", "manifest_checksum_mismatch")
    for candidate, expected_code in zip(cases, expected_codes):
        with pytest.raises(TransferError) as caught:
            service.receive_manifest(candidate, owner="alice")
        assert caught.value.code == expected_code


def test_manifest_owner_job_and_owner_scope_are_bound_to_plan(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plan = _issue(service)
    good = _manifest(plan)
    wrong_owner = TransferManifest(
        transfer_id=good.transfer_id,
        owner="bob",
        owner_scope=good.owner_scope,
        job_id=good.job_id,
        entries=good.entries,
        total_bytes=good.total_bytes,
        completed_at=good.completed_at,
    )
    with pytest.raises(TransferError):
        service.receive_manifest(wrong_owner, owner="bob")

    wrong_scope = TransferManifest(
        transfer_id=good.transfer_id,
        owner=good.owner,
        owner_scope="f" * 64,
        job_id=good.job_id,
        entries=good.entries,
        total_bytes=good.total_bytes,
        completed_at=good.completed_at,
    )
    with pytest.raises(TransferError):
        service.receive_manifest(wrong_scope, owner="alice")


def test_manifest_resubmission_is_idempotent_but_conflicts_are_rejected(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plan = _issue(service, items=(_item(checksum=False),))
    first = _manifest(plan, checksum="1" * 64)
    assert service.receive_manifest(first, owner="alice")["status"] == "completed"
    assert service.receive_manifest(first, owner="alice")["status"] == "already_completed"
    conflict = _manifest(plan, checksum="2" * 64)
    with pytest.raises(TransferError) as caught:
        service.receive_manifest(conflict, owner="alice")
    assert caught.value.code == "manifest_conflict"


def test_client_copy_manifest_and_trusted_resolve_end_to_end(tmp_path: Path) -> None:
    content = b"radar-data" * 10_000
    source = tmp_path / "source"
    source_file = source / "data" / "sample.mf4"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(content)
    service = _service(tmp_path)
    plan = _issue(
        service,
        items=(
            _item(
                "data/sample.mf4",
                content,
                mtime_ns=source_file.stat().st_mtime_ns,
            ),
        ),
    )
    progress = []
    manifest = execute_shared_copy(
        plan,
        source_base=source,
        allow_local_test_root=True,
        chunk_size=4096,
        now_fn=lambda: NOW + 1,
        progress_callback=lambda update: progress.append(update),
    )
    assert progress[-1].bytes_transferred == len(content)
    assert manifest.entries[0].storage_ref.startswith("cluster-staging://v1/")
    assert str(tmp_path) not in manifest.entries[0].storage_ref
    service.receive_manifest(manifest, owner="alice")
    resolved = service.resolve_storage_ref(
        manifest.entries[0].storage_ref, owner="alice", require_exists=True
    )
    assert resolved.read_bytes() == content


def test_storage_ref_cannot_be_resolved_by_another_owner(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plan = _issue(service)
    manifest = _manifest(plan)
    service.receive_manifest(manifest, owner="alice")
    with pytest.raises(TransferError) as caught:
        service.resolve_storage_ref(manifest.entries[0].storage_ref, owner="bob")
    assert caught.value.code == "storage_ref_not_found"


def test_trusted_resolve_checks_existence_and_manifest_size(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plan = _issue(service)
    manifest = _manifest(plan)
    service.receive_manifest(manifest, owner="alice")
    with pytest.raises(TransferError) as missing:
        service.resolve_storage_ref(manifest.entries[0].storage_ref, owner="alice")
    assert missing.value.code == "storage_ref_unresolvable"


def test_cancel_is_owner_scoped_and_persistent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plan = _issue(service)
    with pytest.raises(TransferError):
        service.cancel_transfer(plan.transfer_id, owner="bob")
    assert service.cancel_transfer(plan.transfer_id, owner="alice")["status"] == "cancelled"
    assert service.cancel_transfer(plan.transfer_id, owner="alice")["status"] == "cancelled"
    assert service.get_plan(plan.transfer_id, owner="alice").status == "cancelled"


def test_cleanup_rejects_client_selected_target_and_removes_only_partial(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plan = _issue(service)
    with pytest.raises(TransferError) as rejected:
        cleanup_partial_files(
            plan,
            target_base=tmp_path / "other",
            allow_local_test_root=True,
        )
    assert rejected.value.code == "client_target_root_rejected"

    from core.direct_transfer import make_storage_ref, resolve_storage_ref

    destination = resolve_storage_ref(
        make_storage_ref(
            plan.items[0].checksum,
            transfer_id=plan.transfer_id,
            relative_path=plan.items[0].relative_path,
        ),
        plan.as_kernel_plan(),
        relative_path=plan.items[0].relative_path,
        trusted_root=plan.target_root,
        allow_local_test=True,
    )
    partial = destination.with_name(destination.name + ".partial")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"partial")
    assert cleanup_partial_files(plan, allow_local_test_root=True) == 1
    assert not partial.exists()


def test_plan_and_manifest_dict_round_trip_for_thin_clients(tmp_path: Path) -> None:
    service = _service(tmp_path)
    plan = _issue(service)
    assert TransferPlan.from_dict(plan.to_dict()) == plan
    manifest = _manifest(plan)
    assert TransferManifest.from_dict(manifest.to_dict()) == manifest


def test_workspace_whitelist_reads_nested_direct_transfer_deployment() -> None:
    whitelist = ClusterWorkspaceWhitelist.from_config(
        {
            "cluster": {
                "direct_transfer": {
                    "client_target_root": r"\\cluster-host\rsim-share\staging",
                    "server_probe_root": "/mnt/cluster/rsim-share/staging",
                }
            }
        }
    )

    assert whitelist.client_target_root == r"\\cluster-host\rsim-share\staging"
    assert whitelist.server_probe_root == "/mnt/cluster/rsim-share/staging"


def test_source_change_is_returned_as_stable_service_error(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.bin").write_bytes(b"new")
    service = _service(tmp_path)
    plan = _issue(service, items=(_item("file.bin", b"old"),))
    with pytest.raises(TransferError) as caught:
        execute_shared_copy(
            plan,
            source_base=source,
            allow_local_test_root=True,
            now_fn=lambda: NOW + 1,
        )
    assert caught.value.code == "source_changed_during_transfer"
    assert caught.value.status_code == 409
