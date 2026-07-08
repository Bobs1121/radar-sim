"""Server-side cluster executor — runs cluster.run tasks without a Windows agent.

When the Linux control server enables ``--cluster-executor`` (Mode A: Linux
cluster-only service), this module starts an in-process agent that claims
``cluster.run`` tasks and executes them **directly** via
``core.cluster.prepare_cluster_job`` + ``submit_cluster_job`` — no subprocess,
no Windows machine required.

This lets T3 users (no code repo, no toolchain) and T2 users (compiled Selena
uploaded to a share) submit cluster simulations from the browser/curl and have
the Linux server package + submit to SZHRADAR itself. The cluster nodes (which
have MATLAB/Qt/Boost) run selena; the Linux server only writes the job folder
to the shared workspace and calls the manager XML-RPC.

Prerequisites on the Linux server:
  - PyYAML (to load project config resolving dataset/profile)
  - Write access to ``cluster.workspace_root`` (mount the SMB share)
  - Network reachability to ``SZHRADAR01:8123`` (XML-RPC submit)
  - The project config tree under ``config/projects/<project>/``
"""

from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

LogCallback = Callable[[str, list[str]], None]
ResultCallback = Callable[[str, str, int, dict[str, Any]], None]


def execute_cluster_run(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    log: LogCallback,
    dry_run: bool = False,
) -> tuple[str, int, dict[str, Any]]:
    """Execute one cluster.run task directly (no subprocess).

    Returns ``(status, returncode, result)``. ``log`` is called with incremental
    log lines (task_id, [lines]). Mirrors what the Windows agent does via
    subprocess + append_logs, but in-process.
    """
    task_id = str(task.get("task_id") or "")
    payload = dict(task.get("payload") or {})

    try:
        from core.cluster import prepare_cluster_job, submit_cluster_job
    except ImportError as exc:
        log(task_id, [f"[executor] missing dependency: {exc}"])
        return "failed", -1, {"error": str(exc)}

    # Resolve inputs from the payload. profile is passed to prepare_cluster_job
    # (it applies the cluster profile internally via apply_cluster_profile).
    profile = str(payload.get("profile") or "")
    input_path = str(payload.get("input_mf4") or payload.get("input_path") or "")
    dataset = str(payload.get("dataset") or "")
    run_id = str(payload.get("run_id") or "")
    copy_data = payload.get("copy_data")
    copy_selena = payload.get("copy_selena")

    log(task_id, [
        f"[executor] preparing cluster job (project={config.get('project', {}).get('name', '?')}, "
        f"profile={profile or 'default'}, dataset={dataset or input_path or '(none)'})",
    ])

    try:
        prepared = prepare_cluster_job(
            config,
            input_path=input_path or None,
            dataset=dataset or None,
            run_id=run_id or None,
            profile=profile or None,
            copy_data=copy_data,
            copy_selena=copy_selena,
        )
    except Exception as exc:
        log(task_id, [f"[executor] prepare failed: {exc}", traceback.format_exc()])
        return "failed", -1, {"error": str(exc)}

    # prepare_cluster_job returns a PreparedJob with job_dir, config_path, warnings.
    job_dir = str(getattr(prepared, "job_dir", "") or "")
    config_path = str(getattr(prepared, "config_path", "") or "")
    warnings = list(getattr(prepared, "warnings", []) or [])
    if warnings:
        log(task_id, [f"[executor] warning: {w}" for w in warnings])

    log(task_id, [
        f"[executor] job package prepared: {job_dir}",
        f"[executor] Config.cfg: {config_path}",
    ])

    # Submit (dry_run unless payload.execute is true — matches rsim cluster run default).
    should_execute = bool(payload.get("execute")) and not dry_run
    try:
        result = submit_cluster_job(config_path, config, dry_run=not should_execute)
    except Exception as exc:
        log(task_id, [f"[executor] submit failed: {exc}", traceback.format_exc()])
        return "failed", -1, {"error": str(exc), "job_dir": job_dir}

    mode = getattr(result, "mode", "?")
    rc = int(getattr(result, "returncode", 0) or 0)
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    if stdout:
        log(task_id, [line for line in stdout.splitlines() if line.strip()])
    if stderr:
        log(task_id, [f"[stderr] {line}" for line in stderr.splitlines() if line.strip()])

    status = "succeeded" if rc == 0 else "failed"
    result_dict = {
        "job_dir": job_dir,
        "config_path": config_path,
        "submit_mode": mode,
        "dry_run": bool(getattr(result, "dry_run", True)),
        "command": list(getattr(result, "command", []) or []),
        "stdout": stdout,
        "stderr": stderr,
    }
    if not should_execute:
        log(task_id, ["[executor] DRY-RUN: prepared only. Set payload.execute=true to submit."])
    elif rc != 0:
        # Submit itself failed — don't poll, keep status=failed.
        log(task_id, [f"[executor] submit failed (returncode={rc})"])
    else:
        log(task_id, [f"[executor] submitted via {mode}, returncode={rc}"])
        # Submit succeeded → the cluster worker now runs selena asynchronously.
        # Poll the official cluster web status until the job finishes, so the
        # task's terminal status reflects the real simulation outcome (not just
        # the submit returncode). Returns running→succeeded/failed.
        manager_value = stdout.strip().splitlines()[-1] if stdout.strip() else ""
        poll_status, poll_detail = _poll_cluster_completion(config, job_dir, manager_value, log, task_id)
        if poll_status:
            status = "succeeded" if poll_status == "finished" else "failed"
            result_dict["cluster_state"] = poll_status
            result_dict["cluster_detail"] = poll_detail
            log(task_id, [f"[executor] cluster job {poll_status}: {poll_detail}"])
        else:
            result_dict["cluster_state"] = "unknown"
            log(task_id, ["[executor] cluster status unknown (submit OK); check OUT_ dir manually"])
    return status, rc, result_dict


