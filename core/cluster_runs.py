"""Private Cluster run/result lease store for the v1 Stage pipeline.

Public Stage results carry only ``cluster-run:*`` and ``result:sha256:*``
references.  Shared workspace paths, generated Config.cfg paths and result
mounts are kept in this server-side SQLite store and resolved only by trusted
Linux/Gateway executors.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable

from core.user import normalize_user


class ClusterRunStoreError(ValueError):
    """Stable logical-ref, ownership, state, or lease error."""


_RUN_REF_RE = re.compile(r"^cluster-run:[0-9a-f]{32}$")
_RESULT_REF_RE = re.compile(r"^result:sha256:[0-9a-f]{64}$")
_DATASET_ID_RE = re.compile(r"^dataset:sha256:[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,255}$")
_RUN_STATES = {"prepared", "submitted", "running", "succeeded", "failed", "cancelled"}
_TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


@dataclass(frozen=True)
class ClusterRunRef:
    ref: str
    control_job_id: str
    project: str
    dataset_id: str
    artifact_id: str
    profile: str
    state: str
    external_job_id: str
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClusterRunLease:
    public: ClusterRunRef
    owner: str
    artifact_storage_ref: str
    job_dir: str
    config_path: str
    output_location: str


@dataclass(frozen=True)
class ClusterResultRef:
    ref: str
    run_ref: str
    project: str
    state: str
    files: tuple[str, ...]
    summary: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "run_ref": self.run_ref,
            "project": self.project,
            "state": self.state,
            "files": list(self.files),
            "summary": dict(self.summary),
            "created_at": self.created_at,
        }


class ClusterRunStore:
    def __init__(self, db_path: str | Path, *, now_fn: Callable[[], float] = time.time) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cluster_runs (
                    run_ref TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    control_job_id TEXT NOT NULL,
                    project TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    artifact_storage_ref TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    state TEXT NOT NULL,
                    external_job_id TEXT NOT NULL DEFAULT '',
                    submit_mode TEXT NOT NULL DEFAULT '',
                    job_dir TEXT NOT NULL,
                    config_path TEXT NOT NULL,
                    output_location TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(owner, control_job_id)
                );
                CREATE TABLE IF NOT EXISTS cluster_results (
                    result_ref TEXT PRIMARY KEY,
                    run_ref TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL,
                    project TEXT NOT NULL,
                    state TEXT NOT NULL,
                    files_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    physical_root TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(run_ref) REFERENCES cluster_runs(run_ref)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def create_run(
        self,
        *,
        owner: str,
        control_job_id: str,
        project: str,
        dataset_id: str,
        artifact_id: str,
        artifact_storage_ref: str,
        profile: str,
        job_dir: str,
        config_path: str,
        output_location: str,
    ) -> ClusterRunRef:
        owner = normalize_user(owner)
        control_job_id = str(control_job_id or "").strip()
        project = str(project or "").strip()
        dataset_id = str(dataset_id or "").strip().lower()
        artifact_id = str(artifact_id or "").strip()
        artifact_storage_ref = str(artifact_storage_ref or "").strip()
        if not owner or not control_job_id or not project:
            raise ClusterRunStoreError("cluster run identity is incomplete")
        if not _DATASET_ID_RE.fullmatch(dataset_id):
            raise ClusterRunStoreError("cluster run dataset reference is invalid")
        if not _ARTIFACT_ID_RE.fullmatch(artifact_id) or not artifact_storage_ref.startswith(
            ("shared://selena/", "shared://selena-bundles/")
        ):
            raise ClusterRunStoreError("cluster run artifact reference is invalid")
        private_paths = [str(job_dir or "").strip(), str(config_path or "").strip(), str(output_location or "").strip()]
        if any(not item for item in private_paths):
            raise ClusterRunStoreError("cluster run private lease is incomplete")
        now = float(self._now_fn())
        run_ref = "cluster-run:" + uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM cluster_runs WHERE owner=? AND control_job_id=?",
                (owner, control_job_id),
            ).fetchone()
            if existing is not None:
                expected = (project, dataset_id, artifact_id, artifact_storage_ref, str(profile or "default"))
                actual = (
                    existing["project"], existing["dataset_id"], existing["artifact_id"],
                    existing["artifact_storage_ref"], existing["profile"],
                )
                if actual != expected:
                    raise ClusterRunStoreError("cluster run already exists with different resolved inputs")
                return self._public(existing)
            conn.execute(
                """
                INSERT INTO cluster_runs(
                    run_ref, owner, control_job_id, project, dataset_id, artifact_id,
                    artifact_storage_ref, profile, state, job_dir, config_path,
                    output_location, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?)
                """,
                (
                    run_ref, owner, control_job_id, project, dataset_id, artifact_id,
                    artifact_storage_ref, str(profile or "default"), private_paths[0],
                    private_paths[1], private_paths[2], now, now,
                ),
            )
            row = conn.execute("SELECT * FROM cluster_runs WHERE run_ref=?", (run_ref,)).fetchone()
            return self._public(row)

    def get(self, run_ref: str, *, owner: str) -> ClusterRunRef:
        return self._public(self._run_row(run_ref, owner=owner))

    def resolve_private(self, run_ref: str, *, owner: str) -> ClusterRunLease:
        row = self._run_row(run_ref, owner=owner)
        return ClusterRunLease(
            public=self._public(row),
            owner=str(row["owner"]),
            artifact_storage_ref=str(row["artifact_storage_ref"]),
            job_dir=str(row["job_dir"]),
            config_path=str(row["config_path"]),
            output_location=str(row["output_location"]),
        )

    def mark_submitted(
        self,
        run_ref: str,
        *,
        owner: str,
        external_job_id: str,
        submit_mode: str,
    ) -> ClusterRunRef:
        external_job_id = str(external_job_id or "").strip()
        if not external_job_id:
            raise ClusterRunStoreError("cluster submission did not return an external job id")
        return self._update_state(
            run_ref,
            owner=owner,
            state="submitted",
            external_job_id=external_job_id,
            submit_mode=str(submit_mode or "").strip(),
        )

    def update_state(self, run_ref: str, *, owner: str, state: str) -> ClusterRunRef:
        return self._update_state(run_ref, owner=owner, state=state)

    def finalize_result(
        self,
        run_ref: str,
        *,
        owner: str,
        state: str,
        files: Iterable[str],
        summary: dict[str, Any],
        physical_root: str,
    ) -> ClusterResultRef:
        state = str(state or "").strip().lower()
        if state not in _TERMINAL_STATES:
            raise ClusterRunStoreError("cluster result state is not terminal")
        run = self._run_row(run_ref, owner=owner)
        normalized_files = tuple(sorted({_relative_result_path(item) for item in files}, key=str.casefold))
        if not normalized_files and state == "succeeded":
            raise ClusterRunStoreError("successful cluster result has no files")
        root = str(physical_root or "").strip()
        if not root:
            raise ClusterRunStoreError("cluster result private root is missing")
        public_summary = _path_free_summary(summary)
        created = float(self._now_fn())
        digest_payload = json.dumps(
            {"run_ref": run_ref, "state": state, "files": normalized_files, "summary": public_summary},
            sort_keys=True,
            separators=(",", ":"),
        )
        result_ref = "result:sha256:" + hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
        with self._lock, self._connect() as conn:
            existing = conn.execute("SELECT * FROM cluster_results WHERE run_ref=?", (run_ref,)).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO cluster_results(
                        result_ref, run_ref, owner, project, state, files_json,
                        summary_json, physical_root, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result_ref, run_ref, normalize_user(owner), run["project"], state,
                        json.dumps(normalized_files), json.dumps(public_summary, sort_keys=True), root, created,
                    ),
                )
                existing = conn.execute("SELECT * FROM cluster_results WHERE result_ref=?", (result_ref,)).fetchone()
            elif str(existing["result_ref"]) != result_ref:
                raise ClusterRunStoreError("cluster result already finalized with different content")
        self._update_state(run_ref, owner=owner, state=state)
        return self._result_public(existing)

    def get_result(self, result_ref: str, *, owner: str) -> ClusterResultRef:
        return self._result_public(self._result_row(result_ref, owner=owner))

    def resolve_result_location(self, result_ref: str, *, owner: str) -> Path:
        row = self._result_row(result_ref, owner=owner)
        return Path(str(row["physical_root"]))

    def _update_state(
        self,
        run_ref: str,
        *,
        owner: str,
        state: str,
        external_job_id: str | None = None,
        submit_mode: str | None = None,
    ) -> ClusterRunRef:
        state = str(state or "").strip().lower()
        if state not in _RUN_STATES:
            raise ClusterRunStoreError("cluster run state is invalid")
        row = self._run_row(run_ref, owner=owner)
        current = str(row["state"])
        if current in _TERMINAL_STATES and current != state:
            raise ClusterRunStoreError("terminal cluster run state is immutable")
        now = float(self._now_fn())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE cluster_runs
                SET state=?, external_job_id=COALESCE(?, external_job_id),
                    submit_mode=COALESCE(?, submit_mode), updated_at=?
                WHERE run_ref=? AND owner=?
                """,
                (state, external_job_id, submit_mode, now, run_ref, normalize_user(owner)),
            )
            updated = conn.execute("SELECT * FROM cluster_runs WHERE run_ref=?", (run_ref,)).fetchone()
        return self._public(updated)

    def _run_row(self, run_ref: str, *, owner: str) -> sqlite3.Row:
        if not _RUN_REF_RE.fullmatch(str(run_ref or "")):
            raise ClusterRunStoreError("cluster run reference is invalid")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cluster_runs WHERE run_ref=? AND owner=?",
                (run_ref, normalize_user(owner)),
            ).fetchone()
        if row is None:
            raise ClusterRunStoreError("cluster run is unavailable")
        return row

    def _result_row(self, result_ref: str, *, owner: str) -> sqlite3.Row:
        if not _RESULT_REF_RE.fullmatch(str(result_ref or "")):
            raise ClusterRunStoreError("cluster result reference is invalid")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cluster_results WHERE result_ref=? AND owner=?",
                (result_ref, normalize_user(owner)),
            ).fetchone()
        if row is None:
            raise ClusterRunStoreError("cluster result is unavailable")
        return row

    @staticmethod
    def _public(row: sqlite3.Row) -> ClusterRunRef:
        return ClusterRunRef(
            ref=str(row["run_ref"]),
            control_job_id=str(row["control_job_id"]),
            project=str(row["project"]),
            dataset_id=str(row["dataset_id"]),
            artifact_id=str(row["artifact_id"]),
            profile=str(row["profile"]),
            state=str(row["state"]),
            external_job_id=str(row["external_job_id"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _result_public(row: sqlite3.Row) -> ClusterResultRef:
        return ClusterResultRef(
            ref=str(row["result_ref"]),
            run_ref=str(row["run_ref"]),
            project=str(row["project"]),
            state=str(row["state"]),
            files=tuple(json.loads(row["files_json"])),
            summary=dict(json.loads(row["summary_json"])),
            created_at=float(row["created_at"]),
        )


def _relative_result_path(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    posix = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if not text or posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in posix.parts:
        raise ClusterRunStoreError("cluster result file path must be relative")
    return posix.as_posix()


def _path_free_summary(value: dict[str, Any]) -> dict[str, Any]:
    """Reject path/secret-shaped keys before a summary can reach Web/API."""
    summary = dict(value or {})
    forbidden = {"path", "dir", "directory", "location", "command", "password", "secret", "token"}
    for key in summary:
        lowered = str(key).lower()
        if any(part in lowered for part in forbidden):
            raise ClusterRunStoreError("cluster result summary contains private fields")
    json.dumps(summary, sort_keys=True)
    return summary


__all__ = [
    "ClusterResultRef",
    "ClusterRunLease",
    "ClusterRunRef",
    "ClusterRunStore",
    "ClusterRunStoreError",
]
