"""rsim agent - minimal Windows-friendly polling agent for control jobs."""

from __future__ import annotations

import json
import hashlib
import os
import platform as platform_mod
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Agent doesn't need project config at startup; it gets project from task payloads.
NO_CONFIG = True

# Windows Agent deployment modes (v5 contract, PRD §14.4 / DETAILED_DESIGN §4.4).
#
#   --windows-mode light  (default) -> node_kind=windows_agent
#     Authorized workspace Selena compile, artifact register/validate/upload
#     and local data inspect/validate/upload only. Never declares or receives
#     local simulation, cluster simulation/gateway, cluster run/collect/
#     finalize or the legacy cluster.run.
#
#   --windows-mode full          -> node_kind=windows_full
#     Additionally performs local compile + local simulation. Distinct from
#     platform_gateway; does not receive cluster.gateway.
#
# Legacy Mode A/B capability tuning is replaced by the mode policy. Explicit
# --capability flags still override the default set, but a light agent that
# requests a forbidden capability fails fast (see core.agent_policy).
from core.agent_policy import (
    AgentPolicyError,
    DEFAULT_FULL_CAPABILITIES,
    DEFAULT_LIGHT_CAPABILITIES,
    MODE_FULL,
    MODE_LIGHT,
    NODE_KIND_LEGACY,
    NODE_KIND_WINDOWS_AGENT,
    WINDOWS_MODES,
    default_capabilities_for_mode,
    may_claim_task,
    node_kind_for_mode,
    normalize_capabilities,
    normalize_windows_mode,
    validate_light_capabilities,
)
from core.progress_parser import parse_build_percentage, parse_build_progress

# Default advertised capabilities for the default (light) mode. Kept as a
# module name for backward-compatible imports (e.g. the embedded web agent).
DEFAULT_CAPABILITIES = list(DEFAULT_LIGHT_CAPABILITIES)

# Full capability set for --windows-mode full (Windows full deployment).
FULL_CAPABILITIES = list(DEFAULT_FULL_CAPABILITIES)


def _capabilities_for_mode(mode: object, explicit: object = None) -> tuple[str, str, list[str]]:
    normalized_mode = normalize_windows_mode(mode)
    node_kind = node_kind_for_mode(normalized_mode)
    capabilities = (
        normalize_capabilities(explicit)
        if explicit
        else default_capabilities_for_mode(normalized_mode)
    )
    if node_kind == NODE_KIND_WINDOWS_AGENT:
        capabilities = validate_light_capabilities(capabilities)
    else:
        unsupported = sorted(set(capabilities) - set(DEFAULT_FULL_CAPABILITIES))
        if unsupported:
            raise AgentPolicyError(
                "windows_full node may not declare unsupported capability: "
                + ", ".join(unsupported)
            )
    return normalized_mode, node_kind, capabilities


def register(subparsers):
    parser = subparsers.add_parser("agent", help="Run a polling Windows agent for control jobs")
    parser.add_argument("--server-url", default="http://127.0.0.1:8877", help="Base URL for the control server")
    parser.add_argument(
        "--api-url",
        default="",
        help="Explicit v1 API base URL used for artifact/data uploads (for example http://server:8878)",
    )
    parser.add_argument(
        "--agent-token",
        default="",
        help="Bearer token for Agent control endpoints (or set RSIM_AGENT_TOKEN)",
    )
    parser.add_argument(
        "--api-token",
        default="",
        help="User Bearer token for owner-scoped v1 uploads (or set RSIM_API_TOKEN)",
    )
    parser.add_argument("--agent-id", default="", help="Stable agent id; omit to auto-register a new one")
    parser.add_argument("--name", default="", help="Agent display name")
    parser.add_argument("--hostname", default=socket.gethostname(), help="Agent hostname")
    parser.add_argument("--platform", dest="platform_name", default=platform_mod.platform(), help="Agent platform string")
    parser.add_argument(
        "--windows-mode",
        choices=sorted(WINDOWS_MODES),
        default=MODE_LIGHT,
        help="Windows deployment mode: 'light' (default, windows_agent) does authorized "
        "compile/artifact/data work only; 'full' (windows_full) additionally allows local "
        "compile + local simulation.",
    )
    parser.add_argument(
        "--capability",
        action="append",
        default=[],
        help="Repeatable task capability filter. Overrides the mode default. In light mode, "
        "forbidden capabilities (local simulation, cluster simulation/gateway, cluster "
        "run/collect/finalize, legacy cluster.run, and bypass wildcards) cause a fast failure.",
    )
    parser.add_argument("--poll-interval", type=float, default=3.0, help="Seconds between polls when idle")
    parser.add_argument("--heartbeat-interval", type=float, default=10.0, help="Seconds between heartbeats during task execution")
    parser.add_argument("--request-timeout", type=int, default=30, help="HTTP request timeout in seconds")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")


def run(args, config):
    import os
    from core.user import current_user
    user = current_user()
    mode, node_kind, capabilities = _capabilities_for_mode(
        getattr(args, "windows_mode", MODE_LIGHT),
        getattr(args, "capability", None),
    )
    client = _ControlClient(
        getattr(args, "server_url", "http://127.0.0.1:8877"),
        timeout=int(getattr(args, "request_timeout", 30) or 30),
        api_url=str(getattr(args, "api_url", "") or ""),
        token=str(getattr(args, "agent_token", "") or os.environ.get("RSIM_AGENT_TOKEN", "")),
        api_token=str(getattr(args, "api_token", "") or os.environ.get("RSIM_API_TOKEN", "")),
    )
    hostname = getattr(args, "hostname", "") or socket.gethostname()
    name = getattr(args, "name", "") or f"{hostname}-agent"
    # Default agent_id embeds user+hostname so two users on one machine don't collide.
    default_agent_id = f"agent-{user}-{hostname}"
    workspace_bindings = _public_workspace_bindings()
    data_bindings = _public_data_bindings()
    asset_bindings = _public_asset_bindings()
    agent = client.register_agent(
        name=name,
        agent_id=getattr(args, "agent_id", "") or default_agent_id,
        hostname=hostname,
        platform=getattr(args, "platform_name", "") or platform_mod.platform(),
        capabilities=capabilities,
        metadata={
            "user": user,
            "node_kind": node_kind,
            "windows_mode": mode,
            "auto_configure": True,
            "workspace_bindings": workspace_bindings,
            "data_bindings": data_bindings,
            "asset_bindings": asset_bindings,
        },
    )
    agent_id = agent["agent_id"]
    poll_interval = float(getattr(args, "poll_interval", 3.0) or 3.0)
    once = bool(getattr(args, "once", False))
    while True:
        try:
            claim = client.poll(agent_id)
        except Exception as exc:
            print(f"[WARN] agent poll failed: {exc}", file=sys.stderr)
            if once:
                return 1
            time.sleep(poll_interval)
            continue
        task = claim.get("task")
        if not task:
            if once:
                return 0
            time.sleep(poll_interval)
            continue
        exit_code = _run_task(
            client,
            agent_id,
            task,
            heartbeat_interval=float(getattr(args, "heartbeat_interval", 10.0) or 10.0),
            node_kind=node_kind,
        )
        if once:
            return exit_code


