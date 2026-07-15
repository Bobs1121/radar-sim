"""Trusted application boundary for Selena upload, publication, and catalog registration.

Clients identify a completed build attempt and choose only a project-relative
publish path.  Size, checksum, source state, visibility, ownership, and build
metadata come from the persisted build attempt rather than the HTTP request.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping

from core.artifact_store import (
    ArtifactChecksumError,
    ArtifactConflictError,
    ArtifactPathError,
    ArtifactSessionError,
    ArtifactStore,
    ArtifactStoreError,
    UploadSession,
)
from core.artifacts import ArtifactCatalog, ArtifactError, SelenaArtifact
from core.control_service import ControlService, INTERNAL_V1_SCHEDULER_AGENT_ID
from core.user import normalize_user


class ArtifactUploadServiceError(RuntimeError):
    """Stable application error for API/SDK adapters."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.status_code = int(status_code)


@dataclass(frozen=True)
class TrustedBuildEvidence:
    evidence_ref: str
    owner: str
    project: str
    build_mode: str
    source_kind: str
    created_by: str
    created_at: float
    retain_until: float
    branch: str
    commit: str
    dirty: bool
    dirty_fingerprint: str
    source_changed_during_build: bool
    checksum: str
    size: int
    logical_path: str
    toolchain_fingerprint: str = ""
    interface_manifest: Mapping[str, Any] | None = None
    signal_manifest: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", self.evidence_ref):
            raise ArtifactUploadServiceError("invalid_build_evidence", "Build evidence reference is invalid", status_code=409)
        owner = normalize_user(self.owner)
        for value, label in (
            (self.project, "project"),
            (self.build_mode, "build mode"),
            (self.source_kind, "source kind"),
            (self.created_by, "builder"),
        ):
            if not str(value or "").strip():
                raise ArtifactUploadServiceError("invalid_build_evidence", f"Trusted {label} is missing", status_code=409)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(self.commit or "")):
            raise ArtifactUploadServiceError("invalid_build_evidence", "Trusted commit is invalid", status_code=409)
        if not re.fullmatch(r"[0-9a-f]{64}", str(self.dirty_fingerprint or "").lower()):
            raise ArtifactUploadServiceError("invalid_build_evidence", "Trusted source fingerprint is invalid", status_code=409)
        checksum = str(self.checksum or "").strip().lower()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum):
            raise ArtifactUploadServiceError("invalid_build_evidence", "Trusted artifact checksum is invalid", status_code=409)
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size <= 0:
            raise ArtifactUploadServiceError("invalid_build_evidence", "Trusted artifact size is invalid", status_code=409)
        logical = str(self.logical_path or "").strip()
        posix = PurePosixPath(logical)
        windows = PureWindowsPath(logical)
        if (
            not logical
            or "\\" in logical
            or posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or any(part in {"", ".", ".."} for part in posix.parts)
            or posix.name.lower() != "selena.exe"
        ):
            raise ArtifactUploadServiceError("invalid_build_evidence", "Trusted artifact is not selena.exe", status_code=409)
        if not isinstance(self.dirty, bool) or not isinstance(self.source_changed_during_build, bool):
            raise ArtifactUploadServiceError("invalid_build_evidence", "Trusted source state is invalid", status_code=409)
        created = float(self.created_at)
        retain = float(self.retain_until)
        if created <= 0 or retain < 0:
            raise ArtifactUploadServiceError("invalid_build_evidence", "Trusted timestamps are invalid", status_code=409)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "checksum", checksum)
        object.__setattr__(self, "dirty_fingerprint", self.dirty_fingerprint.lower())
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "retain_until", retain)


TrustedBuildEvidenceProvider = Callable[[str, str], TrustedBuildEvidence]


