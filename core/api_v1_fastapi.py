"""FastAPI adapter for the v5 `/api/v1` application service.

Routes in this module are intentionally thin: they adapt HTTP, Pydantic request
errors, request IDs, and SSE transport to the framework-agnostic
``core.api_v1.ApiV1Service``.
"""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, StreamingResponse
from starlette.staticfiles import StaticFiles

from core.api_v1 import ApiV1Error, ApiV1Service, format_error_envelope, iter_sse, make_json_safe
from core.control_service import ControlService
from core.http_auth import AuthPrincipal, HttpAuthError, HttpTokenAuthenticator
from core.spec import SimulationSpec
from core.user_config import UserRunConfig
from core.user import USER_HEADER, current_user, normalize_user


class SubmitJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: SimulationSpec
    dry_run: bool = Field(default=False)


class ImportSpecRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    yaml_content: str = Field(min_length=1, max_length=1024 * 1024)


class ExportSpecRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: SimulationSpec


class SubmitUserRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: UserRunConfig
    dry_run: bool = Field(default=False)
    prepared_runtime_bundle_id: str = Field(default="", max_length=160)


class ExportUserRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: UserRunConfig


class CreateArtifactUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_evidence_ref: str = Field(min_length=1, max_length=200)
    publish_path: str = Field(default="", max_length=512)


class CreateRuntimeBundleUploadRequest(CreateArtifactUploadRequest):
    pass


class DatasetUploadFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_path: str = Field(min_length=1, max_length=1024)
    size: int = Field(gt=0)
    # Browser uploads may omit this so the server can hash large MF4 files
    # incrementally after resumable transfer. SDK and Agent clients should
    # still provide it to fail fast and enable immediate content reuse.
    checksum: str = Field(default="", pattern=r"^(?:|sha256:[0-9a-f]{64})$")


class CreateDatasetUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1, max_length=64)
    files: list[DatasetUploadFileRequest] = Field(min_length=1)


class CreateRunDataUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[DatasetUploadFileRequest] = Field(min_length=1)


class CreateAgentDatasetUploadRequest(CreateDatasetUploadRequest):
    evidence_ref: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def agent_files_require_checksums(self) -> "CreateAgentDatasetUploadRequest":
        if any(not item.checksum for item in self.files):
            raise ValueError("Agent dataset uploads require file checksums")
        return self


class AgentRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(min_length=1, max_length=200)
    hostname: str = Field(default="", max_length=200)
    platform: str = Field(default="", max_length=100)
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentPollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=1, max_length=200)


