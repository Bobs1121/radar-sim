"""Trusted resumable upload boundary for Runtime Bundle archives."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
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
from core.control_service import ControlService, INTERNAL_V1_SCHEDULER_AGENT_ID
from core.runtime_bundle import RuntimeBundleManifest, RuntimeFile, RuntimeSourceEvidence
from core.runtime_bundle_catalog import RuntimeBundleCatalog, RuntimeBundleCatalogError, RuntimeBundleRecord
from core.runtime_bundle_archive import extract_runtime_bundle_archive
from core.user import normalize_user


class RuntimeBundleUploadServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class TrustedRuntimeBundleEvidence:
    evidence_ref: str
    owner: str
    project: str
    created_by: str
    manifest: RuntimeBundleManifest
    archive_checksum: str
    archive_size: int
    lease_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", normalize_user(self.owner))
        if not re.fullmatch(r".+:\d+", str(self.evidence_ref or "")):
            raise RuntimeBundleUploadServiceError("invalid_bundle_evidence", "Runtime Bundle evidence is invalid", status_code=409)
        if not str(self.project or "").strip() or not str(self.created_by or "").strip():
            raise RuntimeBundleUploadServiceError("invalid_bundle_evidence", "Runtime Bundle builder identity is missing", status_code=409)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(self.archive_checksum or "")):
            raise RuntimeBundleUploadServiceError("invalid_bundle_evidence", "Runtime Bundle archive checksum is invalid", status_code=409)
        if isinstance(self.archive_size, bool) or int(self.archive_size) <= 0:
            raise RuntimeBundleUploadServiceError("invalid_bundle_evidence", "Runtime Bundle archive size is invalid", status_code=409)
        if not str(self.lease_ref or "").startswith("runtime-bundle-lease:sha256:"):
            raise RuntimeBundleUploadServiceError("invalid_bundle_evidence", "Runtime Bundle lease is invalid", status_code=409)


TrustedRuntimeBundleEvidenceProvider = Callable[[str, str], TrustedRuntimeBundleEvidence]


class RuntimeBundleUploadService:
    def __init__(
        self,
        store: ArtifactStore,
        catalog: RuntimeBundleCatalog,
        evidence_provider: TrustedRuntimeBundleEvidenceProvider,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._evidence_provider = evidence_provider

    def create(self, owner: str, *, evidence_ref: str, publish_path: str = "") -> dict[str, Any]:
        evidence = self._evidence(normalize_user(owner), evidence_ref)
        path = str(publish_path or "").strip() or f"bundles/{evidence.manifest.id.rsplit(':', 1)[-1]}"
        try:
            session = self._store.create_upload_session(
                evidence.owner,
                evidence.project,
                path,
                evidence.archive_size,
                evidence.archive_checksum,
                evidence_ref=evidence.evidence_ref,
            )
        except ArtifactPathError as exc:
            raise RuntimeBundleUploadServiceError("invalid_publish_path", str(exc), status_code=422) from exc
        except ArtifactStoreError as exc:
            raise RuntimeBundleUploadServiceError("bundle_upload_invalid", str(exc), status_code=409) from exc
        return _session_dict(session)

    def list_bundles(self, owner: str) -> dict[str, Any]:
        normalize_user(owner)
        return {"items": [record.public_dict for record in self._catalog.list()]}

    def get_bundle(self, owner: str, bundle_id: str) -> dict[str, Any]:
        normalize_user(owner)
        try:
            return self._catalog.get(str(bundle_id or "")).public_dict
        except RuntimeBundleCatalogError as exc:
            raise RuntimeBundleUploadServiceError("runtime_bundle_not_found", "Runtime Bundle is unavailable", status_code=404) from exc

    def get(self, owner: str, session_id: str) -> dict[str, Any]:
        try:
            return _session_dict(self._store.get_session(session_id, owner=normalize_user(owner)))
        except ArtifactSessionError as exc:
            raise RuntimeBundleUploadServiceError("bundle_upload_not_found", "Runtime Bundle upload is unavailable", status_code=404) from exc

    def append(self, owner: str, session_id: str, *, offset: int, data: bytes) -> dict[str, Any]:
        try:
            session = self._store.append_chunk(session_id, offset, data, owner=normalize_user(owner))
        except ArtifactSessionError as exc:
            raise RuntimeBundleUploadServiceError("bundle_upload_offset_conflict", str(exc), status_code=409) from exc
        return _session_dict(session)

    def finalize(self, owner: str, session_id: str) -> dict[str, Any]:
        owner = normalize_user(owner)
        try:
            session = self._store.get_session(session_id, owner=owner)
        except ArtifactSessionError as exc:
            raise RuntimeBundleUploadServiceError("bundle_upload_not_found", "Runtime Bundle upload is unavailable", status_code=404) from exc
        evidence = self._evidence(owner, session.evidence_ref)
        if session.project != evidence.project or session.expected_size != evidence.archive_size or session.expected_checksum != evidence.archive_checksum:
            raise RuntimeBundleUploadServiceError("bundle_evidence_mismatch", "Upload no longer matches Runtime Bundle evidence", status_code=409)
        try:
            published = self._store.finalize_upload(session_id, owner=owner)
        except ArtifactConflictError as exc:
            raise RuntimeBundleUploadServiceError("bundle_path_conflict", str(exc), status_code=409) from exc
        except ArtifactChecksumError as exc:
            raise RuntimeBundleUploadServiceError("bundle_upload_mismatch", str(exc), status_code=409) from exc
        except ArtifactStoreError as exc:
            raise RuntimeBundleUploadServiceError("bundle_upload_invalid", str(exc), status_code=409) from exc
        record = RuntimeBundleRecord(
            manifest=evidence.manifest,
            internal_project=evidence.project,
            storage_ref=str(published["storage_ref"]),
            archive_checksum=evidence.archive_checksum,
            archive_size=evidence.archive_size,
            owner=owner,
            created_by=evidence.created_by,
        )
        try:
            registered = self._catalog.register(record)
        except RuntimeBundleCatalogError as exc:
            raise RuntimeBundleUploadServiceError("bundle_catalog_conflict", str(exc), status_code=409) from exc
        return {
            "session": _session_dict(self._store.get_session(session_id, owner=owner)),
            "runtime_bundle": registered.public_dict,
            "reused": bool(published.get("reused", False)),
        }

    def _evidence(self, owner: str, evidence_ref: str) -> TrustedRuntimeBundleEvidence:
        try:
            evidence = self._evidence_provider(owner, str(evidence_ref or "").strip())
        except RuntimeBundleUploadServiceError:
            raise
        except Exception as exc:
            raise RuntimeBundleUploadServiceError("bundle_evidence_unavailable", "Runtime Bundle evidence is unavailable", status_code=409) from exc
        if not isinstance(evidence, TrustedRuntimeBundleEvidence) or evidence.owner != owner or evidence.evidence_ref != evidence_ref:
            raise RuntimeBundleUploadServiceError("bundle_evidence_mismatch", "Runtime Bundle evidence does not belong to this request", status_code=409)
        return evidence

    def resolve_bundle(self, owner: str, bundle_id: str) -> RuntimeBundleRecord:
        normalize_user(owner)
        try:
            return self._catalog.get(str(bundle_id or ""))
        except RuntimeBundleCatalogError as exc:
            raise RuntimeBundleUploadServiceError("runtime_bundle_not_found", "Runtime Bundle is unavailable", status_code=404) from exc

    def resolve_archive(self, owner: str, bundle_id: str) -> tuple[RuntimeBundleRecord, Path]:
        """Resolve one shared immutable archive without exposing its location.

        Runtime Bundles are intentionally visible to all authenticated users.
        ``owner`` is still normalized so the HTTP boundary cannot be used
        anonymously, while the returned physical path stays inside the
        trusted adapter and is never serialized into a task/result payload.
        """
        record = self.resolve_bundle(owner, bundle_id)
        try:
            location = self._store.resolve_location(record.storage_ref)
        except ArtifactStoreError as exc:
            raise RuntimeBundleUploadServiceError(
                "runtime_bundle_archive_unavailable",
                "Runtime Bundle archive is unavailable",
                status_code=404,
            ) from exc
        try:
            stat = location.stat()
        except OSError as exc:
            raise RuntimeBundleUploadServiceError(
                "runtime_bundle_archive_unavailable",
                "Runtime Bundle archive is unavailable",
                status_code=404,
            ) from exc
        if (
            not location.is_file()
            or location.is_symlink()
            or int(stat.st_size) != int(record.archive_size)
        ):
            raise RuntimeBundleUploadServiceError(
                "runtime_bundle_archive_changed",
                "Runtime Bundle archive integrity check failed",
                status_code=409,
            )
        return record, location

    def import_existing(
        self,
        owner: str,
        *,
        metadata: Mapping[str, Any],
        archive_bytes: bytes,
    ) -> dict[str, Any]:
        """Import an SDK-prepared existing Selena archive without build fields."""
        owner = normalize_user(owner)
        project = str(metadata.get("internal_project") or "").strip()
        adapter_key = str(metadata.get("adapter_key") or "").strip()
        raw_manifest = metadata.get("manifest")
        checksum = str(metadata.get("archive_checksum") or "").strip().lower()
        expected_size = int(metadata.get("archive_size") or 0)
        if (
            not project
            or not adapter_key
            or not isinstance(raw_manifest, Mapping)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum)
            or expected_size <= 0
            or len(archive_bytes) != expected_size
            or "sha256:" + hashlib.sha256(archive_bytes).hexdigest() != checksum
        ):
            raise RuntimeBundleUploadServiceError(
                "invalid_existing_selena", "Existing Selena upload evidence is invalid", status_code=422
            )
        source_value = dict(raw_manifest.get("source") or {})
        source_value["adapter_key"] = adapter_key
        try:
            manifest = RuntimeBundleManifest(
                id=str(raw_manifest.get("id") or ""),
                files=tuple(RuntimeFile(**dict(item)) for item in raw_manifest.get("files") or []),
                source=RuntimeSourceEvidence(**source_value),
                created_at=float(raw_manifest.get("created_at") or 0),
            )
        except Exception as exc:
            raise RuntimeBundleUploadServiceError(
                "invalid_existing_selena", "Existing Selena manifest is invalid", status_code=422
            ) from exc
        identity_payload = {
            "files": [item.to_dict() for item in manifest.files],
            "source": manifest.source.identity_dict(),
        }
        expected_id = "selena-bundle:sha256:" + hashlib.sha256(
            json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if manifest.id != expected_id:
            raise RuntimeBundleUploadServiceError(
                "invalid_existing_selena", "Existing Selena identity does not match its files", status_code=409
            )
        path = f"bundles/{manifest.id.rsplit(':', 1)[-1]}"
        try:
            session = self._store.create_upload_session(
                owner, project, path, expected_size, checksum,
                evidence_ref="existing-sdk:" + manifest.id,
            )
            if session.received_bytes < expected_size:
                session = self._store.append_chunk(
                    session.session_id,
                    session.received_bytes,
                    archive_bytes[session.received_bytes:],
                    owner=owner,
                )
            try:
                published = self._store.finalize_upload(session.session_id, owner=owner)
            except ArtifactConflictError:
                # The same logical path already exists with a different
                # archive checksum.  This happens when the same Selena folder
                # is re-imported and the zip binary differs (timestamps,
                # compression) even though the file contents are identical.
                # Check whether the catalog already has this bundle identity;
                # if so, reuse the existing record instead of failing.
                try:
                    existing_record = self._catalog.get(manifest.id)
                except RuntimeBundleCatalogError:
                    existing_record = None
                if existing_record is not None:
                    return {
                        "runtime_bundle": existing_record.public_dict,
                        "reused": True,
                    }
                raise
            location = self._store.resolve_location(str(published["storage_ref"]))
            temporary = Path(tempfile.mkdtemp(prefix="rsim-existing-verify-"))
            try:
                extract_runtime_bundle_archive(
                    location,
                    temporary / "runtime",
                    manifest=manifest,
                    archive_checksum=checksum,
                )
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
            registered = self._catalog.register(
                RuntimeBundleRecord(
                    manifest=manifest,
                    internal_project=project,
                    storage_ref=str(published["storage_ref"]),
                    archive_checksum=checksum,
                    archive_size=expected_size,
                    owner=owner,
                    created_by="sdk-existing-import",
                )
            )
        except RuntimeBundleUploadServiceError:
            raise
        except Exception as exc:
            raise RuntimeBundleUploadServiceError(
                "existing_selena_import_failed",
                "Existing Selena archive could not be verified and stored",
                status_code=409,
            ) from exc
        return {"runtime_bundle": registered.public_dict, "reused": bool(published.get("reused", False))}


def trusted_runtime_bundle_evidence_from_control(
    control: ControlService,
    owner: str,
    evidence_ref: str,
) -> TrustedRuntimeBundleEvidence:
    owner = normalize_user(owner)
    match = re.fullmatch(r"(.+):(\d+)", str(evidence_ref or "").strip())
    if match is None:
        raise RuntimeBundleUploadServiceError("invalid_bundle_evidence", "Runtime Bundle evidence is invalid", status_code=409)
    stage_id, attempt_number = match.group(1), int(match.group(2))
    try:
        task = control.get_task(stage_id)
        job = control.get_job(task["job_id"])
    except KeyError as exc:
        raise RuntimeBundleUploadServiceError("bundle_evidence_unavailable", "Runtime Bundle evidence is unavailable", status_code=409) from exc
    if normalize_user(job.get("owner") or "") != owner or task.get("stage_type") != "build_selena":
        raise RuntimeBundleUploadServiceError("bundle_evidence_mismatch", "Runtime Bundle evidence does not belong to this request", status_code=409)
    attempts = [item for item in control.list_attempts(stage_id) if int(item.get("attempt") or 0) == attempt_number]
    if len(attempts) != 1 or attempts[0].get("status") != "succeeded":
        raise RuntimeBundleUploadServiceError("bundle_evidence_unavailable", "Build attempt has not succeeded", status_code=409)
    attempt = attempts[0]
    agent_id = str(attempt.get("agent_id") or "")
    agent = next((item for item in control.list_agents() if item.get("agent_id") == agent_id), None)
    metadata = dict((agent or {}).get("metadata") or {})
    if (
        not agent_id
        or agent_id == INTERNAL_V1_SCHEDULER_AGENT_ID
        or metadata.get("node_kind") not in {"windows_agent", "windows_full"}
        or "build.selena" not in (agent or {}).get("capabilities", [])
    ):
        raise RuntimeBundleUploadServiceError("bundle_evidence_untrusted", "Build executor is not an authorized Windows node", status_code=409)
    result = attempt.get("result")
    if not isinstance(result, Mapping) or result.get("source_changed_during_build") is not False:
        raise RuntimeBundleUploadServiceError("invalid_bundle_evidence", "Build source changed during Runtime Bundle creation", status_code=409)
    raw_manifest = result.get("runtime_bundle")
    raw_archive = result.get("runtime_bundle_archive")
    if not isinstance(raw_manifest, Mapping) or not isinstance(raw_archive, Mapping):
        raise RuntimeBundleUploadServiceError("invalid_bundle_evidence", "Runtime Bundle evidence is missing", status_code=409)
    try:
        source_value = dict(raw_manifest.get("source") or {})
        identity = dict(result.get("runtime_bundle_identity") or {})
        source_value["adapter_key"] = str(identity.get("adapter_key") or "")
        manifest = RuntimeBundleManifest(
            id=str(raw_manifest.get("id") or ""),
            files=tuple(RuntimeFile(**dict(item)) for item in raw_manifest.get("files") or []),
            source=RuntimeSourceEvidence(**source_value),
            created_at=float(raw_manifest.get("created_at") or 0),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeBundleUploadServiceError("invalid_bundle_evidence", "Runtime Bundle manifest is invalid", status_code=409) from exc
    if raw_archive.get("bundle_id") != manifest.id or int(raw_archive.get("file_count") or 0) != len(manifest.files):
        raise RuntimeBundleUploadServiceError("invalid_bundle_evidence", "Runtime Bundle archive identity is invalid", status_code=409)
    return TrustedRuntimeBundleEvidence(
        evidence_ref=evidence_ref,
        owner=owner,
        project=str(result.get("project") or ""),
        created_by=agent_id,
        manifest=manifest,
        archive_checksum=str(raw_archive.get("checksum") or ""),
        archive_size=int(raw_archive.get("size") or 0),
        lease_ref=str(result.get("runtime_bundle_lease_ref") or ""),
    )


def _session_dict(session: UploadSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "status": session.status,
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
    "RuntimeBundleUploadService",
    "RuntimeBundleUploadServiceError",
    "TrustedRuntimeBundleEvidence",
    "trusted_runtime_bundle_evidence_from_control",
]
