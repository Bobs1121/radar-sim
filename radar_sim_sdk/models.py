"""Typed SDK response and event models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.spec import SimulationSpec
from core.user_config import UserRunConfig


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    spec: SimulationSpec
    fingerprint: str
    environment_plan: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidationResult":
        return cls(
            valid=bool(data.get("valid")),
            spec=SimulationSpec.from_dict(dict(data.get("spec") or {})),
            fingerprint=str(data.get("fingerprint") or ""),
            environment_plan=dict(data.get("environment_plan") or {}),
        )


@dataclass(frozen=True)
class Job:
    id: str
    status: str
    type: str = ""
    spec_hash: str = ""
    dry_run: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel_requested: bool = False
    progress: float = 0.0
    current_stage: str = ""
    available_actions: list[dict[str, Any]] = field(default_factory=list)
    spec: dict[str, Any] = field(default_factory=dict)
    resolved_spec: dict[str, Any] = field(default_factory=dict)
    stages: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        return cls(
            id=str(data.get("id") or data.get("job_id") or ""),
            status=str(data.get("status") or ""),
            type=str(data.get("type") or ""),
            spec_hash=str(data.get("spec_hash") or ""),
            dry_run=bool(data.get("dry_run", False)),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            completed_at=float(data.get("completed_at") or 0.0),
            started_at=float(data.get("started_at") or 0.0),
            finished_at=float(data.get("finished_at") or 0.0),
            cancel_requested=bool(data.get("cancel_requested", False)),
            progress=float(data.get("progress") or 0.0),
            current_stage=str(data.get("current_stage") or ""),
            available_actions=list(data.get("available_actions") or []),
            spec=dict(data.get("spec") or {}),
            resolved_spec=dict(data.get("resolved_spec") or {}),
            stages=list(data.get("stages") or []),
            tasks=list(data.get("tasks") or []),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class Event:
    id: int | None
    event: str
    data: dict[str, Any]
    message: str = ""
    stage: str = ""
    stage_id: str = ""
    status: str = ""
    progress: float | None = None
    code: str = ""
    action: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        event_id = data.get("id", data.get("sequence"))
        try:
            parsed_id = int(event_id) if event_id not in (None, "") else None
        except (TypeError, ValueError):
            parsed_id = None
        payload = dict(data.get("data") or data)
        progress = data.get("progress", payload.get("progress"))
        return cls(
            id=parsed_id,
            event=str(data.get("event") or "message"),
            data=payload,
            message=str(data.get("message") or payload.get("message") or ""),
            stage=str(data.get("stage") or data.get("stage_type") or payload.get("stage") or payload.get("stage_type") or ""),
            stage_id=str(data.get("stage_id") or payload.get("stage_id") or ""),
            status=str(data.get("status") or payload.get("status") or ""),
            progress=float(progress) if progress is not None else None,
            code=str(data.get("code") or payload.get("code") or ""),
            action=list(data.get("action") or payload.get("action") or []),
        )


@dataclass(frozen=True)
class EventsPage:
    job_id: str
    status: str
    events: list[Event]
    next_cursor: int
    terminal: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EventsPage":
        return cls(
            job_id=str(data.get("job_id") or ""),
            status=str(data.get("status") or ""),
            events=[Event.from_dict(item) for item in data.get("events") or []],
            next_cursor=int(data.get("next_cursor") or 0),
            terminal=bool(data.get("terminal", False)),
        )


@dataclass(frozen=True)
class ManifestResponse:
    job_id: str
    available: bool
    manifest: dict[str, Any] | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ManifestResponse":
        manifest = data.get("manifest")
        return cls(
            job_id=str(data.get("job_id") or ""),
            available=bool(data.get("available", False)),
            manifest=dict(manifest) if isinstance(manifest, dict) else None,
        )


@dataclass(frozen=True)
class RunConfigValidationResult:
    valid: bool
    config: UserRunConfig
    fingerprint: str
    environment_plan: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunConfigValidationResult":
        return cls(
            valid=bool(data.get("valid")),
            config=UserRunConfig.from_dict(dict(data.get("config") or {})),
            fingerprint=str(data.get("fingerprint") or ""),
            environment_plan=dict(data.get("environment_plan") or {}),
        )


@dataclass(frozen=True)
class ArtifactUpload:
    session_id: str
    status: str
    project: str
    publish_path: str
    storage_ref: str
    build_evidence_ref: str
    expected_size: int
    expected_checksum: str
    received_bytes: int
    chunk_size: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactUpload":
        return cls(
            session_id=str(data.get("session_id") or ""),
            status=str(data.get("status") or ""),
            project=str(data.get("project") or ""),
            publish_path=str(data.get("publish_path") or ""),
            storage_ref=str(data.get("storage_ref") or ""),
            build_evidence_ref=str(data.get("build_evidence_ref") or ""),
            expected_size=int(data.get("expected_size") or 0),
            expected_checksum=str(data.get("expected_checksum") or ""),
            received_bytes=int(data.get("received_bytes") or 0),
            chunk_size=int(data.get("chunk_size") or 0),
        )


@dataclass(frozen=True)
class ArtifactUploadResult:
    session: ArtifactUpload
    artifact: dict[str, Any]
    reused: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactUploadResult":
        return cls(
            session=ArtifactUpload.from_dict(dict(data.get("session") or {})),
            artifact=dict(data.get("artifact") or {}),
            reused=bool(data.get("reused", False)),
        )


@dataclass(frozen=True)
class RuntimeBundleUploadResult:
    session: ArtifactUpload
    runtime_bundle: dict[str, Any]
    reused: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeBundleUploadResult":
        return cls(
            session=ArtifactUpload.from_dict(dict(data.get("session") or {})),
            runtime_bundle=dict(data.get("runtime_bundle") or {}),
            reused=bool(data.get("reused", False)),
        )


@dataclass(frozen=True)
class DatasetUploadFile:
    file_id: str
    relative_path: str
    expected_size: int
    expected_checksum: str
    received_bytes: int
    status: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DatasetUploadFile":
        return cls(
            file_id=str(data.get("file_id") or ""),
            relative_path=str(data.get("relative_path") or ""),
            expected_size=int(data.get("expected_size") or 0),
            expected_checksum=str(data.get("expected_checksum") or ""),
            received_bytes=int(data.get("received_bytes") or 0),
            status=str(data.get("status") or ""),
        )


@dataclass(frozen=True)
class DatasetUpload:
    session_id: str
    project: str
    manifest_fingerprint: str
    total_size: int
    chunk_size: int
    status: str
    files: tuple[DatasetUploadFile, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DatasetUpload":
        return cls(
            session_id=str(data.get("session_id") or ""),
            project=str(data.get("project") or ""),
            manifest_fingerprint=str(data.get("manifest_fingerprint") or ""),
            total_size=int(data.get("total_size") or 0),
            chunk_size=int(data.get("chunk_size") or 0),
            status=str(data.get("status") or ""),
            files=tuple(DatasetUploadFile.from_dict(item) for item in data.get("files") or []),
        )


@dataclass(frozen=True)
class DatasetUploadResult:
    session: DatasetUpload
    dataset: dict[str, Any]
    data_path: str
    reused: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DatasetUploadResult":
        return cls(
            session=DatasetUpload.from_dict(dict(data.get("session") or {})),
            dataset=dict(data.get("dataset") or {}),
            data_path=str(data.get("data_path") or ""),
            reused=bool(data.get("reused", False)),
        )