class AgentHeartbeatRequest(AgentPollRequest):
    status: str = Field(default="idle", max_length=32)
    current_task_id: str = Field(default="", max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentLogsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(default="", max_length=200)
    lines: list[str] = Field(default_factory=list)
    stream: str = Field(default="stdout", pattern=r"^(stdout|stderr)$")


class AgentResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(min_length=1, max_length=200)
    status: str = Field(min_length=1, max_length=32)
    returncode: int
    result: dict[str, Any] = Field(default_factory=dict)


def create_app(
    *,
    control_service_factory: Callable[[str], ControlService] | None = None,
    api_service: ApiV1Service | None = None,
    web_root: str | Path | None = None,
    authenticator: HttpTokenAuthenticator | None = None,
) -> FastAPI:
    """Create the FastAPI app for v5 `/api/v1` routes."""
    service = api_service or ApiV1Service(control_service_factory=control_service_factory)
    app = FastAPI(title="radar-sim API", version="v1")

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", "").strip() or f"req_{uuid.uuid4().hex}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    def owner(request: Request) -> str:
        if authenticator is not None:
            try:
                return authenticator.authenticate_user(request.headers.get("Authorization")).owner
            except HttpAuthError as exc:
                raise ApiV1Error(
                    "authentication_failed", "Bearer authentication failed", status_code=401,
                ) from exc
        return normalize_user(request.headers.get(USER_HEADER, "").strip() or current_user())

    def agent_principal(request: Request) -> AuthPrincipal | None:
        if authenticator is None:
            return None
        try:
            return authenticator.authenticate_agent(request.headers.get("Authorization"))
        except HttpAuthError as exc:
            raise ApiV1Error(
                "authentication_failed", "Agent Bearer authentication failed", status_code=401,
            ) from exc

    def agent_identity(request: Request, claimed_agent_id: str) -> tuple[str, str]:
        principal = agent_principal(request)
        if principal is None:
            return owner(request), claimed_agent_id
        if claimed_agent_id != principal.agent_id:
            raise ApiV1Error(
                "agent_identity_mismatch",
                "Request agent_id does not match the authenticated Agent",
                status_code=403,
            )
        return principal.owner, str(principal.agent_id)

    def user_or_agent_owner(request: Request) -> str:
        """Authenticate a shared immutable download for either client role."""
        if authenticator is None:
            return owner(request)
        authorization = request.headers.get("Authorization")
        try:
            return authenticator.authenticate_user(authorization).owner
        except HttpAuthError:
            try:
                return authenticator.authenticate_agent(authorization).owner
            except HttpAuthError as exc:
                raise ApiV1Error(
                    "authentication_failed", "Bearer authentication failed", status_code=401,
                ) from exc

    def envelope(request: Request, code: str, message: str, *, detail: Any = None, actions=None) -> dict[str, Any]:
        return format_error_envelope(
            code,
            message,
            detail=detail,
            actions=actions or [],
            request_id=getattr(request.state, "request_id", ""),
        )

    @app.exception_handler(ApiV1Error)
    async def api_error_handler(request: Request, exc: ApiV1Error):
        return JSONResponse(
            envelope(request, exc.code, exc.message, detail=exc.detail, actions=exc.actions),
            status_code=exc.status_code,
            headers={"X-Request-ID": getattr(request.state, "request_id", "")},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        errors = make_json_safe(jsonable_encoder(exc.errors()))
        path = request.url.path
        def is_spec_error(err: dict[str, Any]) -> bool:
            loc = list(err.get("loc", []))
            if path == "/api/v1/validate":
                return True
            return "spec" in loc and not (loc == ["body", "spec"] and err.get("type") == "missing")

        code = "invalid_spec" if any(is_spec_error(err) for err in errors) else "invalid_request"
        if path.startswith("/api/v1/run-") or path.startswith("/api/v1/run-config"):
            code = "invalid_run_config"
        message = (
            "Simulation configuration validation failed"
            if code == "invalid_run_config"
            else "SimulationSpec validation failed"
            if code == "invalid_spec"
            else "Request validation failed"
        )
        return JSONResponse(
            envelope(
                request,
                code,
                message,
                detail={"errors": errors},
                actions=[{"type": "fix_spec" if code == "invalid_spec" else "fix_request", "label": "Fix the request fields shown in detail"}],
            ),
            status_code=422,
            headers={"X-Request-ID": getattr(request.state, "request_id", "")},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        code = "not_found" if exc.status_code == 404 else "http_error"
        message = "Route not found" if exc.status_code == 404 else "HTTP error"
        return JSONResponse(
            envelope(request, code, message),
            status_code=exc.status_code,
            headers={"X-Request-ID": getattr(request.state, "request_id", "")},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        return JSONResponse(
            envelope(
                request,
                "internal_error",
                "Internal server error",
                actions=[{"type": "retry", "label": "Retry the request or contact an operator"}],
            ),
            status_code=500,
            headers={"X-Request-ID": getattr(request.state, "request_id", "")},
        )

    @app.get("/api/v1/health")
    def health():
        return {**service.health(), "authentication_required": authenticator is not None}

    # Windows full/light Agent endpoints share this process and ControlService
    # with /api/v1, so Stage handoffs cannot drift across two SQLite databases.
    @app.post("/api/agents/register", status_code=201)
    def register_agent(request: Request, body: AgentRegisterRequest):
        identity, agent_id = agent_identity(request, body.agent_id)
        payload = body.model_dump()
        payload["agent_id"] = agent_id
        return service.register_agent(identity, **payload)

    @app.post("/api/agents/poll")
    def poll_agent(request: Request, body: AgentPollRequest):
        identity, agent_id = agent_identity(request, body.agent_id)
        return service.poll_agent(identity, agent_id)

    @app.post("/api/agents/heartbeat")
    def heartbeat_agent(request: Request, body: AgentHeartbeatRequest):
        identity, agent_id = agent_identity(request, body.agent_id)
        return service.heartbeat_agent(
            identity, agent_id, status=body.status,
            current_task_id=body.current_task_id, metadata=body.metadata,
        )

    @app.post("/api/tasks/logs")
    def append_agent_logs(request: Request, body: AgentLogsRequest):
        principal = agent_principal(request)
        if principal is not None:
            identity, authenticated_agent_id = agent_identity(request, body.agent_id)
        else:
            identity, authenticated_agent_id = owner(request), ""
        return service.append_agent_logs(
            identity, body.task_id, lines=body.lines, stream=body.stream,
            agent_id=authenticated_agent_id,
        )

    @app.post("/api/tasks/result")
    def submit_agent_result(request: Request, body: AgentResultRequest):
        identity, agent_id = agent_identity(request, body.agent_id)
        return service.submit_agent_result(
            identity, body.task_id, agent_id=agent_id,
            status=body.status, returncode=body.returncode, result=body.result,
        )

    @app.get("/api/agents/config-assets/{asset_id}/download")
    def download_agent_config_asset(
        request: Request,
        asset_id: str,
        kind: str = Query(pattern=r"^(adapter|mat_filter)$"),
    ):
        principal = agent_principal(request)
        identity = principal.owner if principal is not None else owner(request)
        record, location = service.config_asset_content(identity, asset_id, kind=kind)
        return FileResponse(
            location,
            media_type="text/plain; charset=utf-8",
            filename=record.filename,
            headers={"X-Content-SHA256": record.checksum},
        )

    @app.get("/api/v1/schema/simulation-spec")
    def simulation_spec_schema():
        return service.simulation_spec_schema()

    @app.get("/api/v1/schema/run-config")
    def run_config_schema():
        return service.user_run_config_schema()

    @app.get("/api/v1/projects")
    def list_projects():
        return service.list_projects()

    @app.get("/api/v1/capabilities")
    def execution_capabilities(request: Request):
        return service.execution_capabilities(owner(request))

    @app.post("/api/v1/specs/import")
    def import_spec(body: ImportSpecRequest):
        return service.import_spec_yaml(body.yaml_content)

    @app.post("/api/v1/specs/export")
    def export_spec(body: ExportSpecRequest):
        return service.export_spec_yaml(body.spec.to_dict())

    @app.post("/api/v1/run-configs/import")
    def import_run_config(body: ImportSpecRequest):
        return service.import_user_run_config_yaml(body.yaml_content)

    @app.post("/api/v1/run-configs/export")
    def export_run_config(body: ExportUserRunRequest):
        return service.export_user_run_config_yaml(body.config.to_dict())

    @app.post("/api/v1/run-configs/validate")
    def validate_run_config(config: UserRunConfig):
        return service.validate_user_run_config(config.to_dict())

    @app.post("/api/v1/run-jobs", status_code=201)
    def submit_run_job(
        request: Request,
        body: SubmitUserRunRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        return service.submit_user_run(
            owner(request),
            config_payload=body.config.to_dict(),
            dry_run=body.dry_run,
            idempotency_key=idempotency_key or "",
            prepared_runtime_bundle_id=body.prepared_runtime_bundle_id,
        )

    @app.post("/api/v1/validate")
    def validate(spec: SimulationSpec):
        return service.validate(spec.to_dict())

    @app.post("/api/v1/jobs", status_code=201)
    def submit_job(
        request: Request,
        body: SubmitJobRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        return service.submit_job(
            owner(request),
            spec_payload=body.spec.to_dict(),
            dry_run=body.dry_run,
            idempotency_key=idempotency_key or "",
        )

    @app.get("/api/v1/jobs")
    def list_jobs(
        request: Request,
        status: str = Query(default="", max_length=40),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        return service.list_jobs(owner(request), status=status, limit=limit)

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(request: Request, job_id: str):
        return service.get_job(owner(request), job_id)

    @app.get("/api/v1/jobs/{job_id}/events")
    def events(
        request: Request,
        job_id: str,
        since: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
        stream: bool = Query(default=False),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        cursor = since
        if since == 0 and last_event_id:
            try:
                cursor = max(int(last_event_id), 0)
            except ValueError:
                cursor = 0
        page = service.events(owner(request), job_id, since=cursor, limit=limit)
        if not stream:
            return page
        return StreamingResponse(
            iter_sse(page["events"]),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Request-ID": getattr(request.state, "request_id", ""),
            },
        )

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel_job(request: Request, job_id: str):
        return service.cancel_job(owner(request), job_id)

    @app.post("/api/v1/jobs/{job_id}/stages/{stage_id}/retry")
    def retry_stage(request: Request, job_id: str, stage_id: str):
        return service.retry_stage(owner(request), job_id, stage_id)

    @app.get("/api/v1/jobs/{job_id}/manifest")
    def manifest(request: Request, job_id: str):
        return service.manifest(owner(request), job_id)

    @app.post("/api/v1/artifact-uploads", status_code=201)
    def create_artifact_upload(request: Request, body: CreateArtifactUploadRequest):
        return service.create_artifact_upload(
            owner(request),
            build_evidence_ref=body.build_evidence_ref,
            publish_path=body.publish_path,
        )

    @app.get("/api/v1/artifact-uploads/{session_id}")
    def get_artifact_upload(request: Request, session_id: str):
        return service.get_artifact_upload(owner(request), session_id)

    @app.patch("/api/v1/artifact-uploads/{session_id}")
    async def append_artifact_upload(
        request: Request,
        session_id: str,
        upload_offset: int = Header(alias="Upload-Offset", ge=0),
    ):
        return service.append_artifact_upload(
            owner(request),
            session_id,
            offset=upload_offset,
            data=await request.body(),
        )

    @app.post("/api/v1/artifact-uploads/{session_id}/finalize")
    def finalize_artifact_upload(request: Request, session_id: str):
        return service.finalize_artifact_upload(owner(request), session_id)

    @app.post("/api/v1/runtime-bundle-uploads", status_code=201)
    def create_runtime_bundle_upload(request: Request, body: CreateRuntimeBundleUploadRequest):
        return service.create_runtime_bundle_upload(
            owner(request),
            build_evidence_ref=body.build_evidence_ref,
            publish_path=body.publish_path,
        )

    @app.post("/api/v1/existing-selena-imports", status_code=201)
    async def import_existing_selena(
        request: Request,
        encoded_metadata: str = Header(alias="X-Rsim-Existing-Metadata", min_length=1, max_length=32768),
    ):
        try:
            padding = "=" * (-len(encoded_metadata) % 4)
            metadata = json.loads(base64.urlsafe_b64decode(encoded_metadata + padding).decode("utf-8"))
        except Exception as exc:
            raise ApiV1Error(
                "invalid_existing_selena",
                "Existing Selena upload metadata is invalid",
                status_code=422,
            ) from exc
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 512 * 1024 * 1024:
                raise ApiV1Error(
                    "existing_selena_too_large",
                    "Existing Selena archive exceeds 512 MiB",
                    status_code=413,
                )
        return service.import_existing_selena(
            owner(request), metadata=dict(metadata or {}), archive_bytes=bytes(content)
        )

    @app.post("/api/v1/config-assets", status_code=201)
    async def upload_config_asset(
        request: Request,
        asset_kind: str = Header(alias="X-Asset-Kind", pattern=r"^(adapter|mat_filter)$"),
        asset_filename: str = Header(alias="X-Asset-Filename", min_length=1, max_length=128),
    ):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 8 * 1024 * 1024:
                raise ApiV1Error("config_asset_too_large", "Configuration asset exceeds 8 MiB", status_code=413)
        return service.upload_config_asset(
            owner(request), kind=asset_kind, filename=asset_filename, content=bytes(data)
        )

    @app.get("/api/v1/config-assets")
    def list_config_assets(request: Request, kind: str = Query(default="", pattern=r"^(?:|adapter|mat_filter)$")):
        return service.list_config_assets(owner(request), kind=kind)

    @app.get("/api/v1/config-assets/{asset_id}")
    def get_config_asset(
        request: Request,
        asset_id: str,
        kind: str = Query(pattern=r"^(adapter|mat_filter)$"),
    ):
        return service.get_config_asset(owner(request), asset_id, kind=kind)

    @app.get("/api/v1/runtime-bundles")
    def list_runtime_bundles(request: Request):
        return service.list_runtime_bundles(owner(request))

    @app.get("/api/v1/runtime-bundles/{bundle_id}")
    def get_runtime_bundle(request: Request, bundle_id: str):
        return service.get_runtime_bundle(owner(request), bundle_id)

    @app.get("/api/v1/runtime-bundles/{bundle_id}/download")
    def download_runtime_bundle(request: Request, bundle_id: str):
        identity = user_or_agent_owner(request)
        bundle = service.get_runtime_bundle(identity, bundle_id)
        archive = service.runtime_bundle_archive(identity, bundle_id)
        digest = str(bundle.get("id") or "").rsplit(":", 1)[-1][:12]
        return FileResponse(
            archive,
            media_type="application/zip",
            filename=f"runtime-bundle-{digest}.zip",
            headers={
                "X-Content-SHA256": str(bundle.get("archive_checksum") or ""),
                "X-Content-Length": str(bundle.get("archive_size") or ""),
            },
        )

    @app.get("/api/v1/runtime-bundle-uploads/{session_id}")
    def get_runtime_bundle_upload(request: Request, session_id: str):
        return service.get_runtime_bundle_upload(owner(request), session_id)

    @app.patch("/api/v1/runtime-bundle-uploads/{session_id}")
    async def append_runtime_bundle_upload(
        request: Request,
        session_id: str,
        upload_offset: int = Header(alias="Upload-Offset", ge=0),
    ):
        return service.append_runtime_bundle_upload(
            owner(request), session_id, offset=upload_offset, data=await request.body()
        )

    @app.post("/api/v1/runtime-bundle-uploads/{session_id}/finalize")
    def finalize_runtime_bundle_upload(request: Request, session_id: str):
        return service.finalize_runtime_bundle_upload(owner(request), session_id)

    @app.post("/api/v1/dataset-uploads", status_code=201)
    def create_dataset_upload(request: Request, body: CreateDatasetUploadRequest):
        return service.create_dataset_upload(
            owner(request),
            project=body.project,
            files=[item.model_dump() for item in body.files],
        )

    @app.post("/api/v1/run-data-uploads", status_code=201)
    def create_run_data_upload(request: Request, body: CreateRunDataUploadRequest):
        """Project-free browser/SDK upload namespace for the v2 user contract."""
        return service.create_dataset_upload(
            owner(request),
            project="run-config-v2",
            files=[item.model_dump() for item in body.files],
        )

    @app.post("/api/v1/agent-dataset-uploads", status_code=201)
    def create_agent_dataset_upload(
        request: Request,
        body: CreateAgentDatasetUploadRequest,
        agent_id: str = Header(alias="X-Rsim-Agent-ID", min_length=1, max_length=200),
    ):
        identity, authenticated_agent_id = agent_identity(request, agent_id)
        return service.create_agent_dataset_upload(
            identity,
            project=body.project,
            files=[item.model_dump() for item in body.files],
            evidence_ref=body.evidence_ref,
            agent_id=authenticated_agent_id,
        )

    @app.get("/api/v1/dataset-uploads/{session_id}")
    def get_dataset_upload(request: Request, session_id: str):
        return service.get_dataset_upload(owner(request), session_id)

    @app.patch("/api/v1/dataset-uploads/{session_id}/files/{file_id}")
    async def append_dataset_upload(
        request: Request,
        session_id: str,
        file_id: str,
        upload_offset: int = Header(alias="Upload-Offset", ge=0),
    ):
        identity = owner(request)
        session = service.get_dataset_upload(identity, session_id)
        limit = int(session.get("chunk_size") or 0)
        if limit <= 0:
            raise ApiV1Error("dataset_upload_unavailable", "Dataset upload chunk limit is unavailable", status_code=503)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                raise ApiV1Error(
                    "dataset_upload_chunk_too_large",
                    "Dataset upload chunk exceeds the server limit",
                    status_code=413,
                    detail={"max_bytes": limit},
                )
        return service.append_dataset_upload(
            identity,
            session_id,
            file_id,
            offset=upload_offset,
            data=bytes(data),
        )

    @app.post("/api/v1/dataset-uploads/{session_id}/finalize")
    def finalize_dataset_upload(request: Request, session_id: str):
        return service.finalize_dataset_upload(owner(request), session_id)

    @app.get("/api/v1/results")
    def list_results(request: Request):
        return service.list_results(owner(request))

    @app.get("/api/v1/results/{result_ref}")
    def get_result(request: Request, result_ref: str):
        return service.get_result(owner(request), result_ref)

    @app.get("/api/v1/results/{result_ref}/download")
    def download_result(request: Request, result_ref: str):
        result = service.get_result(owner(request), result_ref)
        archive = service.result_archive(owner(request), result_ref)
        digest = str(result.get("archive_checksum") or "").removeprefix("sha256:")[:12]
        return FileResponse(
            archive,
            media_type="application/zip",
            filename=f"radar-sim-result-{digest}.zip",
        )

    if web_root is None:
        try:
            from radar_sim_web import static_root
            web_root = Path(str(static_root()))
        except (ImportError, TypeError, ValueError):
            web_root = None
    static_dir = Path(web_root).resolve() if web_root is not None else None
    if static_dir is not None and (static_dir / "index.html").is_file():
        @app.get("/", include_in_schema=False)
        def web_console():
            return FileResponse(static_dir / "index.html")

        @app.get("/favicon.ico", include_in_schema=False)
        def empty_favicon():
            return Response(status_code=204)

        app.mount("/console", StaticFiles(directory=static_dir, html=True), name="v1-console")

    return app


__all__ = ["create_app"]
