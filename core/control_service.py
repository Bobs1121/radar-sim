"""Minimal persistent control-plane service for agents, jobs, and task logs."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from core.simulation import normalize_radar_metadata
from core.agent_policy import windows_connector_contract_is_current

TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled", "skipped"}
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}
SUCCESS_TASK_STATUSES = {"succeeded", "skipped"}
INITIAL_TASK_STATUSES = {"queued", "skipped", "blocked"}
INTERNAL_V1_SCHEDULER_AGENT_ID = "__v1_scheduler__"
RESERVED_INTERNAL_AGENT_IDS = {INTERNAL_V1_SCHEDULER_AGENT_ID}
# A Cluster role may use several fixed worker identities.  The role root is
# still the durable task affinity written by the API; only a worker whose ID
# follows the role's ``-worker-N`` convention and whose registration metadata
# names the same role can claim that root-bound task.
_CLUSTER_WORKER_SUFFIX = "-worker-"
_CLUSTER_ROLE_NODE_KINDS = {
    "linux-v2-stage-executor": "linux_executor",
    "cluster-v2-platform-gateway": "platform_gateway",
}
_CLUSTER_WORKER_MARKER = "control_service_v1"
_CLUSTER_WORKER_PROTECTED_KEYS = (
    "cluster_worker_identity",
    "claim_group",
    "cluster_role",
    "cluster_worker_index",
    "cluster_worker_count",
)
_FAILED_MANIFEST_STATUSES = {"failed", "failure", "partial"}
_NON_SUCCESS_MANIFEST_STATUSES = {
    *_FAILED_MANIFEST_STATUSES,
    "cancelled",
    "canceled",
}
_WINDOWS_NODE_KINDS = {"windows_agent", "windows_full"}
_LEGACY_BROWSER_OWNER = re.compile(
    r"^(?:user-)?(?:web|sdk)-[0-9a-f]{24,64}$", re.IGNORECASE
)
_STABLE_HUMAN_OWNER = re.compile(
    r"^user-[a-z0-9][a-z0-9_.-]{0,127}$", re.IGNORECASE
)


def _same_connector_owner(left: str, right: str) -> bool:
    """Compare persisted Connector owners, including one v8 repair alias."""

    first = str(left or "").strip().casefold()
    second = str(right or "").strip().casefold()
    if first == second:
        return True
    if not (
        _LEGACY_BROWSER_OWNER.fullmatch(first)
        and _LEGACY_BROWSER_OWNER.fullmatch(second)
    ):
        return False
    return first.removeprefix("user-") == second.removeprefix("user-")


def _connector_owner_transition_allowed(
    existing_metadata: dict[str, Any], next_metadata: dict[str, Any]
) -> bool:
    old = str(existing_metadata.get("user") or "").strip().casefold()
    new = str(next_metadata.get("user") or "").strip().casefold()
    if _same_connector_owner(old, new):
        return True
    try:
        old_contract = int(existing_metadata.get("connector_contract_version") or 0)
        new_contract = int(next_metadata.get("connector_contract_version") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        old_contract < new_contract
        and _LEGACY_BROWSER_OWNER.fullmatch(old)
        and _STABLE_HUMAN_OWNER.fullmatch(new)
        and not _LEGACY_BROWSER_OWNER.fullmatch(new)
    )


def _validated_cluster_claim_group(agent_id: str, metadata: dict[str, Any]) -> str:
    """Return a trusted Cluster role root or an empty string.

    The allowlist and node-kind check are deliberately server-owned.  A
    generic Agent cannot opt into a role by inventing an arbitrary
    ``claim_group``/prefix in its registration metadata.
    """
    agent_id = str(agent_id or "").strip()
    group = str(metadata.get("claim_group") or "").strip()
    role = str(metadata.get("cluster_role") or "").strip()
    node_kind = str(
        metadata.get("node_kind") or metadata.get("node.kind") or ""
    ).strip()
    expected_kind = _CLUSTER_ROLE_NODE_KINDS.get(group)
    if (
        not agent_id
        or not group
        or group != role
        or expected_kind is None
        or node_kind != expected_kind
        or metadata.get("cluster_worker_identity") != _CLUSTER_WORKER_MARKER
    ):
        return ""
    if agent_id == group:
        if metadata.get("cluster_worker_index") not in {0, "0"}:
            return ""
        return group
    prefix = group + _CLUSTER_WORKER_SUFFIX
    if not agent_id.startswith(prefix):
        return ""
    suffix = agent_id[len(prefix):]
    try:
        worker_index = int(suffix)
    except (TypeError, ValueError):
        return ""
    if worker_index < 1 or metadata.get("cluster_worker_index") not in {
        worker_index, str(worker_index)
    }:
        return ""
    return group


def _claim_agent_ids(agent_id: str, metadata: dict[str, Any]) -> tuple[str, ...]:
    """Return IDs this worker may match for a queued task claim.

    ``agents.current_task_id`` is intentionally single-valued, so each pool
    worker has its own identity.  The role root is added only for a correctly
    registered Cluster worker; unrelated Agents retain exact-ID semantics.
    """
    agent_id = str(agent_id or "").strip()
    group = _validated_cluster_claim_group(agent_id, metadata)
    if not group or group == agent_id:
        return (agent_id,)
    return (agent_id, group)


def _normalize_manifest_outcome(manifest: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Normalize a manifest using only explicit status/count evidence.

    Human-readable errors are deliberately not outcome evidence: successful
    runs may retain warning text.  A positive structured failure count is safe
    evidence that an old ``status: succeeded`` manifest was contradictory.
    """
    normalized = dict(manifest or {})
    summary = normalized.get("summary") if isinstance(normalized.get("summary"), dict) else {}
    summary_failed = False
    for key in ("fail_count", "failed_count", "failed_input_count"):
        try:
            if int(summary.get(key)) > 0:
                summary_failed = True
        except (TypeError, ValueError):
            continue
    status = str(normalized.get("status") or "").strip().lower()
    if summary_failed and status not in {"partial", "failed"}:
        normalized["status"] = "failed"
        status = "failed"
    elif status == "failure":
        normalized["status"] = "failed"
        status = "failed"
    return normalized, status in _NON_SUCCESS_MANIFEST_STATUSES


def _stable_stage_failure_message(code: str) -> str:
    """Map Agent result codes to path-free operator-facing messages."""
    normalized = str(code or "").strip().lower()
    if normalized in {"selena_failed", "simulation_engine_failed", "engine_failed"}:
        return "Selena returned a non-zero result; inspect the simulation engine log in this Stage."
    if normalized == "runtime_timeout":
        return "Selena exceeded the configured runtime limit; inspect the simulation engine log in this Stage."
    if normalized in {"selena_launch_failed", "runner_unavailable"}:
        return "The Windows Agent could not start the configured Selena runner."
    if normalized == "connector_dependency_missing":
        return "The Windows connector is missing an optional package required for this task."
    if normalized in {"paramconfig_failed", "paramconfig_outside_lease", "unsafe_runtime_argument"}:
        return "The Windows Agent could not prepare a safe Selena invocation."
    if normalized == "runner_contract_failed":
        return "The Windows Agent runner returned an invalid execution result."
    return "The execution stage returned a failed result; inspect the Stage log for details."


