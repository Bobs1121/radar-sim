"""Safe Windows-full local simulation lease and runner boundary.

This module deliberately does not call ``cli.run``.  That legacy entry point
may derive an output path beside the input MF4 and writes project-global run
history.  A full Windows Agent instead persists private paths in this store,
gives an injected runner an output path below ``RSIM_HOME/agent/runs``, and
publishes only logical references, relative names, sizes and checksums.

The native Selena command/paramconfig adapter is intentionally not implemented
here yet: the exact paramconfig source is project-adapter specific.  Callers
must inject a :class:`LocalSimulationRunner` until that adapter is available.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping, Protocol

from core.agent_asset_bindings import AgentAssetBindingStore
from core.agent_bindings import default_agent_binding_db_path
from core.agent_data_lease import AgentDataLease
from core.runtime_bundle import RuntimeBundleManifest


class AgentLocalRunError(ValueError):
    """Stable, path-free local execution boundary error."""


class LocalRunnerUnavailable(AgentLocalRunError):
    """Raised when no native project-adapter runner has been connected."""


class LocalRunAlreadyExecuting(AgentLocalRunError):
    """Raised when another live Connector process owns the same run lease."""


_LEASE_RE = re.compile(r"^local-run-lease:sha256:[0-9a-f]{64}$")
_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TERMINAL = {"succeeded", "failed", "cancelled"}


@dataclass(frozen=True)
class LocalRunRequest:
    """Private request passed to exactly one Agent-local runner invocation."""

    lease_id: str
    item_index: int
    input_mf4: Path
    output_mf4: Path
    executable: Path
    runtime_xml: Path
    adapter_file: Path | None
    mat_filter: Path
    working_directory: Path
    timeout_seconds: int
    config: dict[str, Any]


@dataclass(frozen=True)
class LocalRunOutcome:
    """Minimal deterministic outcome; output existence is verified separately."""

    exit_code: int
    error_code: str = ""
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise AgentLocalRunError("local runner returned an invalid exit code")
        code = str(self.error_code or "").strip()
        if code and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise AgentLocalRunError("local runner returned an invalid error code")
        object.__setattr__(self, "error_code", code)
        raw_diagnostics = self.diagnostics
        if isinstance(raw_diagnostics, str):
            raw_diagnostics = (raw_diagnostics,)
        try:
            normalized = tuple(
                str(line).replace("\x00", "")[:2000]
                for line in (raw_diagnostics or ())
                if str(line).strip()
            )[-200:]
        except TypeError as exc:
            raise AgentLocalRunError("local runner returned invalid diagnostics") from exc
        object.__setattr__(self, "diagnostics", normalized)


class LocalSimulationRunner(Protocol):
    def __call__(
        self,
        request: LocalRunRequest,
        cancel_requested: Callable[[], bool],
    ) -> LocalRunOutcome: ...


class AgentLocalRunLeaseStore:
    """Persistent private execution state for the Windows-full Agent."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        runs_root: str | Path | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_agent_binding_db_path().with_name("local-runs.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.runs_root = _controlled_runs_root(runs_root)
        self._now_fn = now_fn
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_local_runs (
                    lease_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    project TEXT NOT NULL,
                    runtime_bundle_id TEXT NOT NULL,
                    data_lease_id TEXT NOT NULL,
                    private_config_json TEXT NOT NULL,
                    inputs_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    run_root TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    outputs_json TEXT NOT NULL DEFAULT '[]',
                    error_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    diagnostics_json TEXT NOT NULL DEFAULT '{}',
                    execution_token TEXT NOT NULL DEFAULT '',
                    execution_pid INTEGER NOT NULL DEFAULT 0,
                    running_since REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(agent_local_runs)")
            }
            if "diagnostics_json" not in columns:
                # Existing Windows installations keep this database across
                # Agent upgrades.  Add diagnostics without invalidating old
                # leases or requiring a destructive migration.
                conn.execute(
                    "ALTER TABLE agent_local_runs ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '{}'"
                )
            for name, declaration in (
                ("execution_token", "TEXT NOT NULL DEFAULT ''"),
                ("execution_pid", "INTEGER NOT NULL DEFAULT 0"),
                ("running_since", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE agent_local_runs ADD COLUMN {name} {declaration}"
                    )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def create_from_authorized_inputs(
        self,
        *,
        job_id: str,
        project: str,
        base_config: dict[str, Any],
        runtime_manifest: RuntimeBundleManifest,
        runtime_locations: Mapping[str, str | Path],
        data_lease: AgentDataLease,
        asset_bindings: AgentAssetBindingStore,
        adapter_binding_id: str,
        adapter_path: str,
        mat_filter_binding_id: str,
        mat_filter_path: str,
        timeout_seconds: int,
        verify_input_checksums: bool = True,
    ) -> dict[str, Any]:
        """Authorize immutable inputs, construct private config and create a lease.

        Cluster/upload paths keep the default content verification.  A local
        Windows run may opt out after ``prepare_data`` has already created an
        immutable lease: that lease records size/mtime and (for upload
        routes) checksums, while rehashing a multi-gigabyte local recording
        just before every run would unnecessarily delay Selena.
        """
        job_id = _required_token(job_id, "local run job id")
        project = _required_token(project, "local run project")
        if not isinstance(base_config, dict):
            raise AgentLocalRunError("local run base config is invalid")
        try:
            base_config_checksum = "sha256:" + hashlib.sha256(_json(base_config).encode("utf-8")).hexdigest()
        except (TypeError, ValueError) as exc:
            raise AgentLocalRunError("local run base config is not JSON serializable") from exc
        if not isinstance(runtime_manifest, RuntimeBundleManifest):
            raise AgentLocalRunError("Runtime Bundle manifest is invalid")
        if not isinstance(data_lease, AgentDataLease) or data_lease.project != project:
            raise AgentLocalRunError("data lease is unavailable for this local run")
        timeout = _positive_timeout(timeout_seconds)

        runtime = _verify_runtime_locations(runtime_manifest, runtime_locations)
        inputs = _verify_data_lease(
            data_lease, verify_checksums=bool(verify_input_checksums)
        )
        adapter: Path | None = None
        try:
            if str(adapter_path or "").strip():
                adapter = asset_bindings.authorize_path(
                    binding_id=adapter_binding_id, asset_path=adapter_path, role="adapter"
                )
            mat_filter = asset_bindings.authorize_path(
                binding_id=mat_filter_binding_id, asset_path=mat_filter_path, role="mat_filter"
            )
        except Exception as exc:
            raise AgentLocalRunError("Adapter or MatFilter is not authorized for local execution") from exc

        evidence = {
            "runtime_bundle_id": runtime_manifest.id,
            "data_lease_id": data_lease.lease_id,
            "adapter_checksum": _sha256_regular_file(adapter) if adapter is not None else "",
            "mat_filter_checksum": _sha256_regular_file(mat_filter),
            "input_checksums": [item["checksum"] for item in inputs],
            "base_config_checksum": base_config_checksum,
        }
        identity = {
            "job_id": job_id,
            "project": project,
            "evidence": evidence,
            "timeout_seconds": timeout,
        }
        lease_id = "local-run-lease:sha256:" + _json_digest(identity)
        run_root = _safe_child(self.runs_root, lease_id.rsplit(":", 1)[-1])
        (run_root / "outputs").mkdir(parents=True, exist_ok=True)
        (run_root / "work").mkdir(parents=True, exist_ok=True)

        config = _private_config(
            base_config,
            project=project,
            manifest=runtime_manifest,
            executable=runtime["entrypoint"],
            runtime_xml=runtime["runtime_config"],
            adapter=adapter,
            mat_filter=mat_filter,
            run_root=run_root,
        )
        stored_inputs = [
            {
                **item,
                "path": str(item["path"]),
                "output_relative_path": _output_relative_path(index, item["relative_path"], item["checksum"]),
            }
            for index, item in enumerate(inputs, start=1)
        ]
        now = float(self._now_fn())
        if not math.isfinite(now) or now < 0:
            raise AgentLocalRunError("system clock is invalid")
        with self._connect() as conn:
            existing = conn.execute("SELECT * FROM agent_local_runs WHERE job_id=?", (job_id,)).fetchone()
            if existing is not None:
                if str(existing["lease_id"]) != lease_id:
                    raise AgentLocalRunError("local run job conflicts with existing immutable evidence")
                return self._public(existing)
            conn.execute(
                """
                INSERT INTO agent_local_runs(
                    lease_id,job_id,project,runtime_bundle_id,data_lease_id,
                    private_config_json,inputs_json,evidence_json,run_root,
                    timeout_seconds,status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,'ready',?,?)
                """,
                (
                    lease_id, job_id, project, runtime_manifest.id, data_lease.lease_id,
                    _json(config), _json(stored_inputs), _json(evidence), str(run_root),
                    timeout, now, now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM agent_local_runs WHERE lease_id=?", (lease_id,)).fetchone()
        return self._public(row)

    def get_private(self, lease_id: str) -> dict[str, Any]:
        row = self._row(lease_id)
        run_root = _existing_controlled_run_root(self.runs_root, Path(str(row["run_root"])))
        return {
            **self._public(row),
            "config": json.loads(row["private_config_json"]),
            "inputs": json.loads(row["inputs_json"]),
            "evidence": json.loads(row["evidence_json"]),
            "run_root": run_root,
            "timeout_seconds": int(row["timeout_seconds"]),
            "execution_pid": int(row["execution_pid"] or 0),
            "outputs": json.loads(row["outputs_json"]),
            "error_count": int(row["error_count"]),
            "error_code": str(row["error_code"]),
            "diagnostics": json.loads(row["diagnostics_json"] or "{}"),
        }

    def mark_running(
        self,
        lease_id: str,
        *,
        execution_token: str,
        execution_pid: int,
    ) -> dict[str, Any]:
        token = str(execution_token or "").strip()
        pid = int(execution_pid or 0)
        if not re.fullmatch(r"[0-9a-f]{32}", token) or pid <= 0:
            raise AgentLocalRunError("local run execution identity is invalid")
        now = float(self._now_fn())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM agent_local_runs WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if row is None:
                raise AgentLocalRunError("local run lease is unavailable")
            status = str(row["status"])
            existing_token = str(row["execution_token"] or "")
            existing_pid = int(row["execution_pid"] or 0)
            try:
                input_count = len(json.loads(row["inputs_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                input_count = 1
            configured_timeout = int(row["timeout_seconds"])
            # ``timeout_seconds=0`` is the public unlimited-runtime contract.
            # Do not turn it into a hidden stale/restart deadline: if the old
            # process is alive, a second Connector must observe it no matter
            # how long the batch has been running.  A dead PID is still
            # recoverable immediately through the liveness check below.
            max_running_age = (
                float("inf")
                if configured_timeout == 0
                else max(configured_timeout, 1) * max(input_count, 1) + 300
            )
            running_age = max(0.0, now - float(row["running_since"] or 0.0))
            if (
                status == "running"
                and existing_token != token
                and running_age <= max_running_age
                and _pid_alive(existing_pid)
            ):
                raise LocalRunAlreadyExecuting("local run is already executing")
            if status in _TERMINAL:
                # A duplicate control callback may arrive just after the
                # original process committed its terminal result.  Route it
                # through the same observer path instead of rejecting a
                # harmless at-least-once delivery or re-running Selena.
                raise LocalRunAlreadyExecuting("local run is already terminal")
            if status not in {"ready", "running"}:
                raise AgentLocalRunError("local run cannot enter running state")
            conn.execute(
                """
                UPDATE agent_local_runs
                SET status='running',execution_token=?,execution_pid=?,running_since=?,updated_at=?
                WHERE lease_id=?
                """,
                (token, pid, now, now, lease_id),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM agent_local_runs WHERE lease_id=?", (lease_id,)
            ).fetchone()
        return self._public(updated)

    def finish(
        self,
        lease_id: str,
        *,
        status: str,
        outputs: list[dict[str, Any]],
        error_count: int,
        error_code: str = "",
        diagnostics: dict[str, Any] | None = None,
        execution_token: str = "",
    ) -> dict[str, Any]:
        if status not in _TERMINAL:
            raise AgentLocalRunError("local run terminal status is invalid")
        return self._update(
            lease_id,
            status=status,
            outputs=outputs,
            error_count=max(0, int(error_count)),
            error_code=_safe_error_code(error_code),
            diagnostics=diagnostics,
            expected_execution_token=execution_token,
        )

    def checkpoint(
        self,
        lease_id: str,
        *,
        outputs: list[dict[str, Any]],
        error_count: int,
        error_code: str = "",
        diagnostics: dict[str, Any] | None = None,
        execution_token: str,
    ) -> dict[str, Any]:
        """Persist per-input progress while a batch is still running.

        A Connector can be terminated after Selena has completed several
        files but before the final callback is sent.  Keeping this checkpoint
        in the Agent-local SQLite lease lets the next process resume only the
        unprocessed inputs; it never treats a partial checkpoint as a public
        success.
        """
        return self._update(
            lease_id,
            status="running",
            outputs=outputs,
            error_count=max(0, int(error_count)),
            error_code=_safe_error_code(error_code),
            diagnostics=diagnostics,
            expected_execution_token=execution_token,
        )

    def result(self, lease_id: str) -> dict[str, Any]:
        private = self.get_private(lease_id)
        if private["status"] not in _TERMINAL:
            raise AgentLocalRunError("local run is not terminal")
        files: list[dict[str, Any]] = []
        for item in private["outputs"]:
            relative = _safe_output_relative(str(item.get("relative_path") or ""))
            path = _safe_child(private["run_root"], *PurePosixPath(relative).parts)
            checksum = _sha256_regular_file(path)
            files.append({"relative_path": relative, "size": path.stat().st_size, "checksum": checksum})
        files.sort(key=lambda item: item["relative_path"].casefold())
        summary = {
            "file_count": len(files),
            "error_count": private["error_count"],
            "error_code": private["error_code"],
        }
        diagnostics = dict(private.get("diagnostics") or {})
        if private["status"] == "failed":
            items = diagnostics.get("items") if isinstance(diagnostics.get("items"), list) else []
            summary["failed_input_count"] = sum(
                1 for item in items if str(item.get("status") or "") == "failed"
            )
            summary["succeeded_input_count"] = sum(
                1 for item in items if str(item.get("status") or "") == "succeeded"
            )
            summary["total_input_count"] = len(private["inputs"])
        payload = {"lease_id": lease_id, "status": private["status"], "files": files, "summary": summary}
        return {
            "result_ref": "result:sha256:" + _json_digest(payload),
            "status": private["status"],
            "files": files,
            "summary": summary,
            "diagnostics": diagnostics,
        }

    def _row(self, lease_id: str) -> sqlite3.Row:
        if not _LEASE_RE.fullmatch(str(lease_id or "")):
            raise AgentLocalRunError("local run lease is unavailable")
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM agent_local_runs WHERE lease_id=?", (lease_id,)).fetchone()
        if row is None:
            raise AgentLocalRunError("local run lease is unavailable")
        return row

    def _update(
        self,
        lease_id: str,
        *,
        status: str,
        outputs: list[dict[str, Any]] | None = None,
        error_count: int | None = None,
        error_code: str | None = None,
        diagnostics: dict[str, Any] | None = None,
        expected_execution_token: str = "",
    ) -> dict[str, Any]:
        now = float(self._now_fn())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM agent_local_runs WHERE lease_id=?", (lease_id,)
            ).fetchone()
            if current is None:
                raise AgentLocalRunError("local run lease is unavailable")
            if expected_execution_token and str(current["execution_token"] or "") != str(
                expected_execution_token
            ):
                raise AgentLocalRunError("local run execution ownership changed")
            conn.execute(
                """
                UPDATE agent_local_runs SET status=?,outputs_json=COALESCE(?,outputs_json),
                    error_count=COALESCE(?,error_count),error_code=COALESCE(?,error_code),
                    diagnostics_json=COALESCE(?,diagnostics_json),updated_at=?
                WHERE lease_id=?
                """,
                (
                    status, _json(outputs) if outputs is not None else None,
                    error_count, error_code,
                    _json(diagnostics) if diagnostics is not None else None,
                    now, lease_id,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM agent_local_runs WHERE lease_id=?", (lease_id,)).fetchone()
        return self._public(row)

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        inputs = json.loads(row["inputs_json"])
        outputs = json.loads(row["outputs_json"])
        return {
            "lease_id": str(row["lease_id"]),
            "job_id": str(row["job_id"]),
            "project": str(row["project"]),
            "runtime_bundle_id": str(row["runtime_bundle_id"]),
            "data_lease_id": str(row["data_lease_id"]),
            "status": str(row["status"]),
            "input_count": len(inputs),
            "output_count": len(outputs),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }


def execute_local_run(
    lease_id: str,
    store: AgentLocalRunLeaseStore | None = None,
    *,
    runner: LocalSimulationRunner | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> int:
    """Execute a lease using an injected runner and a controlled output contract."""
    store = store or AgentLocalRunLeaseStore()
    lease = store.get_private(lease_id)
    cancel = cancel_requested or (lambda: False)
    if runner is None:
        runner = _runner_unavailable
    execution_token = uuid.uuid4().hex
    try:
        store.mark_running(
            lease_id,
            execution_token=execution_token,
            execution_pid=os.getpid(),
        )
    except LocalRunAlreadyExecuting:
        # A watchdog may start a second Connector while the original process
        # is still completing this immutable lease.  Observe the existing
        # owner instead of launching a duplicate Selena process or overwriting
        # its deterministic outputs.
        configured_timeout = int(lease["timeout_seconds"])
        wait_deadline = (
            None
            if configured_timeout == 0
            else time.monotonic() + max(configured_timeout, 1) + 120
        )
        while wait_deadline is None or time.monotonic() < wait_deadline:
            if cancel():
                return 130
            observed = store.get_private(lease_id)
            if observed["status"] in _TERMINAL:
                return 0 if observed["status"] == "succeeded" else (
                    130 if observed["status"] == "cancelled" else 1
                )
            # If the previous Connector died without committing a terminal
            # state, reclaim the immutable lease instead of waiting forever.
            # This is process-liveness recovery, not a simulation wall-clock
            # timeout.  A live owner remains the single executor.
            if observed["status"] == "running" and not _pid_alive(int(observed.get("execution_pid") or 0)):
                try:
                    store.mark_running(
                        lease_id,
                        execution_token=execution_token,
                        execution_pid=os.getpid(),
                    )
                    lease = store.get_private(lease_id)
                    break
                except LocalRunAlreadyExecuting:
                    pass
            time.sleep(0.25)
        else:
            raise AgentLocalRunError("local run execution owner did not finish before timeout")
    # Reload after acquiring the execution token.  If a previous Connector
    # died after checkpointing part of a batch, resume only inputs without a
    # valid terminal checkpoint; do not rerun completed Selena files.
    lease = store.get_private(lease_id)
    output_by_relative = {
        str(item.get("relative_path") or ""): dict(item)
        for item in lease.get("outputs") or ()
        if isinstance(item, Mapping)
    }
    outputs: list[dict[str, Any]] = []
    failures = 0
    terminal_error = ""
    execution_items: list[dict[str, Any]] = []
    completed_indices: set[int] = set()
    for raw_item in (lease.get("diagnostics") or {}).get("items") or ():
        if not isinstance(raw_item, Mapping):
            continue
        try:
            item_index = int(raw_item.get("index"))
        except (TypeError, ValueError):
            continue
        item_status = str(raw_item.get("status") or "").strip().lower()
        if item_status not in {"succeeded", "failed"}:
            continue
        if item_status == "succeeded":
            relative = str(raw_item.get("output_relative_path") or "")
            checkpointed_output = output_by_relative.get(relative)
            if checkpointed_output is None or not _checkpoint_output_is_valid(
                lease["run_root"], checkpointed_output
            ):
                # The checkpoint is not authoritative if its output was
                # removed or modified; rerun that one item and discard the
                # stale success marker.
                continue
            outputs.append(checkpointed_output)
        else:
            failures += 1
            terminal_error = str(raw_item.get("error_code") or "runner_failed")
        execution_items.append(dict(raw_item))
        completed_indices.add(item_index)
    engine_log_tail = list(
        (lease.get("diagnostics") or {}).get("engine_log_tail") or ()
    )

    def checkpoint_progress() -> None:
        store.checkpoint(
            lease_id,
            outputs=outputs,
            error_count=failures,
            error_code=terminal_error,
            diagnostics={
                "items": execution_items,
                "engine_log_tail": _bounded_lines(engine_log_tail),
            },
            execution_token=execution_token,
        )

    for index, item in enumerate(lease["inputs"], start=1):
        if index in completed_indices:
            continue
        if cancel():
            store.finish(
                lease_id, status="cancelled", outputs=outputs,
                error_count=failures, error_code="cancelled",
                diagnostics={"items": execution_items, "engine_log_tail": _bounded_lines(engine_log_tail)},
                execution_token=execution_token,
            )
            return 130
        output_relative = str(item.get("output_relative_path") or "")
        try:
            output_relative = _safe_output_relative(item["output_relative_path"])
        except Exception:
            failures += 1
            terminal_error = "runner_contract_failed"
            execution_items.append(
                {
                    "index": index,
                    "input_relative_path": _relative_input_path(item),
                    "output_relative_path": output_relative,
                    "status": "failed",
                    "returncode": 1,
                    "error_code": terminal_error,
                }
            )
            checkpoint_progress()
            continue
        try:
            # ``prepare_data`` and ``preflight`` validate the complete batch,
            # but a large batch may wait in the Agent queue before Selena
            # reaches a particular file.  Revalidate each input immediately
            # before launching that item so a same-size/same-mtime replacement
            # cannot be simulated under stale evidence.  A file can still be
            # modified after this check (Windows cannot lock arbitrary user
            # recordings without blocking legitimate producers); the result
            # contract and final checksums remain authoritative.
            _verify_stored_input(item)
        except AgentLocalRunError:
            failures += 1
            terminal_error = "input_changed_after_preflight"
            execution_items.append(
                {
                    "index": index,
                    "input_relative_path": _relative_input_path(item),
                    "output_relative_path": output_relative,
                    "status": "failed",
                    "returncode": 1,
                    "error_code": terminal_error,
                }
            )
            # Do not start Selena for this input.  Other files in a batch may
            # still be immutable and can be processed independently.
            checkpoint_progress()
            continue
        try:
            output = _safe_child(lease["run_root"], *PurePosixPath(output_relative).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.unlink(missing_ok=True)
            config = copy.deepcopy(lease["config"])
            config.setdefault("paths", {})["input_mf4"] = str(item["path"])
            config["paths"]["output_mf4"] = str(output)
            sim = config.setdefault("simulation", {})
            sim["input_mf4"] = str(item["path"])
            sim["output_mf4"] = str(output)
            request = LocalRunRequest(
                lease_id=lease_id,
                item_index=index,
                input_mf4=Path(item["path"]),
                output_mf4=output,
                executable=Path(config["_local_run"]["executable"]),
                runtime_xml=Path(config["simulation"]["runtime_xml"]),
                adapter_file=(
                    Path(config["simulation"]["adapter_file"])
                    if str(config["simulation"].get("adapter_file") or "").strip()
                    else None
                ),
                mat_filter=Path(config["simulation"]["matfilefilter"]),
                working_directory=Path(config["_local_run"]["working_directory"]),
                timeout_seconds=lease["timeout_seconds"],
                config=config,
            )
            outcome = runner(request, cancel)
            if not isinstance(outcome, LocalRunOutcome):
                raise AgentLocalRunError("local runner returned an invalid outcome")
            if cancel():
                store.finish(
                    lease_id, status="cancelled", outputs=outputs,
                    error_count=failures, error_code="cancelled",
                    diagnostics={"items": execution_items, "engine_log_tail": _bounded_lines(engine_log_tail)},
                    execution_token=execution_token,
                )
                return 130
            if outcome.exit_code == 0:
                output_checksum = _sha256_regular_file(output)
                outputs.append(
                    {
                        "relative_path": output_relative,
                        "size": int(output.stat().st_size),
                        "checksum": output_checksum,
                    }
                )
                execution_items.append(
                    {
                        "index": index,
                        "input_relative_path": _relative_input_path(item),
                        "output_relative_path": output_relative,
                        "status": "succeeded",
                        "returncode": int(outcome.exit_code),
                    }
                )
            else:
                failures += 1
                terminal_error = outcome.error_code or "runner_failed"
                item_detail = {
                    "index": index,
                    "input_relative_path": _relative_input_path(item),
                    "output_relative_path": output_relative,
                    "status": "failed",
                    "returncode": int(outcome.exit_code),
                    "error_code": terminal_error,
                }
                diagnostics = _redact_runner_diagnostics(outcome.diagnostics, request)
                if diagnostics:
                    item_detail["engine_log_tail"] = diagnostics
                    engine_log_tail.extend(diagnostics)
                execution_items.append(item_detail)
        except LocalRunnerUnavailable:
            failures += 1
            terminal_error = "runner_unavailable"
            execution_items.append(
                {
                    "index": index,
                    "input_relative_path": _relative_input_path(item),
                    "output_relative_path": output_relative,
                    "status": "failed",
                    "returncode": 1,
                    "error_code": terminal_error,
                }
            )
            checkpoint_progress()
            break
        except AgentLocalRunError:
            failures += 1
            terminal_error = "runner_contract_failed"
            execution_items.append(
                {
                    "index": index,
                    "input_relative_path": _relative_input_path(item),
                    "output_relative_path": output_relative,
                    "status": "failed",
                    "returncode": 1,
                    "error_code": terminal_error,
                }
            )

        except Exception:
            # Runner exceptions are untrusted implementation details and may
            # include local paths.  Persist only a stable public error code.
            failures += 1
            terminal_error = "runner_contract_failed"
            execution_items.append(
                {
                    "index": index,
                    "input_relative_path": _relative_input_path(item),
                    "output_relative_path": output_relative,
                    "status": "failed",
                    "returncode": 1,
                    "error_code": terminal_error,
                }
            )

        checkpoint_progress()

    status = "succeeded" if failures == 0 and len(outputs) == len(lease["inputs"]) else "failed"
    diagnostics_payload: dict[str, Any] = {"items": execution_items}
    if engine_log_tail:
        diagnostics_payload["engine_log_tail"] = _bounded_lines(engine_log_tail)
    store.finish(
        lease_id, status=status, outputs=outputs,
        error_count=failures, error_code=terminal_error,
        diagnostics=diagnostics_payload,
        execution_token=execution_token,
    )
    return 0 if status == "succeeded" else 1


def _runner_unavailable(request: LocalRunRequest, cancel_requested: Callable[[], bool]) -> LocalRunOutcome:
    del request, cancel_requested
    raise LocalRunnerUnavailable("native local Selena runner is not connected")


def _pid_alive(pid: int) -> bool:
    pid = int(pid or 0)
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _relative_input_path(item: Mapping[str, Any]) -> str:
    """Return one validated logical input name for public diagnostics."""
    value = str(item.get("relative_path") or "").replace("\\", "/").strip()
    if not value or value.startswith("/") or ".." in PurePosixPath(value).parts:
        return "<input>"
    return value[:512]


def _bounded_lines(lines: list[str] | tuple[str, ...], *, limit: int = 200, chars: int = 16000) -> list[str]:
    """Keep engine diagnostics useful without allowing a log storm."""
    selected = [str(line).replace("\x00", "")[:2000] for line in lines if str(line).strip()]
    selected = selected[-max(1, int(limit)):]
    total = 0
    result: list[str] = []
    for line in reversed(selected):
        if total + len(line) > max(1000, int(chars)) and result:
            break
        result.append(line)
        total += len(line)
    return list(reversed(result))


def _redact_runner_diagnostics(
    lines: tuple[str, ...] | list[str], request: LocalRunRequest,
) -> list[str]:
    """Remove Agent-local physical paths before diagnostics reach Linux."""
    replacements: list[str] = []
    values = [
        request.input_mf4,
        request.output_mf4,
        request.executable,
        request.runtime_xml,
        request.adapter_file,
        request.mat_filter,
        request.working_directory,
    ]
    controlled_work = str((request.config.get("_local_run") or {}).get("controlled_work_directory") or "").strip()
    if controlled_work:
        values.append(Path(controlled_work))
    for value in values:
        text = str(value or "").strip()
        if text:
            replacements.extend((text, text.replace("\\", "/"), text.replace("/", "\\")))
    sanitized: list[str] = []
    drive_path = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\s\"'<>]+")
    for raw in lines or ():
        line = str(raw).replace("\x00", "")
        for value in sorted(set(replacements), key=len, reverse=True):
            line = line.replace(value, "<local-path>")
        line = drive_path.sub("<local-path>", line)
        if line.strip():
            sanitized.append(line[:2000])
    return _bounded_lines(sanitized)


def _private_config(
    base: dict[str, Any], *, project: str, manifest: RuntimeBundleManifest,
    executable: Path, runtime_xml: Path, adapter: Path | None, mat_filter: Path,
    run_root: Path,
) -> dict[str, Any]:
    config = copy.deepcopy(base)
    config.setdefault("_meta", {})["project"] = project
    config.setdefault("project", {})["name"] = project
    config.setdefault("paths", {})["build_output"] = str(executable.parent)
    config.setdefault("selena", {})["exe_pattern"] = "{executable_name}"
    config["selena"]["executable_name"] = executable.name
    config.setdefault("build", {})["selena_branch"] = manifest.source.branch
    simulation = config.setdefault("simulation", {})
    simulation["runtime_xml"] = str(runtime_xml)
    simulation["adapter_file"] = str(adapter) if adapter is not None else ""
    simulation["matfilefilter"] = str(mat_filter)
    # Private-only execution metadata.  This object never leaves Agent storage.
    config["_local_run"] = {
        "executable": str(executable),
        "working_directory": str(executable.parent),
        "controlled_work_directory": str(run_root / "work"),
    }
    return config


def _verify_runtime_locations(
    manifest: RuntimeBundleManifest,
    locations: Mapping[str, str | Path],
) -> dict[str, Path]:
    expected = {item.relative_path: item for item in manifest.files}
    if set(locations) != set(expected):
        raise AgentLocalRunError("Runtime Bundle extracted file set is invalid")
    by_role: dict[str, Path] = {}
    entrypoint_parent: Path | None = None
    for logical, evidence in expected.items():
        path = Path(locations[logical])
        checksum = _sha256_regular_file(path)
        if path.stat().st_size != evidence.size or checksum != evidence.checksum:
            raise AgentLocalRunError("Runtime Bundle extracted content changed")
        if evidence.role in {"entrypoint", "runtime_config"}:
            by_role[evidence.role] = path
        if evidence.role == "entrypoint":
            entrypoint_parent = path.parent
    if set(by_role) != {"entrypoint", "runtime_config"} or entrypoint_parent is None:
        raise AgentLocalRunError("Runtime Bundle required roles are unavailable")
    for logical, evidence in expected.items():
        if evidence.role == "runtime_library" and Path(locations[logical]).parent != entrypoint_parent:
            raise AgentLocalRunError("Runtime Bundle library is not colocated with Selena")
    return by_role


def _verify_data_lease(
    lease: AgentDataLease, *, verify_checksums: bool = True
) -> list[dict[str, Any]]:
    root = lease.source_path if lease.source_path.is_dir() else lease.source_path.parent
    result: list[dict[str, Any]] = []
    for ref in lease.files:
        path = lease.source_path if lease.source_path.is_file() else root.joinpath(*PurePosixPath(ref.relative_path).parts)
        stat_result = path.stat()
        if stat_result.st_size != ref.size or (ref.mtime_ns and stat_result.st_mtime_ns != ref.mtime_ns):
            raise AgentLocalRunError("leased data file changed after discovery")
        checksum = str(ref.checksum or "")
        if verify_checksums:
            checksum = _sha256_regular_file(path)
            if ref.checksum and checksum != ref.checksum:
                raise AgentLocalRunError("leased data file changed after discovery")
        result.append(
            {
                "relative_path": ref.relative_path,
                "path": path,
                "size": int(ref.size),
                "mtime_ns": int(ref.mtime_ns),
                "checksum": checksum,
            }
        )
    if not result:
        raise AgentLocalRunError("data lease contains no simulation input")
    return result


def _verify_stored_input(item: Mapping[str, Any]) -> None:
    """Verify one private lease input immediately before execution."""
    path = Path(str(item.get("path") or ""))
    try:
        stat_result = path.stat()
    except OSError as exc:
        raise AgentLocalRunError("leased data file changed after preflight") from exc
    expected_size = int(item.get("size") or 0)
    expected_mtime = int(item.get("mtime_ns") or 0)
    if stat_result.st_size != expected_size or (
        expected_mtime and stat_result.st_mtime_ns != expected_mtime
    ):
        raise AgentLocalRunError("leased data file changed after preflight")
    expected_checksum = str(item.get("checksum") or "")
    if expected_checksum.startswith("sha256:"):
        if _sha256_regular_file(path) != expected_checksum:
            raise AgentLocalRunError("leased data file changed after preflight")


def _checkpoint_output_is_valid(run_root: str | Path, item: Mapping[str, Any]) -> bool:
    """Check one persisted output before treating it as completed work."""
    try:
        relative = _safe_output_relative(str(item.get("relative_path") or ""))
        path = _safe_child(Path(run_root), *PurePosixPath(relative).parts)
        checksum = _sha256_regular_file(path)
        expected_checksum = str(item.get("checksum") or "")
        if expected_checksum and checksum != expected_checksum:
            return False
        expected_size = item.get("size")
        return expected_size in (None, "") or int(expected_size) == path.stat().st_size
    except (AgentLocalRunError, OSError, TypeError, ValueError):
        return False


def _controlled_runs_root(value: str | Path | None) -> Path:
    if value is None:
        home_text = str(os.environ.get("RSIM_HOME") or "").strip()
        home = Path(home_text).expanduser() if home_text else Path.home() / ".rsim"
        root = home / "agent" / "runs"
    else:
        root = Path(value).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink() or root == Path(root.anchor):
        raise AgentLocalRunError("Agent local runs directory is invalid")
    return root


def _existing_controlled_run_root(root: Path, value: Path) -> Path:
    try:
        resolved = value.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AgentLocalRunError("local run storage is unavailable") from exc
    if not resolved.is_dir() or resolved.is_symlink():
        raise AgentLocalRunError("local run storage is unavailable")
    return resolved


def _safe_child(root: Path, *parts: str) -> Path:
    target = root.joinpath(*parts).resolve(strict=False)
    try:
        target.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise AgentLocalRunError("local run path contract is invalid") from exc
    return target


def _safe_output_relative(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    posix = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if (
        not text or posix.is_absolute() or windows.is_absolute() or windows.drive
        or any(part in {"", ".", ".."} for part in posix.parts)
        or not text.startswith("outputs/") or posix.suffix.casefold() != ".mf4"
    ):
        raise AgentLocalRunError("local run output contract is invalid")
    return posix.as_posix()


def _output_relative_path(index: int, source_relative: str, checksum: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", PurePosixPath(source_relative).stem).strip(".-") or "input"
    digest = checksum.removeprefix("sha256:")[:12]
    return f"outputs/{index:04d}-{stem}-{digest}-out.MF4"


def _sha256_regular_file(path: str | Path) -> str:
    value = Path(path)
    try:
        initial = value.lstat()
    except OSError as exc:
        raise AgentLocalRunError("local run file is unavailable") from exc
    if not stat.S_ISREG(initial.st_mode) or stat.S_ISLNK(initial.st_mode) or value.is_symlink():
        raise AgentLocalRunError("local run file type is invalid")
    if int(getattr(initial, "st_nlink", 1) or 1) != 1:
        raise AgentLocalRunError("local run file link count is invalid")
    digest = hashlib.sha256()
    with value.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    final = value.stat()
    if final.st_size != initial.st_size or final.st_mtime_ns != initial.st_mtime_ns:
        raise AgentLocalRunError("local run file changed during validation")
    return "sha256:" + digest.hexdigest()


def _positive_timeout(value: int) -> int:
    if isinstance(value, bool):
        raise AgentLocalRunError("local run timeout is invalid")
    timeout = int(value)
    # Zero is intentional: it means no framework wall-clock limit.  Positive
    # values remain opt-in and bounded so an accidental YAML value cannot make
    # a Connector lease unbounded by surprise.
    if timeout < 0 or timeout > 7 * 24 * 60 * 60:
        raise AgentLocalRunError("local run timeout is invalid")
    return timeout


def _required_token(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 256 or any(ord(char) < 32 for char in text):
        raise AgentLocalRunError(f"{label} is invalid")
    return text


def _safe_error_code(value: str) -> str:
    text = str(value or "").strip()
    if text and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", text):
        raise AgentLocalRunError("local run error code is invalid")
    return text


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "AgentLocalRunError",
    "AgentLocalRunLeaseStore",
    "LocalRunOutcome",
    "LocalRunRequest",
    "LocalRunnerUnavailable",
    "LocalSimulationRunner",
    "execute_local_run",
]
