"""Resumable Windows-local result archive upload boundary.

The Selena process stays on Windows.  This service only transfers the
already-created immutable result ZIP to the Linux control plane and registers
its public file evidence in ``ResultCatalog``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Mapping

from core.artifact_store import (
    ArtifactChecksumError,
    ArtifactConflictError,
    ArtifactPathError,
    ArtifactSessionError,
    ArtifactStore,
    ArtifactStoreError,
)
from core.local_results import ResultCatalog, ResultCatalogError, ResultFileRef
from core.user import normalize_user


class ResultUploadServiceError(RuntimeError):
    """Stable application error for the HTTP/SDK adapters."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.status_code = int(status_code)


_RUN_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ResultUploadService:
    """Coordinate a resumable archive upload and central catalog import."""

    _PROJECT = "local-results"

    def __init__(self, store: ArtifactStore, catalog: ResultCatalog) -> None:
        self._store = store
        self._catalog = catalog

    def create(
        self,
        owner: str,
        *,
        run_ref: str,
        archive_size: int,
        archive_checksum: str,
    ) -> dict[str, Any]:
        owner = normalize_user(owner)
        run_ref = self._validate_run_ref(run_ref)
        checksum = self._validate_checksum(archive_checksum)
        try:
            session = self._store.create_upload_session(
                owner,
                self._PROJECT,
                # Windows directory names cannot contain the colon used by a
                # logical local-run lease. Preserve the real reference in the
                # session evidence field and use a stable safe path key.
                "run-" + hashlib.sha256(run_ref.encode("utf-8")).hexdigest(),
                int(archive_size),
                checksum,
                evidence_ref=run_ref,
            )
            return self._session_dict(session)
        except (ArtifactPathError, ArtifactSessionError, ArtifactStoreError) as exc:
            raise ResultUploadServiceError("result_upload_invalid", str(exc), status_code=422) from exc

    def get(self, owner: str, session_id: str) -> dict[str, Any]:
        try:
            return self._session_dict(self._store.get_session(session_id, owner=normalize_user(owner)))
        except ArtifactSessionError as exc:
            raise ResultUploadServiceError("result_upload_unavailable", "Result upload session is unavailable", status_code=404) from exc

    def append(self, owner: str, session_id: str, *, offset: int, data: bytes) -> dict[str, Any]:
        try:
            session = self._store.append_chunk(
                session_id,
                int(offset),
                bytes(data),
                owner=normalize_user(owner),
            )
            return self._session_dict(session)
        except ArtifactSessionError as exc:
            raise ResultUploadServiceError("result_upload_offset_conflict", str(exc), status_code=409) from exc

    def finalize(
        self,
        owner: str,
        session_id: str,
        *,
        files: Iterable[ResultFileRef | Mapping[str, Any]],
        retain_until: float = 0,
    ) -> dict[str, Any]:
        owner = normalize_user(owner)
        try:
            session = self._store.get_session(session_id, owner=owner)
            run_ref = str(session.evidence_ref or "")
            finalized = self._store.finalize_upload(session_id, owner=owner)
            archive = self._store.finalized_location(session_id, owner=owner)
            result = self._catalog.import_archive(
                owner=owner,
                run_ref=run_ref,
                archive_path=archive,
                files=files,
                archive_checksum=str(finalized.get("checksum") or session.expected_checksum),
                archive_size=int(finalized.get("size") or session.expected_size),
                retain_until=float(retain_until or 0),
            )
            return {
                "session": self._session_dict(self._store.get_session(session_id, owner=owner)),
                "result": result.public_dict,
                "result_ref": result.ref,
                "reused": bool(finalized.get("reused", False)),
            }
        except (ArtifactChecksumError, ArtifactConflictError) as exc:
            raise ResultUploadServiceError("result_upload_mismatch", str(exc), status_code=409) from exc
        except ArtifactSessionError as exc:
            raise ResultUploadServiceError("result_upload_unavailable", str(exc), status_code=409) from exc
        except ResultCatalogError as exc:
            raise ResultUploadServiceError("result_upload_invalid", str(exc), status_code=422) from exc
        except ArtifactStoreError as exc:
            raise ResultUploadServiceError("result_upload_invalid", str(exc), status_code=422) from exc

    @staticmethod
    def _validate_run_ref(value: str) -> str:
        run_ref = str(value or "").strip()
        if not _RUN_REF_RE.fullmatch(run_ref):
            raise ResultUploadServiceError("invalid_result_run_ref", "Result run reference is invalid", status_code=422)
        return run_ref

    @staticmethod
    def _validate_checksum(value: str) -> str:
        checksum = str(value or "").strip().lower()
        if not _CHECKSUM_RE.fullmatch(checksum):
            raise ResultUploadServiceError("invalid_result_checksum", "Result archive checksum is invalid", status_code=422)
        return checksum

    @staticmethod
    def _session_dict(session: Any) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "owner": session.owner,
            "run_ref": str(session.evidence_ref or ""),
            "expected_size": session.expected_size,
            "expected_checksum": session.expected_checksum,
            "chunk_size": session.chunk_size,
            "received_bytes": session.received_bytes,
            "status": session.status,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "expires_at": session.expires_at,
        }


__all__ = ["ResultUploadService", "ResultUploadServiceError"]
