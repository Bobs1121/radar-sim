"""HTTPX-based radar-sim v5 client."""

from __future__ import annotations

import base64
from dataclasses import replace
import getpass
import ipaddress
import json
import hashlib
import socket
import ssl
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

import httpx

from core.direct_transfer import (
    TransferManifest,
    TransferPlan,
    _TransferProgressReporter,
    execute_transfer,
)
from core.transfer_service import TransferProgress
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
        trust_env: bool | None = None,
    ) -> None:
        self._owns_client = client is None
        merged_headers = dict(headers or {})
        # A no-auth Linux deployment still needs deterministic caller
        # isolation.  Without a header FastAPI falls back to the *server* OS
        # account, which merged every SDK caller into the same job/Agent
        # scope.  Keep the generated value stable for one OS user + machine so
        # reconnecting an SDK-downloaded Windows connector also keeps working
        # after process and machine restarts.
        if not any(str(key).casefold() == USER_HEADER.casefold() for key in merged_headers):
            merged_headers[USER_HEADER] = str(user or _default_sdk_user())
        if token:
            merged_headers.setdefault("Authorization", f"Bearer {token}")
        default_timeout = httpx.Timeout(timeout=60.0, connect=5.0, read=60.0, write=30.0, pool=5.0)
        if client is not None:
            if merged_headers:
                client.headers.update(merged_headers)
            self._client = client
        else:
            effective_trust_env = (
                _trust_environment_proxy(base_url) if trust_env is None else bool(trust_env)
            )
            self._client = httpx.Client(
                base_url=base_url.rstrip("/"),
                headers=merged_headers,
                timeout=timeout or default_timeout,
                verify=verify,
                transport=transport,
                trust_env=effective_trust_env,
            )

    def health(self) -> dict[str, Any]:
        """Check server health and API version."""
        return dict(self._request("GET", "/api/v1/health"))

    def download_windows_connector(
        self,
        destination: str | Path,
        *,
        mode: str = "light",
    ) -> Path:
        """Download the one-time Windows connector launcher for this SDK scope.

        The returned ``.cmd`` is intentionally not executed by the SDK.  A
        Windows integration can run it once (or hand it to its installer),
        while a Linux-only integration simply keeps using shared/Cluster paths
        and never calls this method.  The request carries the same user and
        bearer headers as normal SDK calls, so the generated launcher is bound
        to the caller's control-plane scope.
        """
        normalized_mode = str(mode or "light").strip().lower()
        if normalized_mode not in {"light", "full"}:
            raise ValueError("Windows connector mode must be 'light' or 'full'")
        target = Path(destination).expanduser()
        if target.exists() and target.is_dir():
            target = target / "RadarSim-Connect-Windows.cmd"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        try:
            with self._client.stream(
                "GET",
                "/api/v1/windows-connector/connect.cmd",
                params={"mode": normalized_mode},
            ) as response:
                self._raise_for_status(response)
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
            temporary.replace(target)
            return target
        finally:
            temporary.unlink(missing_ok=True)

    def download_windows_connector_for_run(
        self,
        config: UserRunConfig | dict[str, Any],
        destination: str | Path,
    ) -> Path:
        """Download the correct connector mode for one run configuration.

        Local simulation needs the full Windows capability.  Build/upload plus
        Cluster simulation only needs the light capability.  Callers therefore
        do not need to understand the internal full/light product vocabulary.
        """
        parsed = UserRunConfig.from_dict(self._run_config_payload(config))
        mode = "full" if parsed.simulation.target == "local" else "light"
        return self.download_windows_connector(destination, mode=mode)

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
        auto_transfer: bool = True,
        allow_local_test: bool = False,
        progress_callback: Callable[[TransferProgress], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> Job:
        parsed = UserRunConfig.from_dict(self._run_config_payload(config))
        payload, prepared_bundle_id = self._prepare_user_run(parsed, dry_run=bool(dry_run))
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        job = Job.from_dict(
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
        if not dry_run and auto_transfer:
            return self._auto_prepare_direct_transfers(
                job,
                parsed,
                allow_local_test=bool(allow_local_test),
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
        return job

    def submit_and_prepare(
        self,
        config: UserRunConfig | dict[str, Any],
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
        allow_local_test: bool = False,
        progress_callback: Callable[[TransferProgress], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> Job:
        """Submit a run and, when possible, execute its local direct transfers.

        ``submit_run`` already enables this behavior by default.  The named
        method is provided for callers that want to make the data-plane side
        effect explicit at the call site.
        """
        return self.submit_run(
            config,
            dry_run=dry_run,
            idempotency_key=idempotency_key,
            auto_transfer=True,
            allow_local_test=allow_local_test,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

    def _auto_prepare_direct_transfers(
        self,
        job: Job,
        config: UserRunConfig,
        *,
        allow_local_test: bool,
        progress_callback: Callable[[TransferProgress], None] | None,
        cancel_check: Callable[[], bool] | None,
    ) -> Job:
        """Execute readable local inputs for a persisted direct-transfer stage.

        The public Job response intentionally hides source paths.  The SDK
        already owns the parsed YAML, so it derives the same role-to-path
        mapping locally, sends only metadata to the control plane, and copies
        bytes directly to each signed target root.  A missing Connector or an
        inaccessible target is represented as a path-free ``needs-agent``
        waiting hint; no HTTP request containing file bytes is attempted.
        """

        selected_target = str(
            ((job.resolved_spec.get("decisions") or {}).get("execution") or {}).get("selected_target")
            or config.simulation.target
            or ""
        ).strip().lower()
        if selected_target != "cluster":
            return job
        stage = next(
            (
                item
                for item in job.stages
                if str(item.get("stage_type") or item.get("task_type") or "") == "prepare_data"
            ),
            None,
        )
        if not stage:
            return job
        stage_status = str(stage.get("status") or "").strip().lower()
        if stage_status in {"skipped", "blocked", "failed", "succeeded", "cancelled"}:
            return job
        stage_id = str(stage.get("stage_id") or stage.get("task_id") or "").strip()
        if not stage_id:
            return self._direct_transfer_waiting(
                job,
                code="direct_transfer_stage_unavailable",
                message="The Cluster direct-transfer Stage is not available yet; connect the source Agent and retry.",
            )

        sources = _sdk_local_transfer_sources(config)
        if not sources:
            # Shared/dataset references and build outputs that do not yet
            # exist on this process are intentionally left for the scheduler
            # or a Windows Agent.  This is a successful no-op, not an upload.
            return job

        fingerprints = {"config_fingerprint": config.fingerprint()}
        for source_role, source_path in sources:
            try:
                source_root, items = _scan_sdk_transfer_items(source_path, source_role=source_role)
                plan = self.issue_transfer_plan(
                    job_id=job.id,
                    stage_id=stage_id,
                    source_role=source_role,
                    items=items,
                    source_fingerprints=fingerprints,
                )
                self.execute_transfer_plan(
                    plan,
                    source_root,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                    allow_local_test=allow_local_test,
                )
            except Exception as exc:
                # Keep the server-side Stage queued/running.  The control
                # plane remains the source of truth and a later Connector can
                # resume with a fresh plan; this process never falls back to
                # a Linux body upload.
                code = str(getattr(exc, "code", "") or "cluster_direct_transfer_unavailable")
                if code not in {
                    "cluster_direct_transfer_unavailable",
                    "transfer_io_error",
                    "direct_transfer_stage_unavailable",
                    "source_changed_during_transfer",
                    "transfer_plan_expired",
                    "transfer_cancelled",
                }:
                    code = "cluster_direct_transfer_unavailable"
                return self._direct_transfer_waiting(
                    job,
                    code=code,
                    message="Local input transfer is waiting for a connected Agent or an accessible Cluster target.",
                )

        try:
            return self.get_job(job.id)
        except (RadarSimApiError, RadarSimTransportError):
            # A successful manifest is durable even if the refresh races a
            # transient control-plane outage.  Return the submitted object so
            # callers can poll explicitly.
            return job

    @staticmethod
    def _direct_transfer_waiting(job: Job, *, code: str, message: str) -> Job:
        waiting = dict(job.waiting or {})
        waiting.update(
            {
                "reason": "needs-agent",
                "code": str(code or "cluster_direct_transfer_unavailable"),
                "missing_capability": "client_target_root",
                "message": str(message),
                "action": {
                    "type": "configure_direct_transfer",
                    "label": "Mount the Cluster share or connect the computer that owns the files",
                },
            }
        )
        actions = list(job.available_actions or [])
        action = dict(waiting["action"])
        if action not in actions:
            actions.append(action)
        return replace(job, waiting=waiting, available_actions=actions)

    # ------------------------------------------------------------------
    # Data-plane adapter
    # ------------------------------------------------------------------
    # These methods intentionally live beside the HTTP client instead of in
    # the scheduler.  Linux receives only plan/progress/manifest metadata;
    # ``execute_transfer_plan`` writes bytes directly to the signed target
    # root and never sends a file body through ``_request``.

    def issue_transfer_plan(
        self,
        *,
        job_id: str,
        stage_id: str,
        mode: str = "shared_copy",
        source_role: str,
        items: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        source_fingerprints: dict[str, Any] | None = None,
        ttl_seconds: float | None = None,
    ) -> TransferPlan:
        """Ask the control plane for one owner/job/stage-bound plan.

        ``owner`` and both physical roots are deliberately absent from the
        request.  The authenticated SDK identity selects the owner and the
        deployment selects the target root.
        """
        payload = {
            "source_role": str(source_role),
            "items": [dict(item) for item in items],
            "source_fingerprints": dict(source_fingerprints or {}),
        }
        response = self._request(
            "POST",
            f"/api/v1/jobs/{_quote_path_token(job_id)}/stages/{_quote_path_token(stage_id)}/transfers",
            json=payload,
        )
        raw = dict(response.get("plan") or response)
        return TransferPlan.from_dict(raw)

    def get_transfer_plan(self, transfer_id: str) -> TransferPlan:
        response = self._request(
            "GET", f"/api/v1/transfers/{_quote_path_token(transfer_id)}"
        )
        return TransferPlan.from_dict(dict(response.get("plan") or response))

    def report_transfer_progress(self, progress: TransferProgress | dict[str, Any]) -> dict[str, Any]:
        value = progress if isinstance(progress, TransferProgress) else TransferProgress(**dict(progress))
        response = self._request(
            "POST",
            f"/api/v1/transfers/{_quote_path_token(value.transfer_id)}/progress",
            json={
                "bytes_transferred": int(value.bytes_transferred),
                "bytes_total": int(value.bytes_total),
                "current_file": value.current_file,
                "status": value.status,
            },
        )
        return dict(response)

    def report_transfer_manifest(self, manifest: TransferManifest | dict[str, Any]) -> dict[str, Any]:
        value = manifest if isinstance(manifest, TransferManifest) else TransferManifest.from_dict(dict(manifest))
        payload = value.to_dict()
        # transfer_id/owner are bound by the URL and authenticated identity;
        # keeping them out of the body avoids a second client-selected scope.
        payload.pop("transfer_id", None)
        payload.pop("owner", None)
        response = self._request(
            "POST",
            f"/api/v1/transfers/{_quote_path_token(value.transfer_id)}/manifest",
            json=payload,
        )
        return dict(response)

    def cancel_transfer(self, transfer_id: str) -> dict[str, Any]:
        return dict(
            self._request(
                "POST",
                f"/api/v1/transfers/{_quote_path_token(transfer_id)}/cancel",
            )
        )

    def execute_transfer_plan(
        self,
        plan: TransferPlan | dict[str, Any],
        source_root: str | Path,
        *,
        progress_callback: Callable[[TransferProgress], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        chunk_size: int = 1024 * 1024,
        allow_local_test: bool = False,
    ) -> TransferManifest:
        """Execute a signed plan in the caller's data plane.

        This is intentionally a thin wrapper over ``core.direct_transfer``.
        It can be used by a Windows Agent or a Linux SDK process that has a
        mounted Cluster share.  Callers without such access should leave the
        plan pending and surface ``needs-agent`` instead of uploading bytes
        to this SDK's HTTP endpoint.  ``progress_callback`` receives every
        chunk-level local progress event; control-plane ``/progress`` posts
        are throttled independently and always finish with a verified total.
        """
        signed = plan if isinstance(plan, TransferPlan) else TransferPlan.from_dict(dict(plan))
        per_file: dict[str, int] = {}
        total = sum(item.size for item in signed.items)

        def publish(progress: TransferProgress) -> None:
            # Progress is metadata-only and is throttled by the reporter; the
            # copy/checksum loop itself remains chunk-granular.
            self.report_transfer_progress(progress)

        reporter = _TransferProgressReporter(publish, progress_callback)

        def report(relative_path: str, processed: int, file_total: int) -> None:
            per_file[relative_path] = int(processed)
            progress = TransferProgress(
                signed.transfer_id,
                sum(per_file.values()),
                total,
                relative_path,
                updated_at=time.time(),
                owner_scope=signed.owner_scope,
            )
            reporter.emit(progress)

        manifest = execute_transfer(
            signed,
            Path(source_root).expanduser(),
            signed.items,
            client_target_root=signed.client_target_root,
            allow_local_test=bool(allow_local_test),
            cancel_callback=cancel_check,
            progress_callback=report,
            chunk_size=int(chunk_size),
        )
        final_file = (
            manifest.entries[-1].relative_path
            if manifest.entries
            else (signed.items[-1].relative_path if signed.items else "")
        )
        reporter.finish(
            TransferProgress(
                signed.transfer_id,
                total,
                total,
                final_file,
                updated_at=time.time(),
                owner_scope=signed.owner_scope,
            )
        )
        self.report_transfer_manifest(manifest)
        return manifest

    def submit_cluster_yaml(
        self,
        yaml_path: str | Path,
        *,
        idempotency_key: str | None = None,
        auto_transfer: bool = True,
        allow_local_test: bool = False,
        progress_callback: Callable[[TransferProgress], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> Job:
        """Submit the V1 existing-Selena + Cluster flow from one YAML file."""
        config = UserRunConfig.from_yaml(Path(yaml_path))
        if config.selena.source != "existing" or config.simulation.target != "cluster":
            raise ValueError(
                "V1 submit_cluster_yaml requires selena.source=existing and simulation.target=cluster"
            )
        return self.submit_run(
            config,
            idempotency_key=idempotency_key,
            auto_transfer=auto_transfer,
            allow_local_test=allow_local_test,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )

    def submit_yaml(
        self,
        yaml_path: str | Path,
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
        auto_transfer: bool = True,
        allow_local_test: bool = False,
        progress_callback: Callable[[TransferProgress], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> Job:
        """Submit any supported build/existing and local/Cluster YAML in one call."""
        return self.submit_run(
            UserRunConfig.from_yaml(Path(yaml_path)),
            dry_run=dry_run,
            idempotency_key=idempotency_key,
            auto_transfer=auto_transfer,
            allow_local_test=allow_local_test,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
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

        # A local path is a data-plane source, never an implicit Linux upload.
        # The control plane creates owner/job/stage-bound TransferPlans and a
        # Windows Agent (or a caller with a mounted Cluster share) executes
        # them.  Keeping the user paths in this payload also makes Web and SDK
        # route the same YAML through the same scheduler decisions.
        #
        # ``prepared_runtime_bundle_id`` remains an optional compatibility
        # field for already-registered logical references, but this method no
        # longer manufactures one by archiving a local Selena directory.
        return UserRunConfig.from_dict(payload).to_dict(), ""

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

    def create_result_upload(
        self,
        run_ref: str,
        *,
        archive_size: int,
        archive_checksum: str,
    ) -> dict[str, Any]:
        """Create a resumable upload for a Windows-local result archive."""
        return dict(
            self._request(
                "POST",
                "/api/v1/result-uploads",
                json={
                    "run_ref": str(run_ref),
                    "archive_size": int(archive_size),
                    "archive_checksum": str(archive_checksum),
                },
            )
        )

    def get_result_upload(self, session_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/api/v1/result-uploads/{session_id}"))

    def append_result_upload(self, session_id: str, offset: int, data: bytes) -> dict[str, Any]:
        return dict(
            self._request(
                "PATCH",
                f"/api/v1/result-uploads/{session_id}",
                content=bytes(data),
                headers={"Upload-Offset": str(int(offset))},
            )
        )

    def finalize_result_upload(
        self,
        session_id: str,
        *,
        files: list[dict[str, Any]],
        retain_until: float = 0,
    ) -> dict[str, Any]:
        return dict(
            self._request(
                "POST",
                f"/api/v1/result-uploads/{session_id}/finalize",
                json={"files": list(files), "retain_until": float(retain_until or 0)},
            )
        )

    def upload_result_archive(
        self,
        source: str | Path,
        *,
        run_ref: str,
        files: list[dict[str, Any]],
        retain_until: float = 0,
    ) -> dict[str, Any]:
        """Transfer one immutable local result ZIP and register its evidence."""
        path = Path(source).expanduser()
        if not path.is_file() or path.is_symlink():
            raise ValueError("result archive is unavailable")
        size = int(path.stat().st_size)
        checksum = _sha256_path(path)
        current = self.create_result_upload(
            run_ref,
            archive_size=size,
            archive_checksum=checksum,
        )
        session_id = str(current.get("session_id") or "")
        if not session_id:
            raise ValueError("result upload session is unavailable")
        received = int(current.get("received_bytes") or 0)
        chunk_size = max(1, int(current.get("chunk_size") or 4 * 1024 * 1024))
        with path.open("rb") as handle:
            handle.seek(received)
            while received < size:
                data = handle.read(min(chunk_size, size - received))
                if not data:
                    raise ValueError("local result archive ended before the expected size")
                current = self.append_result_upload(session_id, received, data)
                received = int(current.get("received_bytes") or 0)
        return self.finalize_result_upload(
            session_id,
            files=files,
            retain_until=retain_until,
        )

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
        except (json.JSONDecodeError, httpx.ResponseNotRead):
            # A streaming response has not been read yet; read it first so the
            # error body is available instead of masking the real status with a
            # ResponseNotRead traceback.
            try:
                response.read()
                payload = response.json()
            except (json.JSONDecodeError, httpx.ResponseNotRead):
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


def _default_sdk_user() -> str:
    """Return one stable, opaque no-config identity per OS user and machine."""
    try:
        login = getpass.getuser()
    except Exception:
        login = ""
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    seed = f"{login.strip().casefold()}@{hostname.strip().casefold()}" or "radar-sim-sdk"
    digest = hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()
    return f"sdk-{digest[:24]}"


def _sdk_local_transfer_sources(config: UserRunConfig) -> list[tuple[str, Path]]:
    """Return readable config inputs that can be copied by this SDK process.

    ``shared://``/UNC references stay zero-copy.  Other existing paths are
    considered candidates when the direct-transfer Stage is queued; the
    Stage's server-side role allow-list remains authoritative when a plan is
    issued.
    """

    candidates: list[tuple[str, str]] = [("dataset", str(config.data.path or ""))]
    if config.selena.source == "existing":
        candidates.append(("runtime_bundle", str(config.selena.existing_path or "")))
    candidates.extend(
        [
            ("runtime_xml", str(config.selena.runtime_xml or "")),
            ("mat_filter", str(config.simulation.mat_filter or "")),
            ("adapter", str(config.simulation.adapter_file or "")),
        ]
    )
    result: list[tuple[str, Path]] = []
    seen: set[tuple[str, str]] = set()
    for role, raw in candidates:
        value = str(raw or "").strip()
        if not value or value.casefold().startswith(("shared://", "dataset://")):
            continue
        # UNC/shared paths belong to the deployment namespace even if this
        # process happens to have a mapped view of them.
        if classify_data_path(value) == "shared":
            continue
        source = Path(value).expanduser()
        try:
            if not source.exists() or source.is_symlink():
                continue
            resolved = source.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if not resolved.is_file() and not resolved.is_dir():
            continue
        key = (role, str(resolved).casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append((role, resolved))
    return result


def _scan_sdk_transfer_items(
    source_path: Path,
    *,
    source_role: str,
) -> tuple[Path, list[dict[str, Any]]]:
    """Scan one local source into metadata-only plan items.

    Checksums deliberately remain empty.  The direct-transfer kernel computes
    SHA-256 while streaming each file to the signed target, avoiding a second
    full read for large MF4/Selena binaries.
    """

    original = Path(source_path).expanduser()
    if original.is_symlink():
        raise ValueError("direct transfer source is a symlink")
    path = original.resolve(strict=True)
    if path.is_dir():
        root = path
        paths = [
            item
            for item in sorted(root.rglob("*"), key=lambda candidate: candidate.as_posix().casefold())
            if item.is_file() and not item.is_symlink()
        ]
        if source_role == "dataset":
            paths = [item for item in paths if item.suffix.casefold() == ".mf4"]
    elif path.is_file():
        root = path.parent
        paths = [path]
        if source_role == "dataset" and path.suffix.casefold() != ".mf4":
            raise ValueError("dataset source does not contain an MF4 file")
    else:
        raise ValueError("direct transfer source is unavailable")
    if not paths:
        raise ValueError("direct transfer source is empty")
    items: list[dict[str, Any]] = []
    for item in paths:
        relative = item.relative_to(root).as_posix()
        stat = item.stat()
        items.append(
            {
                "source_role": str(source_role),
                "relative_path": relative,
                "size": int(stat.st_size),
                "checksum": "",
                "mtime_ns": int(stat.st_mtime_ns),
            }
        )
    return root, items


def _quote_path_token(value: str) -> str:
    """Quote an opaque route token without treating slashes as hierarchy."""
    from urllib.parse import quote

    text = str(value or "").strip()
    if not text:
        raise ValueError("route token is required")
    return quote(text, safe="")


def _trust_environment_proxy(base_url: str) -> bool:
    """Use enterprise proxy settings except for literal local/private hosts.

    Corporate environments commonly export HTTP_PROXY without adding every
    lab subnet to NO_PROXY.  Sending multi-GB MF4 uploads through that proxy
    made a working SDK appear stalled.  An explicit ``trust_env=True`` still
    lets an integration override this safe automatic default.
    """
    hostname = (urlsplit(str(base_url or "")).hostname or "").strip()
    if not hostname:
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname.casefold() not in {"localhost"}
    return not (address.is_private or address.is_loopback or address.is_link_local)


def _should_upload_client_data(
    raw_path: str,
    local_path: Path,
    *,
    data_kind: str,
) -> bool:
    """Identify caller-local data without copying shared Cluster namespaces.

    The syntax-only classifier treats POSIX absolute paths as ``central``
    because the Linux control plane may receive deployment mount paths.  The
    SDK also runs on ordinary Linux callers, where a readable absolute path is
    local input. A separate filesystem mount remains central/shared; a path on
    the caller's root filesystem is uploaded.
    """
    text = str(raw_path or "").strip()
    if (
        not text
        or text.lower().startswith("dataset://")
        or data_kind == "shared"
        or not local_path.exists()
    ):
        return False
    if data_kind != "central":
        return True
    return not _is_separate_mount(local_path)


def _is_separate_mount(path: Path) -> bool:
    """Return whether ``path`` lives below a non-root mount point."""
    try:
        probe = path.resolve(strict=True)
    except (OSError, ValueError):
        probe = path.absolute()
    if probe.is_file():
        probe = probe.parent
    anchor = Path(probe.anchor)
    while probe != probe.parent:
        if probe != anchor:
            try:
                if probe.is_mount():
                    return True
            except OSError:
                return False
        probe = probe.parent
    return False


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()