def _apply_node_policy(
    node_kind: Any,
    metadata: Optional[dict[str, Any]],
    capabilities: Optional[list[str]],
) -> tuple[Optional[str], list[str], list[str]]:
    """Resolve the node kind and normalize self-declared capabilities.

    Returns ``(resolved_kind, effective_capabilities, rejected_capabilities)``.

    The node kind is read from the explicit ``node_kind`` argument first, then
    from ``metadata['node_kind']`` (the legacy/HTTP path passes it there). When
    no v5 node kind is declared, capabilities are returned unchanged so legacy
    callers and tests that never declare a node kind keep working — but the
    light-Agent bypass stays closed because claim-time gating applies once a
    kind is known, and legacy Mode A servers restrict task types via
    ``--allowed-task-types``.
    """
    from core.agent_policy import (
        AgentPolicyError,
        filter_capabilities_for_node,
        node_kind_for_mode,
        normalize_node_kind,
    )

    meta = dict(metadata or {})
    declared_kind = node_kind or meta.get("node_kind") or meta.get("node.kind") or ""
    declared_text = str(declared_kind or "").strip()
    mode_text = str(meta.get("windows_mode") or meta.get("deployment") or "").strip()
    if declared_text:
        resolved = normalize_node_kind(declared_text)
        if resolved not in {
            "windows_agent", "windows_full", "linux_executor", "platform_gateway", "legacy"
        }:
            raise AgentPolicyError("unsupported agent node kind")
    elif mode_text:
        resolved = node_kind_for_mode(mode_text)
    else:
        resolved = ""
    if not resolved or resolved == "legacy":
        # Preserve declared capabilities verbatim for legacy / undeclared nodes.
        cleaned: list[str] = []
        seen: set[str] = set()
        for cap in list(capabilities or []):
            text = str(cap or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        return None, cleaned, []
    effective, rejected = filter_capabilities_for_node(resolved, capabilities)
    return resolved, effective, rejected


def _data_root() -> Path:
    """Return the data root (follows RSIM_HOME; stdlib-only, avoids core.config)."""
    import os
    home = os.environ.get("RSIM_HOME", "").strip()
    if home:
        return Path(home).expanduser()
    # Fallback when RSIM_HOME is unset. In a normal repo checkout, __file__ is
    # core/control_service.py → parent.parent is the repo root. But inside a
    # zipapp (.pyz), __file__ points *into* the archive
    # (e.g. /tmp/rsim_server.pyz/core/control_service.py) and parent.parent is
    # the .pyz itself — creating ./results under it fails with NotADirectory.
    # So for zipapp deployment, fall back to a user home dir instead.
    here = Path(__file__).resolve()
    # Detect zipapp: __file__ is /path/to/archive.pyz/core/control_service.py,
    # so one of the *parent* path components is the .pyz file itself (suffix
    # .pyz). Neither here.suffix (.py) nor ".pyz" in here.parts matches a
    # filename like "rsim_server.pyz", so check each parent's suffix.
    if any(p.suffix == ".pyz" for p in here.parents):
        return Path.home() / ".rsim"
    return here.parent.parent


def default_control_db_path() -> Path:
    """Return the default SQLite database path for the control service."""
    results_dir = _data_root() / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir / "_control.db"


class ControlService:
    """Thread-safe SQLite service backing the minimal job/agent control plane."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._db_path = str(db_path or default_control_db_path())
        self._lock = threading.RLock()
        self._now_fn = now_fn or time.time
        self._init_schema()

    def _now(self) -> float:
        return float(self._now_fn())

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Wait up to 5s on SQLite lock contention (e.g. WAL checkpoint) before
        # raising SQLITE_BUSY. All access is serialized under self._lock so this
        # rarely fires, but it turns a rare transient error into a non-issue.
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS agents (
                        agent_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        hostname TEXT NOT NULL,
                        capabilities_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        registered_at REAL NOT NULL,
                        last_heartbeat REAL NOT NULL,
                        current_task_id TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        job_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        completed_at REAL NOT NULL DEFAULT 0,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        owner TEXT NOT NULL DEFAULT '',
                        idempotency_key TEXT NOT NULL DEFAULT '',
                        request_hash TEXT NOT NULL DEFAULT '',
                        spec_json TEXT NOT NULL DEFAULT '{}',
                        resolved_spec_json TEXT NOT NULL DEFAULT '{}',
                        started_at REAL NOT NULL DEFAULT 0,
                        finished_at REAL NOT NULL DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL,
                        task_type TEXT NOT NULL,
                        order_index INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        assigned_agent_id TEXT NOT NULL DEFAULT '',
                        required_agent_id TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        claimed_at REAL NOT NULL DEFAULT 0,
                        started_at REAL NOT NULL DEFAULT 0,
                        completed_at REAL NOT NULL DEFAULT 0,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        returncode INTEGER,
                        stage_type TEXT NOT NULL DEFAULT '',
                        dependencies_json TEXT NOT NULL DEFAULT '[]',
                        progress REAL NOT NULL DEFAULT 0,
                        input_ref_json TEXT NOT NULL DEFAULT '{}',
                        output_ref_json TEXT NOT NULL DEFAULT '{}',
                        error_json TEXT NOT NULL DEFAULT '{}',
                        skip_reason TEXT NOT NULL DEFAULT '',
                        initial_status TEXT NOT NULL DEFAULT 'queued',
                        FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_tasks_status_created
                        ON tasks(status, created_at, order_index);
                    CREATE INDEX IF NOT EXISTS idx_tasks_job
                        ON tasks(job_id, order_index);

                    CREATE TABLE IF NOT EXISTS task_logs (
                        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        stream TEXT NOT NULL,
                        message TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_task_logs_task
                        ON task_logs(task_id, log_id);

                    CREATE TABLE IF NOT EXISTS stage_attempts (
                        attempt_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL,
                        stage_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        agent_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        started_at REAL NOT NULL DEFAULT 0,
                        finished_at REAL NOT NULL DEFAULT 0,
                        returncode INTEGER,
                        result_json TEXT NOT NULL DEFAULT '{}',
                        error_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(stage_id, attempt),
                        FOREIGN KEY(job_id) REFERENCES jobs(job_id),
                        FOREIGN KEY(stage_id) REFERENCES tasks(task_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_stage_attempts_stage
                        ON stage_attempts(stage_id, attempt);

                    CREATE TABLE IF NOT EXISTS job_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        stage_id TEXT NOT NULL DEFAULT '',
                        attempt INTEGER NOT NULL DEFAULT 0,
                        sequence INTEGER NOT NULL,
                        timestamp REAL NOT NULL,
                        level TEXT NOT NULL DEFAULT 'info',
                        event_type TEXT NOT NULL DEFAULT 'message',
                        status TEXT NOT NULL DEFAULT '',
                        progress REAL,
                        code TEXT NOT NULL DEFAULT '',
                        message TEXT NOT NULL DEFAULT '',
                        detail_json TEXT NOT NULL DEFAULT '{}',
                        action_json TEXT NOT NULL DEFAULT '[]',
                        UNIQUE(job_id, sequence),
                        FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_job_events_job_sequence
                        ON job_events(job_id, sequence);
                    """
                )
                conn.execute("BEGIN IMMEDIATE")
                self._migrate_jobs_schema_locked(conn)
                self._migrate_tasks_schema_locked(conn)
                self._reconcile_failed_manifest_jobs_locked(conn)
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_owner_idempotency_key
                    ON jobs(owner, idempotency_key)
                    WHERE idempotency_key <> ''
                    """
                )
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def _migrate_jobs_schema_locked(self, conn: sqlite3.Connection) -> None:
        """Add v5 idempotency columns to older control DBs in place."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        migrations = {
            "owner": "ALTER TABLE jobs ADD COLUMN owner TEXT NOT NULL DEFAULT ''",
            "idempotency_key": "ALTER TABLE jobs ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''",
            "request_hash": "ALTER TABLE jobs ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''",
            "spec_json": "ALTER TABLE jobs ADD COLUMN spec_json TEXT NOT NULL DEFAULT '{}'",
            "resolved_spec_json": "ALTER TABLE jobs ADD COLUMN resolved_spec_json TEXT NOT NULL DEFAULT '{}'",
            "started_at": "ALTER TABLE jobs ADD COLUMN started_at REAL NOT NULL DEFAULT 0",
            "finished_at": "ALTER TABLE jobs ADD COLUMN finished_at REAL NOT NULL DEFAULT 0",
        }
        for column, statement in migrations.items():
            if column not in columns:
                conn.execute(statement)

    def _migrate_tasks_schema_locked(self, conn: sqlite3.Connection) -> None:
        """Add v5 Stage-compatible columns to older task rows in place."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        migrations = {
            "stage_type": "ALTER TABLE tasks ADD COLUMN stage_type TEXT NOT NULL DEFAULT ''",
            "dependencies_json": "ALTER TABLE tasks ADD COLUMN dependencies_json TEXT NOT NULL DEFAULT '[]'",
            "progress": "ALTER TABLE tasks ADD COLUMN progress REAL NOT NULL DEFAULT 0",
            "input_ref_json": "ALTER TABLE tasks ADD COLUMN input_ref_json TEXT NOT NULL DEFAULT '{}'",
            "output_ref_json": "ALTER TABLE tasks ADD COLUMN output_ref_json TEXT NOT NULL DEFAULT '{}'",
            "error_json": "ALTER TABLE tasks ADD COLUMN error_json TEXT NOT NULL DEFAULT '{}'",
            "skip_reason": "ALTER TABLE tasks ADD COLUMN skip_reason TEXT NOT NULL DEFAULT ''",
            "initial_status": "ALTER TABLE tasks ADD COLUMN initial_status TEXT NOT NULL DEFAULT 'queued'",
            "required_agent_id": "ALTER TABLE tasks ADD COLUMN required_agent_id TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in columns:
                conn.execute(statement)
        conn.execute("UPDATE tasks SET stage_type=task_type WHERE stage_type=''")
        conn.execute("UPDATE tasks SET initial_status=status WHERE initial_status=''")

    def _reconcile_failed_manifest_jobs_locked(self, conn: sqlite3.Connection) -> None:
        """Safely normalize historical public manifests and failed Jobs."""
        rows = conn.execute(
            "SELECT job_id, status, result_json FROM jobs WHERE result_json <> '{}'"
        ).fetchall()
        for row in rows:
            result = self._loads(row["result_json"])
            original_result = dict(result)
            manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
            normalized_manifest, manifest_failed = _normalize_manifest_outcome(manifest)
            if normalized_manifest != manifest:
                result["manifest"] = normalized_manifest
            if manifest_failed:
                summary = (
                    normalized_manifest.get("summary")
                    if isinstance(normalized_manifest.get("summary"), dict)
                    else {}
                )
                errors = summary.get("errors") if isinstance(summary.get("errors"), list) else []
                result.setdefault("code", "simulation_failed")
                result.setdefault(
                    "message",
                    str(errors[0]) if errors else "simulation result reported failure",
                )
            if result != original_result:
                conn.execute(
                    "UPDATE jobs SET result_json=? WHERE job_id=?",
                    (self._dumps(result), row["job_id"]),
                )

            # Keep the public finalize-stage result aligned with the Job result.
            # Attempt rows remain untouched as an immutable audit trail.
            if normalized_manifest != manifest:
                task_rows = conn.execute(
                    """
                    SELECT task_id, result_json FROM tasks
                    WHERE job_id=? AND stage_type='finalize_manifest'
                    """,
                    (row["job_id"],),
                ).fetchall()
                for task_row in task_rows:
                    task_result = self._loads(task_row["result_json"])
                    task_manifest = (
                        task_result.get("manifest")
                        if isinstance(task_result.get("manifest"), dict)
                        else {}
                    )
                    if task_manifest and normalized_manifest != task_manifest:
                        task_result["manifest"] = normalized_manifest
                        conn.execute(
                            "UPDATE tasks SET result_json=? WHERE task_id=?",
                            (self._dumps(task_result), task_row["task_id"]),
                        )

            if not manifest_failed:
                continue
            error_json = self._dumps({
                "code": "simulation_failed",
                "message": "simulation result reported failure",
            })
            if str(row["status"]) == "succeeded":
                conn.execute("UPDATE jobs SET status='failed' WHERE job_id=?", (row["job_id"],))
            conn.execute(
                """
                UPDATE tasks
                SET status='failed', returncode=-1, error_json=?
                WHERE job_id=? AND stage_type='finalize_manifest' AND status='succeeded'
                """,
                (error_json, row["job_id"]),
            )

    def register_cluster_worker(
        self,
        name: str,
        *,
        role_id: str,
        worker_id: str,
        worker_index: int,
        worker_count: int,
        platform: str = "",
        hostname: str = "",
        capabilities: Optional[list[str]] = None,
        node_kind: str = "",
    ) -> dict[str, Any]:
        """Register an in-process Cluster worker with a server-owned marker.

        The marker is intentionally unavailable through the generic Agent
        registration contract.  Claim-time role affinity therefore cannot be
        enabled by a normal Agent merely self-reporting ``claim_group``.
        """
        role = str(role_id or "").strip()
        worker = str(worker_id or "").strip()
        expected_kind = _CLUSTER_ROLE_NODE_KINDS.get(role)
        try:
            index = int(worker_index)
            count = int(worker_count)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Cluster worker index/count") from exc
        expected_worker = role if index == 0 else f"{role}{_CLUSTER_WORKER_SUFFIX}{index}"
        if (
            expected_kind is None
            or worker != expected_worker
            or index < 0
            or count < 1
            or index >= count
            or str(node_kind or "").strip() != expected_kind
        ):
            raise ValueError("invalid Cluster worker identity")
        resolved_kind, effective_caps, rejected_caps = _apply_node_policy(
            expected_kind, {}, capabilities
        )
        if resolved_kind != expected_kind:
            raise ValueError("invalid Cluster worker capabilities")
        metadata: dict[str, Any] = {
            "node_kind": expected_kind,
            "cluster_role": role,
            "claim_group": role,
            "cluster_worker_index": index,
            "cluster_worker_count": count,
            "cluster_worker_identity": _CLUSTER_WORKER_MARKER,
        }
        if rejected_caps:
            metadata["capability_policy"] = "filtered"
            metadata["rejected_capability_count"] = len(rejected_caps)
        return self._register_agent_record(
            name,
            agent_id=worker,
            platform=platform,
            hostname=hostname,
            capabilities=effective_caps,
            metadata=metadata,
        )

    def register_agent(
        self,
        name: str,
        *,
        agent_id: str = "",
        platform: str = "",
        hostname: str = "",
        capabilities: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        node_kind: str = "",
    ) -> dict[str, Any]:
        requested_id = str(agent_id or "")
        if requested_id in RESERVED_INTERNAL_AGENT_IDS:
            raise ValueError("agent_id is reserved for an internal scheduler")
        resolved_kind, effective_caps, rejected_caps = _apply_node_policy(
            node_kind, metadata, capabilities
        )
        merged_metadata = dict(metadata or {})
        # These fields are assigned only by register_cluster_worker().  Never
        # persist caller-provided values from the generic Agent surface: a
        # normal Agent must not self-authorize as a Cluster pool worker.
        for protected_key in _CLUSTER_WORKER_PROTECTED_KEYS:
            merged_metadata.pop(protected_key, None)
        # Persist the resolved node kind so claim-time gating can trust it even
        # if a later heartbeat omits it. This is metadata, not an auth identity.
        if resolved_kind:
            merged_metadata["node_kind"] = resolved_kind
        if rejected_caps:
            # Record filtering without persisting caller-controlled tokens;
            # malformed capability strings may contain path-like secrets.
            merged_metadata["capability_policy"] = "filtered"
            merged_metadata["rejected_capability_count"] = len(rejected_caps)
        return self._register_agent_record(
            name,
            agent_id=requested_id,
            platform=platform,
            hostname=hostname,
            capabilities=effective_caps,
            metadata=merged_metadata,
        )

    def register_internal_agent(
        self,
        name: str,
        *,
        agent_id: str,
        platform: str = "internal",
        hostname: str = "",
        capabilities: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Register a process-internal scheduler identity, never an HTTP Agent."""
        if str(agent_id or "") not in RESERVED_INTERNAL_AGENT_IDS:
            raise ValueError("internal registration requires a reserved agent_id")
        return self._register_agent_record(
            name,
            agent_id=agent_id,
            platform=platform,
            hostname=hostname,
            capabilities=capabilities,
            metadata=metadata,
        )

    def _register_agent_record(
        self,
        name: str,
        *,
        agent_id: str = "",
        platform: str = "",
        hostname: str = "",
        capabilities: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        now = self._now()
        capabilities = list(capabilities or [])
        metadata = dict(metadata or {})
        agent_id = str(agent_id or f"agent_{uuid.uuid4().hex[:12]}")
        with self._lock:
            conn = self._conn()
            try:
                # Registration is an UPSERT, so the ownership check must live
                # in this same critical section.  An API-level pre-check alone
                # leaves a race where two users can bind the same stable
                # Windows device identity concurrently.
                existing_row = conn.execute(
                    "SELECT metadata_json FROM agents WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
                if existing_row is not None:
                    try:
                        existing_metadata = json.loads(existing_row["metadata_json"] or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        existing_metadata = {}
                    existing_kind = str(existing_metadata.get("node_kind") or "")
                    next_kind = str(metadata.get("node_kind") or "")
                    existing_owner = str(existing_metadata.get("user") or "")
                    next_owner = str(metadata.get("user") or "")
                    if (
                        existing_kind in _WINDOWS_NODE_KINDS
                        and next_kind in _WINDOWS_NODE_KINDS
                        and existing_owner
                        and next_owner
                        and not _connector_owner_transition_allowed(
                            existing_metadata, metadata
                        )
                    ):
                        raise ValueError(
                            "connector_owner_mismatch: stable device identity is already bound"
                        )
                conn.execute(
                    """
                    INSERT INTO agents (
                        agent_id, name, platform, hostname, capabilities_json,
                        metadata_json, status, registered_at, last_heartbeat, current_task_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                        name=excluded.name,
                        platform=excluded.platform,
                        hostname=excluded.hostname,
                        capabilities_json=excluded.capabilities_json,
                        metadata_json=excluded.metadata_json,
                        status=CASE
                            WHEN agents.current_task_id <> '' THEN agents.status
                            ELSE excluded.status
                        END,
                        last_heartbeat=excluded.last_heartbeat,
                        current_task_id=CASE
                            WHEN agents.current_task_id <> '' THEN agents.current_task_id
                            ELSE excluded.current_task_id
                        END
                    """,
                    (
                        agent_id,
                        name or agent_id,
                        platform,
                        hostname,
                        self._dumps(capabilities),
                        self._dumps(metadata),
                        "idle",
                        now,
                        now,
                        "",
                    ),
                )
                conn.commit()
                return self._get_agent_locked(conn, agent_id)
            finally:
                conn.close()

    def heartbeat(
        self,
        agent_id: str,
        *,
        status: str = "",
        current_task_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        now = self._now()
        with self._lock:
            conn = self._conn()
            try:
                agent = self._get_agent_locked(conn, agent_id)
                next_status = status or agent["status"]
                next_task_id = current_task_id if current_task_id is not None else agent["current_task_id"]
                next_metadata = dict(agent["metadata"])
                if metadata:
                    # Keep server-owned Cluster worker identity immutable and
                    # ignore attempts by generic Agents to add the marker via
                    # a later heartbeat.
                    next_metadata.update({
                        key: value
                        for key, value in metadata.items()
                        if key not in _CLUSTER_WORKER_PROTECTED_KEYS
                    })
                conn.execute(
                    """
                    UPDATE agents
                    SET status=?, current_task_id=?, metadata_json=?, last_heartbeat=?
                    WHERE agent_id=?
                    """,
                    (next_status, next_task_id, self._dumps(next_metadata), now, agent_id),
                )
                conn.commit()
                task_id = next_task_id or agent["current_task_id"]
                cancel_requested = False
                task = None
                if task_id:
                    task = self._get_task_locked(conn, task_id)
                    cancel_requested = bool(task["cancel_requested"])
                return {
                    "agent": self._get_agent_locked(conn, agent_id),
                    "cancel_requested": cancel_requested,
                    "task": task,
                }
            finally:
                conn.close()

    def bind_stage_to_agent(
        self,
        task_id: str,
        *,
        agent_id: str,
        payload_patch: Optional[dict[str, Any]] = None,
        expected_assigned_agent_id: str = INTERNAL_V1_SCHEDULER_AGENT_ID,
    ) -> dict[str, Any]:
        """Atomically bind one queued Stage to a required execution Agent.

        ``required_agent_id`` is durable affinity. Stale-task recovery may
        clear the transient ``assigned_agent_id`` so the same Agent can reclaim
        the task, but it must never move build/upload state to another machine.
        This is an internal scheduler primitive and is intentionally not
        exposed by the legacy HTTP API.
        """
        agent_id = str(agent_id or "").strip()
        if not agent_id or agent_id in RESERVED_INTERNAL_AGENT_IDS:
            raise ValueError("a real execution agent_id is required")
        expected = str(expected_assigned_agent_id or "").strip()
        patch = dict(payload_patch or {})
        now = self._now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                agent = self._get_agent_locked(conn, agent_id)
                task = self._get_task_locked(conn, task_id)
                if task["status"] != "queued":
                    raise ValueError(f"stage {task_id} is not queued")
                assigned = str(task.get("assigned_agent_id") or "")
                if assigned != expected:
                    raise ValueError(f"stage {task_id} assignment changed")
                required = str(task.get("required_agent_id") or "")
                if required and required != agent_id:
                    raise ValueError(f"stage {task_id} requires another agent")
                metadata = dict(agent.get("metadata") or {})
                node_kind = str(metadata.get("node_kind") or metadata.get("node.kind") or "")
                if not self._agent_can_claim_task(
                    node_kind,
                    str(task.get("task_type") or ""),
                    str(task.get("stage_type") or ""),
                    list(agent.get("capabilities") or []),
                ):
                    raise ValueError(f"agent {agent_id} cannot execute stage {task_id}")
                payload = dict(task.get("payload") or {})
                payload.update(patch)
                updated = conn.execute(
                    """
                    UPDATE tasks
                    SET assigned_agent_id=?, required_agent_id=?, payload_json=?, updated_at=?
                    WHERE task_id=? AND status='queued' AND assigned_agent_id=?
                    """,
                    (agent_id, agent_id, self._dumps(payload), now, task_id, expected),
                )
                if updated.rowcount != 1:
                    raise ValueError(f"stage {task_id} assignment changed")
                self._append_event_locked(
                    conn,
                    task["job_id"],
                    stage_id=task_id,
                    event_type="stage.bound",
                    status="queued",
                    message="stage bound to required execution node",
                    detail={"agent_id": agent_id, "affinity": "required"},
                )
                self._touch_job_locked(conn, task["job_id"], now)
                conn.commit()
                return self._get_task_locked(conn, task_id)
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def bind_pending_environment_stage(self, agent_id: str) -> Optional[dict[str, Any]]:
        """Offer one sentinel environment Stage to a matching Windows Agent.

        Agent metadata is only a discovery hint. The executor still resolves
        and validates the binding locally before returning an authoritative
        EnvironmentSnapshot.
        """
        agent_id = str(agent_id or "").strip()
        with self._lock:
            conn = self._conn()
            try:
                agent = self._get_agent_locked(conn, agent_id)
                metadata = dict(agent.get("metadata") or {})
                node_kind = str(metadata.get("node_kind") or "")
                if node_kind not in {"windows_agent", "windows_full"}:
                    return None
                advertised: set[tuple[str, str]] = set()
                raw_bindings = metadata.get("workspace_bindings") or []
                if isinstance(raw_bindings, list):
                    for item in raw_bindings:
                        if not isinstance(item, dict) or item.get("healthy") is not True:
                            continue
                        project = str(item.get("project") or "").strip()
                        binding_id = str(item.get("id") or "").strip()
                        if project and binding_id.startswith("workspace:sha256:"):
                            advertised.add((project, binding_id))
                if not advertised:
                    return None
                rows = conn.execute(
                    """
                    SELECT t.task_id, t.payload_json, j.owner
                    FROM tasks t
                    JOIN jobs j ON j.job_id=t.job_id
                    WHERE t.stage_type='environment_check'
                      AND t.status='queued'
                      AND t.assigned_agent_id=?
                      AND t.required_agent_id=''
                      AND j.job_type LIKE 'simulation.v1%'
                    ORDER BY t.created_at ASC, t.order_index ASC, t.task_id ASC
                    """,
                    (INTERNAL_V1_SCHEDULER_AGENT_ID,),
                ).fetchall()
                candidate = None
                registered_owner = str(metadata.get("user") or "").strip().casefold()
                for row in rows:
                    job_owner = str(row["owner"] or "").strip().casefold()
                    if job_owner and registered_owner and registered_owner != job_owner:
                        continue
                    payload = self._loads(row["payload_json"])
                    key = (
                        str(payload.get("project") or "").strip(),
                        str(payload.get("workspace_binding_id") or "").strip(),
                    )
                    if key in advertised and payload.get("dispatch_scope") == "selena_build":
                        candidate = str(row["task_id"])
                        break
            finally:
                conn.close()
        if not candidate:
            return None
        try:
            return self.bind_stage_to_agent(
                candidate,
                agent_id=agent_id,
                expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
            )
        except ValueError:
            # Another matching Agent may win the compare-and-swap between the
            # discovery read and bind transaction. Polling remains idempotent.
            return None

    def bind_pending_runtime_bundle_cache(self, agent_id: str) -> Optional[dict[str, Any]]:
        """Bind existing-Bundle local execution to one Windows-full Agent.

        The selected Agent must already authorize the requested local data
        path.  This guarantees that the downloaded Bundle lease and the later
        Data lease remain on the same machine instead of relying on accidental
        cross-Agent path compatibility.
        """
        from core.agent_data_bindings import candidate_data_binding_ids

        agent_id = str(agent_id or "").strip()
        with self._lock:
            conn = self._conn()
            try:
                agent = self._get_agent_locked(conn, agent_id)
                metadata = dict(agent.get("metadata") or {})
                if str(metadata.get("node_kind") or "") != "windows_full":
                    return None
                if not windows_connector_contract_is_current(metadata):
                    return None
                # New data bindings are keyed by owner/device/root.  Keep a
                # project-indexed view only for legacy connector adverts.
                advertised: dict[str, set[str]] = {}
                advertised_modern: set[str] = set()
                registered_owner = str(metadata.get("user") or "").strip().casefold()
                for item in metadata.get("data_bindings") or []:
                    if not isinstance(item, dict) or item.get("healthy") is not True:
                        continue
                    project = str(item.get("project") or "").strip()
                    binding_id = str(item.get("id") or "").strip()
                    binding_owner = str(item.get("owner") or "").strip().casefold()
                    binding_device = str(item.get("device_id") or item.get("device") or "").strip()
                    if not binding_id.startswith("data-root:sha256:"):
                        continue
                    if binding_owner or binding_device:
                        if registered_owner and binding_owner and registered_owner != binding_owner:
                            continue
                        if binding_device and binding_device != agent_id:
                            continue
                        advertised_modern.add(binding_id)
                    elif project:
                        advertised.setdefault(project, set()).add(binding_id)
                if not advertised and not advertised_modern:
                    return None
                rows = conn.execute(
                    """
                    SELECT t.task_id,t.payload_json,j.spec_json,j.owner
                    FROM tasks t
                    JOIN jobs j ON j.job_id=t.job_id
                    WHERE t.stage_type='environment_check'
                      AND t.status='queued'
                      AND t.assigned_agent_id=?
                      AND t.required_agent_id=''
                      AND j.job_type LIKE 'simulation.run_config.v2%'
                    ORDER BY t.created_at ASC,t.order_index ASC,t.task_id ASC
                    """,
                    (INTERNAL_V1_SCHEDULER_AGENT_ID,),
                ).fetchall()
                candidate: tuple[str, str] | None = None
                registered_owner = str(metadata.get("user") or "").strip().casefold()
                for row in rows:
                    job_owner = str(row["owner"] or "").strip().casefold()
                    if job_owner and registered_owner and registered_owner != job_owner:
                        continue
                    payload = self._loads(row["payload_json"])
                    if payload.get("dispatch_scope") != "runtime_bundle_cache":
                        continue
                    project = str(payload.get("project") or "").strip()
                    spec = self._loads(row["spec_json"])
                    data_path = str((spec.get("data") or {}).get("path") or "")
                    owner_raw = str(row["owner"] or "").strip()
                    binding_id = next(
                        (
                            value
                            for value in (
                                candidate_data_binding_ids(
                                    owner=owner_raw,
                                    device_id=agent_id,
                                    data_path=data_path,
                                )
                                + candidate_data_binding_ids(project, data_path)
                            )
                            if value in advertised_modern or value in advertised.get(project, set())
                        ),
                        "",
                    )
                    if binding_id:
                        candidate = (str(row["task_id"]), binding_id)
                        break
            finally:
                conn.close()
        if candidate is None:
            return None
        try:
            return self.bind_stage_to_agent(
                candidate[0],
                agent_id=agent_id,
                expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
                payload_patch={"data_binding_id": candidate[1]},
            )
        except ValueError:
            return None

    def bind_pending_run_config_resolution(self, agent_id: str) -> Optional[dict[str, Any]]:
        """Bind one project-free resolver Stage by opaque workspace path id.

        A one-click Agent may authorize its first local path.  Once it has
        bindings, it may also auto-configure a new path for the same owner;
        the owner-scoped control database and registration identity prevent an
        unrelated user's job from becoming a catch-all for this machine.
        """
        from core.agent_bindings import make_workspace_path_id
        from core.agent_asset_bindings import candidate_asset_binding_ids

        agent_id = str(agent_id or "").strip()
        with self._lock:
            conn = self._conn()
            try:
                agent = self._get_agent_locked(conn, agent_id)
                metadata = dict(agent.get("metadata") or {})
                node_kind = str(metadata.get("node_kind") or "")
                if node_kind not in {"windows_agent", "windows_full"}:
                    return None
                if not windows_connector_contract_is_current(metadata):
                    return None
                auto_configure = metadata.get("auto_configure") is True
                path_ids = {
                    str(item.get("path_id") or "")
                    for item in metadata.get("workspace_bindings") or []
                    if isinstance(item, dict)
                    and item.get("healthy") is True
                    and str(item.get("path_id") or "").startswith("workspace-path:sha256:")
                }
                asset_ids = {
                    str(item.get("id") or "")
                    for item in metadata.get("asset_bindings") or []
                    if isinstance(item, dict)
                    and item.get("healthy") is True
                    and str(item.get("id") or "").startswith("asset-root:sha256:")
                }
                data_ids = {
                    str(item.get("id") or "")
                    for item in metadata.get("data_bindings") or []
                    if isinstance(item, dict)
                    and item.get("healthy") is True
                    and str(item.get("id") or "").startswith("data-root:sha256:")
                }
                has_existing_bindings = bool(path_ids or asset_ids or data_ids)
                rows = conn.execute(
                    """
                    SELECT t.task_id,j.spec_json,j.resolved_spec_json,j.owner
                    FROM tasks t
                    JOIN jobs j ON j.job_id=t.job_id
                    WHERE t.stage_type='resolve_spec'
                      AND t.status='queued'
                      AND t.assigned_agent_id=?
                      AND t.required_agent_id=''
                      AND j.job_type LIKE 'simulation.run_config.v2%'
                    ORDER BY t.created_at ASC,t.order_index ASC,t.task_id ASC
                    """,
                    (INTERNAL_V1_SCHEDULER_AGENT_ID,),
                ).fetchall()
                matched: tuple[str, dict[str, Any]] | None = None
                fallback: tuple[str, dict[str, Any]] | None = None
                for row in rows:
                    spec = self._loads(row["spec_json"])
                    job_owner = str(row["owner"] or "").strip().casefold()
                    registered_owner = str(metadata.get("user") or "").strip().casefold()
                    same_owner = bool(
                        registered_owner
                        and job_owner
                        and registered_owner == job_owner
                    )
                    if job_owner and registered_owner and not same_owner:
                        continue
                    selena = dict(spec.get("selena") or {})
                    resolved = self._loads(row["resolved_spec_json"])
                    selected_target = str(
                        (((resolved.get("decisions") or {}).get("execution") or {}).get("selected_target") or "")
                    )
                    if not selected_target:
                        selected_target = str((spec.get("simulation") or {}).get("target") or "auto")
                    if selected_target == "local" and node_kind != "windows_full":
                        continue
                    source = str(selena.get("source") or "")
                    if source == "existing":
                        # A Windows-local path can only be validated on the
                        # Agent. One-click Agents explicitly opt into first
                        # task path configuration; a later path is accepted
                        # only when the registration belongs to this job's
                        # owner (the control DB is owner-scoped as well).
                        if not auto_configure:
                            continue
                        payload = (
                            str(row["task_id"]),
                            {
                                "contract": "user-run-config/2.0",
                                "source": "existing",
                                "existing_path": str(selena.get("existing_path") or ""),
                                "runtime_xml": str(selena.get("runtime_xml") or ""),
                                "code_path": str(selena.get("code_path") or ""),
                                "branch": str(selena.get("branch") or ""),
                                "selena_build_script": str(
                                    selena.get("selena_build_script") or ""
                                ),
                                "package_build_script": str(
                                    selena.get("package_build_script") or ""
                                ),
                                "data_path": str((spec.get("data") or {}).get("path") or ""),
                                "adapter_file": str((spec.get("simulation") or {}).get("adapter_file") or ""),
                                "mat_filter": str((spec.get("simulation") or {}).get("mat_filter") or ""),
                                "selected_target": selected_target,
                                "auto_configure": True,
                            },
                        )
                        code_path = str(selena.get("code_path") or "").strip()
                        if (
                            code_path
                            and make_workspace_path_id(code_path) in path_ids
                        ):
                            matched = payload
                            break
                        if (
                            (not has_existing_bindings or same_owner)
                            and fallback is None
                        ):
                            fallback = payload
                        continue
                    if source != "build":
                        continue
                    code_path = str(selena.get("code_path") or "").strip()
                    runtime_xml = str(selena.get("runtime_xml") or "")
                    payload = (
                        str(row["task_id"]),
                        {
                            "contract": "user-run-config/2.0",
                            "source": "build",
                            "code_path": code_path,
                            "selena_build_script": str(selena.get("selena_build_script") or ""),
                            "package_build_script": str(selena.get("package_build_script") or ""),
                            "runtime_xml": runtime_xml,
                            "data_path": str((spec.get("data") or {}).get("path") or ""),
                            "adapter_file": str((spec.get("simulation") or {}).get("adapter_file") or ""),
                            "mat_filter": str((spec.get("simulation") or {}).get("mat_filter") or ""),
                            "auto_configure": auto_configure,
                        },
                    )
                    workspace_matches = make_workspace_path_id(code_path) in path_ids
                    asset_matches = bool(
                        set(candidate_asset_binding_ids(runtime_xml)).intersection(asset_ids)
                    )
                    if workspace_matches and asset_matches:
                        matched = payload
                        break
                    if (
                        auto_configure
                        and (not has_existing_bindings or same_owner)
                        and fallback is None
                    ):
                        fallback = payload
                candidate = matched or fallback
            finally:
                conn.close()
        if candidate is None:
            return None
        try:
            return self.bind_stage_to_agent(
                candidate[0],
                agent_id=agent_id,
                expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
                payload_patch=candidate[1],
            )
        except ValueError:
            return None

    def bind_pending_data_stage(self, agent_id: str) -> Optional[dict[str, Any]]:
        """Bind one local-data Stage, including one-click first-use authorization.

        A pre-registered data-root remains the preferred, path-free match.
        For a Windows one-click Agent, a Windows local path may be the first
        path that user has submitted.  In that case the Agent owns the single
        local authorization step and creates the same durable data-root
        binding before it discovers or uploads data.  This fallback is
        deliberately restricted to ``agent`` paths: shared and central data
        must keep their existing Linux routes.
        """
        from core.agent_data_bindings import candidate_data_binding_ids
        from core.datasets import classify_data_path

        agent_id = str(agent_id or "").strip()
        with self._lock:
            conn = self._conn()
            try:
                agent = self._get_agent_locked(conn, agent_id)
                metadata = dict(agent.get("metadata") or {})
                node_kind = str(metadata.get("node_kind") or "")
                auto_configure = metadata.get("auto_configure") is True
                if node_kind not in {"windows_agent", "windows_full"}:
                    return None
                advertised: dict[str, set[str]] = {}
                advertised_modern: set[str] = set()
                registered_owner = str(metadata.get("user") or "").strip().casefold()
                for item in metadata.get("data_bindings") or []:
                    if not isinstance(item, dict) or item.get("healthy") is not True:
                        continue
                    project = str(item.get("project") or "").strip()
                    binding_id = str(item.get("id") or "").strip()
                    binding_owner = str(item.get("owner") or "").strip().casefold()
                    binding_device = str(item.get("device_id") or item.get("device") or "").strip()
                    if not binding_id.startswith("data-root:sha256:"):
                        continue
                    if binding_owner or binding_device:
                        if registered_owner and binding_owner and registered_owner != binding_owner:
                            continue
                        if binding_device and binding_device != agent_id:
                            continue
                        advertised_modern.add(binding_id)
                    elif project:
                        advertised.setdefault(project, set()).add(binding_id)
                rows = conn.execute(
                    """
                    SELECT t.task_id,t.payload_json,j.owner,j.job_type
                    FROM tasks t
                    JOIN jobs j ON j.job_id=t.job_id
                    WHERE t.stage_type='prepare_data'
                      AND t.status='queued'
                      AND t.assigned_agent_id=?
                      AND t.required_agent_id=''
                      AND (
                        j.job_type LIKE 'simulation.v1%'
                        OR j.job_type LIKE 'simulation.run_config.v2%'
                      )
                    ORDER BY t.created_at ASC,t.order_index ASC,t.task_id ASC
                    """,
                    (INTERNAL_V1_SCHEDULER_AGENT_ID,),
                ).fetchall()
                candidate: tuple[str, str] | None = None
                auto_candidate = ""
                registered_owner = str(metadata.get("user") or "").strip().casefold()
                for row in rows:
                    if (
                        str(row["job_type"] or "").startswith("simulation.run_config.v2")
                        and not windows_connector_contract_is_current(metadata)
                    ):
                        continue
                    job_owner = str(row["owner"] or "").strip().casefold()
                    if job_owner and registered_owner and registered_owner != job_owner:
                        continue
                    payload = self._loads(row["payload_json"])
                    # P0 direct-transfer stages are claimed by the same
                    # source-side Connector path as the old local-data hook,
                    # but never fall back to a Linux body upload.  Keep the
                    # legacy token readable for old jobs while all new v2
                    # submissions use ``direct_transfer``.
                    if payload.get("dispatch_scope") not in {"data_upload", "direct_transfer", "source_to_local"}:
                        continue
                    project = str(payload.get("project") or "").strip()
                    data_path = str(payload.get("data_path") or "")
                    owner_raw = str(row["owner"] or "").strip()
                    for binding_id in (
                        candidate_data_binding_ids(
                            owner=owner_raw,
                            device_id=agent_id,
                            data_path=data_path,
                        )
                        + candidate_data_binding_ids(project, data_path)
                    ):
                        if binding_id in advertised_modern or binding_id in advertised.get(project, set()):
                            candidate = (str(row["task_id"]), binding_id)
                            break
                    if candidate is not None:
                        break
                    # Only a Windows-path task can use the convenience
                    # bootstrap.  It is never a fallback for Cluster/Linux
                    # data, shared paths, or an already logical dataset.
                    if (
                        auto_configure
                        and project
                        and classify_data_path(data_path) == "agent"
                        and not auto_candidate
                    ):
                        auto_candidate = str(row["task_id"])
            finally:
                conn.close()
        if candidate is None and not auto_candidate:
            return None
        try:
            if candidate is None:
                return self.bind_stage_to_agent(
                    auto_candidate,
                    agent_id=agent_id,
                    expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
                    payload_patch={"auto_configure": True},
                )
            return self.bind_stage_to_agent(
                candidate[0],
                agent_id=agent_id,
                expected_assigned_agent_id=INTERNAL_V1_SCHEDULER_AGENT_ID,
                payload_patch={"data_binding_id": candidate[1]},
            )
        except ValueError:
            return None

    def update_resolved_spec(self, job_id: str, resolved_spec: dict[str, Any]) -> dict[str, Any]:
        """Persist a scheduler-produced, path-free ResolvedSpec snapshot."""
        now = self._now()
        resolved = dict(resolved_spec or {})
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                job = self._get_job_locked(conn, job_id)
                if job["status"] in TERMINAL_JOB_STATUSES:
                    raise ValueError(f"job {job_id} is terminal")
                conn.execute(
                    "UPDATE jobs SET resolved_spec_json=?, updated_at=? WHERE job_id=?",
                    (self._dumps(resolved), now, job_id),
                )
                self._append_event_locked(
                    conn,
                    job_id,
                    event_type="job.resolved_spec",
                    status=str(resolved.get("status") or ""),
                    message="resolved specification updated from node-local evidence",
                )
                conn.commit()
                return self._get_job_locked(conn, job_id)
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def complete_transfer_stage(
        self,
        job_id: str,
        stage_id: str,
        *,
        owner: str,
        source_role: str,
        transfer: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a completed direct-transfer manifest as control metadata.

        The method deliberately receives only the small manifest projection;
        it never resolves or opens a physical object.  Owner, Job and Stage
        are checked inside one transaction so a client cannot complete another
        user's stage or create an orphan resolved-spec reference.
        """

        now = self._now()
        role = str(source_role or "").strip()
        if not role:
            raise ValueError("source_role is required")
        entries = []
        for raw in list((transfer or {}).get("entries") or []):
            if not isinstance(raw, dict):
                continue
            entries.append(
                {
                    "relative_path": str(raw.get("relative_path") or ""),
                    "size": int(raw.get("size") or 0),
                    "sha256": str(raw.get("sha256") or raw.get("checksum") or ""),
                    "storage_ref": str(raw.get("storage_ref") or raw.get("target_logical_ref") or ""),
                }
            )
        resource = {
            "transfer_id": str((transfer or {}).get("transfer_id") or ""),
            "source_role": role,
            "status": "resolved",
            "entries": entries,
        }
        radar_metadata = normalize_radar_metadata(
            (transfer or {}).get("radar")
            or (transfer or {}).get("radar_metadata")
            or (transfer or {}).get("source_fingerprints")
        )
        if radar_metadata:
            resource["radar"] = radar_metadata
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                job = self._get_job_locked(conn, str(job_id))
                if str(job.get("owner") or "") != str(owner or ""):
                    raise PermissionError("job belongs to another owner")
                task = next(
                    (item for item in job.get("stages") or [] if str(item.get("stage_id") or "") == str(stage_id)),
                    None,
                )
                if task is None:
                    raise KeyError(f"unknown stage: {stage_id}")
                resolved = dict(job.get("resolved_spec") or {})
                decisions = dict(resolved.get("decisions") or {})
                transfers = dict(decisions.get("transfers") or {})
                resources = dict(transfers.get("resources") or {})
                previous = resources.get(role)
                if previous is None:
                    resources[role] = resource
                elif isinstance(previous, list):
                    if resource not in previous:
                        previous.append(resource)
                elif previous != resource:
                    resources[role] = [previous, resource]
                transfers["resources"] = resources
                decisions["transfers"] = transfers

                # Keep the mature Cluster/manifest paths fed from the same
                # path-free direct-transfer evidence.  The transfer table is
                # the authoritative transport record, while these two
                # business decisions are the stable DatasetRef/RuntimeBundle
                # projections consumed by environment/final-manifest stages.
                # Only logical metadata is persisted; no deployment roots or
                # file bodies cross into the control plane.
                transfer_id = resource["transfer_id"]
                transfer_digest = hashlib.sha256(transfer_id.encode("utf-8")).hexdigest()
                file_count = len(entries)
                total_size = sum(int(item.get("size") or 0) for item in entries)
                storage_refs = [
                    str(item.get("storage_ref") or "")
                    for item in entries
                    if str(item.get("storage_ref") or "")
                ]
                if role == "dataset":
                    dataset_projection = {
                        "id": f"dataset:sha256:{transfer_digest}",
                        "source_kind": "direct_transfer",
                        "file_count": file_count,
                        "total_size": total_size,
                        "storage_refs": storage_refs,
                    }
                    if radar_metadata:
                        dataset_projection["radar"] = radar_metadata
                    decisions["data"] = {
                        "status": "resolved",
                        "route": "direct_transfer",
                        "dataset": dataset_projection,
                    }
                elif role == "runtime_bundle":
                    decisions["selena"] = {
                        "status": "resolved",
                        "action": "use_direct_transfer",
                        "runtime_bundle": {
                            "id": f"selena-bundle:sha256:{transfer_digest}",
                            "source_kind": "direct_transfer",
                            "file_count": file_count,
                            "total_size": total_size,
                            "storage_refs": storage_refs,
                        },
                    }
                resolved["decisions"] = decisions
                conn.execute(
                    "UPDATE jobs SET resolved_spec_json=?, updated_at=? WHERE job_id=?",
                    (self._dumps(resolved), now, str(job_id)),
                )
                stage_payload = dict(task.get("payload") or {})
                required_roles = {
                    str(item).strip()
                    for item in list(stage_payload.get("source_roles") or [])
                    if str(item).strip()
                }
                if not required_roles:
                    required_roles = {role}
                resolved_roles = {
                    key
                    for key, value in resources.items()
                    if (
                        isinstance(value, dict)
                        and str(value.get("status") or "") == "resolved"
                    )
                    or (
                        isinstance(value, list)
                        and any(
                            isinstance(item, dict)
                            and str(item.get("status") or "") == "resolved"
                            for item in value
                        )
                    )
                }
                remaining_roles = sorted(required_roles - resolved_roles)
                transfer_complete = not remaining_roles
                result = {
                    "code": "transfer_completed" if transfer_complete else "transfer_partial",
                    "message": "direct transfer manifest accepted" if transfer_complete else "direct transfer manifest accepted; waiting for remaining resources",
                    "transfer": resource,
                    "remaining_roles": remaining_roles,
                }
                next_status = "succeeded" if transfer_complete else "running"
                next_progress = 1.0 if transfer_complete else min(0.99, len(resolved_roles) / max(len(required_roles), 1))
                conn.execute(
                    """
                    UPDATE tasks
                    SET status=?, result_json=?, output_ref_json=?, error_json='{}',
                        progress=?, updated_at=?, completed_at=?, returncode=?
                    WHERE task_id=? AND job_id=?
                    """,
                    (
                        next_status,
                        self._dumps(result),
                        self._dumps(
                            {
                                "transfer_status": "transfer_completed" if transfer_complete else "transferring_direct_to_cluster",
                                "transfer_id": resource["transfer_id"],
                                "remaining_roles": remaining_roles,
                            }
                        ),
                        next_progress,
                        now,
                        now if transfer_complete else 0.0,
                        0 if transfer_complete else None,
                        str(stage_id),
                        str(job_id),
                    ),
                )
                self._append_event_locked(
                    conn,
                    str(job_id),
                    stage_id=str(stage_id),
                    event_type="stage.succeeded" if transfer_complete else "stage.running",
                    status=next_status,
                    progress=next_progress,
                    code=str(result["code"]),
                    message=str(result["message"]),
                    detail={"transfer_id": resource["transfer_id"], "source_role": role, "remaining_roles": remaining_roles},
                )
                self._refresh_job_status_locked(conn, str(job_id), now)
                conn.commit()
                return self._get_job_locked(conn, str(job_id))
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def create_job(
        self,
        job_type: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        tasks: Optional[list[dict[str, Any]]] = None,
        metadata: Optional[dict[str, Any]] = None,
        assigned_agent_id: str = "",
        owner: str = "",
        idempotency_key: str = "",
        request_hash: str = "",
        spec: Optional[dict[str, Any]] = None,
        resolved_spec: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not job_type:
            raise ValueError("job_type is required")
        payload = dict(payload or {})
        metadata = dict(metadata or {})
        task_specs = [dict(item) for item in (tasks or [])]
        if not task_specs:
            task_specs = [{"task_type": job_type, "payload": payload}]
        task_ids = [str(spec.get("task_id") or f"task_{uuid.uuid4().hex[:12]}") for spec in task_specs]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("duplicate task_id in job")
        stage_type_to_id: dict[str, str] = {}
        for index, spec_item in enumerate(task_specs):
            explicit_stage_type = "stage_type" in spec_item and str(spec_item.get("stage_type") or "").strip()
            stage_type = str(spec_item.get("stage_type") or spec_item.get("task_type") or job_type)
            if explicit_stage_type and stage_type in stage_type_to_id:
                raise ValueError(f"duplicate stage_type in job: {stage_type}")
            if explicit_stage_type or stage_type not in stage_type_to_id:
                stage_type_to_id[stage_type] = task_ids[index]
        resolved_dependencies = [
            self._normalize_dependencies(
                spec_item.get("dependencies"),
                stage_type_to_id,
                valid_task_ids=set(task_ids),
                self_task_id=task_ids[index],
            )
            for index, spec_item in enumerate(task_specs)
        ]
        # Pre-bind tasks to a specific agent so same-user multi-agent setups
        # can't steal each other's jobs: only the named agent (or any agent if
        # empty) may claim. Per-task spec override allowed.
        bind_agent = str(assigned_agent_id or "").strip()
        now = self._now()
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id, job_type, status, payload_json, metadata_json,
                        result_json, created_at, updated_at, completed_at, cancel_requested,
                        owner, idempotency_key, request_hash, spec_json, resolved_spec_json,
                        started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        job_type,
                        "queued",
                        self._dumps(payload),
                        self._dumps(metadata),
                        self._dumps({}),
                        now,
                        now,
                        0.0,
                        0,
                        str(owner or ""),
                        str(idempotency_key or ""),
                        str(request_hash or ""),
                        self._dumps(dict(spec or {})),
                        self._dumps(dict(resolved_spec or {})),
                        0.0,
                        0.0,
                    ),
                )
                for index, spec in enumerate(task_specs):
                    task_type = str(spec.get("task_type") or job_type)
                    stage_type = str(spec.get("stage_type") or task_type)
                    task_payload = dict(spec.get("payload") or payload)
                    task_bind = str(spec.get("assigned_agent_id") or bind_agent or "").strip()
                    required_agent = str(spec.get("required_agent_id") or "").strip()
                    status = str(spec.get("status") or spec.get("initial_status") or "queued")
                    if status not in INITIAL_TASK_STATUSES:
                        raise ValueError(f"unsupported initial task status: {status}")
                    initial_status = str(spec.get("initial_status") or status)
                    if initial_status not in INITIAL_TASK_STATUSES:
                        raise ValueError(f"unsupported initial task status: {initial_status}")
                    dependencies = resolved_dependencies[index]
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            task_id, job_id, task_type, order_index, status,
                            payload_json, result_json, assigned_agent_id, required_agent_id,
                            created_at, updated_at, completed_at, stage_type,
                            dependencies_json, progress, input_ref_json, output_ref_json,
                            error_json, skip_reason, initial_status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task_ids[index],
                            job_id,
                            task_type,
                            index,
                            status,
                            self._dumps(task_payload),
                            self._dumps({}),
                            task_bind,
                            required_agent,
                            now,
                            now,
                            now if status == "skipped" else 0.0,
                            stage_type,
                            self._dumps(dependencies),
                            1.0 if status == "skipped" else float(spec.get("progress") or 0.0),
                            self._dumps(dict(spec.get("input_ref") or {})),
                            self._dumps(dict(spec.get("output_ref") or {})),
                            self._dumps(dict(spec.get("error") or {})),
                            str(spec.get("skip_reason") or ""),
                            initial_status,
                        ),
                    )
                self._append_event_locked(
                    conn,
                    job_id,
                    event_type="job.created",
                    status="queued",
                    message=f"job {job_id} created",
                    detail={"job_type": job_type},
                )
                for index, spec in enumerate(task_specs):
                    stage_type = str(spec.get("stage_type") or spec.get("task_type") or job_type)
                    stage_status = str(spec.get("status") or spec.get("initial_status") or "queued")
                    skip_reason = str(spec.get("skip_reason") or "")
                    stage_error = dict(spec.get("error") or {})
                    stage_actions = list(stage_error.get("actions") or [])
                    self._append_event_locked(
                        conn,
                        job_id,
                        stage_id=task_ids[index],
                        event_type=f"stage.{stage_status}" if stage_status in {"skipped", "blocked"} else "stage.queued",
                        status=stage_status,
                        progress=1.0 if stage_status == "skipped" else 0.0,
                        code=str(stage_error.get("code") or ""),
                        message=skip_reason or f"{stage_type} {stage_status}",
                        detail={
                            "stage_type": stage_type,
                            "dependencies": list(resolved_dependencies[index]),
                            "skip_reason": skip_reason,
                            "error": stage_error,
                        },
                        action=stage_actions,
                    )
                self._refresh_job_status_locked(conn, job_id, now)
                refreshed_status = self._get_job_locked(conn, job_id)["status"]
                if refreshed_status != "queued":
                    self._append_event_locked(
                        conn,
                        job_id,
                        event_type="job.status",
                        status=refreshed_status,
                        message=f"job {refreshed_status}",
                    )
                conn.commit()
                return self._get_job_locked(conn, job_id)
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def get_job_by_idempotency(self, owner: str, idempotency_key: str) -> Optional[dict[str, Any]]:
        """Return the job for an owner/idempotency key, if any."""
        if not idempotency_key:
            return None
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    """
                    SELECT job_id
                    FROM jobs
                    WHERE owner=? AND idempotency_key=?
                    LIMIT 1
                    """,
                    (str(owner or ""), str(idempotency_key or "")),
                ).fetchone()
                if not row:
                    return None
                return self._get_job_locked(conn, row["job_id"])
            finally:
                conn.close()

    def claim_next_task(self, agent_id: str) -> Optional[dict[str, Any]]:
        now = self._now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                agent = self._get_agent_locked(conn, agent_id)
                capabilities = list(agent.get("capabilities") or [])
                agent_metadata = dict(agent.get("metadata") or {})
                agent_node_kind = str(agent_metadata.get("node_kind") or agent_metadata.get("node.kind") or "")
                registered_owner = str(agent_metadata.get("user") or "").strip().casefold()
                claim_agent_ids = _claim_agent_ids(agent_id, agent_metadata)
                claim_group = _validated_cluster_claim_group(agent_id, agent_metadata)

                def can_resume(row: sqlite3.Row) -> bool:
                    if (
                        str(row["job_type"] or "").startswith("simulation.run_config.v2")
                        and not windows_connector_contract_is_current(agent_metadata)
                    ):
                        return False
                    if agent_node_kind in {"windows_agent", "windows_full"}:
                        job_owner = str(row["owner"] or "").strip().casefold()
                        if job_owner and registered_owner and registered_owner != job_owner:
                            return False
                    return self._agent_can_claim_task(
                        agent_node_kind,
                        str(row["task_type"] or ""),
                        str(row["stage_type"] or ""),
                        capabilities,
                    )

                current_task_id = str(agent.get("current_task_id") or "")
                if current_task_id:
                    current_row = conn.execute(
                        """
                        SELECT t.task_id, t.task_type, t.stage_type, j.owner, j.job_type
                        FROM tasks t
                        JOIN jobs j ON j.job_id=t.job_id
                        WHERE t.task_id=?
                        """,
                        (current_task_id,),
                    ).fetchone()
                    current_task = (
                        self._get_task_locked(conn, current_task_id)
                        if current_row is not None
                        else None
                    )
                    if (
                        current_task is not None
                        and current_task["status"] == "running"
                        and current_row is not None
                        and can_resume(current_row)
                    ):
                        conn.commit()
                        return current_task
                    conn.execute(
                        "UPDATE agents SET status='idle', current_task_id='' WHERE agent_id=?",
                        (agent_id,),
                    )

                # Compatibility repair for Connector restarts produced by
                # older servers: the task row remains running/assigned while
                # re-registration cleared the Agent's current_task_id.  Task
                # assignment is authoritative, so restore it before claiming
                # any queued work.  This is at-least-once recovery and does not
                # create a second attempt.
                orphaned_rows = conn.execute(
                    """
                    SELECT t.task_id, t.job_id, t.task_type, t.stage_type,
                           t.order_index, j.owner, j.job_type
                    FROM tasks t
                    JOIN jobs j ON j.job_id=t.job_id
                    WHERE t.status='running' AND t.assigned_agent_id=?
                    ORDER BY t.claimed_at ASC, t.created_at ASC, t.order_index ASC, t.task_id ASC
                    """,
                    (agent_id,),
                ).fetchall()
                for row in orphaned_rows:
                    if not can_resume(row):
                        continue
                    conn.execute(
                        """
                        UPDATE agents
                        SET status='busy', current_task_id=?, last_heartbeat=?
                        WHERE agent_id=?
                        """,
                        (row["task_id"], now, agent_id),
                    )
                    self._append_event_locked(
                        conn,
                        row["job_id"],
                        stage_id=row["task_id"],
                        event_type="stage.resumed",
                        status="running",
                        message="Connector resumed its assigned stage after restart",
                    )
                    conn.commit()
                    return self._get_task_locked(conn, row["task_id"])

                # Filter by assigned_agent_id: a task pre-bound to a specific
                # agent is only claimable by that agent, except for an
                # explicitly registered Cluster pool worker matching its role
                # root. Empty binding means any agent (backward compatible).
                # The compare-and-swap below still makes the claim atomic.
                claim_slots = ",".join("?" for _ in claim_agent_ids)
                if claim_group:
                    # Prefer an owner with fewer currently running Stages in
                    # this role.  FIFO remains the tie-breaker, and when no
                    # other owner exists the same owner may use every worker.
                    order_clause = """
                    ORDER BY (
                        SELECT COUNT(*)
                        FROM tasks AS running_task
                        JOIN jobs AS running_job ON running_job.job_id=running_task.job_id
                        WHERE running_task.status='running'
                          AND running_task.required_agent_id=?
                          AND LOWER(running_job.owner)=LOWER(j.owner)
                    ), t.created_at ASC, t.order_index ASC, t.task_id ASC
                    """
                    query_params = (*claim_agent_ids, *claim_agent_ids, claim_group)
                else:
                    order_clause = "ORDER BY t.created_at ASC, t.order_index ASC, t.task_id ASC"
                    query_params = (*claim_agent_ids, *claim_agent_ids)
                rows = conn.execute(
                    f"""
                    SELECT t.task_id, t.job_id, t.task_type, t.stage_type,
                           t.order_index, t.dependencies_json, j.owner, j.job_type
                    FROM tasks t
                    JOIN jobs j ON j.job_id=t.job_id
                    WHERE t.status='queued'
                      AND (t.assigned_agent_id = '' OR t.assigned_agent_id IN ({claim_slots}))
                      AND (t.required_agent_id = '' OR t.required_agent_id IN ({claim_slots}))
                    {order_clause}
                    """,
                    query_params,
                ).fetchall()
                for row in rows:
                    if (
                        str(row["job_type"] or "").startswith("simulation.run_config.v2")
                        and not windows_connector_contract_is_current(agent_metadata)
                    ):
                        continue
                    if agent_node_kind in {"windows_agent", "windows_full"}:
                        job_owner = str(row["owner"] or "").strip().casefold()
                        if job_owner and registered_owner and registered_owner != job_owner:
                            continue
                    if not self._agent_can_claim_task(
                        agent_node_kind, row["task_type"], str(row["stage_type"] or ""), capabilities
                    ):
                        continue
                    if not self._task_is_ready_to_claim_locked(conn, row):
                        continue
                    updated = conn.execute(
                        """
                        UPDATE tasks
                        SET status='running', assigned_agent_id=?, claimed_at=?, started_at=?,
                            updated_at=?, attempt_count=attempt_count+1
                        WHERE task_id=? AND status='queued'
                        """,
                        (agent_id, now, now, now, row["task_id"]),
                    )
                    if updated.rowcount != 1:
                        continue
                    attempt = int(conn.execute(
                        "SELECT attempt_count FROM tasks WHERE task_id=?",
                        (row["task_id"],),
                    ).fetchone()["attempt_count"])
                    conn.execute(
                        """
                        INSERT INTO stage_attempts (
                            attempt_id, job_id, stage_id, attempt, agent_id, status,
                            started_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"attempt_{uuid.uuid4().hex[:12]}",
                            row["job_id"],
                            row["task_id"],
                            attempt,
                            agent_id,
                            "running",
                            now,
                            now,
                            now,
                        ),
                    )
                    self._append_event_locked(
                        conn,
                        row["job_id"],
                        stage_id=row["task_id"],
                        attempt=attempt,
                        event_type="stage.running",
                        status="running",
                        progress=0.0,
                        message=f"{row['task_type']} started",
                    )
                    conn.execute(
                        """
                        UPDATE agents
                        SET status='busy', current_task_id=?, last_heartbeat=?
                        WHERE agent_id=?
                        """,
                        (row["task_id"], now, agent_id),
                    )
                    self._refresh_job_status_locked(conn, self._task_job_id_locked(conn, row["task_id"]), now)
                    conn.commit()
                    return self._get_task_locked(conn, row["task_id"])

                conn.execute(
                    "UPDATE agents SET status='idle', current_task_id='', last_heartbeat=? WHERE agent_id=?",
                    (now, agent_id),
                )
                conn.commit()
                return None
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def reclaim_stale_tasks(
        self,
        *,
        stale_after_seconds: float = 300.0,
        max_attempts: Optional[int] = 3,
        assignment_grace_seconds: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Requeue running tasks whose agent has gone silent (dead-agent recovery).

        A task stuck in ``running`` whose assigned agent's ``last_heartbeat`` is
        older than ``stale_after_seconds`` is reset to ``queued`` so another
        agent can claim it. Tasks whose ``attempt_count`` already exceeds
        ``max_attempts`` are instead marked ``failed`` (the agent keeps crashing
        on them — don't loop forever). Returns the list of reclaimed tasks.

        This is the only recovery path for tasks orphaned by a crashed agent;
        without it they stay ``running`` forever. A fresh heartbeat alone is
        not proof that the Agent still owns the Stage: the Agent must report
        the task id, except for the legacy ``busy``/empty-task contract. A
        short assignment grace protects the claim transaction's hand-off
        window. Idempotent: safe to call periodically (e.g. from the server's
        serve loop or an admin CLI).
        """
        now = self._now()
        cutoff = now - float(stale_after_seconds)
        assignment_grace = max(0.0, float(assignment_grace_seconds))
        assignment_cutoff = now - assignment_grace
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                # Running tasks whose agent hasn't heartbeat since the cutoff.
                rows = conn.execute(
                    """
                    SELECT t.task_id, t.job_id, t.status, t.assigned_agent_id, t.attempt_count,
                           t.claimed_at, t.started_at,
                           a.status AS agent_status, a.current_task_id AS agent_current_task_id,
                           a.last_heartbeat AS agent_last_heartbeat,
                           t.cancel_requested, t.result_json, t.error_json, j.cancel_requested AS job_cancel_requested
                    FROM tasks t
                    JOIN jobs j ON j.job_id = t.job_id
                    LEFT JOIN agents a ON a.agent_id = t.assigned_agent_id
                    WHERE t.status IN ('running', 'cancel_requested', 'cancelling')
                      AND (
                          a.agent_id IS NULL
                          OR a.last_heartbeat < ?
                          OR (
                              a.last_heartbeat >= ?
                              AND NOT (
                                  a.current_task_id = t.task_id
                                  OR (a.current_task_id = '' AND LOWER(a.status) = 'busy')
                              )
                              AND MAX(t.claimed_at, t.started_at) <= ?
                          )
                      )
                    """,
                    (cutoff, cutoff, assignment_cutoff),
                ).fetchall()
                reclaimed: list[dict[str, Any]] = []
                for row in rows:
                    attempts = int(row["attempt_count"] or 0)
                    task = self._get_task_locked(conn, row["task_id"])
                    stale_error = {
                        "code": "AGENT_STALE",
                        "message": "agent heartbeat went stale while running the stage",
                        "agent_id": row["assigned_agent_id"],
                        "actions": [{"type": "check_agent", "label": "Check or restart the assigned agent"}],
                    }
                    # A cancellation can be represented either by the
                    # persisted flag (the normal path) or by an intermediate
                    # status written by an older Connector.  Both must become
                    # terminal when the assigned agent is stale; otherwise a
                    # disconnected Windows process can leave the Job in
                    # cancel_requested/cancelling forever.
                    if (
                        bool(row["cancel_requested"])
                        or bool(row["job_cancel_requested"])
                        or str(row["status"] or "") in {"cancel_requested", "cancelling"}
                    ):
                        final = "cancelled"
                        conn.execute(
                            """
                            UPDATE tasks
                            SET status='cancelled', cancel_requested=1, updated_at=?, completed_at=?,
                                error_json=?
                            WHERE task_id=?
                            """,
                            (now, now, self._dumps(stale_error), row["task_id"]),
                        )
                        self._finish_attempt_locked(
                            conn,
                            task,
                            attempt=attempts,
                            status="cancelled",
                            now=now,
                            returncode=None,
                            result=self._loads(row["result_json"]),
                            error=stale_error,
                        )
                        self._append_event_locked(
                            conn,
                            row["job_id"],
                            stage_id=row["task_id"],
                            attempt=attempts,
                            event_type="stage.cancelled",
                            status="cancelled",
                            code="AGENT_STALE",
                            message=stale_error["message"],
                            detail=stale_error,
                            action=stale_error["actions"],
                        )
                    elif max_attempts is not None and attempts >= max_attempts:
                        final = "failed"
                        conn.execute(
                            """
                            UPDATE tasks
                            SET status=?, result_json=?, error_json=?, updated_at=?, completed_at=?,
                                returncode=?
                            WHERE task_id=?
                            """,
                            (
                                final,
                                self._dumps({
                                    "error": f"task exceeded max_attempts ({max_attempts}) after agent went stale",
                                    "code": "AGENT_STALE",
                                }),
                                self._dumps(stale_error),
                                now, now, -1, row["task_id"],
                            ),
                        )
                        self._finish_attempt_locked(
                            conn,
                            task,
                            attempt=attempts,
                            status="failed",
                            now=now,
                            returncode=-1,
                            result={"error": f"task exceeded max_attempts ({max_attempts}) after agent went stale", "code": "AGENT_STALE"},
                            error=stale_error,
                        )
                        self._append_event_locked(
                            conn,
                            row["job_id"],
                            stage_id=row["task_id"],
                            attempt=attempts,
                            event_type="stage.failed",
                            level="error",
                            status="failed",
                            code="AGENT_STALE",
                            message=stale_error["message"],
                            detail=stale_error,
                            action=stale_error["actions"],
                        )
                        self._cancel_remaining_tasks_locked(conn, row["job_id"], now, exclude_task_id=row["task_id"])
                    else:
                        final = "queued"
                        conn.execute(
                            """
                            UPDATE tasks
                            SET status='queued', assigned_agent_id='', claimed_at=0,
                                started_at=0, updated_at=?, error_json=?
                            WHERE task_id=?
                            """,
                            (now, self._dumps(stale_error), row["task_id"]),
                        )
                        self._finish_attempt_locked(
                            conn,
                            task,
                            attempt=attempts,
                            status="failed",
                            now=now,
                            returncode=-1,
                            result={"error": "stage requeued after stale agent", "code": "AGENT_STALE"},
                            error=stale_error,
                        )
                        self._append_event_locked(
                            conn,
                            row["job_id"],
                            stage_id=row["task_id"],
                            attempt=attempts,
                            event_type="stage.requeued",
                            level="error",
                            status="queued",
                            code="AGENT_STALE",
                            message="stage requeued after stale agent",
                            detail=stale_error,
                            action=stale_error["actions"],
                        )
                    # Free the dead agent's current_task_id if it still points here.
                    if row["assigned_agent_id"]:
                        conn.execute(
                            """
                            UPDATE agents
                            SET status='idle', current_task_id=''
                            WHERE agent_id=? AND current_task_id=?
                            """,
                            (row["assigned_agent_id"], row["task_id"]),
                        )
                    self._refresh_job_status_locked(conn, row["job_id"], now)
                    reclaimed.append({
                        "task_id": row["task_id"],
                        "job_id": row["job_id"],
                        "agent_id": row["assigned_agent_id"],
                        "new_status": final,
                        "attempt_count": attempts,
                    })
                conn.commit()
                return reclaimed
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def append_logs(
        self,
        task_id: str,
        lines: list[str] | tuple[str, ...] | str,
        *,
        stream: str = "stdout",
    ) -> dict[str, Any]:
        if isinstance(lines, str):
            lines = [lines]
        entries = [str(line) for line in lines if str(line)]
        now = self._now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                task = self._get_task_locked(conn, task_id)
                if entries:
                    conn.executemany(
                        """
                        INSERT INTO task_logs (task_id, stream, message, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(task_id, stream, message, now) for message in entries],
                    )
                    attempt = int(task.get("attempt_count") or 0)
                    level = "error" if stream == "stderr" else "info"
                    for message in entries:
                        self._append_event_locked(
                            conn,
                            task["job_id"],
                            stage_id=task_id,
                            attempt=attempt,
                            event_type="log",
                            level=level,
                            status=str(task.get("status") or ""),
                            message=message,
                            detail={"stream": stream},
                        )
                    conn.execute("UPDATE tasks SET updated_at=? WHERE task_id=?", (now, task_id))
                    self._touch_job_locked(conn, task["job_id"], now)
                    conn.commit()
                elif conn.in_transaction:
                    conn.commit()
                return {"task_id": task_id, "appended": len(entries)}
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def submit_task_result(
        self,
        task_id: str,
        *,
        agent_id: str = "",
        status: str = "",
        returncode: Optional[int] = None,
        result: Optional[dict[str, Any]] = None,
        error: str = "",
    ) -> dict[str, Any]:
        now = self._now()
        result = dict(result or {})
        if error:
            result.setdefault("error", error)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                task = self._get_task_locked(conn, task_id)
                if task["status"] in TERMINAL_TASK_STATUSES:
                    raise ValueError(f"task already completed: {task_id}")
                assigned_agent_id = str(task["assigned_agent_id"] or "")
                if agent_id and assigned_agent_id and agent_id != assigned_agent_id:
                    raise ValueError(f"task {task_id} is assigned to {assigned_agent_id}")
                effective_agent_id = agent_id or assigned_agent_id
                attempt = self._ensure_attempt_locked(conn, task, agent_id=effective_agent_id, now=now)
                task = self._get_task_locked(conn, task_id)
                final_status = self._resolve_task_result_status(task, status=status, returncode=returncode)
                manifest = result.get("manifest") if isinstance(result.get("manifest"), dict) else {}
                manifest, manifest_failed = _normalize_manifest_outcome(manifest)
                if manifest:
                    result["manifest"] = manifest
                if (
                    str(task.get("stage_type") or "") == "finalize_manifest"
                    and manifest_failed
                ):
                    # A successfully executed finalizer does not mean that the
                    # simulation itself succeeded. Keep the manifest available,
                    # but make the public Job reflect its business outcome.
                    final_status = "failed"
                    if returncode in {None, 0}:
                        returncode = -1
                    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
                    errors = summary.get("errors") if isinstance(summary.get("errors"), list) else []
                    result.setdefault("code", "simulation_failed")
                    result.setdefault("message", str(errors[0]) if errors else "simulation result reported failure")
                error_obj = dict(result.get("error_json") or task.get("error") or {})
                if error and not error_obj:
                    error_obj = {"message": error}
                elif result.get("error") and not error_obj:
                    diagnostic = result.get("diagnostic") if isinstance(result.get("diagnostic"), dict) else {}
                    error_obj = {
                        "code": str(result.get("code") or diagnostic.get("code") or "stage_failed"),
                        "message": str(result.get("error")),
                    }
                    if diagnostic:
                        error_obj["diagnostic"] = dict(diagnostic)
                        if diagnostic.get("action"):
                            error_obj["action"] = str(diagnostic["action"])
                    for field in ("dependency", "repair_hint"):
                        if result.get(field):
                            error_obj[field] = str(result[field])
                elif final_status == "failed" and result.get("message") and not error_obj:
                    error_obj = {
                        "code": str(result.get("code") or "stage_failed"),
                        "message": (
                            "Simulation failed; inspect the result manifest for details"
                            if str(result.get("code") or "") == "simulation_failed"
                            else str(result.get("message"))
                        ),
                    }
                elif final_status == "failed" and not error_obj:
                    # Local Windows runs report per-input failures in their
                    # structured summary.  Promote that stable code to the
                    # Stage error so Web/SDK diagnosis can distinguish a
                    # Selena-engine failure from a scheduler failure even
                    # when the Agent did not send a separate ``error`` field.
                    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
                    summary_code = str(summary.get("error_code") or "").strip()
                    if summary_code:
                        error_obj = {
                            "code": summary_code,
                            "message": _stable_stage_failure_message(summary_code),
                        }
                        if isinstance(result.get("diagnostics"), dict):
                            error_obj["diagnostics"] = dict(result["diagnostics"])
                output_ref = result.get("output_ref") if isinstance(result.get("output_ref"), dict) else {}

                conn.execute(
                    """
                    UPDATE tasks
                    SET status=?, result_json=?, updated_at=?, completed_at=?, returncode=?,
                        progress=?, output_ref_json=?, error_json=?
                    WHERE task_id=?
                    """,
                    (
                        final_status,
                        self._dumps(result),
                        now,
                        now,
                        returncode,
                        1.0 if final_status == "succeeded" else float(task.get("progress") or 0.0),
                        self._dumps(dict(output_ref or {})),
                        self._dumps(error_obj),
                        task_id,
                    ),
                )
                self._finish_attempt_locked(
                    conn,
                    task,
                    attempt=attempt,
                    status=final_status,
                    now=now,
                    returncode=returncode,
                    result=result,
                    error=error_obj,
                )
                self._append_event_locked(
                    conn,
                    task["job_id"],
                    stage_id=task_id,
                    attempt=attempt,
                    event_type=f"stage.{final_status}",
                    level="error" if final_status == "failed" else "info",
                    status=final_status,
                    progress=1.0 if final_status == "succeeded" else None,
                    code=str(result.get("code") or error_obj.get("code") or ""),
                    message=str(result.get("message") or error_obj.get("message") or final_status),
                    detail={"returncode": returncode, "result": result},
                    action=self._normalize_actions(result.get("actions") or result.get("action") or []),
                )
                if final_status == "failed":
                    self._cancel_remaining_tasks_locked(conn, task["job_id"], now, exclude_task_id=task_id)
                if effective_agent_id:
                    conn.execute(
                        """
                        UPDATE agents
                        SET status='idle', current_task_id='', last_heartbeat=?
                        WHERE agent_id=?
                        """,
                        (now, effective_agent_id),
                    )
                old_job_status = self._get_job_locked(conn, task["job_id"])["status"]
                self._refresh_job_status_locked(conn, task["job_id"], now)
                if str(task.get("stage_type") or "") == "finalize_manifest" and manifest:
                    conn.execute(
                        "UPDATE jobs SET result_json=?, updated_at=? WHERE job_id=?",
                        (self._dumps(result), now, task["job_id"]),
                    )
                new_job_status = self._get_job_locked(conn, task["job_id"])["status"]
                if new_job_status != old_job_status:
                    self._append_event_locked(
                        conn,
                        task["job_id"],
                        event_type="job.status",
                        status=new_job_status,
                        message=f"job {new_job_status}",
                    )
                conn.commit()
                return self._get_job_locked(conn, task["job_id"])
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        now = self._now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                job = self._get_job_locked(conn, job_id)
                if job["status"] in TERMINAL_JOB_STATUSES:
                    conn.commit()
                    return job
                conn.execute(
                    "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE job_id=?",
                    (now, job_id),
                )
                old_job_status = job["status"]
                tasks = conn.execute(
                    """
                    SELECT task_id, status
                    FROM tasks
                    WHERE job_id=? AND status NOT IN ('succeeded', 'failed', 'cancelled', 'skipped')
                    """,
                    (job_id,),
                ).fetchall()
                for task in tasks:
                    if task["status"] in {"queued", "blocked"}:
                        conn.execute(
                            """
                            UPDATE tasks
                            SET status='cancelled', cancel_requested=1, updated_at=?, completed_at=?
                            WHERE task_id=?
                            """,
                            (now, now, task["task_id"]),
                        )
                        self._append_event_locked(
                            conn,
                            job_id,
                            stage_id=task["task_id"],
                            event_type="stage.cancelled",
                            status="cancelled",
                            message="stage cancelled",
                        )
                    else:
                        conn.execute(
                            "UPDATE tasks SET cancel_requested=1, updated_at=? WHERE task_id=?",
                            (now, task["task_id"]),
                        )
                        self._append_event_locked(
                            conn,
                            job_id,
                            stage_id=task["task_id"],
                            event_type="stage.cancel_requested",
                            status="cancel_requested",
                            message="stage cancellation requested",
                        )
                self._refresh_job_status_locked(conn, job_id, now)
                new_job_status = self._get_job_locked(conn, job_id)["status"]
                if new_job_status != old_job_status:
                    self._append_event_locked(
                        conn,
                        job_id,
                        event_type="job.status",
                        status=new_job_status,
                        message=f"job {new_job_status}",
                    )
                conn.commit()
                return self._get_job_locked(conn, job_id)
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._conn()
            try:
                return self._get_job_locked(conn, job_id)
            finally:
                conn.close()

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Return one persisted task/stage by id without changing its state."""
        task_id = str(task_id or "").strip()
        if not task_id:
            raise KeyError("unknown task")
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown task: {task_id}")
                return self._task_row_to_dict(row)
            finally:
                conn.close()

    def get_logs(
        self,
        *,
        job_id: str = "",
        task_id: str = "",
        since: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        if not job_id and not task_id:
            raise ValueError("job_id or task_id is required")
        with self._lock:
            conn = self._conn()
            try:
                if job_id:
                    self._get_job_locked(conn, job_id)
                if task_id:
                    self._get_task_locked(conn, task_id)
                params: list[Any] = [int(since or 0)]
                query = [
                    """
                    SELECT task_logs.log_id, task_logs.task_id, task_logs.stream,
                           task_logs.message, task_logs.created_at
                    FROM task_logs
                    """
                ]
                if job_id:
                    query.append("JOIN tasks ON tasks.task_id = task_logs.task_id")
                where = ["task_logs.log_id > ?"]
                if task_id:
                    where.append("task_logs.task_id = ?")
                    params.append(task_id)
                if job_id:
                    where.append("tasks.job_id = ?")
                    params.append(job_id)
                query.append("WHERE " + " AND ".join(where))
                query.append("ORDER BY task_logs.log_id ASC LIMIT ?")
                params.append(int(limit or 200))
                rows = conn.execute("\n".join(query), params).fetchall()
                entries = [
                    {
                        "log_id": row["log_id"],
                        "task_id": row["task_id"],
                        "stream": row["stream"],
                        "message": row["message"],
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ]
                next_since = since
                if entries:
                    next_since = entries[-1]["log_id"]
                return {"entries": entries, "next_since": next_since}
            finally:
                conn.close()

    def append_job_event(
        self,
        job_id: str,
        *,
        stage_id: str = "",
        attempt: int = 0,
        event_type: str = "message",
        level: str = "info",
        status: str = "",
        progress: float | None = None,
        code: str = "",
        message: str = "",
        detail: Optional[dict[str, Any]] = None,
        action: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._get_job_locked(conn, job_id)
                event = self._append_event_locked(
                    conn,
                    job_id,
                    stage_id=stage_id,
                    attempt=attempt,
                    event_type=event_type,
                    level=level,
                    status=status,
                    progress=progress,
                    code=code,
                    message=message,
                    detail=detail,
                    action=action,
                )
                conn.commit()
                return event
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def list_events(
        self,
        job_id: str,
        *,
        since: int = 0,
        limit: int = 200,
        tail: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._conn()
            try:
                self._get_job_locked(conn, job_id)
                if tail and int(since or 0) == 0:
                    rows = conn.execute(
                        """
                        SELECT * FROM (
                            SELECT *
                            FROM job_events
                            WHERE job_id=?
                            ORDER BY sequence DESC
                            LIMIT ?
                        )
                        ORDER BY sequence ASC
                        """,
                        (job_id, int(limit or 200)),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT *
                        FROM job_events
                        WHERE job_id=? AND sequence>?
                        ORDER BY sequence ASC
                        LIMIT ?
                        """,
                        (job_id, int(since or 0), int(limit or 200)),
                    ).fetchall()
                events = [self._event_row_to_dict(row) for row in rows]
                next_cursor = int(since or 0)
                if events:
                    next_cursor = int(events[-1]["sequence"])
                return {"events": events, "next_cursor": next_cursor}
            finally:
                conn.close()

    def report_stage_progress(
        self,
        stage_id: str,
        *,
        progress: float,
        message: str = "",
        code: str = "",
        detail: Optional[dict[str, Any]] = None,
        action: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        value = max(0.0, min(float(progress), 1.0))
        now = self._now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                task = self._get_task_locked(conn, stage_id)
                conn.execute(
                    "UPDATE tasks SET progress=?, updated_at=? WHERE task_id=?",
                    (value, now, stage_id),
                )
                event = self._append_event_locked(
                    conn,
                    task["job_id"],
                    stage_id=stage_id,
                    attempt=int(task.get("attempt_count") or 0),
                    event_type="stage.progress",
                    status=str(task.get("status") or ""),
                    progress=value,
                    code=code,
                    message=message,
                    detail=detail,
                    action=action,
                    timestamp=now,
                )
                self._touch_job_locked(conn, task["job_id"], now)
                conn.commit()
                return event
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def list_attempts(self, stage_id: str) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._conn()
            try:
                self._get_task_locked(conn, stage_id)
                rows = conn.execute(
                    "SELECT * FROM stage_attempts WHERE stage_id=? ORDER BY attempt ASC",
                    (stage_id,),
                ).fetchall()
                return [self._attempt_row_to_dict(row) for row in rows]
            finally:
                conn.close()

    def retry_stage(self, job_id: str, stage_id: str) -> dict[str, Any]:
        now = self._now()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                job = self._get_job_locked(conn, job_id)
                task = self._get_task_locked(conn, stage_id)
                if task["job_id"] != job_id:
                    raise ValueError(f"stage {stage_id} does not belong to job {job_id}")
                if task["status"] not in {"failed", "cancelled"}:
                    raise ValueError(f"stage {stage_id} is {task['status']}; only failed/cancelled stages can be retried")
                conn.execute(
                    """
                    UPDATE jobs
                    SET cancel_requested=0, completed_at=0, finished_at=0, updated_at=?
                    WHERE job_id=?
                    """,
                    (now, job_id),
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET status='queued', cancel_requested=0, claimed_at=0, started_at=0,
                        completed_at=0, returncode=NULL, result_json='{}', error_json='{}',
                        output_ref_json='{}', progress=0, updated_at=?
                    WHERE task_id=?
                    """,
                    (now, stage_id),
                )
                for downstream in self._retry_reset_candidates_locked(conn, job_id, stage_id, task["order_index"]):
                    if downstream["status"] not in {"cancelled", "failed"}:
                        continue
                    initial = str(downstream["initial_status"] or "queued")
                    next_status = "skipped" if initial == "skipped" else "queued"
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status=?, cancel_requested=0, claimed_at=0, started_at=0,
                            completed_at=?, returncode=NULL, result_json='{}', error_json='{}',
                            output_ref_json='{}', progress=?, updated_at=?
                        WHERE task_id=?
                        """,
                        (
                            next_status,
                            now if next_status == "skipped" else 0.0,
                            1.0 if next_status == "skipped" else 0.0,
                            now,
                            downstream["task_id"],
                        ),
                    )
                    self._append_event_locked(
                        conn,
                        job_id,
                        stage_id=downstream["task_id"],
                        event_type="stage.retry_reset",
                        status=next_status,
                        progress=1.0 if next_status == "skipped" else 0.0,
                        message=f"stage reset to {next_status} for retry",
                    )
                self._append_event_locked(
                    conn,
                    job_id,
                    stage_id=stage_id,
                    event_type="stage.retry",
                    status="queued",
                    progress=0.0,
                    message="stage queued for retry",
                )
                self._refresh_job_status_locked(conn, job_id, now)
                new_job = self._get_job_locked(conn, job_id)
                if new_job["status"] != job["status"]:
                    self._append_event_locked(
                        conn,
                        job_id,
                        event_type="job.status",
                        status=new_job["status"],
                        message=f"job {new_job['status']}",
                    )
                conn.commit()
                return self._get_job_locked(conn, job_id)
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def list_agents(self) -> list[dict[str, Any]]:
        """Return all registered agents, newest registration first.

        Used by the observability endpoint (``GET /api/agents``) and
        ``rsim server list-agents`` so operators can see which agents are
        connected, their last heartbeat, and what they're currently running.
        """
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    """
                    SELECT agent_id FROM agents
                    ORDER BY registered_at DESC, agent_id DESC
                    """,
                ).fetchall()
                return [self._get_agent_locked(conn, row["agent_id"]) for row in rows]
            finally:
                conn.close()

    def list_jobs(
        self,
        *,
        limit: int = 20,
        owner: str = "",
        status: str = "",
        job_type_prefix: str = "",
    ) -> list[dict[str, Any]]:
        """Return recent jobs newest-first with optional server-side filters.

        The default remains the legacy all-jobs view.  V1 callers pass owner
        and job_type_prefix so a shared ControlService database cannot leak
        another user's jobs or legacy task types into the product task center.
        """
        with self._lock:
            conn = self._conn()
            try:
                where: list[str] = []
                params: list[Any] = []
                if str(owner or ""):
                    where.append("owner=?")
                    params.append(str(owner))
                if str(status or ""):
                    where.append("status=?")
                    params.append(str(status))
                if str(job_type_prefix or ""):
                    where.append("job_type LIKE ?")
                    params.append(str(job_type_prefix) + "%")
                where_sql = " WHERE " + " AND ".join(where) if where else ""
                params.append(max(1, min(int(limit or 20), 200)))
                rows = conn.execute(
                    f"""
                    SELECT job_id, job_type, status, payload_json, metadata_json,
                           created_at, updated_at, completed_at, cancel_requested,
                           owner, idempotency_key, request_hash, spec_json,
                           resolved_spec_json, started_at, finished_at
                    FROM jobs
                    {where_sql}
                    ORDER BY created_at DESC, job_id DESC
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
                out: list[dict[str, Any]] = []
                for row in rows:
                    out.append({
                        "job_id": row["job_id"],
                        "job_type": row["job_type"],
                        "status": row["status"],
                        "payload": self._loads(row["payload_json"]),
                        "metadata": self._loads(row["metadata_json"]),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "completed_at": row["completed_at"],
                        "cancel_requested": bool(row["cancel_requested"]),
                        "owner": row["owner"],
                        "idempotency_key": row["idempotency_key"],
                        "request_hash": row["request_hash"],
                        "spec": self._loads(row["spec_json"]),
                        "resolved_spec": self._loads(row["resolved_spec_json"]),
                        "started_at": row["started_at"],
                        "finished_at": row["finished_at"],
                    })
                return out
            finally:
                conn.close()

    def _get_agent_locked(self, conn: sqlite3.Connection, agent_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown agent: {agent_id}")
        return {
            "agent_id": row["agent_id"],
            "name": row["name"],
            "platform": row["platform"],
            "hostname": row["hostname"],
            "capabilities": self._loads(row["capabilities_json"]),
            "metadata": self._loads(row["metadata_json"]),
            "status": row["status"],
            "registered_at": row["registered_at"],
            "last_heartbeat": row["last_heartbeat"],
            "current_task_id": row["current_task_id"],
        }

    def _get_job_locked(self, conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown job: {job_id}")
        task_rows = conn.execute(
            "SELECT * FROM tasks WHERE job_id=? ORDER BY order_index ASC, task_id ASC",
            (job_id,),
        ).fetchall()
        return {
            "job_id": row["job_id"],
            "job_type": row["job_type"],
            "status": row["status"],
            "payload": self._loads(row["payload_json"]),
            "metadata": self._loads(row["metadata_json"]),
            "result": self._loads(row["result_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "cancel_requested": bool(row["cancel_requested"]),
            "owner": row["owner"],
            "idempotency_key": row["idempotency_key"],
            "request_hash": row["request_hash"],
            "spec": self._loads(row["spec_json"]),
            "resolved_spec": self._loads(row["resolved_spec_json"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "tasks": [self._task_row_to_dict(task_row) for task_row in task_rows],
            "stages": [self._task_row_to_dict(task_row) for task_row in task_rows],
        }

    def _get_task_locked(self, conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown task: {task_id}")
        return self._task_row_to_dict(row)

    def _task_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "stage_id": row["task_id"],
            "job_id": row["job_id"],
            "task_type": row["task_type"],
            "stage_type": row["stage_type"],
            "order_index": row["order_index"],
            "status": row["status"],
            "payload": self._loads(row["payload_json"]),
            "result": self._loads(row["result_json"]),
            "dependencies": self._loads(row["dependencies_json"]),
            "progress": row["progress"],
            "input_ref": self._loads(row["input_ref_json"]),
            "output_ref": self._loads(row["output_ref_json"]),
            "error": self._loads(row["error_json"]),
            "skip_reason": row["skip_reason"],
            "initial_status": row["initial_status"],
            "assigned_agent_id": row["assigned_agent_id"],
            "required_agent_id": row["required_agent_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "claimed_at": row["claimed_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "cancel_requested": bool(row["cancel_requested"]),
            "attempt_count": row["attempt_count"],
            "returncode": row["returncode"],
        }

    def _touch_job_locked(self, conn: sqlite3.Connection, job_id: str, now: float) -> None:
        conn.execute("UPDATE jobs SET updated_at=? WHERE job_id=?", (now, job_id))

    def _task_job_id_locked(self, conn: sqlite3.Connection, task_id: str) -> str:
        row = conn.execute("SELECT job_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown task: {task_id}")
        return str(row["job_id"])

    def _refresh_job_status_locked(self, conn: sqlite3.Connection, job_id: str, now: float) -> None:
        job_row = conn.execute(
            "SELECT status, cancel_requested, started_at FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not job_row:
            raise KeyError(f"unknown job: {job_id}")
        tasks = conn.execute(
            "SELECT status, cancel_requested, started_at, completed_at, result_json, returncode FROM tasks WHERE job_id=?",
            (job_id,),
        ).fetchall()
        statuses = [str(task["status"]) for task in tasks]
        job_cancel_requested = bool(job_row["cancel_requested"])
        max_completed_at = max([float(task["completed_at"] or 0.0) for task in tasks] or [0.0])
        started_values = [float(task["started_at"] or 0.0) for task in tasks if float(task["started_at"] or 0.0) > 0]
        started_at = float(job_row["started_at"] or 0.0) or (min(started_values) if started_values else 0.0)
        if any(status == "running" for status in statuses):
            next_status = "cancel_requested" if job_cancel_requested or any(bool(task["cancel_requested"]) for task in tasks) else "running"
            completed_at = 0.0
        elif any(status == "queued" for status in statuses):
            next_status = "cancel_requested" if job_cancel_requested else "queued"
            completed_at = 0.0
        elif any(status == "blocked" for status in statuses):
            # Product job state is needs_input; blocked is a Stage state.
            next_status = "cancel_requested" if job_cancel_requested else "needs_input"
            completed_at = 0.0
        elif statuses and all(status in SUCCESS_TASK_STATUSES for status in statuses):
            next_status = "succeeded"
            completed_at = max_completed_at or now
        elif (
            job_cancel_requested
            and statuses
            and "failed" not in statuses
            and all(status in TERMINAL_TASK_STATUSES for status in statuses)
        ):
            # A user cancellation may arrive after earlier stages succeeded.
            # Once every remaining stage is terminal, that is still a cancelled
            # job; only a real failed Stage may override the user's intent.
            next_status = "cancelled"
            completed_at = max_completed_at or now
        elif statuses and all(status in {"cancelled", "skipped"} for status in statuses):
            next_status = "cancelled"
            completed_at = max_completed_at or now
        else:
            next_status = "failed"
            completed_at = max_completed_at or now

        result: dict[str, Any] = {}
        if tasks and next_status in TERMINAL_TASK_STATUSES:
            result = {
                "task_results": [
                    {
                        "status": task["status"],
                        "returncode": task["returncode"],
                        "result": self._loads(task["result_json"]),
                    }
                    for task in tasks
                ]
            }
        conn.execute(
            """
            UPDATE jobs
            SET status=?, updated_at=?, completed_at=?, result_json=?,
                started_at=?, finished_at=?
            WHERE job_id=?
            """,
            (
                next_status,
                now,
                completed_at,
                self._dumps(result),
                started_at,
                completed_at if next_status in TERMINAL_JOB_STATUSES else 0.0,
                job_id,
            ),
        )

    def _task_is_ready_to_claim_locked(self, conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
        dependencies = self._loads(row["dependencies_json"] if "dependencies_json" in row.keys() else "[]")
        if dependencies:
            blockers = conn.execute(
                f"SELECT status FROM tasks WHERE task_id IN ({','.join('?' for _ in dependencies)})",
                tuple(dependencies),
            ).fetchall()
            return len(blockers) == len(dependencies) and all(str(blocker["status"]) in SUCCESS_TASK_STATUSES for blocker in blockers)
        blockers = conn.execute(
            """
            SELECT status
            FROM tasks
            WHERE job_id=? AND order_index<?
            ORDER BY order_index ASC, task_id ASC
            """,
            (row["job_id"], row["order_index"]),
        ).fetchall()
        return all(str(blocker["status"]) in SUCCESS_TASK_STATUSES for blocker in blockers)

    def _resolve_task_result_status(
        self,
        task: dict[str, Any],
        *,
        status: str = "",
        returncode: Optional[int] = None,
    ) -> str:
        normalized_status = str(status or "")
        if normalized_status and normalized_status not in TERMINAL_TASK_STATUSES:
            raise ValueError(f"unsupported task status: {normalized_status}")
        if task["cancel_requested"]:
            if normalized_status in ("", "cancelled"):
                return "cancelled"
            if normalized_status == "succeeded":
                return "succeeded"
            if normalized_status == "failed" or returncode != 0:
                return "cancelled"
        if normalized_status:
            return normalized_status
        if returncode == 0:
            return "succeeded"
        return "failed"

    def _ensure_attempt_locked(
        self,
        conn: sqlite3.Connection,
        task: dict[str, Any],
        *,
        agent_id: str,
        now: float,
    ) -> int:
        attempt = int(task.get("attempt_count") or 0)
        if attempt <= 0:
            attempt = 1
            conn.execute(
                """
                UPDATE tasks
                SET attempt_count=1, assigned_agent_id=CASE WHEN assigned_agent_id='' THEN ? ELSE assigned_agent_id END,
                    claimed_at=CASE WHEN claimed_at=0 THEN ? ELSE claimed_at END,
                    updated_at=?
                WHERE task_id=? AND attempt_count=0
                """,
                (agent_id, now, now, task["task_id"]),
            )
        existing = conn.execute(
            "SELECT attempt_id FROM stage_attempts WHERE stage_id=? AND attempt=?",
            (task["task_id"], attempt),
        ).fetchone()
        if not existing:
            conn.execute(
                """
                INSERT INTO stage_attempts (
                    attempt_id, job_id, stage_id, attempt, agent_id, status,
                    started_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"attempt_{uuid.uuid4().hex[:12]}",
                    task["job_id"],
                    task["task_id"],
                    attempt,
                    agent_id,
                    "running",
                    float(task.get("started_at") or 0.0) or now,
                    now,
                    now,
                ),
            )
        return attempt

    def _finish_attempt_locked(
        self,
        conn: sqlite3.Connection,
        task: dict[str, Any],
        *,
        attempt: int,
        status: str,
        now: float,
        returncode: Optional[int] = None,
        result: Optional[dict[str, Any]] = None,
        error: Optional[dict[str, Any]] = None,
    ) -> None:
        self._ensure_attempt_locked(
            conn,
            {**task, "attempt_count": attempt},
            agent_id=str(task.get("assigned_agent_id") or ""),
            now=now,
        )
        conn.execute(
            """
            UPDATE stage_attempts
            SET status=?, finished_at=?, returncode=?, result_json=?, error_json=?, updated_at=?
            WHERE stage_id=? AND attempt=?
            """,
            (
                status,
                now,
                returncode,
                self._dumps(dict(result or {})),
                self._dumps(dict(error or {})),
                now,
                task["task_id"],
                int(attempt),
            ),
        )

    def _cancel_remaining_tasks_locked(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        now: float,
        *,
        exclude_task_id: str,
    ) -> None:
        upstream_error = {
            "code": "UPSTREAM_FAILED",
            "upstream_stage_id": exclude_task_id,
            "message": "stage cancelled because an upstream stage failed",
            "actions": [{"type": "retry_stage", "label": "Retry the failed upstream stage"}],
        }
        rows = conn.execute(
            """
            SELECT task_id, status
            FROM tasks
            WHERE job_id=? AND task_id<>? AND status NOT IN ('succeeded', 'failed', 'cancelled', 'skipped')
            """,
            (job_id, exclude_task_id),
        ).fetchall()
        for row in rows:
            if row["status"] == "queued":
                conn.execute(
                    """
                    UPDATE tasks
                    SET status='cancelled', cancel_requested=1, updated_at=?, completed_at=?,
                        error_json=?
                    WHERE task_id=?
                    """,
                    (now, now, self._dumps(upstream_error), row["task_id"]),
                )
                self._append_event_locked(
                    conn,
                    job_id,
                    stage_id=row["task_id"],
                    event_type="stage.cancelled",
                    status="cancelled",
                    code="UPSTREAM_FAILED",
                    message="stage cancelled after upstream failure",
                    detail=upstream_error,
                    action=upstream_error["actions"],
                )
                continue
            conn.execute(
                "UPDATE tasks SET cancel_requested=1, updated_at=?, error_json=? WHERE task_id=?",
                (now, self._dumps(upstream_error), row["task_id"]),
            )
            self._append_event_locked(
                conn,
                job_id,
                stage_id=row["task_id"],
                event_type="stage.cancel_requested",
                status="cancel_requested",
                code="UPSTREAM_FAILED",
                message="stage cancellation requested after upstream failure",
                detail=upstream_error,
                action=upstream_error["actions"],
            )

    def _normalize_dependencies(
        self,
        value: Any,
        stage_type_to_id: dict[str, str],
        *,
        valid_task_ids: set[str],
        self_task_id: str,
    ) -> list[str]:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, (list, tuple)):
            items = list(value)
        else:
            raise ValueError("dependencies must be an array of stage ids or stage_type names")
        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = str(item or "").strip()
            if not text:
                continue
            resolved = stage_type_to_id.get(text, text)
            if resolved not in valid_task_ids:
                raise ValueError(f"unknown dependency: {text}")
            if resolved == self_task_id:
                raise ValueError(f"self-dependency is not allowed: {text}")
            if resolved in seen:
                raise ValueError(f"duplicate dependency: {text}")
            seen.add(resolved)
            result.append(resolved)
        return result

    def _append_event_locked(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        *,
        stage_id: str = "",
        attempt: int = 0,
        event_type: str = "message",
        level: str = "info",
        status: str = "",
        progress: float | None = None,
        code: str = "",
        message: str = "",
        detail: Optional[dict[str, Any]] = None,
        action: Optional[list[dict[str, Any]]] = None,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        sequence = int(conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM job_events WHERE job_id=?",
            (job_id,),
        ).fetchone()[0])
        ts = float(timestamp if timestamp is not None else self._now())
        conn.execute(
            """
            INSERT INTO job_events (
                job_id, stage_id, attempt, sequence, timestamp, level, event_type,
                status, progress, code, message, detail_json, action_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                stage_id,
                int(attempt or 0),
                sequence,
                ts,
                level,
                event_type,
                status,
                progress,
                code,
                message,
                self._dumps(dict(detail or {})),
                self._dumps(list(action or [])),
            ),
        )
        return {
            "job_id": job_id,
            "stage_id": stage_id,
            "attempt": int(attempt or 0),
            "sequence": sequence,
            "timestamp": ts,
            "level": level,
            "event": event_type,
            "type": event_type,
            "status": status,
            "progress": progress,
            "code": code,
            "message": message,
            "detail": dict(detail or {}),
            "action": list(action or []),
        }

    def _event_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        progress = row["progress"]
        event_type = row["event_type"]
        data = {
            "job_id": row["job_id"],
            "stage_id": row["stage_id"],
            "attempt": row["attempt"],
            "status": row["status"],
            "progress": progress,
            "code": row["code"],
            "message": row["message"],
            "detail": self._loads(row["detail_json"]),
            "action": self._loads(row["action_json"]),
        }
        return {
            "id": row["sequence"],
            "event": event_type,
            "type": event_type,
            "job_id": row["job_id"],
            "stage_id": row["stage_id"],
            "attempt": row["attempt"],
            "sequence": row["sequence"],
            "timestamp": row["timestamp"],
            "level": row["level"],
            "status": row["status"],
            "progress": progress,
            "code": row["code"],
            "message": row["message"],
            "detail": data["detail"],
            "action": data["action"],
            "data": data,
        }

    def _attempt_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attempt_id": row["attempt_id"],
            "job_id": row["job_id"],
            "stage_id": row["stage_id"],
            "attempt": row["attempt"],
            "agent_id": row["agent_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "returncode": row["returncode"],
            "result": self._loads(row["result_json"]),
            "error": self._loads(row["error_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _retry_reset_candidates_locked(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        stage_id: str,
        order_index: int,
    ) -> list[sqlite3.Row]:
        rows = conn.execute(
            """
            SELECT task_id, status, dependencies_json, initial_status, attempt_count, order_index, error_json
            FROM tasks
            WHERE job_id=? AND task_id<>?
            ORDER BY order_index ASC
            """,
            (job_id, stage_id),
        ).fetchall()
        return [
            row
            for row in rows
            if self._loads(row["error_json"]).get("code") == "UPSTREAM_FAILED"
            and self._loads(row["error_json"]).get("upstream_stage_id") == stage_id
        ]

    @staticmethod
    def _capability_matches(task_type: str, capabilities: list[str]) -> bool:
        if not capabilities or "*" in capabilities:
            return True
        for capability in capabilities:
            if capability == task_type:
                return True
            if capability.endswith(".*") and task_type.startswith(capability[:-1]):
                return True
        return False

    def _agent_can_claim_task(
        self,
        node_kind: str,
        task_type: str,
        stage_type: str,
        capabilities: list[str],
    ) -> bool:
        """Claim-time capability match with the light-Agent boundary enforced.

        A known light node (``windows_agent``) must never claim a forbidden
        task/stage type — runtime simulation or any Cluster runtime stage —
        even if it declared a wildcard, the exact forbidden token, or a corrupt
        record assigned one. This is the safety net for old / corrupt records:
        the registration-time normalization already strips forbidden
        capabilities, but a task created before policy existed (or hand-edited)
        could still match a wildcard. Claim gating closes that hole.

        For an undeclared node kind (legacy agents / tests), the original
        ``_capability_matches`` semantics are preserved so nothing breaks.
        """
        from core.agent_policy import (
            may_claim_task,
            normalize_node_kind,
            required_capabilities_for_task,
        )

        kind = normalize_node_kind(node_kind)
        if not kind:
            return self._capability_matches(task_type, capabilities)
        if not may_claim_task(kind, task_type, stage_type):
            return False
        if not capabilities:
            return False
        if self._capability_matches(task_type, capabilities) or (
            bool(stage_type) and self._capability_matches(stage_type, capabilities)
        ):
            return True
        required = required_capabilities_for_task(task_type, stage_type, kind)
        return bool(required) and all(capability in capabilities for capability in required)

    @staticmethod
    def _normalize_actions(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return [dict(value)]
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _loads(value: str) -> Any:
        return json.loads(value) if value else {}
