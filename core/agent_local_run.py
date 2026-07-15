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

    def __post_init__(self) -> None:
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise AgentLocalRunError("local runner returned an invalid exit code")
        code = str(self.error_code or "").strip()
        if code and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise AgentLocalRunError("local runner returned an invalid error code")
        object.__setattr__(self, "error_code", code)


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
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

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
    ) -> dict[str, Any]:
        """Authorize immutable inputs, construct private config and create a lease."""
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
        inputs = _verify_data_lease(data_lease)
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
            "outputs": json.loads(row["outputs_json"]),
            "error_count": int(row["error_count"]),
            "error_code": str(row["error_code"]),
        }

    def mark_running(self, lease_id: str) -> dict[str, Any]:
        row = self._row(lease_id)
        if str(row["status"]) not in {"ready", "running"}:
            raise AgentLocalRunError("local run cannot enter running state")
        return self._update(lease_id, status="running")

    def finish(
        self,
        lease_id: str,
        *,
        status: str,
        outputs: list[dict[str, Any]],
        error_count: int,
        error_code: str = "",
    ) -> dict[str, Any]:
        if status not in _TERMINAL:
            raise AgentLocalRunError("local run terminal status is invalid")
        return self._update(
            lease_id,
            status=status,
            outputs=outputs,
            error_count=max(0, int(error_count)),
            error_code=_safe_error_code(error_code),
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
        payload = {"lease_id": lease_id, "status": private["status"], "files": files, "summary": summary}
        return {
            "result_ref": "result:sha256:" + _json_digest(payload),
            "status": private["status"],
            "files": files,
            "summary": summary,
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
    ) -> dict[str, Any]:
        self._row(lease_id)
        now = float(self._now_fn())
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE agent_local_runs SET status=?,outputs_json=COALESCE(?,outputs_json),
                    error_count=COALESCE(?,error_count),error_code=COALESCE(?,error_code),updated_at=?
                WHERE lease_id=?
                """,
                (
                    status, _json(outputs) if outputs is not None else None,
                    error_count, error_code, now, lease_id,
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
    store.mark_running(lease_id)
    outputs: list[dict[str, Any]] = []
    failures = 0
    terminal_error = ""

    for index, item in enumerate(lease["inputs"], start=1):
        if cancel():
            store.finish(
                lease_id, status="cancelled", outputs=outputs,
                error_count=failures, error_code="cancelled",
            )
            return 130
        try:
            output_relative = _safe_output_relative(item["output_relative_path"])
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
                )
                return 130
            if outcome.exit_code == 0:
                _sha256_regular_file(output)
                outputs.append({"relative_path": output_relative})
            else:
                failures += 1
                terminal_error = outcome.error_code or "runner_failed"
        except LocalRunnerUnavailable:
            failures += 1
            terminal_error = "runner_unavailable"
            break
        except Exception:
            # Runner exceptions are untrusted implementation details and may
            # include local paths.  Persist only a stable public error code.
            failures += 1
            terminal_error = "runner_contract_failed"

    status = "succeeded" if failures == 0 and len(outputs) == len(lease["inputs"]) else "failed"
    store.finish(
        lease_id, status=status, outputs=outputs,
        error_count=failures, error_code=terminal_error,
    )
    return 0 if status == "succeeded" else 1


def _runner_unavailable(request: LocalRunRequest, cancel_requested: Callable[[], bool]) -> LocalRunOutcome:
    del request, cancel_requested
    raise LocalRunnerUnavailable("native local Selena runner is not connected")


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


def _verify_data_lease(lease: AgentDataLease) -> list[dict[str, Any]]:
    root = lease.source_path if lease.source_path.is_dir() else lease.source_path.parent
    result: list[dict[str, Any]] = []
    for ref in lease.files:
        path = lease.source_path if lease.source_path.is_file() else root.joinpath(*PurePosixPath(ref.relative_path).parts)
        checksum = _sha256_regular_file(path)
        stat_result = path.stat()
        if stat_result.st_size != ref.size or (ref.mtime_ns and stat_result.st_mtime_ns != ref.mtime_ns):
            raise AgentLocalRunError("leased data file changed after discovery")
        if ref.checksum and checksum != ref.checksum:
            raise AgentLocalRunError("leased data file changed after discovery")
        result.append({"relative_path": ref.relative_path, "path": path, "checksum": checksum})
    if not result:
        raise AgentLocalRunError("data lease contains no simulation input")
    return result


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
    if timeout <= 0 or timeout > 7 * 24 * 60 * 60:
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
