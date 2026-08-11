"""Thin metadata adapter around :mod:`core.direct_transfer`.

The service signs client-facing plans and persists small control-plane state.
It never opens source or destination files.  The deployment supplies two
namespaces: ``client_target_root`` (normally a Windows UNC path sent in the
Plan) and ``server_probe_root`` (a Linux mount or explicit test root used only
when resolving a completed logical reference).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Tuple, Union

from core.direct_transfer import (
    DirectTransferError,
    GatewayUnavailableError,
    ManifestEntry,
    SOURCE_ROLES,
    TRANSFER_MODES,
    SourceChangedError,
    TransferCancelled,
    TransferManifest,
    TransferPlan,
    TransferPlanItem,
    TransferSource,
    TransferManifestEntry,
    build_isolated_relative_root,
    execute_transfer,
    generate_opaque_id,
    generate_owner_scope,
    make_storage_ref,
    resolve_storage_ref as resolve_kernel_storage_ref,
    validate_transfer_root,
)


TRANSFER_PLAN_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "failed", "cancelled", "skipped_shared", "skipped_local"}
)


class TransferError(RuntimeError):
    """Stable service error suitable for an API boundary."""

    def __init__(self, code: str, message: str, *, status_code: int = 400, detail: Any = None, actions: Optional[list[dict[str, Any]]] = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.status_code = int(status_code)
        self.detail = detail if detail is not None else {}
        self.actions = list(actions or [])


def _validate_relative_path(value: str) -> str:
    try:
        return TransferSource(relative_path=value).relative_path
    except DirectTransferError as exc:
        raise TransferError("invalid_relative_path", str(exc), status_code=422) from exc


def _validate_metadata_mapping(value: Optional[Mapping[str, Any]]) -> dict[str, str]:
    try:
        from core.direct_transfer import _metadata_dict  # one canonical validator

        return _metadata_dict(value)
    except DirectTransferError as exc:
        raise TransferError(exc.code, str(exc), status_code=422) from exc


@dataclass(frozen=True)
class TransferProgress:
    transfer_id: str
    bytes_transferred: int
    bytes_total: int
    current_file: str = ""
    status: str = "in_progress"
    updated_at: float = 0.0
    owner_scope: str = ""

    def __post_init__(self) -> None:
        if not self.transfer_id:
            raise TransferError("invalid_progress", "transfer_id is required", status_code=422)
        done, total = int(self.bytes_transferred), int(self.bytes_total)
        if done < 0 or total < 0 or done > total:
            raise TransferError("invalid_progress", "invalid progress byte counts", status_code=422)
        if self.status != "in_progress":
            raise TransferError("invalid_progress", "progress status must be in_progress", status_code=422)
        if self.current_file:
            _validate_relative_path(self.current_file)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id,
            "owner_scope": self.owner_scope,
            "bytes_transferred": int(self.bytes_transferred),
            "bytes_total": int(self.bytes_total),
            "current_file": self.current_file,
            "status": self.status,
            "updated_at": float(self.updated_at),
        }


class ClusterWorkspaceWhitelist:
    """Deployment-injected client/probe roots.

    The positional ``allowed_roots`` form remains for old callers.  New code
    should pass explicit ``client_target_root`` and ``server_probe_root``.
    Exact roots are signable; descendants and caller-selected roots are not.
    """

    def __init__(
        self,
        allowed_roots: Union[Sequence[Union[str, Path]], str, Path, None] = None,
        *,
        client_target_root: Union[str, Path, None] = None,
        server_probe_root: Union[str, Path, None] = None,
        allow_local_test_roots: bool = False,
    ) -> None:
        roots: list[str] = []
        if allowed_roots is not None:
            if isinstance(allowed_roots, (str, Path)):
                roots = [str(allowed_roots)]
            else:
                roots = [str(item) for item in allowed_roots if str(item or "").strip()]
        client = str(client_target_root or "").strip() or (roots[0] if roots else "")
        if not client:
            self.allowed_roots = ()
            self.client_target_root = ""
            self.server_probe_root = ""
            self.server_probe_configured = False
            self.allow_local_test_roots = bool(allow_local_test_roots)
            return
        try:
            client_canonical = validate_transfer_root(client, allow_local=allow_local_test_roots)
            probe_raw = str(server_probe_root or "").strip()
            # Compatibility callers passed one local trusted_root.  A
            # production explicit dual injection must provide the probe root.
            # The probe is explicitly deployment-local (Linux mount or test
            # directory), so it is valid even when the client namespace must
            # remain a production UNC root.
            probe_canonical = validate_transfer_root(probe_raw or client_canonical, allow_local=True)
            extra = [validate_transfer_root(item, allow_local=allow_local_test_roots) for item in roots]
        except DirectTransferError as exc:
            raise TransferError("invalid_trusted_transfer_root", str(exc), status_code=500) from exc
        self.client_target_root = client_canonical
        self.server_probe_root = probe_canonical
        # A production writer namespace (normally UNC) does not prove that
        # Linux can inspect those bytes. Only an explicit deployment probe,
        # or a deliberately enabled single-root local-test setup, is ready.
        self.server_probe_configured = bool(probe_raw) or bool(allow_local_test_roots)
        self.allowed_roots = tuple(dict.fromkeys([client_canonical] + extra))
        self.allow_local_test_roots = bool(allow_local_test_roots)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "ClusterWorkspaceWhitelist":
        cluster = dict(config.get("cluster") or {})
        direct = dict(cluster.get("direct_transfer") or {})
        client = (
            direct.get("client_target_root")
            or cluster.get("client_target_root")
            or cluster.get("direct_transfer_root")
            or cluster.get("workspace_root")
        )
        probe = (
            direct.get("server_probe_root")
            or cluster.get("server_probe_root")
            or cluster.get("probe_root")
        )
        extras = direct.get("allowed_staging_roots") or cluster.get("allowed_staging_roots") or ()
        return cls(extras, client_target_root=client, server_probe_root=probe)

    @property
    def trusted_root(self) -> str:
        if not self.client_target_root:
            raise TransferError(
                "cluster_direct_transfer_unavailable",
                "No deployment-managed direct transfer root is configured",
                status_code=503,
                actions=[{"type": "contact_admin", "label": "Contact deployment administrator"}],
            )
        return self.client_target_root

    def validate_target(self, value: str) -> str:
        try:
            candidate = validate_transfer_root(value, allow_local=self.allow_local_test_roots)
        except DirectTransferError as exc:
            raise TransferError("transfer_target_not_allowed", str(exc), status_code=403) from exc
        if any(_same_root(candidate, root) for root in self.allowed_roots):
            return self.client_target_root
        raise TransferError("transfer_target_not_allowed", "Target is not a deployment-managed transfer root", status_code=403)


def _same_root(left: str, right: str) -> bool:
    if str(left).replace("/", "\\").startswith("\\\\") or str(right).replace("/", "\\").startswith("\\\\"):
        return str(left).replace("/", "\\").rstrip("\\").casefold() == str(right).replace("/", "\\").rstrip("\\").casefold()
    return os.path.normcase(os.path.realpath(str(left))) == os.path.normcase(os.path.realpath(str(right)))


class TransferStore:
    """Small SQLite metadata store; no file content or physical target data."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if str(parent) not in {"", "."}:
            parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS transfer_plans (
                    transfer_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    progress_json TEXT,
                    manifest_json TEXT,
                    completed_at REAL
                )"""
            )

    def save_plan(self, plan: TransferPlan) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO transfer_plans(transfer_id,owner,job_id,status,plan_json) VALUES(?,?,?,?,?)",
                (plan.transfer_id, plan.owner, plan.job_id, plan.status, json.dumps(plan.to_dict(), sort_keys=True)),
            )

    def get_plan(self, transfer_id: str) -> Optional[TransferPlan]:
        with self._connect() as connection:
            row = connection.execute("SELECT plan_json,status FROM transfer_plans WHERE transfer_id=?", (transfer_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["plan_json"]))
        payload["status"] = str(row["status"])
        return TransferPlan.from_dict(payload)

    def get_plans_for_job(self, owner: str, job_id: str) -> list[TransferPlan]:
        with self._connect() as connection:
            rows = connection.execute("SELECT plan_json,status FROM transfer_plans WHERE owner=? AND job_id=? ORDER BY rowid", (owner, job_id)).fetchall()
        return [TransferPlan.from_dict({**json.loads(str(row["plan_json"])), "status": str(row["status"])}) for row in rows]

    def get_plans_for_owner(self, owner: str) -> list[TransferPlan]:
        with self._connect() as connection:
            rows = connection.execute("SELECT plan_json,status FROM transfer_plans WHERE owner=? ORDER BY rowid", (owner,)).fetchall()
        return [TransferPlan.from_dict({**json.loads(str(row["plan_json"])), "status": str(row["status"])}) for row in rows]

    def get_manifest(self, transfer_id: str) -> Optional[TransferManifest]:
        with self._connect() as connection:
            row = connection.execute("SELECT manifest_json FROM transfer_plans WHERE transfer_id=?", (transfer_id,)).fetchone()
        return None if row is None or not row["manifest_json"] else TransferManifest.from_dict(json.loads(str(row["manifest_json"])))

    def get_progress(self, transfer_id: str) -> Optional[TransferProgress]:
        with self._connect() as connection:
            row = connection.execute("SELECT progress_json FROM transfer_plans WHERE transfer_id=?", (transfer_id,)).fetchone()
        return None if row is None or not row["progress_json"] else TransferProgress(**json.loads(str(row["progress_json"])))

    def update_status(self, transfer_id: str, status: str, *, progress: Optional[TransferProgress] = None, manifest: Optional[TransferManifest] = None, completed_at: Optional[float] = None) -> None:
        sets, values = ["status=?"], [status]
        if progress is not None:
            sets.append("progress_json=?")
            values.append(json.dumps(progress.to_dict(), sort_keys=True))
        if manifest is not None:
            sets.append("manifest_json=?")
            values.append(json.dumps(manifest.to_dict(), sort_keys=True))
        if completed_at is not None:
            sets.append("completed_at=?")
            values.append(float(completed_at))
        values.append(transfer_id)
        with self._connect() as connection:
            cursor = connection.execute("UPDATE transfer_plans SET %s WHERE transfer_id=?" % ",".join(sets), values)
            if cursor.rowcount != 1:
                raise TransferError("transfer_not_found", "Transfer plan not found", status_code=404)


class TransferService:
    """Issue plans and persist control metadata; copying stays in the kernel."""

    def __init__(
        self,
        store: TransferStore,
        whitelist: Optional[ClusterWorkspaceWhitelist] = None,
        *,
        client_target_root: Optional[Union[str, Path]] = None,
        server_probe_root: Optional[Union[str, Path]] = None,
        trusted_root: Optional[Union[str, Path]] = None,
        allow_local_test_root: bool = False,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        if whitelist is not None and any(value is not None for value in (client_target_root, server_probe_root, trusted_root)):
            raise TransferError("conflicting_trusted_roots", "Supply whitelist or explicit deployment roots, not both")
        if whitelist is not None:
            resolved = whitelist
        elif trusted_root is not None:
            # Legacy trusted_root is an explicit local/test dual root.
            resolved = ClusterWorkspaceWhitelist(trusted_root, allow_local_test_roots=allow_local_test_root)
        else:
            resolved = ClusterWorkspaceWhitelist(client_target_root=client_target_root, server_probe_root=server_probe_root, allow_local_test_roots=allow_local_test_root)
        self._store = store
        self._whitelist = resolved
        self._now_fn = now_fn or time.time

    @property
    def client_target_root(self) -> str:
        return self._whitelist.client_target_root

    @property
    def server_probe_root(self) -> str:
        return self._whitelist.server_probe_root

    @property
    def server_probe_configured(self) -> bool:
        return bool(self._whitelist.server_probe_configured)

    def issue_plan(
        self,
        *,
        owner: str,
        job_id: str,
        stage_id: str,
        mode: str,
        source_role: str,
        items: Sequence[TransferPlanItem],
        client_target_root: str = "",
        target_root: str = "",
        server_probe_root: str = "",
        source_fingerprints: Optional[Mapping[str, Any]] = None,
        ttl_seconds: float = 86400.0,
    ) -> TransferPlan:
        # Both roots are deployment authority.  A request body cannot select
        # either namespace, even when the value happens to be allowlisted.
        if client_target_root or target_root or server_probe_root:
            raise TransferError("client_target_root_rejected", "Transfer targets are selected by deployment configuration", status_code=403)
        if mode == "gateway_upload":
            raise TransferError("cluster_direct_transfer_unavailable", "gateway_upload is not available in the P0 transfer kernel", status_code=503, actions=[{"type": "use_shared_copy", "label": "Use a mounted Cluster share"}])
        if mode == "source_to_local":
            raise TransferError(
                "source_to_local_unavailable",
                "A target-specific Windows cache adapter is not configured",
                status_code=503,
                actions=[
                    {
                        "type": "use_co_located_inputs",
                        "label": "Use inputs readable by the local simulation computer",
                    }
                ],
            )
        if mode != "shared_copy":
            raise TransferError("invalid_transfer_mode", "Unsupported transfer mode", status_code=422)
        if source_role not in SOURCE_ROLES:
            raise TransferError("invalid_source_role", "Unsupported source role", status_code=422)
        if not str(owner or "").strip() or not str(job_id or "").strip() or not str(stage_id or "").strip():
            raise TransferError("invalid_transfer_scope", "owner, job_id, and stage_id are required", status_code=422)
        ttl = float(ttl_seconds)
        if ttl <= 0:
            raise TransferError("invalid_transfer_ttl", "ttl_seconds must be positive", status_code=422)
        plan_items = tuple(items)
        try:
            if any(item.source_role != source_role for item in plan_items):
                raise TransferError("source_role_mismatch", "Plan items do not match source_role", status_code=422)
        except AttributeError as exc:
            raise TransferError("invalid_transfer_item", "Plan item is invalid", status_code=422) from exc
        paths = [item.relative_path for item in plan_items]
        if not plan_items or len(paths) != len(set(paths)):
            raise TransferError("duplicate_transfer_item" if paths else "invalid_transfer_item", "Plan item paths must be unique and non-empty", status_code=422)
        try:
            trusted = self._whitelist.trusted_root
        except TransferError:
            raise
        now = float(self._now_fn())
        transfer_id = generate_opaque_id()
        owner_scope = generate_owner_scope(owner, job_id)
        plan = TransferPlan(
            transfer_id=transfer_id,
            owner_scope=owner_scope,
            job_id=str(job_id),
            stage_id=str(stage_id),
            mode=mode,
            source_role=source_role,
            client_target_root=trusted,
            relative_root=build_isolated_relative_root(owner_scope, job_id, transfer_id),
            items=plan_items,
            owner=str(owner),
            resume=True,
            expires_at=now + ttl,
            created_at=now,
            status="pending",
            source_fingerprints=_validate_metadata_mapping(source_fingerprints),
        )
        self._store.save_plan(plan)
        return plan

    def report_progress(self, progress: TransferProgress, *, owner: str = "") -> None:
        plan = self._owned_plan(progress.transfer_id, owner=owner, owner_scope=progress.owner_scope)
        if plan.status in {"completed", "cancelled", "failed"}:
            return
        if plan.expires_at <= float(self._now_fn()):
            raise TransferError("transfer_plan_expired", "Transfer plan has expired", status_code=410)
        if progress.bytes_total != sum(item.size for item in plan.items):
            raise TransferError("progress_total_mismatch", "Progress total differs from plan", status_code=422)
        if progress.current_file and progress.current_file not in {item.relative_path for item in plan.items}:
            raise TransferError("progress_file_mismatch", "Progress file is not in the plan", status_code=422)
        self._store.update_status(plan.transfer_id, "in_progress", progress=progress)

    def receive_manifest(self, manifest: TransferManifest, *, owner: str = "") -> dict[str, Any]:
        """Validate and persist metadata only; never read a transferred file."""

        authenticated_owner = str(owner or manifest.owner or "")
        plan = self._owned_plan(manifest.transfer_id, owner=authenticated_owner, owner_scope=manifest.owner_scope)
        if manifest.owner != plan.owner or manifest.job_id != plan.job_id:
            raise TransferError("transfer_scope_mismatch", "Manifest owner/job does not match plan", status_code=403)
        if manifest.owner_scope and manifest.owner_scope != plan.owner_scope:
            raise TransferError("transfer_scope_mismatch", "Manifest owner_scope does not match plan", status_code=403)
        existing = self._store.get_manifest(plan.transfer_id)
        if existing is not None:
            if existing.to_dict() != manifest.to_dict():
                raise TransferError("manifest_conflict", "Transfer already completed with different metadata", status_code=409)
            return {"status": "already_completed", "transfer_id": plan.transfer_id}
        if plan.status == "cancelled":
            raise TransferError("transfer_cancelled", "Cancelled transfer cannot complete", status_code=409)
        if plan.expires_at <= float(self._now_fn()):
            raise TransferError("transfer_plan_expired", "Transfer plan has expired", status_code=410)
        planned = {item.relative_path: item for item in plan.items}
        received = {entry.relative_path: entry for entry in manifest.entries}
        if len(received) != len(manifest.entries) or set(received) != set(planned):
            raise TransferError("manifest_items_mismatch", "Manifest entries must exactly match the plan", status_code=422)
        for relative, entry in received.items():
            item = planned[relative]
            if entry.size != item.size:
                raise TransferError("manifest_size_mismatch", "Manifest size differs from plan", status_code=422)
            if item.mtime_ns is not None and entry.mtime_ns != item.mtime_ns:
                raise TransferError("manifest_mtime_mismatch", "Manifest mtime differs from plan", status_code=422)
            if item.checksum and entry.checksum != item.checksum:
                raise TransferError("manifest_checksum_mismatch", "Manifest checksum differs from plan", status_code=422)
            expected_ref = make_storage_ref(entry.checksum, transfer_id=plan.transfer_id, relative_path=relative)
            if entry.target_logical_ref != expected_ref:
                raise TransferError("invalid_storage_ref", "Manifest storage_ref is not bound to plan entry", status_code=422)
        self._store.update_status(plan.transfer_id, "completed", manifest=manifest, completed_at=float(self._now_fn()))
        return {"status": "completed", "transfer_id": plan.transfer_id, "entry_count": len(manifest.entries), "total_bytes": manifest.total_bytes}

    def cancel_transfer(self, transfer_id: str, *, owner: str) -> dict[str, Any]:
        plan = self._owned_plan(transfer_id, owner=owner)
        if plan.status in {"completed", "cancelled"}:
            return {"status": plan.status, "transfer_id": transfer_id}
        self._store.update_status(transfer_id, "cancelled")
        return {"status": "cancelled", "transfer_id": transfer_id}

    def get_plan(self, transfer_id: str, *, owner: str = "") -> TransferPlan:
        return self._owned_plan(transfer_id, owner=owner)

    def get_plans_for_job(self, owner: str, job_id: str) -> list[TransferPlan]:
        if not owner:
            raise TransferError("transfer_identity_required", "owner is required", status_code=403)
        return self._store.get_plans_for_job(owner, job_id)

    def get_job_transfer_status(self, owner: str, job_id: str) -> dict[str, Any]:
        plans = self.get_plans_for_job(owner, job_id)
        if not plans:
            return {"status": "no_transfers", "plans": []}
        statuses = [plan.status for plan in plans]
        if any(status == "in_progress" for status in statuses):
            aggregate = "transferring_direct_to_cluster"
        elif all(status == "completed" for status in statuses):
            aggregate = "transfer_completed"
        elif all(status == "skipped_shared" for status in statuses):
            aggregate = "transfer_skipped_shared"
        elif all(status == "skipped_local" for status in statuses):
            aggregate = "transfer_skipped_local_execution"
        elif any(status == "cancelled" for status in statuses):
            aggregate = "cancelled"
        elif any(status == "failed" for status in statuses):
            aggregate = "failed"
        else:
            aggregate = "waiting_for_local_connector"
        return {"status": aggregate, "plan_count": len(plans), "plans": [{"transfer_id": plan.transfer_id, "status": plan.status, "mode": plan.mode, "source_role": plan.source_role} for plan in plans]}

    def resolve_storage_ref(self, storage_ref: str, *, owner: str, require_exists: bool = True) -> Path:
        matches: list[tuple[TransferPlan, ManifestEntry]] = []
        for plan in self._store.get_plans_for_owner(owner):
            manifest = self._store.get_manifest(plan.transfer_id)
            if manifest is None:
                continue
            matches.extend((plan, entry) for entry in manifest.entries if entry.target_logical_ref == storage_ref)
        if len(matches) != 1:
            raise TransferError("storage_ref_not_found", "Storage reference is unavailable for this owner", status_code=404)
        plan, entry = matches[0]
        try:
            return resolve_kernel_storage_ref(
                storage_ref,
                plan,
                relative_path=entry.relative_path,
                server_probe_root=self._whitelist.server_probe_root,
                allow_local_test=self._whitelist.allow_local_test_roots,
                expected_size=entry.size,
                expected_sha256=entry.checksum,
                require_exists=require_exists,
            )
        except DirectTransferError as exc:
            raise TransferError("storage_ref_unresolvable", str(exc), status_code=409) from exc

    def _owned_plan(self, transfer_id: str, *, owner: str = "", owner_scope: str = "") -> TransferPlan:
        plan = self._store.get_plan(transfer_id)
        if plan is None:
            raise TransferError("transfer_not_found", "Transfer plan not found", status_code=404)
        if not owner and not owner_scope:
            raise TransferError("transfer_identity_required", "owner or owner_scope is required", status_code=403)
        if owner and plan.owner != owner:
            raise TransferError("transfer_owner_mismatch", "Transfer belongs to another owner", status_code=403)
        if owner_scope and plan.owner_scope != owner_scope:
            raise TransferError("transfer_owner_mismatch", "Transfer belongs to another owner scope", status_code=403)
        return plan


def execute_shared_copy(
    plan: TransferPlan,
    *,
    source_base: Union[str, Path],
    progress_callback: Optional[Callable[[TransferProgress], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    chunk_size: int = 1024 * 1024,
    now_fn: Optional[Callable[[], float]] = None,
    allow_local_test_root: bool = False,
) -> TransferManifest:
    """Thin client adapter over the canonical kernel."""

    per_file: dict[str, int] = {}
    total = sum(item.size for item in plan.items)

    def report(relative_path: str, processed: int, _file_total: int) -> None:
        per_file[relative_path] = processed
        if progress_callback:
            progress_callback(TransferProgress(plan.transfer_id, sum(per_file.values()), total, relative_path, updated_at=float((now_fn or time.time)()), owner_scope=plan.owner_scope))

    try:
        return execute_transfer(plan, source_base, plan.items, client_target_root=plan.client_target_root, allow_local_test=allow_local_test_root, cancel_callback=cancel_check, progress_callback=report, chunk_size=chunk_size, now_fn=now_fn)
    except GatewayUnavailableError as exc:
        raise TransferError("cluster_direct_transfer_unavailable", str(exc), status_code=503) from exc
    except TransferCancelled as exc:
        raise TransferError("transfer_cancelled", str(exc), status_code=499) from exc
    except SourceChangedError as exc:
        raise TransferError("source_changed_during_transfer", str(exc), status_code=409, actions=[{"type": "retry_transfer", "label": "Retry with a fresh plan"}]) from exc
    except DirectTransferError as exc:
        raise TransferError(exc.code, str(exc), status_code=500 if exc.code == "transfer_io_error" else 422) from exc


def cleanup_partial_files(plan: TransferPlan, *, target_base: Optional[Union[str, Path]] = None, allow_local_test_root: bool = False) -> int:
    """Remove only ``.partial`` files bounded by the signed client root."""

    if target_base is not None and not _same_root(str(target_base), plan.client_target_root):
        raise TransferError("client_target_root_rejected", "Cleanup target must match signed plan", status_code=403)
    removed = 0
    for item in plan.items:
        try:
            destination = resolve_kernel_storage_ref(
                make_storage_ref(item.checksum or "0" * 64, transfer_id=plan.transfer_id, relative_path=item.relative_path),
                plan,
                relative_path=item.relative_path,
                trusted_root=plan.client_target_root,
                allow_local_test=allow_local_test_root,
            )
        except DirectTransferError as exc:
            raise TransferError("invalid_transfer_plan", str(exc), status_code=422) from exc
        partial = destination.with_name(destination.name + ".partial")
        if partial.exists():
            try:
                partial.unlink()
                removed += 1
            except OSError:
                pass
    return removed


__all__ = [
    "ClusterWorkspaceWhitelist", "TransferError", "TransferManifest", "TransferManifestEntry",
    "TransferPlan", "TransferPlanItem", "TransferProgress", "TransferService", "TransferStore",
    "cleanup_partial_files", "execute_shared_copy",
]
