"""HTTPX-based radar-sim v5 client."""

from __future__ import annotations

import base64
import json
import hashlib
import ssl
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from core.spec import SimulationSpec
from core.user_config import UserRunConfig
from core.data import iter_mf4_inputs
from core.datasets import classify_data_path
from core.user import USER_HEADER
from radar_sim_sdk.errors import RadarSimApiError, RadarSimTransportError
from radar_sim_sdk.events import event_from_sse, parse_sse_lines
from radar_sim_sdk.models import (
    ArtifactUpload,
    ArtifactUploadResult,
    RuntimeBundleUploadResult,
    DatasetUpload,
    DatasetUploadResult,
    Event,
    EventsPage,
    Job,
    JobDiagnosis,
    ManifestResponse,
    RunConfigValidationResult,
    ValidationResult,
)


class RadarSimClient:
    """Synchronous `/api/v1` SDK client using HTTPX connection pooling."""

    def __init__(
        self,
        base_url: str,
        *,
        user: str = "",
        token: str = "",
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        verify: bool | ssl.SSLContext = True,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        trust_env: bool = True,
    ) -> None:
        self._owns_client = client is None
        merged_headers = dict(headers or {})
        if user:
            merged_headers.setdefault(USER_HEADER, user)
        if token:
            merged_headers.setdefault("Authorization", f"Bearer {token}")
        default_timeout = httpx.Timeout(timeout=60.0, connect=5.0, read=60.0, write=30.0, pool=5.0)
        if client is not None:
            if merged_headers:
                client.headers.update(merged_headers)
            self._client = client
        else:
            self._client = httpx.Client(
                base_url=base_url.rstrip("/"),
                headers=merged_headers,
                timeout=timeout or default_timeout,
                verify=verify,
                transport=transport,
                trust_env=trust_env,
            )

    def health(self) -> dict[str, Any]:
        """Check server health and API version."""
        return dict(self._request("GET", "/api/v1/health"))

    def validate(self, spec: SimulationSpec | dict[str, Any]) -> ValidationResult:
        return ValidationResult.from_dict(self._request("POST", "/api/v1/validate", json=self._spec_payload(spec)))

    def validate_run(self, config: UserRunConfig | dict[str, Any]) -> RunConfigValidationResult:
        """Validate the project-free YAML contract used by the Web console."""
        return RunConfigValidationResult.from_dict(
            self._request("POST", "/api/v1/run-configs/validate", json=self._run_config_payload(config))
        )

    def submit_run(
        self,
        config: UserRunConfig | dict[str, Any],
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> Job:
        parsed = UserRunConfig.from_dict(self._run_config_payload(config))
        payload, prepared_bundle_id = self._prepare_user_run(parsed, dry_run=bool(dry_run))
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return Job.from_dict(
            self._request(
                "POST",
                "/api/v1/run-jobs",
                json={
                    "config": payload,
                    "dry_run": bool(dry_run),
                    "prepared_runtime_bundle_id": prepared_bundle_id,
                },
                headers=headers,
            )
        )

    def submit_cluster_yaml(
        self,
        yaml_path: str | Path,
        *,
        idempotency_key: str | None = None,
    ) -> Job:
        """Submit the V1 existing-Selena + Cluster flow from one YAML file."""
        config = UserRunConfig.from_yaml(Path(yaml_path))
        if config.selena.source != "existing" or config.simulation.target != "cluster":
            raise ValueError(
                "V1 submit_cluster_yaml requires selena.source=existing and simulation.target=cluster"
            )
        return self.submit_run(config, idempotency_key=idempotency_key)

    def submit_yaml(
        self,
        yaml_path: str | Path,
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> Job:
        """Submit any supported build/existing and local/Cluster YAML in one call."""
        return self.submit_run(
            UserRunConfig.from_yaml(Path(yaml_path)),
            dry_run=dry_run,
            idempotency_key=idempotency_key,
        )

    def _prepare_user_run(
        self,
        config: UserRunConfig,
        *,
        dry_run: bool,
    ) -> tuple[dict[str, Any], str]:
        """Prepare caller-local inputs without adding fields to the YAML contract."""
        payload = config.to_dict()
        if dry_run:
            return payload, ""

        prepared_bundle_id = ""
        existing = Path(config.selena.existing_path).expanduser()
        runtime = Path(config.selena.runtime_xml).expanduser()
        existing_is_shared = config.selena.existing_path.startswith("//")
        runtime_is_shared = config.selena.runtime_xml.startswith("//")
        if (
            config.selena.source == "existing"
            and not existing_is_shared
            and not runtime_is_shared
            and existing.is_dir()
            and runtime.is_file()
        ):
            prepared_bundle_id = self._upload_existing_selena(
                existing,
                runtime,
                code_path=config.selena.code_path,
                selena_build_script=config.selena.selena_build_script,
                package_build_script=config.selena.package_build_script,
            )

        data_path = Path(config.data.path).expanduser()
        data_kind = classify_data_path(config.data.path)
        if (
            config.simulation.target in {"auto", "cluster"}
            and data_kind not in {"shared", "central"}
            and data_path.exists()
        ):
            uploaded_data = self.upload_run_data(data_path)
            payload["data"] = {"path": uploaded_data.data_path}

        simulation = dict(payload.get("simulation") or {})
        mat_filter = Path(config.simulation.mat_filter).expanduser()
        if mat_filter.is_file():
            simulation["mat_filter"] = self.upload_config_asset(
                "mat_filter", mat_filter
            )["uri"]
        adapter_text = str(config.simulation.adapter_file or "").strip()
        if adapter_text:
            adapter = Path(adapter_text).expanduser()
            if adapter.is_file():
                simulation["adapter_file"] = self.upload_config_asset(
                    "adapter", adapter
                )["uri"]
        payload["simulation"] = simulation
        return UserRunConfig.from_dict(payload).to_dict(), prepared_bundle_id

    def _upload_existing_selena(
        self,
        existing: Path,
        runtime: Path,
        *,
        code_path: str = "",
        selena_build_script: str = "",
        package_build_script: str = "",
    ) -> str:
        from core.existing_selena import import_existing_selena

        with tempfile.TemporaryDirectory(prefix="rsim-sdk-existing-") as temporary:
            imported = import_existing_selena(
                existing,
                runtime,
                code_path=code_path,
                selena_build_script=selena_build_script,
                package_build_script=package_build_script,
                staging_root=Path(temporary) / "staging",
                # Existing runtime identity and archive must be stable across
                # retries; wall-clock time is not source evidence.
                created_at=0.0,
            )
            imported_record = self.import_existing_runtime_bundle(
                {
                "internal_project": imported.internal_project,
                "adapter_key": imported.adapter_key,
                "manifest": imported.bundle.manifest.to_dict(),
                "archive_checksum": imported.archive.checksum,
                "archive_size": imported.archive.size,
                },
                imported.archive.path,
            )
        bundle_id = str((imported_record.get("runtime_bundle") or {}).get("id") or "")
        if not bundle_id.startswith("selena-bundle:sha256:"):
            raise ValueError("server did not return a valid prepared Selena reference")
        return bundle_id

    def import_existing_runtime_bundle(
        self,
        metadata: dict[str, Any],
        archive: str | Path,
    ) -> dict[str, Any]:
        """Register one complete existing Selena archive through the shared API."""
        encoded = base64.urlsafe_b64encode(
            json.dumps(dict(metadata), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return dict(
            self._request(
                "POST",
                "/api/v1/existing-selena-imports",
                content=Path(archive).read_bytes(),
                headers={"X-Rsim-Existing-Metadata": encoded},
            )
        )

    def submit(
        self,
        spec: SimulationSpec | dict[str, Any],
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> Job:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        payload = {"spec": self._spec_payload(spec), "dry_run": bool(dry_run)}
        return Job.from_dict(self._request("POST", "/api/v1/jobs", json=payload, headers=headers))

    def get_job(self, job_id: str) -> Job:
        return Job.from_dict(self._request("GET", f"/api/v1/jobs/{job_id}"))

    def list_jobs(self, *, status: str = "", limit: int = 50) -> list[Job]:
        payload = self._request(
            "GET",
            "/api/v1/jobs",
            params={"status": str(status or ""), "limit": max(1, min(int(limit or 50), 100))},
        )
        return [Job.from_dict(item) for item in payload.get("jobs") or []]

    def events(self, job_id: str, *, since: int = 0, limit: int = 200) -> EventsPage:
        return EventsPage.from_dict(
            self._request("GET", f"/api/v1/jobs/{job_id}/events", params={"since": since, "limit": limit})
        )

    def stream_events(self, job_id: str, *, since: int = 0, limit: int = 200) -> Iterator[Event]:
        headers = {"Last-Event-ID": str(int(since or 0))} if since else None
        params = {"since": int(since or 0), "limit": int(limit or 200), "stream": "true"}
        try:
            with self._client.stream("GET", f"/api/v1/jobs/{job_id}/events", params=params, headers=headers) as response:
                self._raise_for_status(response)
                for message in parse_sse_lines(response.iter_lines()):
                    yield event_from_sse(message)
        except httpx.TransportError as exc:
            raise RadarSimTransportError(str(exc)) from exc

    def watch(
        self,
        job_id: str,
        *,
        cursor: int = 0,
        timeout: float = 60.0,
        poll_interval: float = 1.0,
    ) -> Iterator[Event]:
        deadline = time.monotonic() + float(timeout)
        next_cursor = int(cursor or 0)
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(f"watch timed out after {timeout} seconds")
            had_transport_error = False
            try:
                for event in self.stream_events(job_id, since=next_cursor):
                    if event.id is not None:
                        next_cursor = max(next_cursor, event.id)
                    yield event
            except RadarSimTransportError:
                had_transport_error = True

            try:
                page = self.events(job_id, since=next_cursor)
            except RadarSimTransportError:
                had_transport_error = True
                page = None
            if had_transport_error and page is None:
                sleep_for = min(float(poll_interval), max(deadline - time.monotonic(), 0.0))
                if sleep_for <= 0:
                    raise TimeoutError(f"watch timed out after {timeout} seconds")
                time.sleep(sleep_for)
                continue
            if page is None:
                continue
            for event in page.events:
                if event.id is not None:
                    next_cursor = max(next_cursor, event.id)
                yield event
            next_cursor = max(next_cursor, page.next_cursor)
            if page.terminal:
                return
            sleep_for = min(float(poll_interval), max(deadline - time.monotonic(), 0.0))
            if sleep_for <= 0:
                raise TimeoutError(f"watch timed out after {timeout} seconds")
            time.sleep(sleep_for)

    def wait(
        self,
        job_id: str,
        *,
        timeout: float = 600.0,
        poll_interval: float = 1.0,
        on_event: Callable[[Event], None] | None = None,
    ) -> Job:
        for event in self.watch(job_id, timeout=timeout, poll_interval=poll_interval):
            if on_event is not None:
                on_event(event)
        return self.get_job(job_id)

    def cancel(self, job_id: str) -> Job:
        return Job.from_dict(self._request("POST", f"/api/v1/jobs/{job_id}/cancel"))

    def retry_stage(self, job_id: str, stage_id: str) -> Job:
        return Job.from_dict(self._request("POST", f"/api/v1/jobs/{job_id}/stages/{stage_id}/retry"))

    def manifest(self, job_id: str) -> ManifestResponse:
        return ManifestResponse.from_dict(self._request("GET", f"/api/v1/jobs/{job_id}/manifest"))

    def diagnosis(self, job_id: str) -> JobDiagnosis:
        """Return the shared path-free diagnosis used by Web and AI adapters."""
        return JobDiagnosis.from_dict(
            self._request("GET", f"/api/v1/jobs/{job_id}/diagnosis")
        )

    def list_results(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/results")
        return [dict(item) for item in payload.get("items") or []]

    def get_result(self, result_ref: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/api/v1/results/{result_ref}"))

    def download_result(self, result_ref: str, destination: str | Path) -> Path:
        """Download one owner-scoped result ZIP and verify its catalog checksum."""
        metadata = self.get_result(result_ref)
        target = Path(destination)
        if target.exists() and target.is_dir():
            digest = str(metadata.get("archive_checksum") or "").removeprefix("sha256:")[:12]
            target = target / f"radar-sim-result-{digest}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        try:
            with self._client.stream("GET", f"/api/v1/results/{result_ref}/download") as response:
                self._raise_for_status(response)
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
                        digest.update(chunk)
            checksum = "sha256:" + digest.hexdigest()
            if checksum != str(metadata.get("archive_checksum") or ""):
                raise ValueError("downloaded result checksum does not match catalog")
            temporary.replace(target)
            return target
        finally:
            temporary.unlink(missing_ok=True)

    def create_artifact_upload(self, build_evidence_ref: str, *, publish_path: str = "") -> ArtifactUpload:
        return ArtifactUpload.from_dict(
            self._request(
                "POST",
                "/api/v1/artifact-uploads",
                json={"build_evidence_ref": build_evidence_ref, "publish_path": publish_path},
            )
        )

    def get_artifact_upload(self, session_id: str) -> ArtifactUpload:
        return ArtifactUpload.from_dict(self._request("GET", f"/api/v1/artifact-uploads/{session_id}"))

    def append_artifact_upload(self, session_id: str, offset: int, data: bytes) -> ArtifactUpload:
        return ArtifactUpload.from_dict(
            self._request(
                "PATCH",
                f"/api/v1/artifact-uploads/{session_id}",
                content=bytes(data),
                headers={"Upload-Offset": str(int(offset))},
            )
        )

    def finalize_artifact_upload(self, session_id: str) -> ArtifactUploadResult:
        return ArtifactUploadResult.from_dict(
            self._request("POST", f"/api/v1/artifact-uploads/{session_id}/finalize")
        )

    def upload_artifact(
        self,
        build_evidence_ref: str,
        source: str | Path,
        *,
        publish_path: str = "",
    ) -> ArtifactUploadResult:
        """Resume or complete one trusted build-artifact upload from a local file."""
        session = self.create_artifact_upload(build_evidence_ref, publish_path=publish_path)
        path = Path(source)
        with path.open("rb") as handle:
            handle.seek(session.received_bytes)
            current = session
            while current.received_bytes < current.expected_size:
                data = handle.read(min(current.chunk_size, current.expected_size - current.received_bytes))
                if not data:
                    raise ValueError("local artifact ended before the trusted build size")
                current = self.append_artifact_upload(current.session_id, current.received_bytes, data)
        return self.finalize_artifact_upload(session.session_id)

    def create_runtime_bundle_upload(self, build_evidence_ref: str, *, publish_path: str = "") -> ArtifactUpload:
        return ArtifactUpload.from_dict(
            self._request(
                "POST",
                "/api/v1/runtime-bundle-uploads",
                json={"build_evidence_ref": build_evidence_ref, "publish_path": publish_path},
            )
        )

    def list_runtime_bundles(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._request("GET", "/api/v1/runtime-bundles").get("items", [])]

    def get_runtime_bundle(self, bundle_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/api/v1/runtime-bundles/{bundle_id}"))

    def upload_config_asset(self, kind: str, source: str | Path) -> dict[str, Any]:
        """Upload one reusable Adapter or MatFilter and return its logical URI."""
        path = Path(source)
        return dict(
            self._request(
                "POST",
                "/api/v1/config-assets",
                content=path.read_bytes(),
                headers={"X-Asset-Kind": str(kind), "X-Asset-Filename": path.name},
            )
        )

    def list_config_assets(self, *, kind: str = "") -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/v1/config-assets", params={"kind": kind} if kind else None)
        return [dict(item) for item in payload.get("items", [])]

    def get_config_asset(self, asset_id: str, *, kind: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/api/v1/config-assets/{asset_id}", params={"kind": kind}))

    def download_config_asset(
        self,
        asset_id: str,
        *,
        kind: str,
        destination: str | Path,
    ) -> Path:
        """Download an Agent-authorized Adapter/MatFilter and verify its digest."""
        target = Path(destination)
        digest_text = str(asset_id or "").strip().lower()
        if digest_text.startswith("config-asset://sha256/"):
            expected_digest = digest_text.rsplit("/", 1)[-1]
        elif digest_text.startswith("config-asset:sha256:"):
            expected_digest = digest_text.rsplit(":", 1)[-1]
        else:
            raise ValueError("configuration asset id is invalid")
        if len(expected_digest) != 64 or any(ch not in "0123456789abcdef" for ch in expected_digest):
            raise ValueError("configuration asset id is invalid")
        if target.exists() and target.is_dir():
            target = target / f"{kind}-{expected_digest[:12]}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        try:
            with self._client.stream(
                "GET",
                f"/api/agents/config-assets/{asset_id}/download",
                params={"kind": kind},
            ) as response:
                self._raise_for_status(response)
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
                        digest.update(chunk)
            if digest.hexdigest() != expected_digest:
                raise ValueError("downloaded configuration asset checksum does not match its id")
            temporary.replace(target)
            return target
        finally:
            temporary.unlink(missing_ok=True)

    def get_runtime_bundle_upload(self, session_id: str) -> ArtifactUpload:
        return ArtifactUpload.from_dict(self._request("GET", f"/api/v1/runtime-bundle-uploads/{session_id}"))

    def append_runtime_bundle_upload(self, session_id: str, offset: int, data: bytes) -> ArtifactUpload:
        return ArtifactUpload.from_dict(
            self._request(
                "PATCH",
                f"/api/v1/runtime-bundle-uploads/{session_id}",
                content=bytes(data),
                headers={"Upload-Offset": str(int(offset))},
            )
        )

    def finalize_runtime_bundle_upload(self, session_id: str) -> RuntimeBundleUploadResult:
        return RuntimeBundleUploadResult.from_dict(
            self._request("POST", f"/api/v1/runtime-bundle-uploads/{session_id}/finalize")
        )

    def upload_runtime_bundle(
        self,
        build_evidence_ref: str,
        source: str | Path,
        *,
        publish_path: str = "",
    ) -> RuntimeBundleUploadResult:
        session = self.create_runtime_bundle_upload(build_evidence_ref, publish_path=publish_path)
        path = Path(source)
        with path.open("rb") as handle:
            handle.seek(session.received_bytes)
            current = session
            while current.received_bytes < current.expected_size:
                data = handle.read(min(current.chunk_size, current.expected_size - current.received_bytes))
                if not data:
                    raise ValueError("local Runtime Bundle ended before the trusted archive size")
                current = self.append_runtime_bundle_upload(current.session_id, current.received_bytes, data)
        return self.finalize_runtime_bundle_upload(session.session_id)

    def create_dataset_upload(self, project: str, files: list[dict[str, Any]]) -> DatasetUpload:
        return DatasetUpload.from_dict(
            self._request("POST", "/api/v1/dataset-uploads", json={"project": project, "files": files})
        )

    def create_run_data_upload(self, files: list[dict[str, Any]]) -> DatasetUpload:
        """Create a data upload without exposing an internal project namespace."""
        return DatasetUpload.from_dict(
            self._request("POST", "/api/v1/run-data-uploads", json={"files": files})
        )

    def upload_run_data(self, source: str | Path) -> DatasetUploadResult:
        """Upload one local data.path without exposing an internal project."""
        source_path = Path(source).expanduser()
        paths = list(iter_mf4_inputs(source_path, limit=0))
        if not paths:
            raise ValueError("no input MF4 files were found under data.path")
        root = source_path if source_path.is_dir() else source_path.parent
        local: dict[str, Path] = {}
        manifest: list[dict[str, Any]] = []
        for path in paths:
            relative = path.name if source_path.is_file() else path.relative_to(root).as_posix()
            local[relative] = path
            manifest.append(
                {
                    "relative_path": relative,
                    "size": path.stat().st_size,
                    "checksum": _sha256_path(path),
                }
            )
        current = self.create_run_data_upload(manifest)
        for upload_file in current.files:
            path = local.get(upload_file.relative_path)
            if path is None:
                raise ValueError(f"server returned an unknown upload file: {upload_file.relative_path}")
            with path.open("rb") as handle:
                handle.seek(upload_file.received_bytes)
                offset = upload_file.received_bytes
                while offset < upload_file.expected_size:
                    chunk = handle.read(min(current.chunk_size, upload_file.expected_size - offset))
                    if not chunk:
                        raise ValueError(f"local dataset file ended early: {upload_file.relative_path}")
                    current = self.append_dataset_upload(
                        current.session_id, upload_file.file_id, offset, chunk
                    )
                    state = next(item for item in current.files if item.file_id == upload_file.file_id)
                    offset = state.received_bytes
        return self.finalize_dataset_upload(current.session_id)

    def create_agent_dataset_upload(
        self,
        project: str,
        files: list[dict[str, Any]],
        *,
        evidence_ref: str,
        agent_id: str,
    ) -> DatasetUpload:
        return DatasetUpload.from_dict(
            self._request(
                "POST",
                "/api/v1/agent-dataset-uploads",
                json={"project": project, "files": files, "evidence_ref": evidence_ref},
                headers={"X-Rsim-Agent-ID": agent_id},
            )
        )

    def get_dataset_upload(self, session_id: str) -> DatasetUpload:
        return DatasetUpload.from_dict(self._request("GET", f"/api/v1/dataset-uploads/{session_id}"))

    def append_dataset_upload(
        self, session_id: str, file_id: str, offset: int, data: bytes
    ) -> DatasetUpload:
        return DatasetUpload.from_dict(
            self._request(
                "PATCH",
                f"/api/v1/dataset-uploads/{session_id}/files/{file_id}",
                content=bytes(data),
                headers={"Upload-Offset": str(int(offset))},
            )
        )

    def finalize_dataset_upload(self, session_id: str) -> DatasetUploadResult:
        return DatasetUploadResult.from_dict(
            self._request("POST", f"/api/v1/dataset-uploads/{session_id}/finalize")
        )

    def upload_dataset(self, project: str, source: str | Path) -> DatasetUploadResult:
        """Discover every input MF4, upload with resume, and return reusable data.path."""
        source_path = Path(source)
        paths = list(iter_mf4_inputs(source_path, limit=0))
        if not paths:
            raise ValueError("no input MF4 files were found")
        root = source_path if source_path.is_dir() else source_path.parent
        local: dict[str, Path] = {}
        manifest: list[dict[str, Any]] = []
        for path in paths:
            relative = path.name if source_path.is_file() else path.relative_to(root).as_posix()
            checksum = _sha256_path(path)
            local[relative] = path
            manifest.append({"relative_path": relative, "size": path.stat().st_size, "checksum": checksum})
        session = self.create_dataset_upload(project, manifest)
        current = session
        for upload_file in current.files:
            path = local.get(upload_file.relative_path)
            if path is None:
                raise ValueError(f"server returned an unknown upload file: {upload_file.relative_path}")
            with path.open("rb") as handle:
                handle.seek(upload_file.received_bytes)
                offset = upload_file.received_bytes
                while offset < upload_file.expected_size:
                    data = handle.read(min(current.chunk_size, upload_file.expected_size - offset))
                    if not data:
                        raise ValueError(f"local dataset file ended early: {upload_file.relative_path}")
                    current = self.append_dataset_upload(current.session_id, upload_file.file_id, offset, data)
                    state = next(item for item in current.files if item.file_id == upload_file.file_id)
                    offset = state.received_bytes
        return self.finalize_dataset_upload(session.session_id)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "RadarSimClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
            self._raise_for_status(response)
            return response.json() if response.content else {}
        except httpx.TransportError as exc:
            raise RadarSimTransportError(str(exc)) from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = {"code": "http_error", "message": response.text}
        if not isinstance(payload, dict):
            payload = {"code": "http_error", "message": str(payload)}
        raise RadarSimApiError.from_envelope(
            payload,
            status_code=response.status_code,
            request_id=response.headers.get("X-Request-ID", ""),
        )

    @staticmethod
    def _spec_payload(spec: SimulationSpec | dict[str, Any]) -> dict[str, Any]:
        if isinstance(spec, SimulationSpec):
            return spec.to_dict()
        payload = dict(spec)
        try:
            return SimulationSpec.from_dict(payload).to_dict()
        except Exception:
            return payload

    @staticmethod
    def _run_config_payload(config: UserRunConfig | dict[str, Any]) -> dict[str, Any]:
        if isinstance(config, UserRunConfig):
            return config.to_dict()
        payload = dict(config)
        try:
            return UserRunConfig.from_dict(payload).to_dict()
        except Exception:
            return payload


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()
