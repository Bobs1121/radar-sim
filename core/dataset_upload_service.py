"""Application boundary for private MF4 dataset uploads."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Iterable, Mapping

from core.dataset_store import (
    DatasetStore,
    DatasetStoreError,
    DatasetUploadChecksumError,
    DatasetUploadPathError,
    DatasetUploadQuotaError,
    DatasetUploadSessionError,
)
from core.datasets import DatasetCatalog, DatasetError, DatasetFileRef
from core.user import normalize_user
from core.control_service import ControlService, INTERNAL_V1_SCHEDULER_AGENT_ID


class DatasetUploadServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = int(status_code)


@dataclass(frozen=True)
class TrustedDataStageEvidence:
    """Server-derived authorization for one Agent prepare_data attempt."""

    evidence_ref: str
    owner: str
    project: str
    job_id: str
    stage_id: str
    attempt: int
    required_agent_id: str

    def __post_init__(self) -> None:
        if not all(
            str(value or "").strip()
            for value in (self.evidence_ref, self.owner, self.project, self.job_id, self.stage_id, self.required_agent_id)
        ) or int(self.attempt) <= 0:
            raise DatasetUploadServiceError(
                "invalid_data_stage_evidence", "Trusted prepare_data stage evidence is invalid", status_code=409
            )


class DatasetUploadService:
    def __init__(
        self,
        store: DatasetStore,
        catalog: DatasetCatalog,
        *,
        project_validator: Callable[[str], bool] | None = None,
        evidence_provider: Callable[[str, str], TrustedDataStageEvidence] | None = None,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._project_validator = project_validator
        self._evidence_provider = evidence_provider

    def create(
        self,
        owner: str,
        *,
        project: str,
        files: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Create a browser/SDK upload. Source kind is never client-selectable."""
        owner = normalize_user(owner)
        self._validate_project(project)
        return self._call(
            lambda: self._store.create_session(
                owner=owner,
                project=project,
                files=files,
                source_kind="central_upload",
            ).to_dict()
        )

    def create_for_agent(
        self,
        owner: str,
        *,
        project: str,
        files: Iterable[Mapping[str, Any]],
        evidence: TrustedDataStageEvidence,
        requesting_agent_id: str,
    ) -> dict[str, Any]:
        """Create an Agent upload only from server-derived Stage evidence."""
        owner = normalize_user(owner)
        self._validate_project(project)
        if (
            normalize_user(evidence.owner) != owner
            or evidence.project != project
            or evidence.required_agent_id != str(requesting_agent_id or "").strip()
        ):
            raise DatasetUploadServiceError(
                "data_stage_evidence_mismatch",
                "prepare_data stage evidence does not authorize this Agent upload",
                status_code=409,
            )
        return self._call(
            lambda: self._store.create_session(
                owner=owner,
                project=project,
                files=files,
                source_kind="agent_upload",
                evidence_ref=evidence.evidence_ref,
            ).to_dict()
        )

    def create_agent_from_evidence(
        self,
        owner: str,
        *,
        project: str,
        files: Iterable[Mapping[str, Any]],
        evidence_ref: str,
        requesting_agent_id: str,
    ) -> dict[str, Any]:
        if self._evidence_provider is None:
            raise DatasetUploadServiceError(
                "data_stage_evidence_unavailable", "Trusted prepare_data evidence is unavailable", status_code=503
            )
        try:
            evidence = self._evidence_provider(normalize_user(owner), str(evidence_ref or "").strip())
        except DatasetUploadServiceError:
            raise
        except Exception as exc:
            raise DatasetUploadServiceError(
                "data_stage_evidence_unavailable", "Trusted prepare_data evidence is unavailable", status_code=409
            ) from exc
        return self.create_for_agent(
            owner,
            project=project,
            files=files,
            evidence=evidence,
            requesting_agent_id=requesting_agent_id,
        )

    def get(self, owner: str, session_id: str) -> dict[str, Any]:
        return self._call(lambda: self._store.get_session(session_id, owner=normalize_user(owner)).to_dict())

    def append(
        self,
        owner: str,
        session_id: str,
        file_id: str,
        *,
        offset: int,
        data: bytes,
    ) -> dict[str, Any]:
        return self._call(
            lambda: self._store.append_file(
                session_id,
                file_id,
                owner=normalize_user(owner),
                offset=int(offset),
                data=bytes(data),
            ).to_dict()
        )

    def finalize(self, owner: str, session_id: str) -> dict[str, Any]:
        owner = normalize_user(owner)

        def operation() -> dict[str, Any]:
            completed = self._store.finalize(session_id, owner=owner)
            files = tuple(
                DatasetFileRef(
                    relative_path=item.relative_path,
                    size=item.expected_size,
                    checksum=item.expected_checksum,
                    signal_status="not-scanned",
                    mtime_ns=0,
                )
                for item in completed.files
            )
            dataset = self._catalog.register_uploaded(
                project=completed.project,
                owner=owner,
                source_kind=completed.source_kind,
                source_path=completed.source_location,
                storage_ref=completed.storage_ref,
                files=files,
            )
            digest = dataset.id.removeprefix("dataset:sha256:")
            return {
                "session": self._store.get_session(session_id, owner=owner).to_dict(),
                "dataset": dataset.to_dict(),
                "data_path": f"dataset://sha256/{digest}",
                "reused": completed.reused,
            }

        return self._call(operation)

    def _validate_project(self, project: str) -> None:
        if self._project_validator is not None and not self._project_validator(str(project or "")):
            raise DatasetUploadServiceError("unknown_project", "Project is not available", status_code=404)

    @staticmethod
    def _call(callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return callback()
        except DatasetUploadServiceError:
            raise
        except DatasetUploadQuotaError as exc:
            raise DatasetUploadServiceError("dataset_upload_quota_exceeded", str(exc), status_code=413) from exc
        except DatasetUploadPathError as exc:
            raise DatasetUploadServiceError("invalid_dataset_path", str(exc), status_code=422) from exc
        except DatasetUploadChecksumError as exc:
            raise DatasetUploadServiceError("dataset_checksum_mismatch", str(exc), status_code=409) from exc
        except DatasetUploadSessionError as exc:
            message = str(exc)
            status = 404 if "unavailable" in message else 409
            raise DatasetUploadServiceError("dataset_upload_unavailable", message, status_code=status) from exc
        except (DatasetStoreError, DatasetError) as exc:
            raise DatasetUploadServiceError("dataset_upload_invalid", str(exc), status_code=400) from exc


__all__ = ["DatasetUploadService", "DatasetUploadServiceError", "TrustedDataStageEvidence"]


def trusted_data_stage_evidence_from_control(
    control: ControlService,
    owner: str,
    evidence_ref: str,
) -> TrustedDataStageEvidence:
    """Authorize the currently running prepare_data attempt on one Windows Agent."""
    owner = normalize_user(owner)
    match = re.fullmatch(r"(.+):(\d+)", str(evidence_ref or "").strip())
    if match is None:
        raise DatasetUploadServiceError("invalid_data_stage_evidence", "prepare_data evidence is invalid", status_code=409)
    stage_id, attempt_number = match.group(1), int(match.group(2))
    try:
        task = control.get_task(stage_id)
        job = control.get_job(task["job_id"])
    except KeyError as exc:
        raise DatasetUploadServiceError(
            "data_stage_evidence_unavailable", "prepare_data evidence is unavailable", status_code=409
        ) from exc
    spec = dict(job.get("spec") or {})
    project = str(spec.get("project") or "")
    if (
        normalize_user(job.get("owner") or "") != owner
        or task.get("stage_type") != "prepare_data"
        or task.get("status") != "running"
    ):
        raise DatasetUploadServiceError(
            "data_stage_evidence_mismatch", "prepare_data evidence does not belong to this request", status_code=409
        )
    attempts = [item for item in control.list_attempts(stage_id) if int(item.get("attempt") or 0) == attempt_number]
    if len(attempts) != 1 or attempts[0].get("status") != "running":
        raise DatasetUploadServiceError(
            "data_stage_evidence_unavailable", "prepare_data attempt is not running", status_code=409
        )
    agent_id = str(attempts[0].get("agent_id") or "").strip()
    required_agent_id = str(task.get("required_agent_id") or "").strip()
    if not agent_id or agent_id == INTERNAL_V1_SCHEDULER_AGENT_ID or agent_id != required_agent_id:
        raise DatasetUploadServiceError(
            "data_stage_evidence_untrusted", "prepare_data attempt has no trusted Windows executor", status_code=409
        )
    agent = next((item for item in control.list_agents() if item.get("agent_id") == agent_id), None)
    metadata = dict((agent or {}).get("metadata") or {})
    if (
        agent is None
        or metadata.get("node_kind") not in {"windows_agent", "windows_full"}
        or "data.upload" not in (agent.get("capabilities") or [])
    ):
        raise DatasetUploadServiceError(
            "data_stage_evidence_untrusted", "prepare_data executor is not an authorized Windows node", status_code=409
        )
    return TrustedDataStageEvidence(
        evidence_ref=str(evidence_ref),
        owner=owner,
        project=project,
        job_id=str(task["job_id"]),
        stage_id=stage_id,
        attempt=attempt_number,
        required_agent_id=required_agent_id,
    )


__all__.append("trusted_data_stage_evidence_from_control")