class ArtifactUploadService:
    """Coordinates resumable content storage with immutable artifact metadata."""

    def __init__(
        self,
        store: ArtifactStore,
        catalog: ArtifactCatalog,
        evidence_provider: TrustedBuildEvidenceProvider,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._evidence_provider = evidence_provider

    def create(self, owner: str, *, evidence_ref: str, publish_path: str = "") -> dict[str, Any]:
        owner = normalize_user(owner)
        evidence = self._evidence(owner, evidence_ref)
        path = str(publish_path or "").strip() or _generated_publish_path(evidence)
        try:
            session = self._store.create_upload_session(
                owner,
                evidence.project,
                path,
                evidence.size,
                evidence.checksum,
                evidence_ref=evidence.evidence_ref,
            )
        except ArtifactPathError as exc:
            raise ArtifactUploadServiceError("invalid_publish_path", str(exc), status_code=422) from exc
        except ArtifactStoreError as exc:
            raise ArtifactUploadServiceError("artifact_upload_invalid", str(exc), status_code=409) from exc
        return _session_dict(session)

    def get(self, owner: str, session_id: str) -> dict[str, Any]:
        try:
            return _session_dict(self._store.get_session(session_id, owner=normalize_user(owner)))
        except ArtifactSessionError as exc:
            raise ArtifactUploadServiceError("artifact_upload_not_found", "Artifact upload session is unavailable", status_code=404) from exc

    def append(self, owner: str, session_id: str, *, offset: int, data: bytes) -> dict[str, Any]:
        try:
            session = self._store.append_chunk(session_id, offset, data, owner=normalize_user(owner))
        except ArtifactSessionError as exc:
            raise ArtifactUploadServiceError("artifact_upload_offset_conflict", str(exc), status_code=409) from exc
        return _session_dict(session)

    def finalize(self, owner: str, session_id: str) -> dict[str, Any]:
        owner = normalize_user(owner)
        try:
            session = self._store.get_session(session_id, owner=owner)
        except ArtifactSessionError as exc:
            raise ArtifactUploadServiceError("artifact_upload_not_found", "Artifact upload session is unavailable", status_code=404) from exc
        evidence = self._evidence(owner, session.evidence_ref)
        if (
            session.project != evidence.project
            or session.expected_size != evidence.size
            or session.expected_checksum != evidence.checksum
        ):
            raise ArtifactUploadServiceError("build_evidence_mismatch", "Upload session no longer matches build evidence", status_code=409)
        try:
            published = self._store.finalize_upload(session_id, owner=owner)
        except ArtifactConflictError as exc:
            raise ArtifactUploadServiceError("artifact_path_conflict", str(exc), status_code=409) from exc
        except ArtifactChecksumError as exc:
            raise ArtifactUploadServiceError("artifact_upload_mismatch", str(exc), status_code=409) from exc
        except ArtifactStoreError as exc:
            raise ArtifactUploadServiceError("artifact_upload_invalid", str(exc), status_code=409) from exc

        artifact = SelenaArtifact(
            id="",
            project=evidence.project,
            owner=owner,
            visibility="shared",
            branch=evidence.branch,
            commit=evidence.commit,
            source_kind=evidence.source_kind,
            dirty=evidence.dirty,
            dirty_fingerprint=evidence.dirty_fingerprint,
            source_changed_during_build=evidence.source_changed_during_build,
            build_mode=evidence.build_mode,
            toolchain_fingerprint=evidence.toolchain_fingerprint,
            binary_checksum=evidence.checksum,
            interface_manifest=dict(evidence.interface_manifest or {}),
            signal_manifest=dict(evidence.signal_manifest or {}),
            storage_ref=str(published["storage_ref"]),
            accessibility="shared",
            health="ready",
            created_by=evidence.created_by,
            created_at=evidence.created_at,
            retain_until=evidence.retain_until,
        )
        try:
            registered = self._catalog.register(artifact)
        except ArtifactError as exc:
            raise ArtifactUploadServiceError("artifact_catalog_conflict", str(exc), status_code=409) from exc
        return {
            "session": _session_dict(self._store.get_session(session_id, owner=owner)),
            "artifact": registered.to_dict(),
            "reused": bool(published.get("reused", False)),
        }

    def _evidence(self, owner: str, evidence_ref: str) -> TrustedBuildEvidence:
        evidence_ref = str(evidence_ref or "").strip()
        try:
            evidence = self._evidence_provider(owner, str(evidence_ref or "").strip())
        except ArtifactUploadServiceError:
            raise
        except Exception as exc:
            raise ArtifactUploadServiceError("build_evidence_unavailable", "Trusted build evidence is unavailable", status_code=409) from exc
        if not isinstance(evidence, TrustedBuildEvidence) or evidence.owner != owner or evidence.evidence_ref != evidence_ref:
            raise ArtifactUploadServiceError("build_evidence_mismatch", "Build evidence does not belong to this request", status_code=409)
        return evidence


def trusted_build_evidence_from_control(
    control: ControlService,
    owner: str,
    evidence_ref: str,
) -> TrustedBuildEvidence:
    """Resolve one immutable succeeded build attempt from central persistence."""
    owner = normalize_user(owner)
    match = re.fullmatch(r"(.+):(\d+)", str(evidence_ref or "").strip())
    if match is None:
        raise ArtifactUploadServiceError("invalid_build_evidence", "Build evidence reference is invalid", status_code=409)
    stage_id, attempt_number = match.group(1), int(match.group(2))
    try:
        task = control.get_task(stage_id)
        job = control.get_job(task["job_id"])
    except KeyError as exc:
        raise ArtifactUploadServiceError("build_evidence_unavailable", "Build evidence is unavailable", status_code=409) from exc
    if normalize_user(job.get("owner") or "") != owner or task.get("stage_type") != "build_selena":
        raise ArtifactUploadServiceError("build_evidence_mismatch", "Build evidence does not belong to this request", status_code=409)
    attempts = [item for item in control.list_attempts(stage_id) if int(item.get("attempt") or 0) == attempt_number]
    if len(attempts) != 1 or attempts[0].get("status") != "succeeded":
        raise ArtifactUploadServiceError("build_evidence_unavailable", "Build attempt has not succeeded", status_code=409)
    attempt = attempts[0]
    agent_id = str(attempt.get("agent_id") or "").strip()
    if not agent_id or agent_id == INTERNAL_V1_SCHEDULER_AGENT_ID:
        raise ArtifactUploadServiceError("build_evidence_untrusted", "Build attempt has no trusted Windows executor", status_code=409)
    agent = next((item for item in control.list_agents() if item.get("agent_id") == agent_id), None)
    node_kind = (agent or {}).get("metadata", {}).get("node_kind") if isinstance((agent or {}).get("metadata"), Mapping) else ""
    if agent is None or node_kind not in {"windows_agent", "windows_full"} or "build.selena" not in agent.get("capabilities", []):
        raise ArtifactUploadServiceError("build_evidence_untrusted", "Build attempt executor is not an authorized Windows node", status_code=409)
    result = attempt.get("result")
    if not isinstance(result, Mapping):
        raise ArtifactUploadServiceError("invalid_build_evidence", "Build result is invalid", status_code=409)
    before = _snapshot(result.get("before"), "before")
    after = _snapshot(result.get("after"), "after")
    binary = result.get("artifact")
    if not isinstance(binary, Mapping):
        raise ArtifactUploadServiceError("invalid_build_evidence", "Build artifact evidence is missing", status_code=409)
    changed = before["sha256"] != after["sha256"] or before["commit"] != after["commit"]
    if result.get("source_changed_during_build") is not changed:
        raise ArtifactUploadServiceError("invalid_build_evidence", "Build source-change evidence is inconsistent", status_code=409)
    spec = job.get("spec") or (job.get("payload") or {}).get("spec") or {}
    selena = spec.get("selena") if isinstance(spec, Mapping) else {}
    result_spec = spec.get("result") if isinstance(spec, Mapping) else {}
    created_at = float(attempt.get("finished_at") or task.get("completed_at") or time.time())
    retain_days = int((result_spec or {}).get("retain_days") or 30) if isinstance(result_spec, Mapping) else 30
    project = str(result.get("project") or "")
    spec_project = str((spec or {}).get("project") or "") if isinstance(spec, Mapping) else ""
    build_mode = str(result.get("build_mode") or "")
    spec_build_mode = str((selena or {}).get("build_mode") or "") if isinstance(selena, Mapping) else ""
    if not project or project != spec_project or not build_mode or build_mode != spec_build_mode:
        raise ArtifactUploadServiceError("invalid_build_evidence", "Build result does not match the submitted SimulationSpec", status_code=409)
    return TrustedBuildEvidence(
        evidence_ref=evidence_ref,
        owner=owner,
        project=project,
        build_mode=build_mode,
        source_kind=str((selena or {}).get("mode") or "current_workspace"),
        created_by=agent_id,
        created_at=created_at,
        retain_until=created_at + retain_days * 86400,
        branch=before["branch"],
        commit=before["commit"],
        dirty=before["dirty"],
        dirty_fingerprint=before["sha256"],
        source_changed_during_build=changed,
        checksum=str(binary.get("checksum") or ""),
        size=binary.get("size"),
        logical_path=str(binary.get("logical_path") or ""),
        toolchain_fingerprint=str(result.get("toolchain_fingerprint") or ""),
    )


def _snapshot(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ArtifactUploadServiceError("invalid_build_evidence", f"Build {label} snapshot is missing", status_code=409)
    branch = str(value.get("branch") or "")
    commit = str(value.get("commit") or "")
    sha256 = str(value.get("sha256") or "").lower()
    dirty = value.get("dirty")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit) or not re.fullmatch(r"[0-9a-f]{64}", sha256) or not isinstance(dirty, bool):
        raise ArtifactUploadServiceError("invalid_build_evidence", f"Build {label} snapshot is invalid", status_code=409)
    return {"branch": branch, "commit": commit, "sha256": sha256, "dirty": dirty}


def _generated_publish_path(evidence: TrustedBuildEvidence) -> str:
    branch = re.sub(r"[^A-Za-z0-9._-]+", "-", evidence.branch).strip("-.") or "detached"
    suffix = hashlib.sha256(evidence.evidence_ref.encode("utf-8")).hexdigest()[:12]
    return f"builds/{branch}/{evidence.commit[:12]}-{suffix}"


def _session_dict(session: UploadSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "status": session.status,
        "project": session.project,
        "publish_path": session.logical_path,
        "storage_ref": session.storage_ref,
        "build_evidence_ref": session.evidence_ref,
        "expected_size": session.expected_size,
        "expected_checksum": session.expected_checksum,
        "received_bytes": session.received_bytes,
        "chunk_size": session.chunk_size,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "expires_at": session.expires_at,
    }


__all__ = [
    "ArtifactUploadService",
    "ArtifactUploadServiceError",
    "TrustedBuildEvidence",
    "TrustedBuildEvidenceProvider",
    "trusted_build_evidence_from_control",
]
