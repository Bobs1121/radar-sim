"""Focused tests for the stdlib-only direct-transfer kernel."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from core.direct_transfer import (
    DirectTransferError,
    GatewayUnavailableError,
    SourceChangedError,
    TransferCancelled,
    TransferPlan,
    TransferSource,
    build_isolated_relative_root,
    execute_transfer,
    generate_opaque_id,
    generate_owner_scope,
    make_storage_ref,
    resolve_storage_ref,
    validate_transfer_root,
)


def _plan(
    trusted_root: Path | str,
    *,
    owner: str = "alice",
    job_id: str = "job_001",
    stage_id: str = "stage_prepare",
    mode: str = "shared_copy",
    transfer_id: str = "",
) -> TransferPlan:
    transfer_id = transfer_id or generate_opaque_id()
    owner_scope = generate_owner_scope(owner, job_id)
    return TransferPlan(
        transfer_id=transfer_id,
        owner_scope=owner_scope,
        job_id=job_id,
        stage_id=stage_id,
        mode=mode,
        source_role="dataset",
        target_root=str(trusted_root),
        relative_root=build_isolated_relative_root(owner_scope, job_id, transfer_id),
        expires_at=time.time() + 3600,
    )


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _target(plan: TransferPlan, root: Path, relative_path: str, *, exists: bool = False) -> Path:
    return resolve_storage_ref(
        make_storage_ref(
            "a" * 64,
            transfer_id=plan.transfer_id,
            relative_path=relative_path,
        ),
        plan,
        relative_path=relative_path,
        trusted_root=root,
        allow_local_test=True,
        require_exists=exists,
    )


def test_plan_requires_exact_owner_job_transfer_isolation(tmp_path: Path) -> None:
    owner_scope = generate_owner_scope("alice", "job_001")
    transfer_id = generate_opaque_id()
    with pytest.raises(DirectTransferError, match="not owner/job/transfer isolated"):
        TransferPlan(
            transfer_id=transfer_id,
            owner_scope=owner_scope,
            job_id="job_001",
            stage_id="stage_prepare",
            mode="shared_copy",
            source_role="dataset",
            target_root=str(tmp_path / "target"),
            relative_root="user-controlled/path",
            expires_at=time.time() + 60,
        )


def test_isolation_path_contains_no_raw_owner_job_or_transfer(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "target", owner="alice@example.com", job_id="secret_job")
    target = _target(plan, tmp_path / "target", "data/file.mf4")
    rendered = str(target).lower()
    assert "alice" not in rendered
    assert "secret_job" not in rendered
    assert plan.transfer_id.lower() not in rendered
    assert "/o_" in plan.relative_root and "/j_" in plan.relative_root and "/t_" in plan.relative_root


def test_owner_job_and_transfer_each_change_destination(tmp_path: Path) -> None:
    roots = []
    for owner, job, transfer in (
        ("alice", "job_1", "transfer:sha256:" + "1" * 64),
        ("bob", "job_1", "transfer:sha256:" + "1" * 64),
        ("alice", "job_2", "transfer:sha256:" + "1" * 64),
        ("alice", "job_1", "transfer:sha256:" + "2" * 64),
    ):
        plan = _plan(tmp_path / "target", owner=owner, job_id=job, transfer_id=transfer)
        roots.append(_target(plan, tmp_path / "target", "file.bin").parent)
    assert len(set(roots)) == 4


def test_unc_root_is_supported_and_device_roots_are_rejected() -> None:
    assert validate_transfer_root(r"\\server\share\staging") == r"\\server\share\staging"
    for unsafe in (r"\\?\C:\staging", r"\\.\PhysicalDrive0", r"\??\C:\staging"):
        with pytest.raises(DirectTransferError, match="device"):
            validate_transfer_root(unsafe, allow_local=True)


def test_local_root_requires_explicit_test_opt_in(tmp_path: Path) -> None:
    with pytest.raises(DirectTransferError, match="UNC"):
        validate_transfer_root(tmp_path / "target")
    assert validate_transfer_root(tmp_path / "target", allow_local=True)


def test_plan_target_must_equal_injected_trusted_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "file.bin", b"x")
    plan = _plan(tmp_path / "signed")
    with pytest.raises(DirectTransferError, match="not the trusted root"):
        execute_transfer(
            plan,
            source,
            ["file.bin"],
            trusted_root=tmp_path / "attacker-selected",
            allow_local_test=True,
        )


def test_plan_serializes_only_client_namespace_and_probe_resolves_same_relative_root(
    tmp_path: Path,
) -> None:
    """A server probe is a deployment mapping, never client-supplied plan data."""

    client_root = tmp_path / "client-root"
    probe_root = tmp_path / "linux-mount"
    plan = _plan(client_root)
    payload = plan.to_dict()
    assert payload["client_target_root"] == str(client_root)
    assert "server_probe_root" not in payload

    content = b"probe-only"
    target = probe_root / plan.relative_root / "data" / "file.mf4"
    _write(target, content)
    ref = make_storage_ref(
        hashlib.sha256(content).hexdigest(),
        transfer_id=plan.transfer_id,
        relative_path="data/file.mf4",
    )
    resolved = resolve_storage_ref(
        ref,
        plan,
        relative_path="data/file.mf4",
        server_probe_root=probe_root,
        allow_local_test=True,
        expected_size=len(content),
        expected_sha256=hashlib.sha256(content).hexdigest(),
        require_exists=True,
    )
    assert resolved == target


@pytest.mark.parametrize(
    "relative_path",
    ["../outside.bin", "/absolute.bin", r"C:\outside.bin", "sub//file.bin", "NUL.txt"],
)
def test_unsafe_source_paths_are_rejected(tmp_path: Path, relative_path: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plan = _plan(tmp_path / "target")
    with pytest.raises(DirectTransferError):
        execute_transfer(
            plan,
            source,
            [relative_path],
            trusted_root=tmp_path / "target",
            allow_local_test=True,
        )


def test_nested_copy_streams_hash_and_publishes_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source"
    content = b"radar" * 50_000
    _write(source / "nested" / "sample.mf4", content)
    plan = _plan(tmp_path / "target")

    manifest = execute_transfer(
        plan,
        source,
        ["nested/sample.mf4"],
        trusted_root=tmp_path / "target",
        allow_local_test=True,
        chunk_size=4096,
    )

    entry = manifest.entries[0]
    destination = resolve_storage_ref(
        entry.storage_ref,
        plan,
        relative_path=entry.relative_path,
        trusted_root=tmp_path / "target",
        allow_local_test=True,
        expected_size=len(content),
        require_exists=True,
    )
    assert destination.read_bytes() == content
    assert entry.relative_path == "nested/sample.mf4"
    assert entry.sha256 == hashlib.sha256(content).hexdigest()
    assert not destination.with_name(destination.name + ".partial").exists()


def test_partial_resume_hashes_prefix_and_remainder(tmp_path: Path) -> None:
    source = tmp_path / "source"
    content = b"A" * 100_000 + b"B" * 125_000
    _write(source / "big.bin", content)
    plan = _plan(tmp_path / "target")
    destination = _target(plan, tmp_path / "target", "big.bin")
    partial = destination.with_name(destination.name + ".partial")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(content[:73_000])
    progress = []

    manifest = execute_transfer(
        plan,
        source,
        ["big.bin"],
        trusted_root=tmp_path / "target",
        allow_local_test=True,
        chunk_size=4096,
        progress_callback=lambda rel, done, total: progress.append((rel, done, total)),
    )

    assert manifest.entries[0].sha256 == hashlib.sha256(content).hexdigest()
    assert destination.read_bytes() == content
    assert progress[0][1] == 73_000
    assert progress[-1][1:] == (len(content), len(content))


def test_corrupt_partial_is_restarted_instead_of_appended(tmp_path: Path) -> None:
    source = tmp_path / "source"
    content = b"correct-content" * 5000
    _write(source / "file.bin", content)
    plan = _plan(tmp_path / "target")
    destination = _target(plan, tmp_path / "target", "file.bin")
    partial = destination.with_name(destination.name + ".partial")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"wrong" * 1000)

    execute_transfer(
        plan,
        source,
        ["file.bin"],
        trusted_root=tmp_path / "target",
        allow_local_test=True,
        chunk_size=1024,
    )
    assert destination.read_bytes() == content


def test_idempotent_retry_reuses_matching_published_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "file.bin", b"same-content")
    plan = _plan(tmp_path / "target")
    first = execute_transfer(
        plan,
        source,
        ["file.bin"],
        trusted_root=tmp_path / "target",
        allow_local_test=True,
    )
    second = execute_transfer(
        plan,
        source,
        ["file.bin"],
        trusted_root=tmp_path / "target",
        allow_local_test=True,
    )
    assert first.entries[0].status == "completed"
    assert second.entries[0].status == "skipped"
    assert second.entries[0].sha256 == first.entries[0].sha256


def test_cancellation_removes_partial_but_not_published_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "file.bin", b"x" * 200_000)
    plan = _plan(tmp_path / "target")
    calls = 0

    def cancel() -> bool:
        nonlocal calls
        calls += 1
        return calls > 3

    with pytest.raises(TransferCancelled):
        execute_transfer(
            plan,
            source,
            ["file.bin"],
            trusted_root=tmp_path / "target",
            allow_local_test=True,
            cancel_callback=cancel,
            chunk_size=1024,
        )
    destination = _target(plan, tmp_path / "target", "file.bin")
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".partial").exists()


def test_source_size_change_during_copy_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_file = source / "file.bin"
    _write(source_file, b"x" * 30_000)
    plan = _plan(tmp_path / "target")
    calls = 0

    def cancel_probe() -> bool:
        nonlocal calls
        calls += 1
        if calls == 3:
            with source_file.open("ab") as stream:
                stream.write(b"changed")
        return False

    with pytest.raises(SourceChangedError, match="size or mtime"):
        execute_transfer(
            plan,
            source,
            ["file.bin"],
            trusted_root=tmp_path / "target",
            allow_local_test=True,
            cancel_callback=cancel_probe,
            chunk_size=1024,
        )


def test_source_mtime_only_change_during_copy_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_file = source / "file.bin"
    _write(source_file, b"x" * 30_000)
    original = source_file.stat().st_mtime_ns
    plan = _plan(tmp_path / "target")
    calls = 0

    def cancel_probe() -> bool:
        nonlocal calls
        calls += 1
        if calls == 3:
            os.utime(source_file, ns=(original + 1_000_000_000, original + 1_000_000_000))
        return False

    with pytest.raises(SourceChangedError, match="size or mtime"):
        execute_transfer(
            plan,
            source,
            ["file.bin"],
            trusted_root=tmp_path / "target",
            allow_local_test=True,
            cancel_callback=cancel_probe,
            chunk_size=1024,
        )


def test_planned_size_digest_and_mtime_are_enforced(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_file = source / "file.bin"
    _write(source_file, b"current")
    plan = _plan(tmp_path / "target")
    with pytest.raises(SourceChangedError):
        execute_transfer(
            plan,
            source,
            [TransferSource("file.bin", size=999)],
            trusted_root=tmp_path / "target",
            allow_local_test=True,
        )
    with pytest.raises(SourceChangedError):
        execute_transfer(
            plan,
            source,
            [TransferSource("file.bin", size=7, sha256="0" * 64)],
            trusted_root=tmp_path / "target",
            allow_local_test=True,
        )
    with pytest.raises(SourceChangedError):
        execute_transfer(
            plan,
            source,
            [TransferSource("file.bin", size=7, mtime_ns=1)],
            trusted_root=tmp_path / "target",
            allow_local_test=True,
        )


def test_progress_callback_reports_relative_path_and_completion(tmp_path: Path) -> None:
    source = tmp_path / "source"
    content = b"x" * 20_000
    _write(source / "sub" / "file.bin", content)
    plan = _plan(tmp_path / "target")
    events = []
    execute_transfer(
        plan,
        source,
        ["sub/file.bin"],
        trusted_root=tmp_path / "target",
        allow_local_test=True,
        chunk_size=1024,
        progress_callback=lambda *event: events.append(event),
    )
    assert events
    assert events[-1] == ("sub/file.bin", len(content), len(content))


def test_storage_ref_is_path_free_and_bound_to_transfer_and_entry(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "target")
    ref = make_storage_ref(
        "b" * 64, transfer_id=plan.transfer_id, relative_path="secret/folder/file.bin"
    )
    assert "secret" not in ref
    assert "folder" not in ref
    assert str(tmp_path).replace("\\", "/") not in ref

    with pytest.raises(DirectTransferError, match="another manifest entry"):
        resolve_storage_ref(
            ref,
            plan,
            relative_path="other.bin",
            trusted_root=tmp_path / "target",
            allow_local_test=True,
        )
    other = _plan(tmp_path / "target")
    with pytest.raises(DirectTransferError, match="another transfer"):
        resolve_storage_ref(
            ref,
            other,
            relative_path="secret/folder/file.bin",
            trusted_root=tmp_path / "target",
            allow_local_test=True,
        )


def test_manifest_never_contains_absolute_source_or_target_paths(tmp_path: Path) -> None:
    source = tmp_path / "private-source"
    _write(source / "nested" / "file.bin", b"content")
    plan = _plan(tmp_path / "private-target")
    manifest = execute_transfer(
        plan,
        source,
        ["nested/file.bin"],
        trusted_root=tmp_path / "private-target",
        allow_local_test=True,
    )
    payload = str(manifest.to_dict())
    assert str(source) not in payload
    assert str(tmp_path / "private-target") not in payload
    assert manifest.entries[0].relative_path == "nested/file.bin"


def test_symlink_source_escape_is_rejected_when_supported(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    link = source / "link.bin"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)
    plan = _plan(tmp_path / "target")
    with pytest.raises(DirectTransferError, match="symlink|reparse"):
        execute_transfer(
            plan,
            source,
            ["link.bin"],
            trusted_root=tmp_path / "target",
            allow_local_test=True,
        )


def test_symlink_target_escape_is_rejected_when_supported(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "file.bin", b"content")
    plan = _plan(tmp_path / "target")
    destination = _target(plan, tmp_path / "target", "sub/file.bin")
    destination.parent.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        destination.parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)
    with pytest.raises(DirectTransferError, match="symlink|reparse"):
        execute_transfer(
            plan,
            source,
            ["sub/file.bin"],
            trusted_root=tmp_path / "target",
            allow_local_test=True,
        )


def test_duplicate_file_entries_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "file.bin", b"content")
    plan = _plan(tmp_path / "target")
    with pytest.raises(DirectTransferError, match="duplicate"):
        execute_transfer(
            plan,
            source,
            ["file.bin", "file.bin"],
            trusted_root=tmp_path / "target",
            allow_local_test=True,
        )


def test_expired_plan_is_rejected_before_copy(tmp_path: Path) -> None:
    valid = _plan(tmp_path / "target")
    expired = TransferPlan(
        transfer_id=valid.transfer_id,
        owner_scope=valid.owner_scope,
        job_id=valid.job_id,
        stage_id=valid.stage_id,
        mode=valid.mode,
        source_role=valid.source_role,
        target_root=valid.target_root,
        relative_root=valid.relative_root,
        expires_at=1.0,
    )
    with pytest.raises(DirectTransferError, match="expired"):
        execute_transfer(
            expired,
            tmp_path,
            ["missing.bin"],
            trusted_root=tmp_path / "target",
            allow_local_test=True,
            now_fn=lambda: 2.0,
        )


def test_gateway_upload_is_explicitly_unavailable(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "target", mode="gateway_upload")
    with pytest.raises(GatewayUnavailableError) as caught:
        execute_transfer(
            plan,
            tmp_path,
            ["missing.bin"],
            trusted_root=tmp_path / "target",
            allow_local_test=True,
        )
    assert caught.value.code == "cluster_direct_transfer_unavailable"
