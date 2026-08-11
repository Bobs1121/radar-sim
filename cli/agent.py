"""rsim agent - minimal Windows-friendly polling agent for control jobs."""

from __future__ import annotations

import json
import hashlib
import base64
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
#   --windows-mode unified (default) -> one user-facing connector.  It
#     registers the existing windows_full node kind and can prepare/compile,
#     direct-transfer inputs to Cluster, or execute local simulation.
#
#   --windows-mode light/full       -> administrator/legacy compatibility
#     values only.  New installers and SDK/Web flows never ask users to pick
#     one of them.
#
# Legacy Mode A/B capability tuning is replaced by the mode policy. Explicit
# --capability flags still override the default set, but a light agent that
# requests a forbidden capability fails fast (see core.agent_policy).
from core.agent_policy import (
    AgentPolicyError,
    DEFAULT_FULL_CAPABILITIES,
    DEFAULT_LIGHT_CAPABILITIES,
    MODE_FULL,
    MODE_UNIFIED,
    MODE_LIGHT,
    NODE_KIND_LEGACY,
    NODE_KIND_WINDOWS_AGENT,
    WINDOWS_CONNECTOR_CONTRACT_VERSION,
    WINDOWS_MODES,
    default_capabilities_for_mode,
    may_claim_task,
    node_kind_for_mode,
    normalize_capabilities,
    normalize_windows_mode,
    validate_light_capabilities,
)
from core.progress_parser import parse_build_percentage, parse_build_progress


def _is_retryable_agent_transport(exc: BaseException) -> bool:
    """Return whether an Agent control/data request may be retried.

    The connector runs for days and must not treat a short TCP/proxy reset as
    a permanent task failure.  Request helpers mark standard-library network
    exceptions explicitly; the name fallback also covers HTTPX exceptions
    raised by compatibility paths without making file/path errors retryable.
    """
    if bool(getattr(exc, "transport_error", False)):
        return True
    name = type(exc).__name__.casefold()
    return any(
        token in name
        for token in ("transport", "timeout", "connect", "network", "urlerror")
    )


def _agent_transport_error(method: str, path: str, exc: BaseException) -> RuntimeError:
    """Normalize a stdlib connection failure without leaking local details."""
    error = RuntimeError(f"{method} {path} transport failed")
    error.transport_error = True  # type: ignore[attr-defined]
    error.cause_type = type(exc).__name__  # type: ignore[attr-defined]
    error.status_code = 0  # type: ignore[attr-defined]
    return error


def _missing_connector_dependency(exc: BaseException) -> str:
    """Return a stable optional-dependency name for task diagnostics."""
    if isinstance(exc, ModuleNotFoundError):
        name = str(getattr(exc, "name", "") or "").strip()
        if name:
            return name.split(".", 1)[0]
    text = str(exc or "")
    marker = "No module named "
    if marker in text:
        name = text.split(marker, 1)[1].strip().strip("'\"")
        if name:
            return name.split(".", 1)[0]
    return ""

# Default advertised capabilities for the public unified connector.  Keep the
# exported name for backward-compatible imports (e.g. the embedded web agent).
DEFAULT_CAPABILITIES = list(DEFAULT_FULL_CAPABILITIES)

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
        default=MODE_UNIFIED,
        help="Internal compatibility mode. The public default 'unified' connector "
        "supports local simulation, compile, data preparation and Cluster transfer.",
    )
    parser.add_argument(
        "--capability",
        action="append",
        default=[],
        help="Repeatable internal capability filter. New users should not set this.",
    )
    parser.add_argument("--poll-interval", type=float, default=3.0, help="Seconds between polls when idle")
    parser.add_argument("--heartbeat-interval", type=float, default=10.0, help="Seconds between heartbeats during task execution")
    parser.add_argument("--request-timeout", type=int, default=30, help="HTTP request timeout in seconds")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")


def run(args, config):
    import os
    from core.user import current_user, stable_user_identity
    user = stable_user_identity(current_user())
    mode, node_kind, capabilities = _capabilities_for_mode(
        getattr(args, "windows_mode", MODE_UNIFIED),
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
            "connector_contract_version": WINDOWS_CONNECTOR_CONTRACT_VERSION,
            "auto_configure": True,
            "workspace_bindings": workspace_bindings,
            "data_bindings": data_bindings,
            "asset_bindings": asset_bindings,
        },
    )
    agent_id = agent["agent_id"]
    poll_interval = float(getattr(args, "poll_interval", 3.0) or 3.0)
    once = bool(getattr(args, "once", False))
    poll_failure_count = 0
    last_poll_failure_log_at = 0.0
    while True:
        try:
            claim = client.poll(agent_id)
        except Exception as exc:
            poll_failure_count += 1
            now = time.monotonic()
            if _poll_failure_is_reportable(
                poll_failure_count,
                now=now,
                last_reported_at=last_poll_failure_log_at,
            ):
                print(
                    f"[WARN] Linux control plane is temporarily unreachable "
                    f"(attempt {poll_failure_count}): {exc}; reconnecting automatically",
                    file=sys.stderr,
                )
                last_poll_failure_log_at = now
            if once:
                return 1
            time.sleep(_poll_retry_delay(poll_failure_count, poll_interval))
            continue
        # A single missed poll is normal during a short deployment/network
        # handoff. Only announce recovery if an outage warning was shown.
        if poll_failure_count:
            if last_poll_failure_log_at:
                print(
                    f"[INFO] Linux control plane connection restored after "
                    f"{poll_failure_count} failed poll(s)",
                    file=sys.stderr,
                )
            poll_failure_count = 0
            last_poll_failure_log_at = 0.0
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


def _poll_retry_delay(failure_count: int, poll_interval: float) -> float:
    """Bound reconnect traffic while keeping transient recovery responsive."""
    base = max(float(poll_interval or 0.0), 1.0)
    exponent = min(max(int(failure_count) - 1, 0), 4)
    return min(base * (2 ** exponent), 30.0)


