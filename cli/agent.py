"""rsim agent - minimal Windows-friendly polling agent for control jobs."""

from __future__ import annotations

import json
import platform as platform_mod
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Agent doesn't need project config at startup; it gets project from task payloads.
NO_CONFIG = True

# Mode A (Linux cluster-only service) default: the agent only claims cluster.run
# tasks. This keeps a Windows agent connecting to a Mode A server from ever
# picking up local.check / local.build_selena / local.run_sim, which would need
# the full local toolchain (MATLAB/Qt/Boost/VS) — Mode A users don't have that.
#
# Mode B (Windows local repo, full toolchain): pass
#   --capability local.check --capability local.build_selena
#   --capability local.run_sim --capability cluster.run
# (or the local.* wildcard) to also claim local tasks. The _build_task_command
# branches for local.* are kept below so Mode B agents still work.
DEFAULT_CAPABILITIES = [
    "cluster.run",
    "tcc.bootstrap_itc2",
    "tcc.install_toolcollection",
    "tcc.auto_repair_all",
]

# Full capability set for Mode B (Windows local repo with the full toolchain).
# Use by passing each entry via --capability, or by importing and extending
# DEFAULT_CAPABILITIES in a wrapper script.
FULL_CAPABILITIES = DEFAULT_CAPABILITIES + [
    "local.check",
    "local.build_selena",
    "local.run_sim",
]


def register(subparsers):
    parser = subparsers.add_parser("agent", help="Run a polling Windows agent for control jobs")
    parser.add_argument("--server-url", default="http://127.0.0.1:8877", help="Base URL for the control server")
    parser.add_argument("--agent-id", default="", help="Stable agent id; omit to auto-register a new one")
    parser.add_argument("--name", default="", help="Agent display name")
    parser.add_argument("--hostname", default=socket.gethostname(), help="Agent hostname")
    parser.add_argument("--platform", dest="platform_name", default=platform_mod.platform(), help="Agent platform string")
    parser.add_argument(
        "--capability",
        action="append",
        default=[],
        help="Repeatable task capability filter. Default (Mode A): cluster.run + tcc.* only. "
        "For Mode B (Windows local repo with full toolchain) add local.check / "
        "local.build_selena / local.run_sim to also claim local build/sim tasks.",
    )
    parser.add_argument("--poll-interval", type=float, default=3.0, help="Seconds between polls when idle")
    parser.add_argument("--heartbeat-interval", type=float, default=10.0, help="Seconds between heartbeats during task execution")
    parser.add_argument("--request-timeout", type=int, default=30, help="HTTP request timeout in seconds")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")


def run(args, config):
    from core.user import current_user
    user = current_user()
    client = _ControlClient(getattr(args, "server_url", "http://127.0.0.1:8877"), timeout=int(getattr(args, "request_timeout", 30) or 30))
    capabilities = list(getattr(args, "capability", []) or []) or list(DEFAULT_CAPABILITIES)
    hostname = getattr(args, "hostname", "") or socket.gethostname()
    name = getattr(args, "name", "") or f"{hostname}-agent"
    # Default agent_id embeds user+hostname so two users on one machine don't collide.
    default_agent_id = f"agent-{user}-{hostname}"
    agent = client.register_agent(
        name=name,
        agent_id=getattr(args, "agent_id", "") or default_agent_id,
        hostname=hostname,
        platform=getattr(args, "platform_name", "") or platform_mod.platform(),
        capabilities=capabilities,
        metadata={"cwd": str(ROOT), "user": user},
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
        exit_code = _run_task(client, agent_id, task, heartbeat_interval=float(getattr(args, "heartbeat_interval", 10.0) or 10.0))
        if once:
            return exit_code


def _run_task(client: "_ControlClient", agent_id: str, task: dict, *, heartbeat_interval: float) -> int:
    task_id = task["task_id"]
    try:
        command = _build_task_command(task)
    except (KeyError, TypeError, ValueError) as exc:
        message = f"[agent] task setup error: {exc}"
        client.append_logs(task_id, [message])
        client.submit_result(
            task_id,
            agent_id=agent_id,
            status="failed",
            returncode=-1,
            result={"cwd": str(ROOT), "error": str(exc)},
        )
        return 1
    command_text = _quote_command(command)
    client.append_logs(task_id, [f"[agent] starting {task['task_type']}", f"[agent] command: {command_text}"])
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
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(ROOT),
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
        client.append_logs(task_id, [f"[agent] execution error: {exc}"])
        status = "failed"
        returncode = returncode if returncode is not None else -1
    finally:
        stop_event.set()
        thread.join(timeout=max(1.0, heartbeat_interval))

    if returncode is None:
        returncode = proc.returncode if "proc" in locals() and proc.returncode is not None else (-15 if status == "cancelled" else -1)
    result = {
        "command": command,
        "cwd": str(ROOT),
    }
    client.submit_result(task_id, agent_id=agent_id, status=status, returncode=returncode, result=result)
    return 0 if status == "succeeded" else 1


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
    def __init__(self, server_url: str, *, timeout: int) -> None:
        self._server_url = server_url.rstrip("/")
        self._timeout = timeout

    def register_agent(self, *, name: str, agent_id: str, hostname: str, platform: str, capabilities: list[str], metadata: dict) -> dict:
        return self._request(
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

    def poll(self, agent_id: str) -> dict:
        return self._request("POST", "/api/agents/poll", {"agent_id": agent_id})

    def heartbeat(self, agent_id: str, *, status: str, current_task_id: str = "") -> dict:
        return self._request(
            "POST",
            "/api/agents/heartbeat",
            {
                "agent_id": agent_id,
                "status": status,
                "current_task_id": current_task_id,
            },
        )

    def append_logs(self, task_id: str, lines: list[str]) -> dict:
        return self._request("POST", "/api/tasks/logs", {"task_id": task_id, "lines": lines, "stream": "stdout"})

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

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        from core.user import USER_HEADER, current_user
        data = None
        headers = {"Accept": "application/json", USER_HEADER: current_user()}
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
