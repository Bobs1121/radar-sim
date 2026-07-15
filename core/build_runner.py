"""Background task runner for Selena builds and simulations.

Provides a process-level TaskRegistry so the web frontend can start a long
build/sim, poll for incremental logs, and cancel it — without blocking the
HTTP request or needing SSE.

A task runs in a daemon thread: subprocess.Popen with a line-buffered stdout
pipe, appended to a list the poller can tail from a given offset.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.config import load_config, resolve_selena_executable
from core.recipes import get_for_config
from core.repo import prepare_repo_context


@dataclass
class BuildTask:
    task_id: str
    project: str
    kind: str = "build"  # build | sim
    status: str = "queued"  # queued | running | success | failed | cancelled
    started_at: float = 0.0
    finished_at: float = 0.0
    stdout_lines: list[str] = field(default_factory=list)
    returncode: Optional[int] = None
    errors: list[str] = field(default_factory=list)
    exe_path: str = ""
    current_file: str = ""
    files_done: int = 0
    files_total: int = 0

    @property
    def duration_sec(self) -> float:
        end = self.finished_at or time.time()
        return round(end - self.started_at, 1) if self.started_at else 0.0


class TaskRegistry:
    """Process-singleton holding active build/sim tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, BuildTask] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def start_build(self, project: str, mode: str = "RelWithDebInfo", clean: bool = False, config: Optional[dict] = None) -> str:
        """Start a Selena build in a background thread. Returns task_id."""
        task_id = f"build_{uuid.uuid4().hex[:8]}"
        task = BuildTask(task_id=task_id, project=project, kind="build", status="queued", started_at=time.time())
        with self._lock:
            self._tasks[task_id] = task
        self._persist(task)
        thread = threading.Thread(target=self._run_build, args=(task_id, project, mode, clean, config), daemon=True)
        thread.start()
        return task_id

    def start_sim(self, project: str, *, backend: str, data_path: str, dry_run: bool = False, config: Optional[dict] = None) -> str:
        """Start a simulation (local or cluster dry-run). Returns task_id."""
        task_id = f"sim_{uuid.uuid4().hex[:8]}"
        task = BuildTask(task_id=task_id, project=project, kind="sim", status="queued", started_at=time.time())
        with self._lock:
            self._tasks[task_id] = task
        self._persist(task)
        thread = threading.Thread(target=self._run_sim, args=(task_id, project, backend, data_path, dry_run, config), daemon=True)
        thread.start()
        return task_id

    def start_tcc_task(self, project: str, action: str, toolcollection: str = "", config: Optional[dict] = None) -> str:
        """Start a TCC bootstrap/install task in a background thread. Returns task_id.

        action: 'bootstrap_itc2' | 'install_toolcollection' | 'auto_repair_all'.
        Reuses BuildTask (kind='tcc'); poll via /api/build/status.
        """
        task_id = f"tcc_{uuid.uuid4().hex[:8]}"
        task = BuildTask(task_id=task_id, project=project, kind="tcc", status="queued", started_at=time.time())
        with self._lock:
            self._tasks[task_id] = task
        self._persist(task)
        thread = threading.Thread(target=self._run_tcc, args=(task_id, project, action, toolcollection, config), daemon=True)
        thread.start()
        return task_id

    def get(self, task_id: str) -> Optional[BuildTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def tail(self, task_id: str, since: int = 0) -> dict[str, Any]:
        task = self.get(task_id)
        if task:
            # Active task in memory — return incrementally.
            lines = task.stdout_lines[since:] if since < len(task.stdout_lines) else []
            return {
                "found": True, "task_id": task_id, "status": task.status,
                "returncode": task.returncode, "lines": lines,
                "total_lines": len(task.stdout_lines), "errors": task.errors,
                "exe_path": task.exe_path, "current_file": task.current_file,
                "files_done": task.files_done, "files_total": task.files_total,
                "duration_sec": task.duration_sec,
            }
        # Not in memory — check SQLite (historical or refreshed-mid-run task).
        return self._tail_from_store(task_id, since)

    def _tail_from_store(self, task_id: str, since: int) -> dict[str, Any]:
        from core.task_store import get_store
        stored = get_store().load_task(task_id)
        if not stored:
            return {"found": False}
        lines = get_store().tail_logs(task_id, since)
        started = stored.get("started_at") or 0.0
        finished = stored.get("finished_at") or 0.0
        duration = round((finished or time.time()) - started, 1) if started else 0.0
        return {
            "found": True, "task_id": task_id, "status": stored["status"],
            "returncode": stored.get("returncode"), "lines": lines,
            "total_lines": stored.get("total_lines", 0), "errors": stored.get("errors", []),
            "exe_path": stored.get("exe_path", ""), "current_file": stored.get("current_file", ""),
            "files_done": stored.get("files_done", 0), "files_total": stored.get("files_total", 0),
            "duration_sec": duration,
        }

    def list_tasks(self, limit: int = 20) -> list[dict]:
        """Return recent tasks (newest first) from SQLite."""
        from core.task_store import get_store
        return get_store().list_tasks(limit)

    def _persist(self, task: BuildTask, new_lines: Optional[list[str]] = None) -> None:
        """Save task + new log lines to SQLite (called from run threads)."""
        try:
            from core.task_store import get_store
            get_store().save_task(task, new_lines=new_lines)
        except Exception:
            pass  # persistence failures must not break the run

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(task_id)
            task = self._tasks.get(task_id)
        if not task or task.status not in ("running", "queued"):
            return False
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
        task.status = "cancelled"
        task.finished_at = time.time()
        return True

    def register_proc(self, task_id: str, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs[task_id] = proc

    # ------------------------------------------------------------------
    # Build execution
    # ------------------------------------------------------------------

    def _run_build(self, task_id: str, project: str, mode: str, clean: bool, preloaded_config: Optional[dict] = None) -> None:
        task = self.get(task_id)
        if not task:
            return
        task.status = "running"
        last_persist = 0
        try:
            config = preloaded_config or load_config(project)
            # Switch repo branch first (same as CLI build).
            repo_msg = prepare_repo_context(config)
            if repo_msg:
                task.stdout_lines.append(f"[ERROR] {repo_msg}")
                task.errors.append(repo_msg)
                task.status = "failed"
                task.returncode = 1
                task.finished_at = time.time()
                return
            if config.get("build", {}).get("selena_branch"):
                task.stdout_lines.append(f"[INFO] Inner repo on branch: {config['build']['selena_branch']}")

            # Ensure TCC environment (itc2 + required toolcollection) before compiling.
            from core.tcc import ensure_environment
            itc2_status, tc_status = ensure_environment(config, log=lambda m: task.stdout_lines.append(f"[TCC] {m}"))
            if not itc2_status.installed:
                task.stdout_lines.append(f"[ERROR] itc2 unavailable: {itc2_status.detail}")
                task.errors.append(itc2_status.detail)
                task.status = "failed"
                task.returncode = 1
                task.finished_at = time.time()
                return
            if tc_status.name and not tc_status.installed:
                task.stdout_lines.append(f"[ERROR] toolcollection {tc_status.name} not available: {tc_status.detail}")
                task.errors.append(tc_status.detail)
                task.status = "failed"
                task.returncode = 1
                task.finished_at = time.time()
                return

            cmd, cwd = _build_selena_command(config, mode, clean)
            task.stdout_lines.append(f"[INFO] Build command: {' '.join(cmd)}")
            task.stdout_lines.append(f"[INFO] Working dir: {cwd}")
            env = _build_env(config)
            from core.build_lock import WorkspaceBuildLock, build_workspace_from_config
            build_lock = WorkspaceBuildLock(build_workspace_from_config(config)).acquire()
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, env=env, bufsize=1, cwd=cwd,
            )
            self.register_proc(task_id, proc)
            last_persist = len(task.stdout_lines)
            for line in iter(proc.stdout.readline, ""):
                if line:
                    task.stdout_lines.append(line.rstrip())
                    # Parse build progress tokens ([n/N] Compiling x.cpp) so the
                    # Web UI gets a real progress bar (PRD §1.7.4). Only the most
                    # recent token wins; legacy sim-style file counts set via
                    # start_sim are left untouched when no build token is present.
                    from core.progress_parser import parse_build_progress
                    parsed = parse_build_progress(line.rstrip())
                    if parsed:
                        done, total, label = parsed
                        task.files_done = done
                        task.files_total = total
                        if label:
                            task.current_file = label
                    # Persist every 20 lines for live progress without DB thrash.
                    if len(task.stdout_lines) - last_persist >= 20:
                        self._persist(task, new_lines=task.stdout_lines[last_persist:])
                        last_persist = len(task.stdout_lines)
            proc.stdout.close()
            retcode = proc.wait()
            task.returncode = retcode
            if retcode == 0:
                task.exe_path = resolve_selena_executable(config, build_mode=mode)
                task.stdout_lines.append(f"[OK] Build success. selena.exe: {task.exe_path}")
                task.status = "success"
            else:
                from core.build_diagnostics import extract_actionable_build_errors

                task.errors.extend(extract_actionable_build_errors(task.stdout_lines))
                task.errors.append(f"build exit code {retcode}")
                task.status = "failed"
            self._persist(task, new_lines=task.stdout_lines[last_persist:])
        except Exception as exc:
            task.errors.append(str(exc))
            task.status = "failed"
        finally:
            if "build_lock" in locals():
                build_lock.release()
            task.finished_at = time.time()
            self._persist(task, new_lines=task.stdout_lines[last_persist:])
    # Sim execution
    # ------------------------------------------------------------------

    def _run_sim(self, task_id: str, project: str, backend: str, data_path: str, dry_run: bool, preloaded_config: Optional[dict] = None) -> None:
        task = self.get(task_id)
        if not task:
            return
        task.status = "running"
        try:
            from core.api import prepare_simulation, run_local, submit_cluster

            # prepare_simulation loads its own config; for path-driven mode we pass
            # the project name (load_config will resolve it). preloaded_config is used
            # by build_runner callers that already have a config, via start_sim(config=).
            prepared = prepare_simulation(project, input_path=data_path, backend=backend)
            task.files_total = len(prepared.input_files)
            if not prepared.input_files:
                task.errors.append("no input MF4 files resolved")
                task.status = "failed"
                task.finished_at = time.time()
                return
            if backend == "cluster":
                task.stdout_lines.append(f"[INFO] Preparing cluster job for {data_path}")
                result = submit_cluster(prepared, dry_run=dry_run, input_path=data_path)
                task.stdout_lines.append(f"[INFO] submit mode={result.mode} returncode={result.returncode}")
                if result.stdout:
                    task.stdout_lines.append(result.stdout)
                if result.stderr:
                    task.stdout_lines.append(result.stderr)
                task.returncode = result.returncode
                task.status = "success" if result.returncode == 0 else "failed"
            else:
                for i, mf4 in enumerate(prepared.input_files, 1):
                    task.current_file = mf4
                    task.stdout_lines.append(f"[INFO] ({i}/{task.files_total}) {mf4}")
                    result = run_local(prepared, dry_run=dry_run, input_mf4=mf4)
                    task.files_done = i
                    if result.stdout:
                        task.stdout_lines.extend(result.stdout.splitlines()[-20:])
                    if not result.success:
                        task.errors.append(f"{mf4}: {result.errors}")
                        task.status = "failed"
                        break
                else:
                    task.returncode = 0
                    task.status = "success"
                    task.stdout_lines.append(f"[OK] Simulated {task.files_done}/{task.files_total} file(s)")
        except Exception as exc:
            task.errors.append(str(exc))
            task.status = "failed"
        finally:
            task.finished_at = time.time()

    def _run_tcc(self, task_id: str, project: str, action: str, toolcollection: str, preloaded_config: Optional[dict] = None) -> None:
        task = self.get(task_id)
        if not task:
            return
        task.status = "running"
        try:
            from core.config import load_config
            from core.tcc import ensure_itc2, install_toolcollection, read_required_toolcollection
            config = preloaded_config or load_config(project)
            log = lambda m: task.stdout_lines.append(m)
            if action == "bootstrap_itc2":
                status = ensure_itc2(config, log=log)
                task.returncode = 0 if status.installed else 1
                task.status = "success" if status.installed else "failed"
                if not status.installed:
                    task.errors.append(status.detail)
            elif action == "install_toolcollection":
                tc = toolcollection or read_required_toolcollection(config)
                if not tc:
                    task.errors.append("no toolcollection specified and none configured")
                    task.status = "failed"
                    task.returncode = 1
                else:
                    result = install_toolcollection(config, tc, log=log)
                    task.returncode = result.returncode
                    task.status = "success" if result.ok else "failed"
                    if not result.ok:
                        task.errors.append(result.detail)
            elif action == "auto_repair_all":
                from core.tcc import auto_repair_environment
                report = auto_repair_environment(config, log=log)
                task.returncode = 0 if report.ok else 1
                task.status = "success" if report.ok else "failed"
                task.stdout_lines.append(f"[RESULT] {report.summary}")
                for step in report.steps:
                    task.stdout_lines.append(f"  [{step.name}] {'OK' if step.ok else 'FAIL'}: {step.detail}")
                if not report.ok:
                    task.errors.append(report.summary)
            else:
                task.errors.append(f"unknown tcc action: {action}")
                task.status = "failed"
                task.returncode = 1
        except Exception as exc:
            task.errors.append(str(exc))
            task.status = "failed"
        finally:
            task.finished_at = time.time()


# Module-level singleton.
_REGISTRY = TaskRegistry()


def get_registry() -> TaskRegistry:
    return _REGISTRY


# ------------------------------------------------------------------
# Build command helpers (extracted from cli/build.py)
# ------------------------------------------------------------------

def _build_selena_command(config: dict, mode: str, clean: bool) -> tuple[list[str], Optional[str]]:
    """Construct the Selena build command + cwd from config."""
    build = config.get("build", {}) or {}
    script = build.get("selena_build_script", "") or config.get("selena_build_script", "")
    if script and os.path.exists(script):
        return _build_selena_script_command(config, mode)
    # Fallback: direct R2D2 invocation.
    r2d2 = config.get("paths", {}).get("r2d2_script", "")
    build_config = config.get("paths", {}).get("build_config", "")
    build_config_full = build.get("build_config", build_config)
    python3 = config.get("environment", {}).get("python3_path", "python3")
    config_path = _resolve_config_path(build_config_full, config.get("project_root", ""))
    cmd = [python3, r2d2, "-m", config_path]
    if clean:
        cmd.append("-clean")
    cmd.extend(["-ghs_math", "-use_mat", "-notests", "-bm", mode])
    vs_postfix = config.get("vs_postfix", "") or _detect_vs_postfix()
    if vs_postfix:
        cmd.extend(vs_postfix.split())
    return cmd, None


def _build_selena_script_command(config: dict, mode: str) -> tuple[list[str], str]:
    handler = get_for_config(config)
    build = config.get("build", {})
    script = build.get("selena_build_script", "") or config.get("selena_build_script", "")
    args = handler.shape_selena_script_args(config, mode)
    cwd = build.get("script_workdir") or str(Path(script).parent)
    return ["cmd", "/c", script, *args], cwd


def _build_env(config: dict) -> dict[str, str]:
    """Build environment: pass through the system env unchanged.

    The jenkins_selena_build.bat script calls init.bat itself, which sets all
    TCCPATH_* vars (boost/python3/cmake/mingw64/selena_environment ...). radar-sim
    no longer injects BOOST/Qt/MATLAB into PATH — that previously conflicted with
    init.bat. The config environment.* fields are still used by VS-debug path
    rendering (render_selena_environment_path), independent of this function.
    """
    return os.environ.copy()


def _resolve_config_path(build_config: str, project_root: str) -> str:
    if os.path.isabs(build_config) and os.path.exists(build_config):
        return build_config
    config_name = build_config[:-7] if build_config.endswith(".config") else build_config
    candidates = [
        os.path.join(project_root, "apl", "byd", "selena", "cmake_build_cfg", f"{config_name}.config"),
        os.path.join(project_root, "apl", "byd", "selena", "config", "cmake", f"{config_name}.config"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0] if candidates else build_config


def _detect_vs_postfix() -> str:
    if os.path.exists(r"C:\Program Files (x86)\Microsoft Visual Studio\2019"):
        return "-vs vs16"
    if os.path.exists(r"C:\Program Files (x86)\Microsoft Visual Studio\2022"):
        return "-vs vs17"
    if os.path.exists(r"C:\Program Files (x86)\Microsoft Visual Studio\2017"):
        return "-vs vs15"
    return ""