def _poll_failure_is_reportable(
    failure_count: int,
    *,
    now: float,
    last_reported_at: float,
) -> bool:
    """Hide brief control-plane jitter but keep persistent outages visible."""
    count = max(int(failure_count), 0)
    if count < 3:
        return False
    if count == 3 or float(last_reported_at) <= 0:
        return True
    return float(now) - float(last_reported_at) >= 60.0


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
    is_existing_runtime = (
        is_v5_environment
        and str((task.get("payload") or {}).get("dispatch_scope") or "") == "existing_runtime"
    )
    is_v5_source = str(task.get("task_type") or "") == "prepare_source"
    is_v5_register = str(task.get("task_type") or "") == "register_artifact"
    is_v5_data = str(task.get("task_type") or "") == "prepare_data"
    is_v5_local_stage = (
        str(task.get("task_type") or "") in {"preflight", "run_simulation", "collect_results", "finalize_manifest"}
        and str((task.get("payload") or {}).get("dispatch_scope") or "") == "local_simulation"
    )
    prepared_build = None
    prepared_build_environment = None
    command_cwd = ROOT
    try:
        if not may_claim_task(node_kind, task.get("task_type"), task.get("stage_type")):
            raise AgentPolicyError("agent node policy forbids this task type")
        if is_v2_resolution:
            resolution_source = str((task.get("payload") or {}).get("source") or "build")
            client.append_logs(
                task_id,
                [
                    "[agent] validating the user-selected Selena inputs"
                    if resolution_source == "existing"
                    else "[agent] recognizing the user-selected workspace"
                ],
            )
            task_payload = dict(task.get("payload") or {})
            task_owner = str(task_payload.get("owner") or task.get("owner") or "").strip()
            try:
                recognition = (
                    _resolve_existing_v2_run_config(
                        task,
                        owner=task_owner,
                        device_id=agent_id,
                    )
                    if resolution_source == "existing"
                    else _resolve_v2_run_config(
                        task_payload,
                        owner=task_owner,
                        device_id=agent_id,
                    )
                )
            except TypeError as exc:
                # Embedded callers may still monkeypatch/import the old
                # one-argument resolver.  Retry only for that signature
                # mismatch; TypeError raised by the resolver itself remains a
                # real task failure.
                if "unexpected keyword argument" not in str(exc):
                    raise
                recognition = (
                    _resolve_existing_v2_run_config(task)
                    if resolution_source == "existing"
                    else _resolve_v2_run_config(task_payload)
                )
            client.append_logs(task_id, ["[agent] local Selena/runtime evidence prepared"])
            # Resource bodies are not accepted by the Linux control plane.
            # Later role-specific stages request signed TransferPlans after
            # this node-local recognition; no archive/config upload is done
            # during resolve_spec.
            recognition["config_assets"] = {}
            recognition["registered_runtime_bundle"] = {}
            client.append_logs(task_id, ["[agent] local MatFilter/Adapter/Selena resources reserved for direct transfer"])
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
            if is_existing_runtime:
                existing = _execute_v5_existing_runtime(task)
                client.append_logs(task_id, ["[agent] existing Runtime Bundle lease verified"])
                client.submit_result(
                    task_id,
                    agent_id=agent_id,
                    status="succeeded",
                    returncode=0,
                    result=existing,
                )
                return 0
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
            def report_environment_progress(message: str) -> None:
                client.append_logs(task_id, ["[agent] " + str(message)])
                heartbeat = getattr(client, "heartbeat", None)
                if callable(heartbeat):
                    try:
                        heartbeat(
                            agent_id,
                            status="busy",
                            current_task_id=task_id,
                            metadata={
                                "workspace_bindings": _public_workspace_bindings(),
                                "data_bindings": _public_data_bindings(),
                                "asset_bindings": _public_asset_bindings(),
                            },
                        )
                    except Exception:
                        # Log delivery must not turn a successful local check
                        # into a failed task when the control plane reconnects.
                        pass

            snapshot = _check_v5_environment(
                dict(task.get("payload") or {}),
                agent_id=agent_id,
                node_kind=node_kind,
                progress_fn=report_environment_progress,
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
            authorized = getattr(prepared_build, "authorized", None)
            workspace_root = getattr(authorized, "workspace_root", None)
            if workspace_root is not None:
                from core.windows_build_environment import prepare_windows_build_environment

                prepared_build_environment = prepare_windows_build_environment(
                    workspace_root=workspace_root,
                    selena_build_script=getattr(prepared_build, "build_script_path", None),
                    package_build_script=getattr(prepared_build, "package_build_script_path", None),
                )
        else:
            command = _build_task_command(task)
    except Exception as exc:
        # FileNotFoundError / OSError: agent has no local config or repo for
        # the requested project.  Unexpected optional-dependency/import errors
        # must follow the same terminal path: otherwise the Agent supervisor
        # restarts the process while the claimed Stage remains "running".
        # Report every setup failure so the task never waits forever.
        missing_dependency = _missing_connector_dependency(exc)
        if is_runtime_bundle_cache:
            message = "[agent] Runtime Bundle cache failed"
        elif missing_dependency:
            message = (
                "[agent] connector dependency missing: "
                f"{missing_dependency}; install the optional build/local-simulation "
                "dependencies on this PC, then retry the Stage"
            )
        else:
            message = f"[agent] task setup error: {exc}"
        client.append_logs(task_id, [message])
        client.submit_result(
            task_id,
            agent_id=agent_id,
            status="failed",
            returncode=-1,
            result=(
                {"error": "runtime_bundle_cache_failed", "code": "runtime_bundle_cache_failed"}
                if is_runtime_bundle_cache
                else {
                    "error": "connector_dependency_missing",
                    "code": "connector_dependency_missing",
                    "dependency": missing_dependency,
                    "repair_hint": "Install the connector optional dependencies and retry this Stage",
                }
                if missing_dependency
                else {"error": str(exc)}
                if (is_v2_resolution or is_v5_build or is_v5_environment or is_v5_source or is_v5_register or is_v5_data or is_v5_local_stage)
                else {"cwd": str(ROOT), "error": str(exc)}
            ),
        )
        return 1
    start_logs = [f"[agent] starting {task['task_type']}"]
    if is_v5_build:
        start_logs.append("[agent] authorized Selena build command prepared")
        dependencies = tuple(
            getattr(prepared_build_environment, "dependencies", ()) or ()
        )
        if dependencies:
            start_logs.append(
                "[agent] script-derived build environment prepared: "
                + ", ".join(dependencies)
            )
        build_payload = dict(task.get("payload") or {})
        if build_payload.get("branch_mismatch") is True:
            start_logs.append(
                "[warning] expected branch "
                f"'{build_payload.get('expected_branch')}', current branch "
                f"'{build_payload.get('actual_branch')}'; compiling the current workspace unchanged"
            )
        elif build_payload.get("actual_branch"):
            start_logs.append(
                f"[agent] compiling current workspace on branch '{build_payload.get('actual_branch')}'"
            )
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
    diagnostic_lines: list[str] = []
    last_reported_progress = 0.0
    last_progress_report_at = 0.0
    try:
        if prepared_build is not None:
            _verify_v5_selena_build(prepared_build)
        proc = subprocess.Popen(
            command,
            cwd=str(command_cwd),
            # User-selected build wrappers are non-interactive Agent jobs.
            # DEVNULL makes an accidental `pause` reach EOF immediately instead
            # of leaving the Stage running forever after success or failure.
            stdin=subprocess.DEVNULL,
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
            env=_child_env_utf8(
                getattr(prepared_build_environment, "environment", None)
            ),
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
                        diagnostic_lines.append(text)
                        if len(diagnostic_lines) > 4000:
                            diagnostic_lines = diagnostic_lines[-4000:]
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
                        if is_v5_build:
                            diagnostic_lines.append(text)
                            if len(diagnostic_lines) > 4000:
                                diagnostic_lines = diagnostic_lines[-4000:]
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
            from core.build_diagnostics import classify_build_failure

            diagnostic = classify_build_failure(diagnostic_lines)
            result = {
                "error": execution_error or diagnostic.summary,
                "code": diagnostic.code,
                "diagnostic": diagnostic.to_dict(),
            }
            client.append_logs(
                task_id,
                [
                    f"[diagnostic] {diagnostic.summary}",
                    f"[action] {diagnostic.action}",
                ],
            )
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
    try:
        from core.agent_bindings import AgentBindingError, AgentBindingStore
        return [binding.public_dict for binding in AgentBindingStore().list()]
    except (ModuleNotFoundError, OSError, ValueError) as exc:
        # Agent registration must remain available so the Web can show the
        # machine and guide one-time binding repair.  The light Agent may be
        # running without optional YAML/configuration dependencies; workspace
        # metadata is not needed for a data-upload-only task.
        return []


def _public_data_bindings() -> list[dict]:
    """Advertise path-free authorized MF4 roots for central Stage matching."""
    try:
        from core.agent_data_bindings import AgentDataBindingError, AgentDataBindingStore
        return [binding.public_dict for binding in AgentDataBindingStore().list()]
    except (ModuleNotFoundError, OSError, ValueError):
        return []


def _public_asset_bindings() -> list[dict]:
    """Advertise path-free configuration asset roots."""
    try:
        from core.agent_asset_bindings import AgentAssetBindingError, AgentAssetBindingStore
        return [binding.public_dict for binding in AgentAssetBindingStore().list()]
    except (ModuleNotFoundError, OSError, ValueError):
        return []


def _check_v5_environment(
    payload: dict,
    *,
    agent_id: str,
    node_kind: str,
    progress_fn=None,
) -> dict:
    from core.agent_bindings import AgentBindingStore
    from core.environment_snapshot import inspect_selena_build_environment

    return inspect_selena_build_environment(
        payload,
        AgentBindingStore(),
        agent_id=agent_id,
        node_kind=node_kind,
        progress_fn=progress_fn,
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
    # Kept as a guard for third-party embedded callers that imported the old
    # helper.  Sending Selena bytes to a Linux artifact upload endpoint would
    # violate the control/data-plane contract; callers must execute a signed
    # plan through ``_direct_transfer_v5_artifact`` instead.
    error = RuntimeError("direct transfer plan is required; Linux artifact upload is disabled")
    error.code = "cluster_direct_transfer_unavailable"  # type: ignore[attr-defined]
    raise error


def _run_v5_register_artifact(
    client: "_ControlClient",
    agent_id: str,
    task: dict,
    *,
    heartbeat_interval: float,
) -> int:
    """Direct-copy a complete Selena bundle with a live heartbeat.

    The task may carry a signed ``transfer_plan`` (the normal path) or enough
    owner/job/stage metadata for the Agent to request one after validating its
    local lease.  Legacy Linux artifact/runtime-bundle upload sessions are
    deliberately rejected.
    """
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
        payload = dict(task.get("payload") or {})
        if payload.get("already_registered") is True:
            bundle = dict(payload.get("runtime_bundle") or {})
            if (
                not str(bundle.get("id") or "").startswith("selena-bundle:sha256:")
                or not str(bundle.get("storage_ref") or "").startswith("shared://selena-bundles/")
            ):
                raise ValueError("registered Runtime Bundle evidence is invalid")
            result = {
                "runtime_bundle": bundle,
                "storage_ref": str(bundle.get("storage_ref") or ""),
                "runtime_bundle_lease_ref": str(payload.get("runtime_bundle_lease_ref") or ""),
                "build_evidence_ref": str(payload.get("build_evidence_ref") or ""),
                "reused": True,
            }
        else:
            result = _direct_transfer_v5_artifact(
                client,
                agent_id,
                task,
                owner=str(task.get("owner") or ""),
                cancel_check=lambda: False,
            )
        status = "succeeded"
        returncode = 0
        client.append_logs(task_id, ["[agent] complete Selena directory copied directly to Cluster data plane"])
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
                f"[agent] direct Selena transfer failed ({code}"
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


def _direct_transfer_v5_artifact(
    client: "_ControlClient",
    agent_id: str,
    task: dict,
    *,
    owner: str,
    cancel_check,
) -> dict:
    """Resolve a local bundle/artifact lease and execute one signed plan."""
    import tempfile

    payload = dict(task.get("payload") or {})
    source_root: Path
    temporary_root: Path | None = None
    runtime_lease_ref = str(payload.get("runtime_bundle_lease_ref") or "").strip()
    artifact_lease_ref = str(payload.get("artifact_lease_ref") or "").strip()
    evidence_ref = str(payload.get("build_evidence_ref") or "").strip()
    runtime_manifest: dict = dict(payload.get("runtime_bundle") or {})
    source_role = str(payload.get("source_role") or "runtime_bundle")

    if runtime_lease_ref:
        from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseStore
        from core.runtime_bundle_archive import extract_runtime_bundle_archive

        lease = AgentRuntimeBundleLeaseStore().get(runtime_lease_ref, build_evidence_ref=evidence_ref)
        temporary_root = Path(tempfile.mkdtemp(prefix="rsim-direct-runtime-"))
        extract_runtime_bundle_archive(
            lease.archive_path,
            temporary_root / "bundle",
            manifest=lease.manifest,
            archive_checksum=lease.archive_checksum,
        )
        source_root = temporary_root / "bundle"
        runtime_manifest = lease.manifest.to_dict()
    elif artifact_lease_ref:
        from core.agent_artifact_lease import AgentArtifactLeaseStore

        lease = AgentArtifactLeaseStore().get(artifact_lease_ref, build_evidence_ref=evidence_ref)
        source_root = lease.artifact_path.parent
        source_role = "runtime_bundle"
    else:
        raw_source = str(payload.get("source_path") or payload.get("existing_path") or "").strip()
        if not raw_source:
            raise ValueError("direct Selena source lease is unavailable")
        source_root = Path(raw_source).expanduser()
        if source_root.is_file():
            source_root = source_root.parent

    try:
        payload_plan = dict(payload.get("transfer_plan") or {})
        if payload_plan:
            plan = payload_plan
        else:
            items = _scan_direct_transfer_items(source_root, source_role=source_role)
            plan = client.issue_transfer_plan(
                owner=owner,
                job_id=str(task.get("job_id") or ""),
                stage_id=str(task.get("task_id") or task.get("stage_id") or ""),
                mode="shared_copy",
                source_role=source_role,
                items=items,
                source_fingerprints={"evidence_ref": evidence_ref} if evidence_ref else {},
            )
        manifest = client.execute_transfer_plan(
            plan,
            source_root=source_root,
            owner=owner,
            cancel_check=cancel_check,
        )
        return {
            "runtime_bundle": runtime_manifest,
            "transfer_id": manifest.transfer_id,
            "transfer_status": "transfer_completed",
            "manifest": manifest.to_dict(),
            "storage_refs": [entry.storage_ref for entry in manifest.entries],
            "build_evidence_ref": evidence_ref,
            "agent_id": agent_id,
        }
    finally:
        if temporary_root is not None:
            import shutil

            shutil.rmtree(temporary_root, ignore_errors=True)


def _scan_direct_transfer_items(source_root: Path, *, source_role: str) -> list[dict]:
    """Discover every regular file below a local source, preserving folders.

    The signed plan needs stable size/mtime evidence, while the transfer
    kernel computes each file's SHA-256 during the copy stream.  Leaving the
    request checksum empty avoids a separate full-file read for large MF4 or
    Selena binaries; an existing lease may still supply a checksum when one is
    already available.
    """

    root = Path(source_root).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("direct transfer source root is unavailable")
    items: list[dict] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.is_symlink():
            continue
        # Existing Selena outputs commonly contain very large linker/debug
        # products (.pdb/.ilk/.lib/.exp).  They are not runtime inputs and the
        # mature local/Cluster adapter has always staged only Selena.exe plus
        # DLL dependencies.  Apply the same project-independent rule to the
        # direct-transfer path so source-to-source transfer does not turn the
        # thin control plane into a build-output mirror.
        if source_role == "runtime_bundle":
            name = path.name.casefold()
            if name != "selena.exe" and path.suffix.casefold() != ".dll":
                continue
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        items.append(
            {
                "source_role": source_role,
                "relative_path": relative,
                "size": int(stat.st_size),
                "checksum": "",
                "mtime_ns": int(stat.st_mtime_ns),
            }
        )
    if not items:
        if source_role == "runtime_bundle":
            raise ValueError("Selena runtime folder contains no selena.exe or DLL files")
        raise ValueError("direct transfer source directory is empty")
    if source_role == "runtime_bundle" and not any(
        Path(str(item.get("relative_path") or "")).name.casefold() == "selena.exe"
        for item in items
    ):
        raise ValueError("Selena runtime folder does not contain selena.exe")
    return items


def _dataset_transfer_fingerprints(
    source_root: Path,
    items: list[dict],
) -> dict[str, str]:
    """Infer optional radar metadata from the first local MF4."""

    first_mf4 = next(
        (
            source_root / str(item.get("relative_path") or "")
            for item in items
            if str(item.get("relative_path") or "").casefold().endswith(".mf4")
        ),
        None,
    )
    if first_mf4 is None:
        return {}
    try:
        from core.simulation import detect_radar_transfer_metadata_safe

        return dict(detect_radar_transfer_metadata_safe(str(first_mf4)))
    except Exception:
        return {}


def _direct_transfer_asset(
    client: "_ControlClient",
    task: dict,
    *,
    owner: str,
    source_role: str,
    source_path: str,
    cancel_check,
) -> dict:
    """Transfer one runtime/config asset independently of dataset bytes."""
    path = Path(source_path).expanduser().resolve(strict=True)
    if path.is_dir():
        source_root = path
        items = _scan_direct_transfer_items(source_root, source_role=source_role)
    elif path.is_file() and not path.is_symlink():
        source_root = path.parent
        stat = path.stat()
        items = [
            {
                "source_role": source_role,
                "relative_path": path.name,
                "size": int(stat.st_size),
                "checksum": "",
                "mtime_ns": int(stat.st_mtime_ns),
            }
        ]
    else:
        raise ValueError(f"authorized {source_role} source is unavailable")

    payload = dict(task.get("payload") or {})
    plan_value = payload.get("transfer_plans") or {}
    plan = plan_value.get(source_role) if isinstance(plan_value, dict) else None
    if not plan:
        fingerprints = (
            _dataset_transfer_fingerprints(source_root, items)
            if source_role == "dataset"
            else {}
        )
        plan = client.issue_transfer_plan(
            owner=owner,
            job_id=str(task.get("job_id") or ""),
            stage_id=str(task.get("task_id") or task.get("stage_id") or ""),
            mode="shared_copy",
            source_role=source_role,
            items=items,
            source_fingerprints=fingerprints,
        )
    manifest = client.execute_transfer_plan(
        plan,
        source_root=source_root,
        owner=owner,
        cancel_check=cancel_check,
    )
    return {
        "source_role": source_role,
        "transfer_id": manifest.transfer_id,
        "transfer_status": "transfer_completed",
        "manifest": manifest.to_dict(),
        "storage_refs": [entry.storage_ref for entry in manifest.entries],
    }


def _infer_task_mat_filter(payload: dict) -> Path:
    """Discover an omitted MatFilter on the source/execution computer."""

    from core.mat_filter_resolver import resolve_mat_filter

    hints = dict(payload.get("resource_discovery") or {})
    return resolve_mat_filter(
        str(payload.get("mat_filter") or ""),
        code_path=str(hints.get("code_path") or payload.get("code_path") or ""),
        existing_path=str(hints.get("existing_path") or payload.get("existing_path") or ""),
        selena_build_script=str(
            hints.get("selena_build_script") or payload.get("selena_build_script") or ""
        ),
        runtime_xml=str(hints.get("runtime_xml") or payload.get("runtime_xml") or ""),
    ).path


def _run_v5_prepare_data(
    client: "_ControlClient",
    agent_id: str,
    task: dict,
    *,
    heartbeat_interval: float,
) -> int:
    """Authorize/discover local MF4s and direct-copy Cluster routes."""
    from core.agent_data_bindings import AgentDataBindingStore
    from core.agent_data_lease import AgentDataLeaseStore
    from core.datasets import DatasetDiscoveryCancelled

    task_id = str(task.get("task_id") or "")
    attempt = int(task.get("attempt_count") or 0)
    evidence_ref = f"{task_id}:{attempt}"
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

    client.append_logs(task_id, ["[agent] validating authorized local data root and discovering MF4 inputs"])
    thread = threading.Thread(target=heartbeat_loop, daemon=True)
    thread.start()
    status = "failed"
    returncode = -1
    stage_payload = dict(task.get("payload") or {})
    local_route = str(stage_payload.get("dispatch_scope") or "") == "local_data"
    # ``source_to_local`` remains a future transfer-kernel mode.  This Agent
    # has no target-specific Windows cache/target-agent authorization yet, so
    # never execute it against the Cluster root by accident.
    transfer_mode = str(stage_payload.get("transfer_mode") or "shared_copy").strip().lower()
    result: dict = {"error": "local dataset preparation failed"}
    direct_transfers: list[dict] = []
    try:
        if transfer_mode == "source_to_local":
            error = RuntimeError(
                "source_to_local requires a target-specific Windows cache and target-agent authorization"
            )
            error.code = "source_to_local_unavailable"  # type: ignore[attr-defined]
            raise error
        if transfer_mode != "shared_copy":
            raise ValueError("unsupported direct-transfer mode")
        response = client.heartbeat(agent_id, status="busy", current_task_id=task_id)
        if response.get("cancel_requested"):
            cancel_event.set()
        if cancel_event.is_set():
            raise DatasetDiscoveryCancelled("dataset preparation cancelled")
        payload = dict(task.get("payload") or {})
        source_entries = [
            item
            for item in list(payload.get("source_paths") or [])
            if isinstance(item, dict)
            and str(item.get("source_role") or "").strip()
            and str(item.get("path") or "").strip()
        ]
        discovery_hints = dict(payload.get("resource_discovery") or {})
        can_discover_mat_filter = any(
            str(discovery_hints.get(key) or payload.get(key) or "").strip()
            for key in ("code_path", "existing_path", "selena_build_script", "runtime_xml")
        )
        if can_discover_mat_filter and not any(
            str(item.get("source_role") or "").strip() == "mat_filter"
            for item in source_entries
        ):
            inferred = _infer_task_mat_filter(payload)
            source_entries.append(
                {"source_role": "mat_filter", "path": str(inferred)}
            )
            client.append_logs(
                task_id,
                ["[agent] MatFilter omitted; selected one high-confidence repository candidate"],
            )
        dataset_sources = [
            item for item in source_entries
            if str(item.get("source_role") or "").strip() == "dataset"
        ]
        asset_sources = [
            item for item in source_entries
            if str(item.get("source_role") or "").strip() != "dataset"
        ]
        # Shared/Cluster-visible data may coexist with local Runtime XML or
        # config assets. Only a local dataset role requires an AgentDataLease;
        # discovering the shared path here would make the mixed-source Stage
        # fail before its independent asset transfers.
        has_dataset_plan = bool(payload.get("transfer_plan")) or bool(
            isinstance(payload.get("transfer_plans"), dict)
            and payload.get("transfer_plans", {}).get("dataset")
        )
        needs_dataset_lease = bool(local_route or dataset_sources or has_dataset_plan)
        bindings = AgentDataBindingStore()
        # One-click Windows deployment has no separate "register data root"
        # screen.  The control plane assigns this only for a syntactically
        # Windows-local path; authorize that submitted path on this Agent once
        # and retain the resulting durable binding for later tasks.
        if needs_dataset_lease and payload.get("auto_configure") is True and not str(payload.get("data_binding_id") or ""):
            project = str(payload.get("project") or "").strip()
            data_path = str(payload.get("data_path") or "").strip()
            candidate = Path(data_path).expanduser()
            if not project or not data_path or not candidate.exists():
                raise ValueError("submitted local data path is unavailable for one-click authorization")
            root = candidate if candidate.is_dir() else candidate.parent
            owner_scope = str(payload.get("owner") or task.get("owner") or "").strip()
            if owner_scope:
                binding = bindings.register(
                    owner=owner_scope,
                    device_id=agent_id,
                    root_path=root,
                )
            else:
                # Older control planes did not include owner/device in the
                # private payload. Keep their project-scoped binding readable
                # while the modern path is owner/device scoped.
                binding = bindings.register(project=project, root_path=root)
            payload["data_binding_id"] = binding.binding_id
            client.heartbeat(
                agent_id,
                status="busy",
                current_task_id=task_id,
                metadata={"data_bindings": _public_data_bindings()},
            )
            client.append_logs(task_id, ["[agent] submitted local data path authorized for this Windows computer"])
        leases = AgentDataLeaseStore() if needs_dataset_lease else None
        lease = (
            leases.create(
                payload,
                bindings,
                stage_id=task_id,
                attempt=attempt,
                # The direct-transfer kernel computes SHA-256 while copying
                # each file. Discovery records only size/mtime so large MF4
                # inputs are not read twice before transfer.
                checksum=False,
                cancel_requested=cancel_event.is_set,
            )
            if leases is not None
            else None
        )
        if lease is None:
            # No local dataset role was advertised. Transfer each local
            # Runtime/config resource independently and leave shared data
            # zero-copy for the Cluster-side resolver.
            owner = str(task.get("owner") or "")
            for source in asset_sources:
                role = str(source.get("source_role") or "").strip()
                raw_path = str(source.get("path") or "").strip()
                direct_transfers.append(
                    _direct_transfer_asset(
                        client,
                        task,
                        owner=owner,
                        source_role=role,
                        source_path=raw_path,
                        cancel_check=cancel_event.is_set,
                    )
                )
            result = {
                "source_kind": "agent_direct_transfer",
                "accessibility": "cluster",
                "transfers": direct_transfers,
                "transfer_status": "transfer_completed" if direct_transfers else "transfer_skipped_shared",
                "evidence_ref": evidence_ref,
            }
            status = "succeeded"
            returncode = 0
            client.append_logs(
                task_id,
                ["[agent] local Runtime/config assets copied directly; shared dataset remains zero-copy"],
            )
        elif local_route:
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
            # Cluster input is copied directly from the authorized Windows
            # lease to the signed data-plane root.  The old
            # ``agent-dataset-uploads`` HTTP session is intentionally not a
            # fallback: a missing direct-transfer deployment is a stable
            # needs-input error, never a Linux staging upload.
            owner = str(task.get("owner") or "")
            client.append_logs(
                task_id,
                [f"[agent] discovered {len(lease.files)} MF4 input(s); requesting direct-transfer plan"],
            )
            items = [
                {
                    "source_role": "dataset",
                    "relative_path": str(item.relative_path).replace("\\", "/"),
                    "size": int(item.size),
                    # Hash once while streaming to the signed target; the
                    # AgentDataLease itself is metadata-only.
                    "checksum": "",
                    "mtime_ns": int(item.mtime_ns),
                }
                for item in lease.files
            ]
            payload_plan = dict(payload.get("transfer_plan") or {})
            radar_fingerprints = _dataset_transfer_fingerprints(
                lease.source_path if lease.source_path.is_dir() else lease.source_path.parent,
                items,
            )
            plan = payload_plan or client.issue_transfer_plan(
                owner=owner,
                job_id=str(task.get("job_id") or ""),
                stage_id=task_id,
                mode="shared_copy",
                source_role="dataset",
                items=items,
                source_fingerprints={"evidence_ref": evidence_ref, **radar_fingerprints},
            )
            manifest = client.execute_transfer_plan(
                plan,
                source_root=lease.source_path if lease.source_path.is_dir() else lease.source_path.parent,
                owner=owner,
                cancel_check=cancel_event.is_set,
            )
            import hashlib
            from core.datasets import dataset_fingerprint

            fingerprint = dataset_fingerprint(lease.files)
            dataset_id = "dataset:sha256:" + hashlib.sha256(
                "\0".join((lease.project, lease.binding_id, fingerprint)).encode("utf-8")
            ).hexdigest()
            entries = [entry.to_dict() for entry in manifest.entries]
            result = {
                "dataset": {
                    "id": dataset_id,
                    "source_kind": "agent_direct_transfer",
                    "accessibility": "cluster",
                    "file_count": len(entries),
                    "total_size": manifest.total_bytes,
                    "source_fingerprint": fingerprint,
                    "storage_refs": [entry.storage_ref for entry in manifest.entries],
                },
                "dataset_id": dataset_id,
                "data_lease_ref": lease.lease_id,
                "transfer_id": manifest.transfer_id,
                "transfer_status": "transfer_completed",
                "manifest": manifest.to_dict(),
                "evidence_ref": evidence_ref,
            }
            status = "succeeded"
            returncode = 0
            client.append_logs(
                task_id,
                [
                    "[agent] local dataset copied directly to Cluster data plane; Agent may now disconnect"
                ],
            )
            # Runtime XML, MatFilter and Adapter are independent resources.
            # They share the Stage only for scheduling; each gets its own
            # owner/job/stage-bound plan and manifest.
            for source in asset_sources:
                role = str(source.get("source_role") or "").strip()
                raw_path = str(source.get("path") or "").strip()
                if not role or not raw_path:
                    continue
                asset_manifest = _direct_transfer_asset(
                    client,
                    task,
                    owner=owner,
                    source_role=role,
                    source_path=raw_path,
                    cancel_check=cancel_event.is_set,
                )
                direct_transfers.append(asset_manifest)
            if direct_transfers:
                result["transfers"] = direct_transfers
    except DatasetDiscoveryCancelled:
        status = "cancelled"
        returncode = 130
        result = {"status": "cancelled", "code": "cancelled"}
        client.append_logs(task_id, ["[agent] local dataset preparation cancelled"])
    except Exception as exc:
        from core.preflight import RuntimeDataSignalContractError

        if isinstance(exc, RuntimeDataSignalContractError):
            status = "failed"
            returncode = 2
            result = {
                "error": exc.detail,
                "code": exc.code,
                "actions": ([{"type": "fix_configuration", "label": exc.repair_hint}] if exc.repair_hint else []),
            }
            client.append_logs(task_id, [f"[agent] {exc.detail}"])
        else:
            code = str(getattr(exc, "code", "") or "direct_transfer_failed")
            detail = str(getattr(exc, "message", "") or "").strip()
            result = {"error": detail or "direct dataset transfer failed", "code": code}
            client.append_logs(
                task_id,
                [
                    "[agent] direct dataset transfer failed"
                    + (f" ({code}: {detail})" if detail else f" ({code})")
                    + "; retry is resumable"
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

    _safe_append_logs(client, task_id, [f"[agent] Windows-full {stage_type} started"])
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
            returncode = 0
        elif stage_type == "run_simulation":
            result, returncode = _execute_v5_local_simulation(task, cancel_event.is_set)
        elif stage_type == "collect_results":
            result = _execute_v5_local_collect(task, client=client)
            returncode = 0
        elif stage_type == "finalize_manifest":
            result = _execute_v5_local_finalize(task)
            returncode = 0
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
        if stage_type == "run_simulation":
            _append_local_simulation_diagnostics(
                client,
                task_id,
                result,
                failed=(status == "failed"),
                partial=(str(result.get("status") or "") == "partial"),
            )
        _safe_append_logs(client, task_id, [f"[agent] Windows-full {stage_type} {status}"])
    except Exception as exc:
        # Local exceptions often carry paths.  Keep details in local diagnostics
        # and send one stable public code only.
        missing_dependency = _missing_connector_dependency(exc)
        code = (
            "connector_dependency_missing"
            if missing_dependency
            else str(getattr(exc, "code", "") or "local_stage_failed").strip().lower()
        )
        if not code or not all(char.isalnum() or char == "_" for char in code):
            code = "local_stage_failed"
        result = {"error": code, "code": code}
        if missing_dependency:
            result["dependency"] = missing_dependency
            result["repair_hint"] = "Install the connector optional dependencies and retry this Stage"
        status = "failed"
        returncode = 1
        cause = str(getattr(exc, "cause_type", "") or "")
        cause_status = int(getattr(exc, "cause_status", 0) or 0)
        _safe_append_logs(
            client,
            task_id,
            [
                (
                    f"[agent] Windows-full {stage_type} failed "
                    f"(connector dependency missing: {missing_dependency})"
                    if missing_dependency
                    else f"[agent] Windows-full {stage_type} failed "
                    f"({code}; {type(exc).__name__}"
                    + (f" caused_by={cause}/status={cause_status or 'transport'}" if cause else "")
                    + ")"
                )
            ],
            stream="stderr",
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
    return 0 if status == "succeeded" else (130 if status == "cancelled" else 1)


def _safe_append_logs(
    client: "_ControlClient",
    task_id: str,
    lines: list[str],
    *,
    stream: str = "stdout",
) -> None:
    """Treat task-log transport as advisory; terminal result remains authoritative."""
    try:
        client.append_logs(task_id, lines, stream=stream)
    except Exception:
        pass


def _append_local_simulation_diagnostics(
    client: "_ControlClient",
    task_id: str,
    result: dict,
    *,
    failed: bool,
    partial: bool = False,
) -> None:
    """Publish a compact, path-free internal-engine diagnostic to the Stage log."""
    summary = dict(result.get("summary") or {})
    diagnostics = dict(result.get("diagnostics") or {})
    lines: list[str] = []
    if failed:
        lines.append(
            "[simulation] Selena returned a failed outcome "
            f"(error_code={summary.get('error_code') or 'unknown'}, "
            f"failed_inputs={summary.get('failed_input_count', summary.get('error_count', 0))})"
        )
    elif partial:
        lines.append(
            "[simulation] Selena completed with a partial outcome; "
            f"successful_inputs={summary.get('succeeded_input_count', 0)}, "
            f"failed_inputs={summary.get('failed_input_count', summary.get('error_count', 0))}"
        )
    for item in diagnostics.get("items") or []:
        item_status = str(item.get("status") or "").strip().lower()
        if item_status not in {"succeeded", "failed"}:
            continue
        if item_status == "failed":
            lines.append(
                "[simulation] input #{} failed: path={}, error_code={}, returncode={}".format(
                    item.get("index", "?"),
                    item.get("input_relative_path", "<input>"),
                    item.get("error_code", "unknown"),
                    item.get("returncode", "unknown"),
                )
            )
        else:
            lines.append(
                "[simulation] input #{} succeeded: path={}, returncode={}".format(
                    item.get("index", "?"),
                    item.get("input_relative_path", "<input>"),
                    item.get("returncode", "0"),
                )
            )
    for line in diagnostics.get("engine_log_tail") or []:
        lines.append(f"[selena] {line}")
    if lines:
        _safe_append_logs(
            client,
            task_id,
            lines[-220:],
            stream="stderr" if failed or partial else "stdout",
        )


def _execute_v5_local_preflight(task: dict, *, client: "_ControlClient | None" = None) -> dict:
    from dataclasses import replace
    from pathlib import Path

    from core.agent_asset_bindings import AgentAssetBindingStore
    from core.agent_data_lease import AgentDataLeaseStore
    from core.agent_local_run import AgentLocalRunLeaseStore
    from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseStore
    from core.config import load_local_execution_config
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
    mat_filter_value = str(payload.get("mat_filter") or "").strip()
    if not mat_filter_value:
        mat_filter_value = str(_infer_task_mat_filter(payload))
        _safe_append_logs(
            client,
            str(task.get("task_id") or task.get("stage_id") or ""),
            ["[agent] MatFilter omitted; selected one high-confidence repository candidate"],
        )
    mat_filter_path = _materialize_local_config_asset(
        mat_filter_value, kind="mat_filter", assets=assets, client=client
    )
    def authorize_user_asset(path_text: str, *, role: str):
        """Authorize a YAML-provided local asset on first use.

        The one-click Agent already owns this user's Windows session.  A
        MatFilter/Adapter path supplied in the run YAML is therefore safe to
        persist as a parent-directory binding after verifying that it is a
        readable regular file.  This keeps the public install flow to one
        setup step while preserving the existing path-containment checks for
        every later use.
        """
        try:
            return assets.authorize_any(asset_path=path_text, role=role)
        except Exception as exc:
            from core.agent_asset_bindings import AgentAssetBindingError

            if not isinstance(exc, AgentAssetBindingError):
                raise
            try:
                candidate = Path(path_text).expanduser().resolve(strict=True)
            except OSError:
                raise exc
            if not candidate.is_file() or candidate.is_symlink():
                raise exc
            binding = assets.register(candidate.parent)
            return binding, assets.authorize_path(
                binding_id=binding.binding_id,
                asset_path=str(candidate),
                role=role,
            )

    adapter_binding = None
    if adapter_path:
        adapter_binding, _ = authorize_user_asset(adapter_path, role="adapter")
    mat_binding, _ = authorize_user_asset(mat_filter_path, role="mat_filter")
    timeout_minutes = int(payload.get("timeout_minutes") or 0)
    discovery = dict(payload.get("resource_discovery") or {})
    base_config = load_local_execution_config(
        project,
        project_root=str(discovery.get("code_path") or payload.get("code_path") or ""),
    )
    _apply_project_independent_runtime_environment(base_config, discovery, payload)
    # Product/project defaults are not user intent.  An explicit public YAML
    # source wins; when it is empty the per-file metadata detector selects a
    # stable first acquisition source at execution time.
    from core.simulation import normalize_radar_metadata

    base_simulation = base_config.setdefault("simulation", {})
    base_simulation.pop("source", None)
    base_simulation.pop("mounting_position", None)
    selected_radar = normalize_radar_metadata({"source": payload.get("radar_source")})
    if selected_radar:
        base_simulation.update(selected_radar)
        base_simulation["auto_detect_radar"] = False
    else:
        base_simulation["auto_detect_radar"] = True

    lease = store.create_from_authorized_inputs(
        job_id=str(task.get("job_id") or ""),
        project=project,
        base_config=base_config,
        runtime_manifest=bundle_lease.manifest,
        runtime_locations=locations,
        data_lease=data_lease,
        asset_bindings=assets,
        adapter_binding_id=adapter_binding.binding_id if adapter_binding is not None else "",
        adapter_path=adapter_path,
        mat_filter_binding_id=mat_binding.binding_id,
        mat_filter_path=mat_filter_path,
        timeout_seconds=(timeout_minutes * 60 if timeout_minutes > 0 else 3600),
        # prepare_data already established the local lease's size/mtime
        # evidence.  Avoid hashing the complete local dataset again before
        # Selena starts; Cluster/upload flows retain checksum verification.
        verify_input_checksums=False,
    )
    private = store.get_private(lease["lease_id"])
    # The Runtime/Data port list is already captured during resolution.  Do
    # not reopen every MF4 in the local preflight: large recordings can be
    # tens of gigabytes and Selena itself is the authoritative compatibility
    # check.  Keep the preflight project-independent so repository signal
    # lists cannot block a valid user-supplied runtime/data pair.
    private["config"]["_project_independent_execution"] = True
    preflight = run_preflight(private["config"])
    if not preflight.ok:
        detail = next((item.detail for item in preflight.checks if not item.passed), "")
        raise ValueError(detail or "local compatibility preflight failed")
    return {
        "local_run_lease_ref": lease["lease_id"],
        "runtime_bundle_id": lease["runtime_bundle_id"],
        "dataset_id": str(payload.get("dataset_id") or ""),
        "preflight": {
            "ok": True,
            "checks": [
                {
                    "name": item.name,
                    "level": item.level,
                    "passed": bool(item.passed),
                        # Some injected/legacy preflight checks expose only
                        # name/level/passed.  Diagnostics are best-effort and
                        # must not turn a passed compatibility check into a
                        # local Stage exception merely because detail is
                        # absent.
                        "detail": str(getattr(item, "detail", "") or ""),
                }
                for item in preflight.checks
            ],
        },
    }


def _apply_project_independent_runtime_environment(
    config: dict,
    discovery: dict,
    payload: dict,
) -> None:
    """Inject only build-recorded DLL paths into one private local config."""

    from core.selena_runtime_environment import infer_selena_runtime_environment

    existing_path = str(
        discovery.get("existing_path") or payload.get("existing_path") or ""
    )
    code_path = str(discovery.get("code_path") or payload.get("code_path") or "")
    build_script = str(
        discovery.get("selena_build_script")
        or payload.get("selena_build_script")
        or ""
    )
    hints: list[str] = [existing_path]
    if build_script:
        try:
            from core.config import derive_project_context_from_selena_script

            derived = derive_project_context_from_selena_script(
                build_script,
                project_root_hint=code_path,
            )
            hints.append(str(derived.get("build_output") or ""))
        except (OSError, TypeError, ValueError):
            pass
    hints.append(code_path)
    resolved = infer_selena_runtime_environment(hints)
    environment = config.setdefault("environment", {})
    current = list(environment.get("path_prefix") or [])
    inferred = [str(path) for path in resolved.path_prefix]
    environment["path_prefix"] = inferred + [item for item in current if item not in inferred]


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


def _execute_v5_existing_runtime(task: dict) -> dict:
    """Verify and reuse the Runtime Bundle lease created by existing-folder resolution."""
    from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseStore

    payload = dict(task.get("payload") or {})
    lease = AgentRuntimeBundleLeaseStore().get(
        str(payload.get("runtime_bundle_lease_ref") or "")
    )
    expected = dict(payload.get("runtime_bundle") or {})
    if lease.manifest.id != str(expected.get("id") or ""):
        raise ValueError("existing Runtime Bundle identity mismatch")
    return {
        "runtime_bundle_lease_ref": lease.lease_id,
        "runtime_bundle": lease.manifest.to_dict(),
        "environment_snapshot": {
            "status": "ready",
            "node_kind": "windows_full",
            "runtime_bundle_id": lease.manifest.id,
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


def _upload_resolution_config_assets(
    payload: dict,
    *,
    client: "_ControlClient",
    owner: str,
) -> dict[str, str]:
    """Turn Agent-local simulation files into path-free central references."""
    from core.config_assets import is_config_asset_ref
    from core.shared_namespace import looks_like_shared_path

    uploaded: dict[str, str] = {}
    for kind, field in (("adapter", "adapter_file"), ("mat_filter", "mat_filter")):
        value = str(payload.get(field) or "").strip()
        if not value:
            continue
        if is_config_asset_ref(value):
            uploaded[field] = value
            continue
        # Shared paths stay as business input and are resolved by the Linux
        # deployment namespace.  Every regular non-shared file visible to this
        # Windows process is local to this Agent and must be copied centrally.
        if looks_like_shared_path(value):
            continue
        try:
            path = Path(value).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"{field} is unavailable on this Windows computer") from exc
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"{field} must be a regular local file")
        record = client.upload_config_asset(path, kind=kind, owner=owner)
        reference = str(record.get("uri") or record.get("id") or "")
        if not is_config_asset_ref(reference):
            raise ValueError(f"{field} upload did not return a valid reference")
        uploaded[field] = reference
    return uploaded


def _execute_v5_local_simulation(task: dict, cancel_requested) -> tuple[dict, int]:
    from core.agent_local_run import AgentLocalRunLeaseStore, execute_local_run
    from core.local_selena_runner import run_local_selena

    lease_ref = str((task.get("payload") or {}).get("local_run_lease_ref") or "")
    store = AgentLocalRunLeaseStore()
    private = store.get_private(lease_ref)
    if private["status"] in {"succeeded", "failed", "cancelled"}:
        result = store.result(lease_ref)
        return _local_stage_result(lease_ref, result)
    returncode = execute_local_run(
        lease_ref,
        store,
        runner=run_local_selena,
        cancel_requested=cancel_requested,
    )
    result = store.result(lease_ref)
    if returncode == 0 or _is_partial_local_result(result):
        return _local_stage_result(lease_ref, result)
    return {"local_run_lease_ref": lease_ref, **result}, returncode


def _is_partial_local_result(result: dict) -> bool:
    """True when at least one input succeeded and another failed.

    A runner-unavailable or all-input failure remains a hard Stage failure;
    only a real mixed outcome is allowed to continue to result collection.
    """
    if str(result.get("status") or "") != "failed":
        return False
    summary = dict(result.get("summary") or {})
    try:
        successful = int(summary.get("succeeded_input_count") or 0)
        failed = int(summary.get("failed_input_count") or summary.get("error_count") or 0)
    except (TypeError, ValueError):
        return False
    return successful > 0 and failed > 0 and bool(result.get("files"))


def _local_stage_result(lease_ref: str, result: dict) -> tuple[dict, int]:
    if _is_partial_local_result(result):
        return {"local_run_lease_ref": lease_ref, **result, "status": "partial"}, 0
    return {"local_run_lease_ref": lease_ref, **result}, 0 if result["status"] == "succeeded" else 1


def _local_result_input_results(local_result: dict) -> list[dict]:
    """Return path-free per-input outcomes for the extracted manifest."""
    diagnostics = dict(local_result.get("diagnostics") or {})
    input_results: list[dict] = []
    for item in diagnostics.get("items") or []:
        if not isinstance(item, dict):
            continue
        input_results.append(
            {
                "index": item.get("index"),
                "input_relative_path": str(item.get("input_relative_path") or "<input>"),
                "output_relative_path": str(item.get("output_relative_path") or ""),
                "status": str(item.get("status") or "unknown"),
                "returncode": item.get("returncode"),
                "error_code": str(item.get("error_code") or ""),
            }
        )
    return input_results


def _result_path_from_payload(payload: dict) -> str:
    """Read the canonical ``result.path`` field without exposing it upstream."""
    if "result_path" in payload:
        return str(payload.get("result_path") or "")
    section = payload.get("result")
    if isinstance(section, dict):
        return str(section.get("path") or "")
    return ""


def _materialize_local_result(
    task: dict,
    *,
    source_root: Path,
    local_result: dict,
    result_ref: str,
) -> dict:
    """Deliver local outputs to the Connector device, keeping ZIP publication.

    Delivery is deliberately best-effort from the simulation business
    outcome's perspective: a filesystem conflict/unavailability returns a
    stable path-free status and the caller can still finalize the catalog ZIP.
    """
    from core.result_delivery import (
        ResultDeliveryError,
        materialize_result_directory,
        resolve_result_destination,
    )

    payload = dict(task.get("payload") or {})
    job_id = str(payload.get("job_id") or task.get("job_id") or "").strip()
    result_ref = str(result_ref or local_result.get("result_ref") or "").strip()
    files = list(local_result.get("files") or [])
    partial = _is_partial_local_result(local_result)
    manifest = {
        "job_id": job_id,
        "status": "partial" if partial else str(local_result.get("status") or "succeeded"),
        "result_ref": result_ref,
        "files": files,
        "summary": dict(local_result.get("summary") or {}),
        "input_results": _local_result_input_results(local_result),
    }
    try:
        destination = resolve_result_destination(
            _result_path_from_payload(payload),
            job_id,
        )
        return materialize_result_directory(
            source_root,
            destination,
            files=files,
            input_results=manifest["input_results"],
            manifest=manifest,
        )
    except ResultDeliveryError as exc:
        # The physical path is intentionally absent from the callback.  The
        # ZIP result remains available and a later local retry can re-run this
        # best-effort delivery using the same immutable source lease.
        return {
            "status": "failed",
            "file_count": 0,
            "checksum": "",
            "code": str(exc.code or "result_delivery_failed"),
        }


def _execute_v5_local_collect(task: dict, *, client: "_ControlClient | None" = None) -> dict:
    import time

    from core.agent_local_run import AgentLocalRunLeaseStore
    from core.local_results import default_result_catalog
    from core.user import normalize_user

    payload = dict(task.get("payload") or {})
    lease_ref = str(payload.get("local_run_lease_ref") or "")
    store = AgentLocalRunLeaseStore()
    private = store.get_private(lease_ref)
    local_result = store.result(lease_ref)
    if local_result["status"] != "succeeded" and not _is_partial_local_result(local_result):
        raise ValueError("local run did not succeed")
    owner = normalize_user(str(payload.get("owner") or ""))
    retain_days = max(1, int(payload.get("retain_days") or 30))
    catalog = default_result_catalog()
    # A result collection Stage may be retried after a transient control-plane
    # or network failure.  Rebuilding the same archive with a new retention
    # timestamp used to look like a content conflict and made a successful
    # Selena run permanently fail in ``collect_results``.  Reuse the immutable
    # per-owner/run record when it already exists; the upload operation below
    # remains independently resumable/idempotent.
    published = next(
        (item for item in catalog.list(owner=owner) if item.run_ref == lease_ref),
        None,
    )
    if published is None:
        published = catalog.publish(
            owner=owner,
            run_ref=lease_ref,
            source_root=private["run_root"],
            files=[str(item.get("relative_path") or "") for item in local_result["files"]],
            retain_until=time.time() + retain_days * 86400,
        )
    central = published.public_dict
    delivery = _materialize_local_result(
        task,
        source_root=Path(private["run_root"]),
        local_result=local_result,
        result_ref=published.ref,
    )
    if client is not None and getattr(client, "_api_url", ""):
        archive = catalog.resolve_archive(
            published.ref,
            owner=owner,
        )
        uploaded = None
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                uploaded = client.upload_result_archive(
                    archive,
                    run_ref=lease_ref,
                    files=[item.to_dict() for item in published.files],
                    retain_until=published.retain_until,
                    owner=str(payload.get("owner") or ""),
                )
                break
            except Exception as exc:
                last_error = exc
                status_code = int(getattr(exc, "status_code", 0) or 0)
                if client is not None:
                    try:
                        client.append_logs(
                            str(task.get("task_id") or ""),
                            [
                                "[agent] result archive upload attempt "
                                f"{attempt + 1}/3 failed ({type(exc).__name__}; "
                                f"status={status_code or 'transport'})"
                            ],
                        )
                    except Exception:
                        pass
                retryable = (
                    status_code >= 500
                    or status_code in {408, 409, 429}
                    or _is_retryable_agent_transport(exc)
                )
                if not retryable or attempt >= 2:
                    failure = RuntimeError("result_upload_failed")
                    setattr(failure, "code", "result_upload_failed")
                    setattr(failure, "cause_type", type(exc).__name__)
                    setattr(failure, "cause_status", status_code)
                    raise failure from exc
                time.sleep(1.0 * (2**attempt))
        if uploaded is None:
            failure = RuntimeError("result_upload_failed")
            setattr(failure, "code", "result_upload_failed")
            setattr(failure, "cause_type", type(last_error).__name__ if last_error else "")
            setattr(failure, "cause_status", int(getattr(last_error, "status_code", 0) or 0) if last_error else 0)
            raise failure from last_error
        central = dict(uploaded.get("result") or central)
    return {
        "local_run_lease_ref": lease_ref,
        "result_ref": str(central.get("ref") or published.ref),
        "result": central,
        "delivery": delivery,
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
    local_summary = dict(local["summary"])
    partial = _is_partial_local_result(local)
    manifest_diagnostics = dict(local.get("diagnostics") or {})
    input_results = _local_result_input_results(local)
    # ``collect_results`` already performed the one physical copy.  Finalize
    # receives only its path-free summary through the signed successor payload
    # and must not traverse/copy a large local run a second time.
    delivery = dict(payload.get("delivery") or {})
    if not delivery:
        # Compatibility with direct embedded callers that invoke finalize
        # without the stage binder.  The catalog ZIP remains authoritative;
        # no second materialization is attempted here.
        delivery = {
            "status": "not_reported",
            "file_count": len(result.files),
            "checksum": "",
        }
    manifest = {
        "schema_version": "radar-sim.run-manifest/2.0",
        "job_id": str(payload.get("job_id") or task.get("job_id") or ""),
        "status": "partial" if partial else local["status"],
        "config_fingerprint": str(payload.get("config_fingerprint") or ""),
        "runtime_bundle_id": str(payload.get("runtime_bundle_id") or ""),
        "dataset_id": str(payload.get("dataset_id") or ""),
        "result_ref": result.ref,
        "files": [item.to_dict() for item in result.files],
        "summary": local_summary,
        "input_results": input_results,
        "created_at": result.created_at,
        "retain_until": result.retain_until,
        "delivery": delivery,
    }
    if manifest_diagnostics.get("engine_log_tail"):
        manifest["diagnostics"] = {
            "engine_log_tail": list(manifest_diagnostics["engine_log_tail"])[-200:]
        }
    if (
        not manifest["runtime_bundle_id"].startswith("selena-bundle:sha256:")
        or not manifest["dataset_id"].startswith("dataset:sha256:")
    ):
        raise ValueError("local manifest logical inputs are invalid")
    return {"manifest": manifest, "delivery": delivery}


def _resolve_v2_run_config(
    payload: dict,
    *,
    owner: str = "",
    device_id: str = "",
) -> dict:
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
    path_bindings = [
        binding
        for binding in binding_store.list()
        if make_workspace_path_id(str(binding.workspace_root)) == requested_path_id
    ]
    if not path_bindings and payload.get("auto_configure") is not True:
        raise ValueError("workspace is not uniquely authorized on this Agent")
    outcome = WorkspaceRecognizer().recognize(
        code_path,
        str(payload.get("build_script") or ""),
        selena_build_script=str(payload.get("selena_build_script") or ""),
        package_build_script=str(payload.get("package_build_script") or ""),
        generic_only=True,
    )
    if outcome.status != "resolved" or not outcome.adapter_key:
        raise ValueError("workspace adapter could not be recognized")
    project = str(outcome.internal_project or "").strip()
    bindings = [binding for binding in path_bindings if binding.project == project]
    if len(bindings) > 1:
        raise ValueError("workspace is not uniquely authorized on this Agent")
    if bindings:
        binding = bindings[0]
    elif payload.get("auto_configure") is True:
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
    # Runtime.xml is branch/binary-specific. Discover its DataPlayer inputs on
    # the Windows machine that can read the user file, then pass only the port
    # names to the later data stage. That stage validates MF4 headers before a
    # potentially very large upload starts.
    from core.preflight import RuntimeDataSignalContractError, runtime_data_player_ports

    try:
        runtime_data_player_signals = runtime_data_player_ports(asset_paths["runtime_xml"])
    except RuntimeDataSignalContractError:
        # Runtime/MF4 inspection is diagnostic-only. The later Cluster
        # preflight records the parse warning beside the task; it must not
        # prevent a valid Runtime asset from reaching Selena.
        runtime_data_player_signals = []
    data_binding_id = ""
    data_path = str(payload.get("data_path") or "").strip()
    if data_path and payload.get("auto_configure") is True:
        from core.datasets import classify_data_path

        local_data = Path(data_path).expanduser()
        if classify_data_path(data_path) not in {"shared", "central"} and local_data.exists():
            root = local_data if local_data.is_dir() else local_data.parent
            if owner:
                data_binding_id = AgentDataBindingStore().register(
                    owner=owner,
                    device_id=device_id or "agent",
                    root_path=root,
                ).binding_id
            else:
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

    branch_repo_ref = _resolve_branch_repo_ref(
        binding.workspace_root,
        outcome.selena_build_script or outcome.build_script,
    )
    package_ref = ""
    if outcome.package_build_script:
        if "explicit_package_build_script" in outcome.evidence:
            package_ref = relative_ref(outcome.package_build_script)
        else:
            # Adapter defaults are optional dependency hints.  A checkout may
            # legitimately omit or relocate that script; only an explicit user
            # path is allowed to block resolution.
            try:
                package_ref = relative_ref(outcome.package_build_script)
            except ValueError:
                package_ref = ""

    return {
        "status": "resolved",
        "adapter_key": outcome.adapter_key,
        "internal_project": binding.project,
        "workspace_binding_id": binding.binding_id,
        "asset_bindings": asset_bindings,
        "data_binding_id": data_binding_id,
        "runtime_data_player_signals": runtime_data_player_signals,
        "selena_build_script_ref": relative_ref(outcome.selena_build_script or outcome.build_script),
        "package_build_script_ref": package_ref,
        # The user-facing code_path is the build workspace.  Selena scripts in
        # large products commonly live in a nested Git repository whose
        # branch, rather than the outer workspace branch, identifies Selena.
        # Keep only a workspace-relative internal reference here: it is later
        # resolved and authorized on the same Windows Agent.
        "branch_repo_ref": branch_repo_ref,
        "confidence": outcome.confidence,
        "evidence": list(outcome.evidence),
    }


def _resolve_branch_repo_ref(workspace_root: Path, selena_build_script: str) -> str:
    """Return the nearest Git repository for a selected script, as a safe ref.

    ``code_path`` deliberately remains the full build workspace because the
    script and its output may depend on sibling repositories.  The selected
    Selena script can nevertheless live in a nested sub-repository.  Git's
    own ``--show-toplevel`` resolves that repository without checking out or
    modifying anything.  The returned value is relative to the authorized
    workspace so no local path crosses the Agent/control-plane boundary.
    """
    try:
        workspace = Path(workspace_root).resolve(strict=True)
        script = Path(str(selena_build_script or "")).resolve(strict=True)
    except OSError as exc:
        raise ValueError("Selena branch repository is unavailable") from exc
    try:
        script.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("Selena branch repository is outside the authorized workspace") from exc
    try:
        result = subprocess.run(
            ["git", "-C", str(script.parent), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Compatibility for older generic workspaces: before this refinement
        # they were allowed to compile without a Git branch expectation.
        return ""
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    try:
        repository = Path(result.stdout.strip()).resolve(strict=True)
        reference = repository.relative_to(workspace).as_posix()
    except (OSError, ValueError) as exc:
        raise ValueError("Selena branch repository is outside the authorized workspace") from exc
    return reference or "."


def _resolve_existing_v2_run_config(
    task: dict,
    *,
    owner: str = "",
    device_id: str = "",
) -> dict:
    """Import a node-local existing folder into a path-free Agent lease."""
    import hashlib

    from core.agent_data_bindings import AgentDataBindingStore
    from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseStore
    from core.existing_selena import import_existing_selena
    from core.preflight import RuntimeDataSignalContractError, runtime_data_player_ports

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
        code_path=str(payload.get("code_path") or ""),
        selena_build_script=str(payload.get("selena_build_script") or ""),
        package_build_script=str(payload.get("package_build_script") or ""),
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
            if owner:
                data_binding_id = AgentDataBindingStore().register(
                    owner=owner,
                    device_id=device_id or "agent",
                    root_path=data_root,
                ).binding_id
            else:
                data_binding_id = AgentDataBindingStore().register(
                    project=imported.internal_project,
                    root_path=data_root,
                ).binding_id
    evidence_ref = f"{stage_id}:{attempt}"
    try:
        runtime_data_player_signals = runtime_data_player_ports(
            str(payload.get("runtime_xml") or "")
        )
    except RuntimeDataSignalContractError:
        runtime_data_player_signals = []
    return {
        "status": "resolved",
        "source": "existing",
        "internal_project": imported.internal_project,
        "adapter_key": imported.adapter_key,
        "runtime_bundle_lease_ref": lease.lease_id,
        "runtime_bundle": imported.bundle.manifest.to_dict(),
        "archive": imported.archive.public_dict,
        "data_binding_id": data_binding_id,
        "runtime_data_player_signals": runtime_data_player_signals,
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


def _raw_sha256(value: object) -> str:
    """Normalize ``sha256:<hex>`` and raw digest evidence for TransferPlan."""
    text = str(value or "").strip().lower()
    return text.split(":", 1)[1] if text.startswith("sha256:") else text


def _child_env_utf8(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return os.environ copy with UTF-8 IO encoding forced for the child.

    The agent decodes child stdout as utf-8 (above), so the child must emit
    utf-8 too — otherwise Chinese-Windows cp936 output gets garbled into
    replacement chars. PYTHONUTF8=1 makes Python children use utf-8 regardless
    of the system locale; PYTHONIOENCODING covers non-Python children that
    respect it.
    """
    import os
    env = dict(os.environ if base_env is None else base_env)
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

    def append_logs(self, task_id: str, lines: list[str], *, stream: str = "stdout") -> dict:
        return self._request(
            "POST", "/api/tasks/logs",
            {
                "task_id": task_id,
                "agent_id": self._agent_id,
                "lines": lines,
                "stream": str(stream or "stdout"),
            },
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

    # ------------------------------------------------------------------
    # Metadata-only direct-transfer adapter
    # ------------------------------------------------------------------
    # These calls carry plan/progress/manifest metadata only.  File bytes are
    # copied by ``core.direct_transfer`` from an authorized local source to
    # the signed ``client_target_root`` returned by the control plane.

    def issue_transfer_plan(
        self,
        *,
        owner: str,
        job_id: str,
        stage_id: str,
        mode: str = "shared_copy",
        source_role: str,
        items: list[dict],
        source_fingerprints: dict | None = None,
        ttl_seconds: float | None = None,
    ) -> dict:
        payload = {
            "source_role": str(source_role),
            "items": [dict(item) for item in items],
            "source_fingerprints": dict(source_fingerprints or {}),
        }
        return self._transfer_request(
            "POST",
            f"/api/v1/jobs/{urllib.parse.quote(str(job_id), safe='')}/stages/{urllib.parse.quote(str(stage_id), safe='')}/transfers",
            owner=owner,
            payload=payload,
        )

    def get_transfer_plan(self, transfer_id: str, *, owner: str = "") -> dict:
        return self._transfer_request(
            "GET",
            f"/api/v1/transfers/{urllib.parse.quote(str(transfer_id), safe='')}",
            owner=owner,
        )

    def report_transfer_progress(self, progress, *, owner: str = "") -> dict:
        value = progress.to_dict() if hasattr(progress, "to_dict") else dict(progress)
        transfer_id = str(value.get("transfer_id") or "")
        if not transfer_id:
            raise ValueError("transfer progress transfer_id is required")
        payload = {
            "bytes_transferred": int(value.get("bytes_transferred") or 0),
            "bytes_total": int(value.get("bytes_total") or 0),
            "current_file": str(value.get("current_file") or ""),
            "status": str(value.get("status") or "in_progress"),
        }
        return self._transfer_request(
            "POST",
            f"/api/v1/transfers/{urllib.parse.quote(transfer_id, safe='')}/progress",
            owner=owner,
            payload=payload,
        )

    def report_transfer_manifest(self, manifest, *, owner: str = "") -> dict:
        value = manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest)
        transfer_id = str(value.get("transfer_id") or "")
        if not transfer_id:
            raise ValueError("transfer manifest transfer_id is required")
        payload = dict(value)
        payload.pop("transfer_id", None)
        payload.pop("owner", None)
        return self._transfer_request(
            "POST",
            f"/api/v1/transfers/{urllib.parse.quote(transfer_id, safe='')}/manifest",
            owner=owner,
            payload=payload,
        )

    def cancel_transfer(self, transfer_id: str, *, owner: str = "") -> dict:
        return self._transfer_request(
            "POST",
            f"/api/v1/transfers/{urllib.parse.quote(str(transfer_id), safe='')}/cancel",
            owner=owner,
        )

    def execute_transfer_plan(
        self,
        plan,
        *,
        source_root: str | Path,
        owner: str = "",
        cancel_check=None,
        progress_callback=None,
        chunk_size: int = 1024 * 1024,
        allow_local_test: bool = False,
    ):
        """Copy one signed plan directly and publish only metadata.

        ``progress_callback`` receives every chunk-level local event.  The
        Linux ``/progress`` metadata stream is throttled separately and is
        forced to a verified total before the manifest is submitted.
        """
        from core.direct_transfer import (
            TransferPlan,
            _TransferProgressReporter,
            execute_transfer,
        )
        from core.transfer_service import TransferProgress

        signed = plan if isinstance(plan, TransferPlan) else TransferPlan.from_dict(dict(plan.get("plan") or plan))
        per_file: dict[str, int] = {}
        total = sum(item.size for item in signed.items)

        def publish(progress: TransferProgress) -> None:
            self.report_transfer_progress(progress, owner=owner)

        reporter = _TransferProgressReporter(publish, progress_callback)

        def report(relative_path: str, processed: int, _file_total: int) -> None:
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
        self.report_transfer_manifest(manifest, owner=owner)
        return manifest

    def submit_result(self, task_id: str, *, agent_id: str, status: str, returncode: int, result: dict) -> dict:
        payload = {
            "task_id": task_id,
            "agent_id": agent_id,
            "status": status,
            "returncode": returncode,
            "result": result,
        }
        # The task body is immutable and the server callback is idempotent for
        # an assigned Agent. Retry only transport/transient HTTP failures; a
        # permanent validation/assignment error must remain visible instead of
        # creating an infinite duplicate-result loop.
        for attempt in range(5):
            try:
                return self._request("POST", "/api/tasks/result", payload)
            except Exception as exc:
                status_code = int(getattr(exc, "status_code", 0) or 0)
                if not status_code:
                    text = str(exc)
                    marker = " failed: "
                    if marker in text:
                        try:
                            status_code = int(text.split(marker, 1)[1].split(" ", 1)[0])
                        except (TypeError, ValueError):
                            status_code = 0
                retryable = (
                    status_code >= 500
                    or status_code in {408, 429}
                    or _is_retryable_agent_transport(exc)
                )
                if not retryable or attempt >= 4:
                    raise
                time.sleep(float(2**attempt))
        raise RuntimeError("task result callback retry exhausted")

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
        from core.user import current_user, stable_user_identity
        from radar_sim_sdk import RadarSimClient

        with RadarSimClient(
            self._api_url,
            user=str(owner or stable_user_identity(current_user())),
            token=self._api_token,
            trust_env=False,
        ) as sdk:
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
        from core.user import current_user, stable_user_identity
        from radar_sim_sdk import RadarSimClient

        with RadarSimClient(
            self._api_url,
            user=str(owner or stable_user_identity(current_user())),
            token=self._api_token,
            trust_env=False,
        ) as sdk:
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

    def import_existing_runtime_bundle(self, recognition: dict, *, owner: str = "") -> dict:
        """Upload an Agent-local existing Selena archive under the task owner."""
        if not self._api_url:
            raise ValueError("Agent v1 api-url is required for existing Selena import")
        from core.agent_runtime_bundle_lease import AgentRuntimeBundleLeaseStore

        evidence_ref = str(recognition.get("build_evidence_ref") or "")
        lease = AgentRuntimeBundleLeaseStore().get(
            str(recognition.get("runtime_bundle_lease_ref") or ""),
            build_evidence_ref=evidence_ref,
        )
        metadata = {
            "internal_project": str(recognition.get("internal_project") or ""),
            "adapter_key": str(recognition.get("adapter_key") or ""),
            "manifest": lease.manifest.to_dict(),
            "archive_checksum": lease.archive_checksum,
            "archive_size": lease.archive_size,
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return self._api_binary_request(
            "POST",
            "/api/v1/existing-selena-imports",
            owner=owner,
            content=lease.archive_path.read_bytes(),
            headers={"X-Rsim-Existing-Metadata": encoded},
        )

    def download_config_asset(self, asset_id: str, *, kind: str) -> Path:
        """Cache one owner-scoped Adapter/MatFilter on this authenticated Agent."""
        import os

        from radar_sim_sdk import RadarSimClient

        base_url = self._api_url or self._server_url
        digest = str(asset_id or "").strip().rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        home = str(os.environ.get("RSIM_HOME") or "").strip()
        root = (Path(home).expanduser() if home else Path.home() / ".rsim") / "agent" / "config-assets"
        target = root / str(kind) / f"{digest}.txt"
        with RadarSimClient(base_url, token=self._token, trust_env=False) as sdk:
            return sdk.download_config_asset(
                asset_id,
                kind=kind,
                destination=target,
            )

    def upload_config_asset(
        self,
        source: Path,
        *,
        kind: str,
        owner: str = "",
    ) -> dict:
        """Upload one Agent-local Adapter/MatFilter under the task owner."""
        if not self._api_url:
            raise ValueError("Agent v1 api-url is required for configuration asset upload")
        return self._api_binary_request(
            "POST",
            "/api/v1/config-assets",
            owner=owner,
            content=source.read_bytes(),
            headers={
                "X-Asset-Kind": str(kind),
                "X-Asset-Filename": source.name,
            },
        )

    def upload_result_archive(
        self,
        source: Path,
        *,
        run_ref: str,
        files: list[dict],
        retain_until: float = 0,
        owner: str = "",
    ) -> dict:
        """Upload a completed Windows-local result ZIP to the Linux catalog."""
        if not self._api_url:
            raise ValueError("Agent v1 api-url is required for result upload")
        # Keep the Windows Agent's result path on the standard library.  The
        # Agent is a thin connector and should not depend on HTTPX's pooled
        # sockets for a resumable upload; a stale pooled connection was able
        # to leave a stage waiting after the create request while no PATCH
        # reached Linux.  Each request below is short-lived, owner-scoped and
        # resumes from the server's committed offset.
        path = Path(source).expanduser()
        if not path.is_file() or path.is_symlink():
            raise ValueError("result archive is unavailable")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        size = int(path.stat().st_size)
        checksum = "sha256:" + digest.hexdigest()
        create = self._api_request(
            "POST",
            "/api/v1/result-uploads",
            owner=owner,
            payload={
                "run_ref": str(run_ref),
                "archive_size": size,
                "archive_checksum": checksum,
            },
        )
        session_id = str(create.get("session_id") or "")
        if not session_id:
            raise ValueError("result upload session is unavailable")
        received = int(create.get("received_bytes") or 0)
        chunk_size = max(1, int(create.get("chunk_size") or 4 * 1024 * 1024))
        with path.open("rb") as handle:
            handle.seek(received)
            while received < size:
                data = handle.read(min(chunk_size, size - received))
                if not data:
                    raise ValueError("local result archive ended before the expected size")
                for attempt in range(4):
                    try:
                        current = self._api_request(
                            "PATCH",
                            "/api/v1/result-uploads/" + urllib.parse.quote(session_id, safe=""),
                            owner=owner,
                            data=data,
                            headers={"Upload-Offset": str(received)},
                        )
                        break
                    except Exception:
                        if attempt >= 3:
                            raise
                        try:
                            current = self._api_request(
                                "GET",
                                "/api/v1/result-uploads/" + urllib.parse.quote(session_id, safe=""),
                                owner=owner,
                            )
                            if str(current.get("status") or "") == "finalized":
                                received = size
                                break
                            server_received = int(current.get("received_bytes") or 0)
                            if server_received > received:
                                received = server_received
                                handle.seek(received)
                                break
                        except Exception:
                            pass
                        time.sleep(float(2**attempt))
                if received == size:
                    break
                new_received = int(current.get("received_bytes") or 0)
                if new_received < received:
                    raise ValueError("result upload returned a backwards offset")
                received = new_received
                handle.seek(received)
        return self._api_request(
            "POST",
            "/api/v1/result-uploads/" + urllib.parse.quote(session_id, safe="") + "/finalize",
            owner=owner,
            payload={"files": list(files), "retain_until": float(retain_until or 0)},
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
        # Prefer a user token for owner-scoped immutable downloads when one is
        # available; otherwise use the authenticated Agent token.
        auth_token = self._api_token or self._token
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
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
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise _agent_transport_error("GET", endpoint, exc) from exc
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
        cancel_requested=None,
    ) -> dict:
        # Source-compatible name retained for embedded callers.  The method
        # now executes a metadata-only TransferPlan; it never opens a Linux
        # dataset upload session or sends a file body over HTTP.
        from core.datasets import DatasetDiscoveryCancelled
        from core.user import current_user, stable_user_identity

        cancelled = cancel_requested or (lambda: False)
        if cancelled():
            raise DatasetDiscoveryCancelled("dataset transfer cancelled")
        items = [
            {
                "source_role": "dataset",
                "relative_path": item.relative_path,
                "size": item.size,
                "checksum": _raw_sha256(item.checksum),
                "mtime_ns": int(item.mtime_ns),
            }
            for item in lease.files
        ]
        source = lease.source_path
        root = source if source.is_dir() else source.parent
        transfer_owner = str(owner or stable_user_identity(current_user()))
        plan = self.issue_transfer_plan(
            owner=transfer_owner,
            job_id=task_id.split(":", 1)[0] or task_id,
            stage_id=task_id,
            mode="shared_copy",
            source_role="dataset",
            items=items,
            source_fingerprints={
                "evidence_ref": evidence_ref,
                **_dataset_transfer_fingerprints(root, items),
            },
        )
        manifest = self.execute_transfer_plan(
            plan,
            source_root=root,
            owner=transfer_owner,
            cancel_check=cancelled,
        )
        import hashlib
        from core.datasets import dataset_fingerprint

        fingerprint = dataset_fingerprint(lease.files)
        dataset_id = "dataset:sha256:" + hashlib.sha256(
            "\0".join((lease.project, lease.binding_id, fingerprint)).encode("utf-8")
        ).hexdigest()
        return {
            "dataset": {
                "id": dataset_id,
                "source_kind": "agent_direct_transfer",
                "accessibility": "cluster",
                "file_count": len(manifest.entries),
                "total_size": manifest.total_bytes,
                "source_fingerprint": fingerprint,
                "storage_refs": [entry.storage_ref for entry in manifest.entries],
            },
            "dataset_id": dataset_id,
            "transfer_id": manifest.transfer_id,
            "transfer_status": "transfer_completed",
            "manifest": manifest.to_dict(),
        }

    def _dataset_request(
        self,
        method: str,
        path: str,
        *,
        owner: str,
        token: str = "",
        agent_id: str = "",
        upload_offset: int | None = None,
        payload: dict | None = None,
        data: bytes | None = None,
    ) -> dict:
        """Call the v1 dataset upload API using only urllib/std-lib types."""
        headers = {
            "Accept": "application/json",
            "X-Rsim-User": str(owner or ""),
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if agent_id:
            headers["X-Rsim-Agent-ID"] = str(agent_id)
        if upload_offset is not None:
            headers["Upload-Offset"] = str(int(upload_offset))
        body = data
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._api_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            try:
                envelope = json.loads(body_text)
            except (TypeError, ValueError):
                envelope = {}
            error = RuntimeError(
                f"{method} {path} failed: {exc.code} "
                + str(envelope.get("message") or body_text)
            )
            error.code = str(envelope.get("code") or "transfer_request_failed")  # type: ignore[attr-defined]
            error.message = str(envelope.get("message") or body_text)  # type: ignore[attr-defined]
            raise error from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise _agent_transport_error(method, path, exc) from exc

    def _transfer_request(
        self,
        method: str,
        path: str,
        *,
        owner: str = "",
        payload: dict | None = None,
    ) -> dict:
        """Call metadata-only transfer endpoints with the Agent identity."""
        from core.user import current_user, stable_user_identity

        headers = {
            "Accept": "application/json",
            "X-Rsim-User": str(owner or stable_user_identity(current_user())),
        }
        auth_token = self._api_token or self._token
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        body = None
        if payload is not None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._server_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            try:
                envelope = json.loads(body_text)
            except (TypeError, ValueError):
                envelope = {}
            error = RuntimeError(
                f"{method} {path} failed: {exc.code} "
                + str(envelope.get("message") or body_text)
            )
            error.code = str(envelope.get("code") or "transfer_request_failed")  # type: ignore[attr-defined]
            error.message = str(envelope.get("message") or body_text)  # type: ignore[attr-defined]
            raise error from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise _agent_transport_error(method, path, exc) from exc

    def _api_binary_request(
        self,
        method: str,
        path: str,
        *,
        owner: str = "",
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> dict:
        """Call a v1 upload endpoint without importing the optional SDK stack."""
        from core.user import current_user, stable_user_identity

        request_headers = {
            "Accept": "application/json",
            "X-Rsim-User": str(owner or stable_user_identity(current_user())),
        }
        if self._api_token:
            request_headers["Authorization"] = f"Bearer {self._api_token}"
        request_headers.update({str(key): str(value) for key, value in (headers or {}).items()})
        request = urllib.request.Request(
            self._api_url + path,
            data=bytes(content),
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            error = RuntimeError(f"{method} {path} failed: {exc.code} {body_text}")
            error.status_code = int(exc.code)  # type: ignore[attr-defined]
            raise error from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise _agent_transport_error(method, path, exc) from exc

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        owner: str = "",
        payload: dict | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        """Issue one short-lived owner-scoped JSON/bytes request.

        This is intentionally separate from the Agent polling client: upload
        requests use the v1 API URL and the user's owner scope, while the
        control poll uses the Agent identity and legacy endpoints.
        """
        from core.user import current_user, stable_user_identity

        request_headers = {
            "Accept": "application/json",
            "X-Rsim-User": str(owner or stable_user_identity(current_user())),
        }
        if self._api_token:
            request_headers["Authorization"] = f"Bearer {self._api_token}"
        body = data
        if payload is not None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.update({str(key): str(value) for key, value in (headers or {}).items()})
        request = urllib.request.Request(
            self._api_url + path,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            error = RuntimeError(f"{method} {path} failed: {exc.code} {body_text}")
            error.status_code = int(exc.code)  # type: ignore[attr-defined]
            raise error from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise _agent_transport_error(method, path, exc) from exc

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        from core.user import USER_HEADER, current_user, stable_user_identity
        data = None
        headers = {
            "Accept": "application/json",
            USER_HEADER: stable_user_identity(current_user()),
        }
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
            error = RuntimeError(f"{method} {path} failed: {exc.code} {body}")
            error.status_code = int(exc.code)  # type: ignore[attr-defined]
            raise error from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise _agent_transport_error(method, path, exc) from exc