def _run_task(
    client: "_ControlClient",
    agent_id: str,
    task: dict,
    *,
    heartbeat_interval: float,
    node_kind: str = NODE_KIND_LEGACY,
) -> int:
    task_id = task["task_id"]
    is_v2_resolution = str(task.get("task_type") or "") == "resolve_spec"
    is_v5_build = str(task.get("task_type") or "") == "build_selena"
    is_v5_environment = str(task.get("task_type") or "") == "environment_check"
    is_runtime_bundle_cache = (
        is_v5_environment
        and str((task.get("payload") or {}).get("dispatch_scope") or "") == "runtime_bundle_cache"
    )
    is_v5_source = str(task.get("task_type") or "") == "prepare_source"
    is_v5_register = str(task.get("task_type") or "") == "register_artifact"
    is_v5_data = str(task.get("task_type") or "") == "prepare_data"
    is_v5_local_stage = (
        str(task.get("task_type") or "") in {"preflight", "run_simulation", "collect_results", "finalize_manifest"}
        and str((task.get("payload") or {}).get("dispatch_scope") or "") == "local_simulation"
    )
    prepared_build = None
    command_cwd = ROOT
    try:
        if not may_claim_task(node_kind, task.get("task_type"), task.get("stage_type")):
            raise AgentPolicyError("agent node policy forbids this task type")
        if is_v2_resolution:
            resolution_source = str((task.get("payload") or {}).get("source") or "build")
            recognition = (
                _resolve_existing_v2_run_config(task)
                if resolution_source == "existing"
                else _resolve_v2_run_config(dict(task.get("payload") or {}))
            )
            client.heartbeat(
                agent_id,
                status="busy",
                current_task_id=task_id,
                metadata={
                    "workspace_bindings": _public_workspace_bindings(),
                    "data_bindings": _public_data_bindings(),
                    "asset_bindings": _public_asset_bindings(),
                },
            )
            client.append_logs(
                task_id,
                [
                    "[agent] existing Selena folder, DLL dependencies and Runtime XML validated"
                    if resolution_source == "existing"
                    else "[agent] workspace and dependencies configured"
                ],
            )
            client.submit_result(
                task_id,
                agent_id=agent_id,
                status="succeeded",
                returncode=0,
                result={"recognition": recognition},
            )
            return 0
        if is_v5_environment:
            if is_runtime_bundle_cache:
                cached = _execute_v5_runtime_bundle_cache(task, client=client)
                client.append_logs(task_id, ["[agent] existing Runtime Bundle cached and verified"])
                client.submit_result(
                    task_id,
                    agent_id=agent_id,
                    status="succeeded",
                    returncode=0,
                    result=cached,
                )
                return 0
            snapshot = _check_v5_environment(
                dict(task.get("payload") or {}),
                agent_id=agent_id,
                node_kind=node_kind,
            )
            client.append_logs(task_id, ["[agent] node-local environment check completed"])
            client.submit_result(
                task_id,
                agent_id=agent_id,
                status="succeeded" if snapshot.get("status") == "ready" else "failed",
                returncode=0 if snapshot.get("status") == "ready" else 1,
                result={"environment_snapshot": snapshot},
            )
            return 0 if snapshot.get("status") == "ready" else 1
        if is_v5_source:
            source = _prepare_v5_branch_source(task)
            client.append_logs(task_id, ["[agent] isolated Selena branch source prepared"])
            client.submit_result(
                task_id,
                agent_id=agent_id,
                status="succeeded",
                returncode=0,
                result={"source_lease": source},
            )
            return 0
        if is_v5_register:
            return _run_v5_register_artifact(
                client,
                agent_id,
                task,
                heartbeat_interval=heartbeat_interval,
            )
        if is_v5_data:
            return _run_v5_prepare_data(
                client,
                agent_id,
                task,
                heartbeat_interval=heartbeat_interval,
            )
        if is_v5_local_stage:
            return _run_v5_local_stage(
                client,
                agent_id,
                task,
                heartbeat_interval=heartbeat_interval,
            )
        if is_v5_build:
            prepared_build = _prepare_v5_selena_build(dict(task.get("payload") or {}))
            command = list(prepared_build.command)
            command_cwd = prepared_build.cwd
        else:
            command = _build_task_command(task)
    except (KeyError, TypeError, ValueError, FileNotFoundError, OSError) as exc:
        # FileNotFoundError / OSError: agent has no local config or repo for
        # the requested project. Report as failed so the task doesn't stay
        # stuck in "running" forever (PRD §12 agent resilience).
        message = (
            "[agent] Runtime Bundle cache failed"
            if is_runtime_bundle_cache
            else f"[agent] task setup error: {exc}"
        )
        client.append_logs(task_id, [message])
        client.submit_result(
            task_id,
            agent_id=agent_id,
            status="failed",
            returncode=-1,
            result=(
                {"error": "runtime_bundle_cache_failed", "code": "runtime_bundle_cache_failed"}
                if is_runtime_bundle_cache
                else {"error": str(exc)}
                if (is_v2_resolution or is_v5_build or is_v5_environment or is_v5_source or is_v5_register or is_v5_data or is_v5_local_stage)
                else {"cwd": str(ROOT), "error": str(exc)}
            ),
        )
        return 1
    start_logs = [f"[agent] starting {task['task_type']}"]
    if is_v5_build:
        start_logs.append("[agent] authorized Selena build command prepared")
    else:
        start_logs.append(f"[agent] command: {_quote_command(command)}")
    client.append_logs(task_id, start_logs)
    cancel_event = threading.Event()
    stop_event = threading.Event()

    def heartbeat_loop() -> None:
        while not stop_event.wait(max(1.0, heartbeat_interval)):
            try:
                response = client.heartbeat(agent_id, status="busy", current_task_id=task_id)
                if response.get("cancel_requested"):
                    cancel_event.set()
            except Exception:
                pass

    thread = threading.Thread(target=heartbeat_loop, daemon=True)
    thread.start()
    status = "failed"
    returncode = None
    execution_error = ""
    lines: list[str] = []
    last_reported_progress = 0.0
    last_progress_report_at = 0.0
    try:
        if prepared_build is not None:
            _verify_v5_selena_build(prepared_build)
        proc = subprocess.Popen(
            command,
            cwd=str(command_cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            # Force the child (rsim build/run/cluster) to also emit UTF-8 so
            # Chinese-Windows cp936/gbk compiler output doesn't get garbled when
            # we decode it as utf-8 above. Cross-machine: logs land on the Linux
            # server, so a stable encoding matters.
            env=_child_env_utf8(),
        )
        client.heartbeat(agent_id, status="busy", current_task_id=task_id)
        assert proc.stdout is not None
        # Stream stdout to the server via a background reader thread feeding a
        # queue. We can't read proc.stdout directly in the main loop because
        # some children (notably selena.exe) leave a descendant process holding
        # the stdout pipe open after the main process exits — readline() would
        # then block forever and the task would never complete. The queue +
        # timeout lets the main loop notice proc.poll() (process exited) and
        # stop waiting on the pipe, so the task finishes even if a descendant
        # holds the write end. A few trailing buffered lines may be dropped in
        # that case; a stuck task is worse.
        out_queue: "queue.Queue[str | None]" = queue.Queue()

        def _reader() -> None:
            try:
                for line in proc.stdout:
                    out_queue.put(line)
            finally:
                out_queue.put(None)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()
        while True:
            try:
                line = out_queue.get(timeout=0.5)
            except queue.Empty:
                line = None
            if line is not None:
                text = line.rstrip()
                if text:
                    lines.append(text)
                    if is_v5_build:
                        progress_value, progress_label = _build_progress_from_output(text)
                        now = time.monotonic()
                        if (
                            progress_value is not None
                            and progress_value > last_reported_progress
                            and (
                                progress_value - last_reported_progress >= 0.005
                                or now - last_progress_report_at >= 5.0
                            )
                        ):
                            try:
                                client.report_progress(
                                    task_id,
                                    min(progress_value, 0.99),
                                    message=progress_label,
                                )
                                last_reported_progress = progress_value
                                last_progress_report_at = now
                            except Exception:
                                # Progress is advisory. Logs, heartbeat and the
                                # terminal result remain authoritative.
                                pass
                if len(lines) >= 20:
                    client.append_logs(task_id, lines)
                    lines = []
            if cancel_event.is_set():
                proc.terminate()
                break
            if proc.poll() is not None:
                # Main child exited. Drain whatever the reader has already
                # queued (non-blocking), then stop — don't block on a pipe a
                # descendant may still hold.
                while True:
                    try:
                        line = out_queue.get_nowait()
                    except queue.Empty:
                        break
                    if line is None:
                        break
                    text = line.rstrip()
                    if text:
                        lines.append(text)
                break
        if lines:
            client.append_logs(task_id, lines)
        try:
            proc.stdout.close()
        except Exception:
            pass
        if cancel_event.is_set():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            status = "cancelled"
        else:
            # poll() already returned the exit code; wait() returns it again
            # immediately. Guard with a timeout so a lingering descendant
            # process can't hang the agent.
            try:
                returncode = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait(timeout=10)
            status = "succeeded" if returncode == 0 else "failed"
    except Exception as exc:
        if "proc" in locals() and proc.poll() is None:
            proc.terminate()
        execution_error = "v5 Selena build execution failed" if is_v5_build else str(exc)
        client.append_logs(task_id, [f"[agent] execution error: {execution_error}"])
        status = "failed"
        returncode = returncode if returncode is not None else -1
    finally:
        stop_event.set()
        thread.join(timeout=max(1.0, heartbeat_interval))

    if returncode is None:
        returncode = proc.returncode if "proc" in locals() and proc.returncode is not None else (-15 if status == "cancelled" else -1)
    if is_v5_build:
        if status == "succeeded" and prepared_build is not None:
            try:
                result = _finish_v5_selena_build(
                    prepared_build,
                    build_stage_id=task_id,
                    build_attempt=int(task.get("attempt_count") or 0),
                )
                if str(getattr(prepared_build, "contract", "") or "") == "user-run-config/2.0":
                    # Runtime Bundle archive is the transport; never retain a
                    # bare executable lease for the v2 product contract.
                    result["artifact_lease_ref"] = "runtime-bundle-transport"
                else:
                    lease = _create_v5_artifact_lease(
                        prepared_build,
                        result,
                        build_stage_id=task_id,
                        build_attempt=int(task.get("attempt_count") or 0),
                    )
                    result["artifact_lease_ref"] = lease["lease_id"]
            except Exception:
                status = "failed"
                returncode = -1
                execution_error = "v5 Selena build evidence finalization failed"
                client.append_logs(task_id, [f"[agent] execution error: {execution_error}"])
                result = {"error": execution_error}
        else:
            result = {"error": execution_error or "v5 Selena build failed"}
    else:
        result = {
            "command": command,
            "cwd": str(ROOT),
        }
    source_lease_ref = str(getattr(prepared_build, "source_lease_ref", "") or "")
    if source_lease_ref:
        try:
            _release_v5_source_lease(source_lease_ref)
            client.append_logs(task_id, ["[agent] isolated Selena source worktree released"])
        except Exception:
            client.append_logs(task_id, ["[agent] isolated source cleanup is pending; bundle evidence is retained"])
    client.submit_result(task_id, agent_id=agent_id, status=status, returncode=returncode, result=result)
    return 0 if status == "succeeded" else 1


def _build_progress_from_output(line: str) -> tuple[float | None, str]:
    """Return normalized Selena build progress from one compiler output line."""
    counted = parse_build_progress(line)
    if counted is not None:
        done, total, label = counted
        return done / total, label[:500]
    percentage = parse_build_percentage(line)
    if percentage is not None:
        return percentage / 100.0, "Selena build in progress"
    return None, ""


def _prepare_v5_selena_build(payload: dict):
    from core.agent_bindings import AgentBindingStore
    from core.agent_build_stage import prepare_selena_build

    source_lease = None
    source_lease_ref = str(payload.get("source_lease_ref") or "")
    if source_lease_ref:
        from core.agent_source_lease import AgentSourceLeaseStore

        source_lease = AgentSourceLeaseStore().get(
            source_lease_ref,
            source_evidence_ref=str(payload.get("source_evidence_ref") or ""),
        )
    return prepare_selena_build(payload, AgentBindingStore(), source_lease=source_lease)


def _release_v5_source_lease(lease_ref: str) -> None:
    from core.agent_source_lease import AgentSourceLeaseStore

    AgentSourceLeaseStore().release(lease_ref)


def _public_workspace_bindings() -> list[dict]:
    """Advertise healthy logical bindings without exposing local paths."""
    from core.agent_bindings import AgentBindingError, AgentBindingStore

    try:
        return [binding.public_dict for binding in AgentBindingStore().list()]
    except (AgentBindingError, OSError):
        # Agent registration must remain available so the Web can show the
        # machine and guide one-time binding repair.
        return []


def _public_data_bindings() -> list[dict]:
    """Advertise path-free authorized MF4 roots for central Stage matching."""
    from core.agent_data_bindings import AgentDataBindingError, AgentDataBindingStore

    try:
        return [binding.public_dict for binding in AgentDataBindingStore().list()]
    except (AgentDataBindingError, OSError):
        return []


def _public_asset_bindings() -> list[dict]:
    """Advertise path-free configuration asset roots."""
    from core.agent_asset_bindings import AgentAssetBindingError, AgentAssetBindingStore

    try:
        return [binding.public_dict for binding in AgentAssetBindingStore().list()]
    except (AgentAssetBindingError, OSError):
        return []


def _check_v5_environment(payload: dict, *, agent_id: str, node_kind: str) -> dict:
    from core.agent_bindings import AgentBindingStore
    from core.environment_snapshot import inspect_selena_build_environment

    return inspect_selena_build_environment(
        payload,
        AgentBindingStore(),
        agent_id=agent_id,
        node_kind=node_kind,
    ).to_dict()


def _prepare_v5_branch_source(task: dict) -> dict:
    from core.agent_bindings import AgentBindingStore
    from core.agent_source_lease import AgentSourceLeaseStore

    payload = dict(task.get("payload") or {})
    lease = AgentSourceLeaseStore().create(
        project=str(payload.get("project") or ""),
        workspace_binding_id=str(payload.get("workspace_binding_id") or ""),
        requested_ref=str(payload.get("branch") or ""),
        prepare_stage_id=str(task.get("stage_id") or task.get("task_id") or ""),
        prepare_attempt=int(task.get("attempt_count") or 0),
        job_id=str(task.get("job_id") or ""),
        binding_store=AgentBindingStore(),
    )
    return lease.public_dict


def _verify_v5_selena_build(prepared) -> None:
    from core.agent_build_stage import verify_prepared_build

    verify_prepared_build(prepared)


def _finish_v5_selena_build(prepared, *, build_stage_id: str = "", build_attempt: int = 0) -> dict:
    from core.agent_build_stage import finish_selena_build, stage_runtime_bundle_from_build

    result = finish_selena_build(prepared)
    if prepared.contract == "user-run-config/2.0":
        from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseStore

        result.update(
            stage_runtime_bundle_from_build(
                prepared,
                result,
                created_at=time.time(),
                lease_store=AgentRuntimeBundleLeaseStore(),
                build_stage_id=build_stage_id,
                build_attempt=build_attempt,
            )
        )
    return result


def _create_v5_artifact_lease(
    prepared,
    result: dict,
    *,
    build_stage_id: str,
    build_attempt: int,
) -> dict:
    from core.agent_artifact_lease import AgentArtifactLeaseStore

    lease = AgentArtifactLeaseStore().create(
        prepared,
        result,
        build_stage_id=build_stage_id,
        build_attempt=build_attempt,
    )
    return lease.public_dict


def _upload_v5_artifact(client: "_ControlClient", payload: dict, *, owner: str = "") -> dict:
    from core.agent_artifact_lease import AgentArtifactLeaseStore

    lease_ref = str(payload.get("artifact_lease_ref") or "").strip()
    runtime_bundle_lease_ref = str(payload.get("runtime_bundle_lease_ref") or "").strip()
    evidence_ref = str(payload.get("build_evidence_ref") or "").strip()
    if runtime_bundle_lease_ref:
        from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseStore

        store = AgentRuntimeBundleLeaseStore()
        lease = store.get(runtime_bundle_lease_ref, build_evidence_ref=evidence_ref)
        result = client.upload_runtime_bundle(
            evidence_ref,
            lease.archive_path,
            publish_path=str(payload.get("publish_path") or ""),
            owner=owner,
        )
        runtime_bundle = dict(result.get("runtime_bundle") or {})
        storage_ref = str(runtime_bundle.get("storage_ref") or "")
        store.mark_uploaded(runtime_bundle_lease_ref, storage_ref)
        return {
            "runtime_bundle": runtime_bundle,
            "storage_ref": storage_ref,
            "upload_session_id": str(result.get("upload_session_id") or ""),
            "reused": bool(result.get("reused", False)),
            "build_evidence_ref": evidence_ref,
        }
    lease = AgentArtifactLeaseStore().get(
        lease_ref,
        build_evidence_ref=evidence_ref,
    )
    result = client.upload_artifact(
        evidence_ref,
        lease.artifact_path,
        publish_path=str(payload.get("publish_path") or ""),
        owner=owner,
    )
    artifact = dict(result.get("artifact") or {})
    storage_ref = str(artifact.get("storage_ref") or "")
    AgentArtifactLeaseStore().mark_uploaded(lease_ref, storage_ref)
    return {
        "artifact": artifact,
        "storage_ref": storage_ref,
        "upload_session_id": str(result.get("upload_session_id") or ""),
        "reused": bool(result.get("reused", False)),
        "build_evidence_ref": evidence_ref,
    }


def _run_v5_register_artifact(
    client: "_ControlClient",
    agent_id: str,
    task: dict,
    *,
    heartbeat_interval: float,
) -> int:
    """Upload with a live heartbeat; never expose the Agent-local lease path."""
    task_id = str(task.get("task_id") or "")
    stop_event = threading.Event()

    def heartbeat_loop() -> None:
        while not stop_event.wait(max(1.0, heartbeat_interval)):
            try:
                client.heartbeat(agent_id, status="busy", current_task_id=task_id)
            except Exception:
                pass

    client.append_logs(task_id, ["[agent] starting trusted Selena artifact upload"])
    thread = threading.Thread(target=heartbeat_loop, daemon=True)
    thread.start()
    status = "failed"
    returncode = -1
    result: dict = {"error": "artifact upload failed"}
    try:
        client.heartbeat(agent_id, status="busy", current_task_id=task_id)
        result = _upload_v5_artifact(
            client,
            dict(task.get("payload") or {}),
            owner=str(task.get("owner") or ""),
        )
        status = "succeeded"
        returncode = 0
        client.append_logs(task_id, ["[agent] Selena artifact upload and registration completed"])
    except Exception as exc:
        code = str(getattr(exc, "code", "") or "artifact_upload_failed")
        api_message = str(getattr(exc, "message", "") or "").strip()
        result = {
            "code": code,
            "error": api_message or "artifact upload failed",
        }
        client.append_logs(
            task_id,
            [
                f"[agent] artifact upload failed ({code}"
                + (f": {api_message}" if api_message else "")
                + "); retry is safe and resumable"
            ],
        )
    finally:
        stop_event.set()
        thread.join(timeout=max(1.0, heartbeat_interval))
    client.submit_result(
        task_id,
        agent_id=agent_id,
        status=status,
        returncode=returncode,
        result=result,
    )
    return 0 if status == "succeeded" else 1


def _run_v5_prepare_data(
    client: "_ControlClient",
    agent_id: str,
    task: dict,
    *,
    heartbeat_interval: float,
) -> int:
    """Authorize and discover local MF4s, uploading only for Cluster routes."""
    from core.agent_data_bindings import AgentDataBindingStore
    from core.agent_data_lease import AgentDataLeaseStore

    task_id = str(task.get("task_id") or "")
    attempt = int(task.get("attempt_count") or 0)
    evidence_ref = f"{task_id}:{attempt}"
    stop_event = threading.Event()

    def heartbeat_loop() -> None:
        while not stop_event.wait(max(1.0, heartbeat_interval)):
            try:
                client.heartbeat(agent_id, status="busy", current_task_id=task_id)
            except Exception:
                pass

    client.append_logs(task_id, ["[agent] validating authorized local data root and discovering MF4 inputs"])
    thread = threading.Thread(target=heartbeat_loop, daemon=True)
    thread.start()
    status = "failed"
    returncode = -1
    local_route = str((task.get("payload") or {}).get("dispatch_scope") or "") == "local_data"
    result: dict = {"error": "local dataset preparation failed"}
    try:
        leases = AgentDataLeaseStore()
        lease = leases.create(
            dict(task.get("payload") or {}),
            AgentDataBindingStore(),
            stage_id=task_id,
            attempt=attempt,
        )
        if local_route:
            import hashlib
            from core.datasets import dataset_fingerprint

            fingerprint = dataset_fingerprint(lease.files)
            dataset_id = "dataset:sha256:" + hashlib.sha256(
                "\0".join((lease.project, lease.binding_id, fingerprint)).encode("utf-8")
            ).hexdigest()
            result = {
                "dataset": {
                    "id": dataset_id,
                    "source_kind": "agent_local",
                    "accessibility": "local",
                    "file_count": len(lease.files),
                    "total_size": sum(item.size for item in lease.files),
                    "source_fingerprint": fingerprint,
                },
                "dataset_id": dataset_id,
                "data_lease_ref": lease.lease_id,
                "evidence_ref": evidence_ref,
            }
            status = "succeeded"
            returncode = 0
            client.append_logs(task_id, ["[agent] local data lease prepared for Windows-full simulation"])
        else:
            client.append_logs(
                task_id,
                [f"[agent] discovered {len(lease.files)} MF4 input(s); starting resumable upload"],
            )
            uploaded = client.upload_data_lease(
                evidence_ref,
                agent_id=agent_id,
                lease=lease,
                task_id=task_id,
                owner=str(task.get("owner") or ""),
            )
            dataset = dict(uploaded.get("dataset") or {})
            dataset_id = str(dataset.get("id") or "")
            leases.mark_uploaded(lease.lease_id, dataset_id)
            result = {
                "dataset": dataset,
                "dataset_id": dataset_id,
                "data_path": str(uploaded.get("data_path") or ""),
                "data_lease_ref": lease.lease_id,
                "upload_session_id": str(uploaded.get("upload_session_id") or ""),
                "reused": bool(uploaded.get("reused", False)),
                "evidence_ref": evidence_ref,
            }
            status = "succeeded"
            returncode = 0
            client.append_logs(task_id, ["[agent] local dataset upload completed; Agent may now disconnect"])
    except Exception:
        client.append_logs(task_id, ["[agent] local dataset upload failed; retry is resumable"])
    finally:
        stop_event.set()
        thread.join(timeout=max(1.0, heartbeat_interval))
    client.submit_result(
        task_id,
        agent_id=agent_id,
        status=status,
        returncode=returncode,
        result=result,
    )
    return 0 if status == "succeeded" else 1


def _run_v5_local_stage(
    client: "_ControlClient",
    agent_id: str,
    task: dict,
    *,
    heartbeat_interval: float,
) -> int:
    """Execute one path-private Windows-full Stage with cancellation heartbeat."""
    task_id = str(task.get("task_id") or "")
    stage_type = str(task.get("stage_type") or task.get("task_type") or "")
    cancel_event = threading.Event()
    stop_event = threading.Event()

    def heartbeat_loop() -> None:
        while not stop_event.wait(max(0.5, heartbeat_interval)):
            try:
                response = client.heartbeat(agent_id, status="busy", current_task_id=task_id)
                if response.get("cancel_requested"):
                    cancel_event.set()
            except Exception:
                pass

    client.append_logs(task_id, [f"[agent] Windows-full {stage_type} started"])
    thread = threading.Thread(target=heartbeat_loop, daemon=True)
    thread.start()
    status = "failed"
    returncode = 1
    result: dict = {"error": "local_stage_failed", "code": "local_stage_failed"}
    try:
        response = client.heartbeat(agent_id, status="busy", current_task_id=task_id)
        if response.get("cancel_requested"):
            cancel_event.set()
        if stage_type == "preflight":
            result = _execute_v5_local_preflight(task, client=client)
        elif stage_type == "run_simulation":
            result, returncode = _execute_v5_local_simulation(task, cancel_event.is_set)
        elif stage_type == "collect_results":
            result = _execute_v5_local_collect(task)
        elif stage_type == "finalize_manifest":
            result = _execute_v5_local_finalize(task)
        else:
            raise ValueError("unsupported Windows-full local Stage")
        if cancel_event.is_set() or returncode == 130:
            status = "cancelled"
            returncode = 130
            result = {
                "local_run_lease_ref": str((task.get("payload") or {}).get("local_run_lease_ref") or ""),
                "status": "cancelled",
            }
        elif returncode == 0:
            status = "succeeded"
        client.append_logs(task_id, [f"[agent] Windows-full {stage_type} {status}"])
    except Exception:
        # Local exceptions often carry paths.  Keep details in local diagnostics
        # and send one stable public code only.
        result = {"error": "local_stage_failed", "code": "local_stage_failed"}
        status = "failed"
        returncode = 1
        client.append_logs(task_id, [f"[agent] Windows-full {stage_type} failed"])
    finally:
        stop_event.set()
        thread.join(timeout=max(1.0, heartbeat_interval))
    client.submit_result(
        task_id,
        agent_id=agent_id,
        status=status,
        returncode=returncode,
        result=result,
    )
    return 0 if status == "succeeded" else (130 if status == "cancelled" else 1)


def _execute_v5_local_preflight(task: dict, *, client: "_ControlClient | None" = None) -> dict:
    from dataclasses import replace
    from pathlib import Path

    from core.agent_asset_bindings import AgentAssetBindingStore
    from core.agent_data_lease import AgentDataLeaseStore
    from core.agent_local_run import AgentLocalRunLeaseStore
    from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseStore
    from core.config import load_config
    from core.preflight import run_preflight
    from core.runtime_bundle_archive import extract_runtime_bundle_archive

    payload = dict(task.get("payload") or {})
    project = str(payload.get("project") or "")
    bundle_lease = AgentRuntimeBundleLeaseStore().get(str(payload.get("runtime_bundle_lease_ref") or ""))
    if bundle_lease.project != project or bundle_lease.manifest.id != str(payload.get("runtime_bundle_id") or ""):
        raise ValueError("Runtime Bundle lease does not match local Stage")
    store = AgentLocalRunLeaseStore()
    cache = store.runs_root.parent / "runtime-cache" / bundle_lease.manifest.id.rsplit(":", 1)[-1]
    locations = extract_runtime_bundle_archive(
        bundle_lease.archive_path,
        cache,
        manifest=bundle_lease.manifest,
        archive_checksum=bundle_lease.archive_checksum,
    )
    data_lease = AgentDataLeaseStore().get(str(payload.get("data_lease_ref") or ""))
    limit = int(payload.get("limit") or 0)
    if limit > 0:
        data_lease = replace(data_lease, files=data_lease.files[:limit])
    assets = AgentAssetBindingStore()
    adapter_value = str(payload.get("adapter_file") or "").strip()
    adapter_path = (
        _materialize_local_config_asset(
            adapter_value, kind="adapter", assets=assets, client=client
        )
        if adapter_value
        else ""
    )
    mat_filter_path = _materialize_local_config_asset(
        str(payload.get("mat_filter") or ""), kind="mat_filter", assets=assets, client=client
    )
    adapter_binding = None
    if adapter_path:
        adapter_binding, _ = assets.authorize_any(
            asset_path=adapter_path, role="adapter"
        )
    mat_binding, _ = assets.authorize_any(
        asset_path=mat_filter_path, role="mat_filter"
    )
    timeout_minutes = int(payload.get("timeout_minutes") or 0)
    lease = store.create_from_authorized_inputs(
        job_id=str(task.get("job_id") or ""),
        project=project,
        base_config=load_config(project),
        runtime_manifest=bundle_lease.manifest,
        runtime_locations=locations,
        data_lease=data_lease,
        asset_bindings=assets,
        adapter_binding_id=adapter_binding.binding_id if adapter_binding is not None else "",
        adapter_path=adapter_path,
        mat_filter_binding_id=mat_binding.binding_id,
        mat_filter_path=mat_filter_path,
        timeout_seconds=(timeout_minutes * 60 if timeout_minutes > 0 else 3600),
    )
    private = store.get_private(lease["lease_id"])
    preflight = run_preflight(private["config"])
    if not preflight.ok:
        raise ValueError("local compatibility preflight failed")
    return {
        "local_run_lease_ref": lease["lease_id"],
        "runtime_bundle_id": lease["runtime_bundle_id"],
        "dataset_id": str(payload.get("dataset_id") or ""),
        "preflight": {
            "ok": True,
            "checks": [
                {"name": item.name, "level": item.level, "passed": bool(item.passed)}
                for item in preflight.checks
            ],
        },
    }


def _execute_v5_runtime_bundle_cache(task: dict, *, client: "_ControlClient") -> dict:
    """Download and lease one shared Bundle under the Agent private cache."""
    from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseStore
    from core.runtime_bundle import RuntimeBundleManifest, RuntimeFile, RuntimeSourceEvidence

    payload = dict(task.get("payload") or {})
    raw_manifest = dict(payload.get("runtime_bundle") or {})
    source = dict(raw_manifest.get("source") or {})
    source.setdefault("adapter_key", "")
    manifest = RuntimeBundleManifest(
        id=str(raw_manifest.get("id") or ""),
        files=tuple(RuntimeFile(**dict(item)) for item in raw_manifest.get("files") or []),
        source=RuntimeSourceEvidence(**source),
        created_at=float(raw_manifest.get("created_at") or 0),
    )
    if manifest.id != str(payload.get("runtime_bundle_id") or ""):
        raise ValueError("Runtime Bundle identity mismatch")
    checksum = str(payload.get("archive_checksum") or "").strip().lower()
    size = int(payload.get("archive_size") or 0)
    archive_path = client.download_runtime_bundle(
        manifest.id,
        expected_checksum=checksum,
        expected_size=size,
    )
    lease = AgentRuntimeBundleLeaseStore().create_from_catalog_archive(
        project=str(payload.get("project") or ""),
        cache_stage_id=str(task.get("stage_id") or task.get("task_id") or ""),
        cache_attempt=int(task.get("attempt_count") or 0),
        manifest=manifest,
        archive_path=archive_path,
        archive_checksum=checksum,
        archive_size=size,
    )
    return {
        "runtime_bundle_lease_ref": lease.lease_id,
        "runtime_bundle": manifest.to_dict(),
        "cache": {
            "status": "ready",
            "checksum": lease.archive_checksum,
            "size": lease.archive_size,
        },
    }


def _materialize_local_config_asset(
    value: str,
    *,
    kind: str,
    assets,
    client: "_ControlClient | None",
) -> str:
    """Resolve one task asset without exposing its physical Agent cache path."""
    from core.config_assets import is_config_asset_ref

    text = str(value or "").strip()
    if not is_config_asset_ref(text):
        return text
    if client is None:
        raise ValueError("authenticated Agent client is required for configuration asset")
    path = client.download_config_asset(text, kind=kind)
    assets.register(path.parent)
    return str(path)


def _execute_v5_local_simulation(task: dict, cancel_requested) -> tuple[dict, int]:
    from core.agent_local_run import AgentLocalRunLeaseStore, execute_local_run
    from core.local_selena_runner import run_local_selena

    lease_ref = str((task.get("payload") or {}).get("local_run_lease_ref") or "")
    store = AgentLocalRunLeaseStore()
    private = store.get_private(lease_ref)
    if private["status"] in {"succeeded", "failed", "cancelled"}:
        result = store.result(lease_ref)
        return {"local_run_lease_ref": lease_ref, **result}, 0 if result["status"] == "succeeded" else 1
    returncode = execute_local_run(
        lease_ref,
        store,
        runner=run_local_selena,
        cancel_requested=cancel_requested,
    )
    result = store.result(lease_ref)
    return {"local_run_lease_ref": lease_ref, **result}, returncode


def _execute_v5_local_collect(task: dict) -> dict:
    import time

    from core.agent_local_run import AgentLocalRunLeaseStore
    from core.local_results import default_result_catalog
    from core.user import normalize_user

    payload = dict(task.get("payload") or {})
    lease_ref = str(payload.get("local_run_lease_ref") or "")
    store = AgentLocalRunLeaseStore()
    private = store.get_private(lease_ref)
    local_result = store.result(lease_ref)
    if local_result["status"] != "succeeded":
        raise ValueError("local run did not succeed")
    retain_days = max(1, int(payload.get("retain_days") or 30))
    published = default_result_catalog().publish(
        owner=normalize_user(str(payload.get("owner") or "")),
        run_ref=lease_ref,
        source_root=private["run_root"],
        files=[str(item.get("relative_path") or "") for item in local_result["files"]],
        retain_until=time.time() + retain_days * 86400,
    )
    return {
        "local_run_lease_ref": lease_ref,
        "result_ref": published.ref,
        "result": published.public_dict,
    }


def _execute_v5_local_finalize(task: dict) -> dict:
    from core.agent_local_run import AgentLocalRunLeaseStore
    from core.local_results import default_result_catalog
    from core.user import normalize_user

    payload = dict(task.get("payload") or {})
    lease_ref = str(payload.get("local_run_lease_ref") or "")
    result_ref = str(payload.get("result_ref") or "")
    result = default_result_catalog().get(
        result_ref,
        owner=normalize_user(str(payload.get("owner") or "")),
    )
    local = AgentLocalRunLeaseStore().result(lease_ref)
    manifest = {
        "schema_version": "radar-sim.run-manifest/2.0",
        "job_id": str(payload.get("job_id") or task.get("job_id") or ""),
        "status": local["status"],
        "config_fingerprint": str(payload.get("config_fingerprint") or ""),
        "runtime_bundle_id": str(payload.get("runtime_bundle_id") or ""),
        "dataset_id": str(payload.get("dataset_id") or ""),
        "result_ref": result.ref,
        "files": [item.to_dict() for item in result.files],
        "summary": dict(local["summary"]),
        "created_at": result.created_at,
        "retain_until": result.retain_until,
    }
    if (
        not manifest["runtime_bundle_id"].startswith("selena-bundle:sha256:")
        or not manifest["dataset_id"].startswith("dataset:sha256:")
    ):
        raise ValueError("local manifest logical inputs are invalid")
    return {"manifest": manifest}


def _resolve_v2_run_config(payload: dict) -> dict:
    """Resolve a project-free workspace only after local binding authorization."""
    from core.agent_bindings import AgentBindingStore, make_workspace_path_id
    from core.workspace_recognizer import WorkspaceRecognizer
    from core.agent_asset_bindings import AgentAssetBindingStore
    from core.agent_data_bindings import AgentDataBindingStore

    code_path = str(payload.get("code_path") or "").strip()
    requested_path_id = make_workspace_path_id(code_path)
    if not requested_path_id:
        raise ValueError("workspace path is unavailable")
    binding_store = AgentBindingStore()
    bindings = [
        binding
        for binding in binding_store.list()
        if make_workspace_path_id(str(binding.workspace_root)) == requested_path_id
    ]
    if not bindings and payload.get("auto_configure") is not True:
        raise ValueError("workspace is not uniquely authorized on this Agent")
    outcome = WorkspaceRecognizer().recognize(
        code_path,
        str(payload.get("build_script") or ""),
        selena_build_script=str(payload.get("selena_build_script") or ""),
        package_build_script=str(payload.get("package_build_script") or ""),
    )
    if outcome.status != "resolved" or not outcome.adapter_key:
        raise ValueError("workspace adapter could not be recognized")
    if len(bindings) > 1:
        raise ValueError("workspace is not uniquely authorized on this Agent")
    if bindings:
        binding = bindings[0]
    elif payload.get("auto_configure") is True:
        project = str(outcome.internal_project or "").strip()
        output_text = str(outcome.output_dir or "").strip()
        workspace = Path(code_path).expanduser().resolve(strict=True)
        if not project or not output_text:
            raise ValueError("workspace adapter cannot derive an authorized build output")
        output = Path(output_text)
        if not output.is_absolute():
            output = workspace / output
        output = output.resolve(strict=False)
        try:
            output.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("derived build output is outside the workspace") from exc
        output.mkdir(parents=True, exist_ok=True)
        binding = binding_store.register(project, workspace, (output,))
    else:
        raise ValueError("workspace is not uniquely authorized on this Agent")
    if outcome.internal_project and binding.project != outcome.internal_project:
        raise ValueError("recognized project does not match the authorized workspace")
    asset_store = AgentAssetBindingStore()
    asset_paths = {
        "runtime_xml": str(payload.get("runtime_xml") or "").strip(),
    }
    if str(payload.get("contract") or "") == "user-run-config/2.0" and not asset_paths["runtime_xml"]:
        raise ValueError("Runtime XML path is required for Selena build")
    asset_bindings = {}
    for role, asset_path in asset_paths.items():
        if not asset_path:
            continue
        try:
            asset_binding, _authorized = asset_store.authorize_any(asset_path=asset_path, role=role)
        except Exception:
            if payload.get("auto_configure") is not True:
                raise
            asset_binding = asset_store.register(Path(asset_path).expanduser().resolve(strict=True).parent)
            asset_store.authorize_path(
                binding_id=asset_binding.binding_id, asset_path=asset_path, role=role
            )
        asset_bindings[role] = asset_binding.binding_id
    data_binding_id = ""
    data_path = str(payload.get("data_path") or "").strip()
    if data_path and payload.get("auto_configure") is True:
        from core.datasets import classify_data_path

        local_data = Path(data_path).expanduser()
        if classify_data_path(data_path) not in {"shared", "central"} and local_data.exists():
            root = local_data if local_data.is_dir() else local_data.parent
            data_binding_id = AgentDataBindingStore().register(
                project=binding.project, root_path=root
            ).binding_id

    def relative_ref(value: str) -> str:
        if not value:
            return ""
        try:
            return Path(value).expanduser().resolve(strict=True).relative_to(
                binding.workspace_root.resolve(strict=True)
            ).as_posix()
        except (OSError, ValueError) as exc:
            raise ValueError("build script is outside the authorized workspace") from exc

    return {
        "status": "resolved",
        "adapter_key": outcome.adapter_key,
        "internal_project": binding.project,
        "workspace_binding_id": binding.binding_id,
        "asset_bindings": asset_bindings,
        "data_binding_id": data_binding_id,
        "selena_build_script_ref": relative_ref(outcome.selena_build_script or outcome.build_script),
        "package_build_script_ref": relative_ref(outcome.package_build_script),
        "confidence": outcome.confidence,
        "evidence": list(outcome.evidence),
    }


def _resolve_existing_v2_run_config(task: dict) -> dict:
    """Import a node-local existing folder into a path-free Agent lease."""
    import hashlib

    from core.agent_data_bindings import AgentDataBindingStore
    from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseStore
    from core.existing_selena import import_existing_selena

    payload = dict(task.get("payload") or {})
    if payload.get("auto_configure") is not True:
        raise ValueError("existing Selena folder is not authorized on this Agent")
    stage_id = str(task.get("stage_id") or task.get("task_id") or "").strip()
    attempt = max(1, int(task.get("attempt_count") or 0))
    if not stage_id:
        raise ValueError("resolve_spec Stage identity is unavailable")
    imported = import_existing_selena(
        str(payload.get("existing_path") or ""),
        str(payload.get("runtime_xml") or ""),
    )
    private_binding_id = "existing-path:sha256:" + hashlib.sha256(
        str(imported.exe_path.parent).casefold().encode("utf-8")
    ).hexdigest()
    lease = AgentRuntimeBundleLeaseStore().create(
        project=imported.internal_project,
        workspace_binding_id=private_binding_id,
        build_stage_id=stage_id,
        build_attempt=attempt,
        manifest=imported.bundle.manifest,
        archive=imported.archive,
    )
    data_binding_id = ""
    data_path = str(payload.get("data_path") or "").strip()
    if data_path:
        candidate = Path(data_path).expanduser()
        if candidate.exists():
            data_root = candidate if candidate.is_dir() else candidate.parent
            data_binding_id = AgentDataBindingStore().register(
                project=imported.internal_project,
                root_path=data_root,
            ).binding_id
    evidence_ref = f"{stage_id}:{attempt}"
    return {
        "status": "resolved",
        "source": "existing",
        "internal_project": imported.internal_project,
        "adapter_key": imported.adapter_key,
        "runtime_bundle_lease_ref": lease.lease_id,
        "runtime_bundle": imported.bundle.manifest.to_dict(),
        "archive": imported.archive.public_dict,
        "data_binding_id": data_binding_id,
        "build_evidence_ref": evidence_ref,
        "confidence": 1.0,
        "evidence": [
            "existing_folder_validated",
            "selena_exe_unique",
            "colocated_dlls_bound",
            "runtime_xml_bound",
        ],
    }


def _build_task_command(task: dict) -> list[str]:
    task_type = str(task.get("task_type") or "")
    payload = dict(task.get("payload") or {})
    base = [sys.executable, str(ROOT / "rsim.py")]
    if payload.get("config_path"):
        base.extend(["--config", str(payload["config_path"])])
    elif payload.get("project"):
        base.extend(["--project", str(payload["project"])])

    if task_type == "local.check":
        cmd = [*base, "check"]
        if payload.get("backend"):
            cmd.extend(["--backend", str(payload["backend"])])
        if payload.get("profile"):
            cmd.extend(["--profile", str(payload["profile"])])
        if payload.get("deps"):
            cmd.append("--deps")
        return cmd

    if task_type == "local.build_selena":
        cmd = [*base, "build", "selena"]
        if payload.get("mode"):
            cmd.extend(["--mode", str(payload["mode"])])
        if payload.get("clean"):
            cmd.append("--clean")
        if payload.get("no_progress"):
            cmd.append("--no-progress")
        return cmd

    if task_type == "local.run_sim":
        cmd = [*base, "run"]
        input_mf4 = payload.get("input_mf4") or payload.get("input_path") or ""
        if input_mf4:
            cmd.append(str(input_mf4))
        if payload.get("dataset"):
            cmd.extend(["--dataset", str(payload["dataset"])])
        if payload.get("profile"):
            cmd.extend(["--profile", str(payload["profile"])])
        if payload.get("select"):
            cmd.append("--select")
        if payload.get("limit"):
            cmd.extend(["--limit", str(payload["limit"])])
        for signal in payload.get("required_signals", []) or []:
            cmd.extend(["--required-signal", str(signal)])
        if payload.get("output_mf4"):
            cmd.extend(["--output-mf4", str(payload["output_mf4"])])
        if payload.get("timeout"):
            cmd.extend(["--timeout", str(payload["timeout"])])
        if payload.get("max_duration"):
            cmd.extend(["--max-duration", str(payload["max_duration"])])
        if payload.get("stall_timeout"):
            cmd.extend(["--stall-timeout", str(payload["stall_timeout"])])
        if payload.get("no_retry"):
            cmd.append("--no-retry")
        if payload.get("no_wait"):
            cmd.append("--no-wait")
        extra_args = list(payload.get("extra_args", []) or [])
        for item in extra_args:
            cmd.append(f"--extra-arg={item}")
        if payload.get("dry_run"):
            cmd.append("--dry-run")
        return cmd

    if task_type == "cluster.run":
        cmd = [*base, "cluster", "run"]
        input_mf4 = payload.get("input_mf4") or payload.get("input_path") or ""
        if input_mf4:
            cmd.append(str(input_mf4))
        if payload.get("dataset"):
            cmd.extend(["--dataset", str(payload["dataset"])])
        if payload.get("profile"):
            cmd.extend(["--profile", str(payload["profile"])])
        if payload.get("select"):
            cmd.append("--select")
        if payload.get("limit"):
            cmd.extend(["--limit", str(payload["limit"])])
        if payload.get("run_id"):
            cmd.extend(["--run-id", str(payload["run_id"])])
        if payload.get("copy_data"):
            cmd.append("--copy-data")
        if payload.get("copy_selena"):
            cmd.append("--copy-selena")
        for signal in payload.get("required_signals", []) or []:
            cmd.extend(["--required-signal", str(signal)])
        if payload.get("no_wait"):
            cmd.append("--no-wait")
        if payload.get("no_fetch"):
            cmd.append("--no-fetch")
        if payload.get("max_minutes"):
            cmd.extend(["--max-minutes", str(payload["max_minutes"])])
        if payload.get("execute"):
            cmd.append("--execute")
        return cmd

    if task_type == "tcc.bootstrap_itc2":
        return [*base, "tcc", "bootstrap-itc2"]

    if task_type == "tcc.install_toolcollection":
        cmd = [*base, "tcc", "install"]
        tc = payload.get("toolcollection") or ""
        if tc:
            cmd.append(str(tc))
        return cmd

    if task_type == "tcc.auto_repair_all":
        return [*base, "tcc", "auto-repair"]

    raise ValueError(f"unsupported task type: {task_type}")


def _quote_command(command: list[str]) -> str:
    parts = []
    for item in command:
        text = str(item)
        if any(ch in text for ch in (" ", "\t", '"')):
            text = '"' + text.replace('"', '\\"') + '"'
        parts.append(text)
    return " ".join(parts)


def _child_env_utf8() -> dict[str, str]:
    """Return os.environ copy with UTF-8 IO encoding forced for the child.

    The agent decodes child stdout as utf-8 (above), so the child must emit
    utf-8 too — otherwise Chinese-Windows cp936 output gets garbled into
    replacement chars. PYTHONUTF8=1 makes Python children use utf-8 regardless
    of the system locale; PYTHONIOENCODING covers non-Python children that
    respect it.
    """
    import os
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class _ControlClient:
    def __init__(
        self,
        server_url: str,
        *,
        timeout: int,
        api_url: str = "",
        token: str = "",
        api_token: str = "",
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._timeout = timeout
        self._api_url = str(api_url or "").rstrip("/")
        self._token = str(token or "")
        self._api_token = str(api_token or "")
        self._agent_id = ""

    def register_agent(self, *, name: str, agent_id: str, hostname: str, platform: str, capabilities: list[str], metadata: dict) -> dict:
        registered = self._request(
            "POST",
            "/api/agents/register",
            {
                "name": name,
                "agent_id": agent_id,
                "hostname": hostname,
                "platform": platform,
                "capabilities": capabilities,
                "metadata": metadata,
            },
        )
        self._agent_id = str(registered.get("agent_id") or agent_id)
        return registered

    def poll(self, agent_id: str) -> dict:
        return self._request("POST", "/api/agents/poll", {"agent_id": agent_id})

    def heartbeat(
        self,
        agent_id: str,
        *,
        status: str,
        current_task_id: str = "",
        metadata: dict | None = None,
    ) -> dict:
        return self._request(
            "POST",
            "/api/agents/heartbeat",
            {
                "agent_id": agent_id,
                "status": status,
                "current_task_id": current_task_id,
                "metadata": dict(metadata or {}),
            },
        )

    def append_logs(self, task_id: str, lines: list[str]) -> dict:
        return self._request(
            "POST", "/api/tasks/logs",
            {"task_id": task_id, "agent_id": self._agent_id, "lines": lines, "stream": "stdout"},
        )

    def report_progress(self, task_id: str, progress: float, *, message: str = "") -> dict:
        return self._request(
            "POST",
            "/api/tasks/progress",
            {
                "task_id": task_id,
                "agent_id": self._agent_id,
                "progress": max(0.0, min(float(progress), 1.0)),
                "message": str(message or ""),
            },
        )

    def submit_result(self, task_id: str, *, agent_id: str, status: str, returncode: int, result: dict) -> dict:
        return self._request(
            "POST",
            "/api/tasks/result",
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "status": status,
                "returncode": returncode,
                "result": result,
            },
        )

    def upload_artifact(
        self,
        build_evidence_ref: str,
        source: Path,
        *,
        publish_path: str = "",
        owner: str = "",
    ) -> dict:
        if not self._api_url:
            raise ValueError("Agent v1 api-url is required for artifact upload")
        from core.user import current_user
        from radar_sim_sdk import RadarSimClient

        with RadarSimClient(self._api_url, user=str(owner or current_user()), token=self._api_token) as sdk:
            uploaded = sdk.upload_artifact(
                build_evidence_ref,
                source,
                publish_path=publish_path,
            )
        return {
            "artifact": dict(uploaded.artifact),
            "upload_session_id": uploaded.session.session_id,
            "reused": bool(uploaded.reused),
        }

    def upload_runtime_bundle(
        self,
        build_evidence_ref: str,
        source: Path,
        *,
        publish_path: str = "",
        owner: str = "",
    ) -> dict:
        if not self._api_url:
            raise ValueError("Agent v1 api-url is required for Runtime Bundle upload")
        from core.user import current_user
        from radar_sim_sdk import RadarSimClient

        with RadarSimClient(self._api_url, user=str(owner or current_user()), token=self._api_token) as sdk:
            uploaded = sdk.upload_runtime_bundle(
                build_evidence_ref,
                source,
                publish_path=publish_path,
            )
        return {
            "runtime_bundle": dict(uploaded.runtime_bundle),
            "upload_session_id": uploaded.session.session_id,
            "reused": bool(uploaded.reused),
        }

    def download_config_asset(self, asset_id: str, *, kind: str) -> Path:
        """Cache one owner-scoped Adapter/MatFilter on this authenticated Agent."""
        import os

        from radar_sim_sdk import RadarSimClient

        base_url = self._api_url or self._server_url
        digest = str(asset_id or "").strip().rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        home = str(os.environ.get("RSIM_HOME") or "").strip()
        root = (Path(home).expanduser() if home else Path.home() / ".rsim") / "agent" / "config-assets"
        target = root / str(kind) / f"{digest}.txt"
        with RadarSimClient(base_url, token=self._token) as sdk:
            return sdk.download_config_asset(
                asset_id,
                kind=kind,
                destination=target,
            )

    def download_runtime_bundle(
        self,
        bundle_id: str,
        *,
        expected_checksum: str,
        expected_size: int,
    ) -> Path:
        """Atomically cache an authenticated shared Bundle by immutable ID."""
        bundle_id = str(bundle_id or "").strip()
        checksum = str(expected_checksum or "").strip().lower()
        size = int(expected_size or 0)
        if (
            not bundle_id.startswith("selena-bundle:sha256:")
            or not checksum.startswith("sha256:")
            or size <= 0
        ):
            raise ValueError("Runtime Bundle download evidence is invalid")
        digest = bundle_id.rsplit(":", 1)[-1]
        home = str(os.environ.get("RSIM_HOME") or "").strip()
        root = (Path(home).expanduser() if home else Path.home() / ".rsim") / "agent" / "runtime-downloads"
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve(strict=True)
        if not root.is_dir() or root.is_symlink():
            raise ValueError("Runtime Bundle cache is invalid")
        target = root / f"{digest}.zip"

        def verify(path: Path) -> bool:
            if not path.is_file() or path.is_symlink() or path.stat().st_size != size:
                return False
            sha = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    sha.update(chunk)
            return "sha256:" + sha.hexdigest() == checksum

        if target.exists():
            if verify(target):
                return target
            raise ValueError("Runtime Bundle cache conflicts with immutable evidence")
        temporary = root / f".{digest}.{os.getpid()}.{threading.get_ident()}.part"
        base_url = self._api_url or self._server_url
        endpoint = "/api/v1/runtime-bundles/" + urllib.parse.quote(bundle_id, safe="") + "/download"
        headers = {"Accept": "application/octet-stream"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(base_url + endpoint, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response, temporary.open("xb") as writer:
                total = 0
                sha = hashlib.sha256()
                while True:
                    chunk = response.read(min(1024 * 1024, size - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > size:
                        raise ValueError("Runtime Bundle download exceeds expected size")
                    sha.update(chunk)
                    writer.write(chunk)
            if total != size or "sha256:" + sha.hexdigest() != checksum:
                raise ValueError("Runtime Bundle download integrity check failed")
            os.replace(temporary, target)
            return target
        except urllib.error.HTTPError as exc:
            raise RuntimeError("Runtime Bundle download request failed") from exc
        finally:
            if temporary.exists():
                temporary.unlink()

    def upload_data_lease(
        self,
        evidence_ref: str,
        *,
        agent_id: str,
        lease,
        task_id: str,
        owner: str = "",
    ) -> dict:
        if not self._api_url:
            raise ValueError("Agent v1 api-url is required for dataset upload")
        from core.user import current_user
        from radar_sim_sdk import RadarSimClient

        manifest = [
            {
                "relative_path": item.relative_path,
                "size": item.size,
                "checksum": item.checksum,
            }
            for item in lease.files
        ]
        source = lease.source_path
        root = source if source.is_dir() else source.parent
        transfer_owner = str(owner or current_user())
        with RadarSimClient(self._api_url, user=transfer_owner, token=self._token) as agent_sdk:
            session = agent_sdk.create_agent_dataset_upload(
                lease.project,
                manifest,
                evidence_ref=evidence_ref,
                agent_id=agent_id,
            )
        with RadarSimClient(self._api_url, user=transfer_owner, token=self._api_token) as sdk:
            current = session
            total = len(session.files)
            for index, upload_file in enumerate(session.files, start=1):
                path = source if source.is_file() else root.joinpath(*Path(upload_file.relative_path).parts)
                with path.open("rb") as handle:
                    handle.seek(upload_file.received_bytes)
                    offset = upload_file.received_bytes
                    while offset < upload_file.expected_size:
                        data = handle.read(min(current.chunk_size, upload_file.expected_size - offset))
                        if not data:
                            raise ValueError("leased data file ended during upload")
                        current = sdk.append_dataset_upload(
                            current.session_id,
                            upload_file.file_id,
                            offset,
                            data,
                        )
                        state = next(item for item in current.files if item.file_id == upload_file.file_id)
                        offset = state.received_bytes
                self.append_logs(task_id, [f"[agent] uploaded MF4 {index}/{total}"])
            uploaded = sdk.finalize_dataset_upload(session.session_id)
        return {
            "dataset": dict(uploaded.dataset),
            "data_path": uploaded.data_path,
            "upload_session_id": uploaded.session.session_id,
            "reused": bool(uploaded.reused),
        }

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        from core.user import USER_HEADER, current_user
        data = None
        headers = {"Accept": "application/json", USER_HEADER: current_user()}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._server_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed: {exc.code} {body}") from exc