def _poll_cluster_completion(config: dict, job_dir: str, manager_value: str, log, task_id: str):
    """Poll the official cluster web status until the job finishes or times out.

    Returns ``(state, detail)`` where state is "finished"/"failed"/"running".
    Times out after ~30 minutes of polling (worker runs minutes to tens of minutes).
    """
    from core.cluster import get_cluster_web_status

    # Try job_dir path first (most reliable — _find_web_job_id_by_path matches it
    # on the official jobs page); fall back to the manager-returned value.
    queries = [job_dir]
    if manager_value and manager_value.lower().startswith("value="):
        queries.append(manager_value.split("=", 1)[1].strip())
    max_minutes = 30
    deadline = time.time() + max_minutes * 60
    last_state = ""
    while time.time() < deadline:
        for q in queries:
            try:
                info = get_cluster_web_status(config, q)
            except Exception as exc:
                log(task_id, [f"[executor] status poll error: {exc}"])
                continue
            if not info.get("found"):
                continue
            state = str(info.get("state") or "")
            tasks = info.get("tasks") or []
            if tasks:
                states = [str(t.get("simulation_state") or "") for t in tasks]
                n_ok = sum(1 for s in states if s == "finished")
                n_fail = sum(1 for s in states if s in ("failed", "error", "aborted"))
                detail = f"{n_ok}/{len(tasks)} finished, {n_fail} failed"
                if state != last_state:
                    log(task_id, [f"[executor] cluster progress: {detail} ({state})"])
                    last_state = state
                if all(s == "finished" for s in states):
                    return "finished", detail
                if n_fail > 0 and n_ok + n_fail == len(tasks):
                    return "failed", detail
        time.sleep(15)
    return "running", f"timed out after {max_minutes} min (still running on cluster)"


class ClusterExecutor:
    """Background agent that claims cluster.run tasks and runs them in-process.

    Talks to the control server over HTTP (same as a Windows agent) so it reuses
    the existing job/task/logs/heartbeat machinery. Runs as a daemon thread
    inside the server process when ``--cluster-executor`` is set.
    """

    def __init__(
        self,
        server_url: str,
        config_loader: Callable[[str], dict[str, Any]],
        *,
        agent_id: str = "server-cluster-executor",
        poll_interval: float = 3.0,
        heartbeat_interval: float = 10.0,
        request_timeout: int = 30,
    ) -> None:
        self.server_url = server_url
        self.config_loader = config_loader  # (project_name) -> config dict
        self.agent_id = agent_id
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.request_timeout = request_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="cluster-executor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        from cli.agent import _ControlClient  # reuse the HTTP client
        import platform as platform_mod

        client = _ControlClient(self.server_url, timeout=self.request_timeout)
        registered = False
        while not self._stop.is_set():
            try:
                if not registered:
                    client.register_agent(
                        name="server-cluster-executor",
                        agent_id=self.agent_id,
                        hostname=platform_mod.node() or "linux-server",
                        platform=platform_mod.platform(),
                        capabilities=["cluster.run"],
                        metadata={},
                    )
                    registered = True
                task = client.poll(self.agent_id).get("task")
                if not task:
                    time.sleep(self.poll_interval)
                    continue
                self._run_one(client, task)
            except Exception as exc:
                # Network blip / server down — back off and retry.
                time.sleep(self.poll_interval)

    def _run_one(self, client, task: dict[str, Any]) -> None:
        task_id = str(task.get("task_id") or "")
        task_type = str(task.get("task_type") or "")
        if task_type != "cluster.run":
            # Not ours — shouldn't happen since we registered capability=cluster.run.
            client.submit_result(
                task_id, agent_id=self.agent_id, status="failed", returncode=-1,
                result={"error": f"executor only handles cluster.run, got {task_type}"},
            )
            return

        payload = dict(task.get("payload") or {})
        project = str(payload.get("project") or "")
        try:
            config = self.config_loader(project)
        except Exception as exc:
            client.append_logs(task_id, [f"[executor] config load failed for project '{project}': {exc}"])
            client.submit_result(
                task_id, agent_id=self.agent_id, status="failed", returncode=-1,
                result={"error": str(exc)},
            )
            return

        def log(_task_id: str, lines: list[str]) -> None:
            try:
                client.append_logs(_task_id, lines)
            except Exception:
                pass

        # Heartbeat in a background thread while the job runs.
        stop_hb = threading.Event()

        def heartbeat() -> None:
            while not stop_hb.wait(max(1.0, self.heartbeat_interval)):
                try:
                    client.heartbeat(self.agent_id, status="busy", current_task_id=task_id)
                except Exception:
                    pass

        hb_thread = threading.Thread(target=heartbeat, daemon=True)
        hb_thread.start()
        try:
            status, returncode, result = execute_cluster_run(task, config, log=log)
        except Exception as exc:
            status, returncode, result = "failed", -1, {"error": str(exc)}
        finally:
            stop_hb.set()
            hb_thread.join(timeout=max(1.0, self.heartbeat_interval))

        client.submit_result(
            task_id, agent_id=self.agent_id, status=status, returncode=returncode, result=result,
        )
