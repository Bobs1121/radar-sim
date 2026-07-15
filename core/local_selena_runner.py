"""Native Selena adapter for a controlled Windows-full local run lease.

The adapter writes paramconfig, Selena stdout and MF4 output only below the
lease-controlled work/output roots.  It returns stable error codes and never
emits physical paths to the control plane.
"""

from __future__ import annotations

import copy
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from core.agent_local_run import LocalRunOutcome, LocalRunRequest
from core.config import render_selena_config, render_selena_environment_path
from core.recipes import get_for_config
from core.simulation import apply_simulation_to_config, build_effective_simulation, get_simulation_config


def run_local_selena(
    request: LocalRunRequest,
    cancel_requested: Callable[[], bool],
) -> LocalRunOutcome:
    """Render and execute one Selena input within the private lease roots."""
    config = copy.deepcopy(request.config)
    controlled_work = Path(str(config.get("_local_run", {}).get("controlled_work_directory") or ""))
    lease_root = controlled_work.parent
    if (
        not _contained(lease_root, request.output_mf4)
        or not _contained(lease_root, controlled_work)
        or request.output_mf4.parent.name != "outputs"
    ):
        return LocalRunOutcome(1, "runner_contract_failed")
    try:
        controlled_work.mkdir(parents=True, exist_ok=True)
        paramconfig = controlled_work / f"paramconfig-{request.item_index:04d}.txt"
        private_log = controlled_work / f"selena-{request.item_index:04d}.log"
        sim_base = config.setdefault("simulation", {})
        sim_base["paramconfig_dir"] = str(controlled_work)
        sim_base["paramconfig_path"] = str(paramconfig)
        sim_base["log_file"] = str(controlled_work / f"CRlog-{request.item_index:04d}.log")
        sim_base["input_mf4"] = str(request.input_mf4)
        sim_base["output_mf4"] = str(request.output_mf4)
        config.setdefault("assets", {})["fixed_config_path"] = str(paramconfig)
        config.setdefault("paths", {})["input_mf4"] = str(request.input_mf4)
        config["paths"]["output_mf4"] = str(request.output_mf4)

        handler = get_for_config(config)
        sim = handler.prepare_simulation(config, get_simulation_config(config), stage="base")
        config = apply_simulation_to_config(config, sim)
        sim = build_effective_simulation(
            config,
            str(request.input_mf4),
            output_mf4=str(request.output_mf4),
        )
        sim = handler.prepare_simulation(config, sim, stage="run")
        config = apply_simulation_to_config(config, sim)
        config.setdefault("assets", {})["fixed_config_path"] = str(paramconfig)
        rendered = render_selena_config(config)
        rendered_path = Path(str((rendered.get("assets") or {}).get("fixed_config_path") or ""))
        if rendered_path.resolve(strict=True) != paramconfig.resolve(strict=True):
            return LocalRunOutcome(1, "paramconfig_outside_lease")
        extra = [str(item) for item in sim.get("extra_args", []) or []]
        if any(not _safe_extra_arg(item) for item in extra):
            return LocalRunOutcome(1, "unsafe_runtime_argument")
        if sim.get("tolerant") and "--tolerant" not in extra:
            extra.append("--tolerant")
        command = [str(request.executable), "--paramconfig", str(paramconfig), *extra]
        environment = _runtime_environment(config)
    except Exception:
        return LocalRunOutcome(1, "paramconfig_failed")

    timeout = max(1, int(request.timeout_seconds))
    started = time.monotonic()
    process = None
    job = None
    try:
        with private_log.open("wb") as log_handle:
            process = subprocess.Popen(
                command,
                cwd=str(request.working_directory),
                env=environment,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            job = _WindowsKillJob(process)
            while process.poll() is None:
                if cancel_requested():
                    job.terminate(130)
                    return LocalRunOutcome(130, "cancelled")
                if time.monotonic() - started >= timeout:
                    job.terminate(124)
                    return LocalRunOutcome(124, "runtime_timeout")
                time.sleep(0.25)
            return LocalRunOutcome(int(process.returncode or 0), "" if process.returncode == 0 else "selena_failed")
    except (OSError, subprocess.SubprocessError):
        if job is not None:
            job.terminate(1)
        elif process is not None and process.poll() is None:
            process.kill()
        return LocalRunOutcome(1, "selena_launch_failed")
    finally:
        if job is not None:
            job.close()


def _runtime_environment(config: dict) -> dict[str, str]:
    env = dict(os.environ)
    rendered = render_selena_environment_path(config)
    rendered = rendered.replace("$(Path)", env.get("PATH", "")).replace(
        "$(LocalDebuggerEnvironment)", ""
    )
    segments: list[str] = []
    seen: set[str] = set()
    for segment in [item for item in rendered.split(";") if item] + env.get("PATH", "").split(";"):
        normalized = os.path.normcase(os.path.normpath(segment))
        if normalized and normalized not in seen:
            seen.add(normalized)
            segments.append(segment)
    env["PATH"] = ";".join(segments)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    boost = str((config.get("environment") or {}).get("boost_root") or config.get("boost_root") or "")
    if boost:
        env["BOOST_ROOT"] = boost
    return env


def _safe_extra_arg(value: str) -> bool:
    text = str(value or "")
    return bool(text) and len(text) <= 256 and "\x00" not in text and "\r" not in text and "\n" not in text


def _contained(root: Path, target: Path) -> bool:
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


class _WindowsKillJob:
    """Best-effort Windows Job Object that kills Selena descendants on close."""

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self._handle = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(name, ctypes.c_ulonglong) for name in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
                )]

            class BASIC_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class EXTENDED_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BASIC_LIMIT),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            info = EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                kernel32.CloseHandle(handle)
                return
            process_handle = wintypes.HANDLE(int(getattr(process, "_handle")))
            if not kernel32.AssignProcessToJobObject(handle, process_handle):
                kernel32.CloseHandle(handle)
                return
            self._handle = handle
            self._kernel32 = kernel32
        except Exception:
            self._handle = None

    def terminate(self, code: int) -> None:
        if self.process.poll() is not None:
            return
        if self._handle is not None:
            try:
                self._kernel32.TerminateJobObject(self._handle, max(1, int(code)))
                self.process.wait(timeout=5)
                return
            except Exception:
                pass
        self.process.kill()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._kernel32.CloseHandle(self._handle)
            finally:
                self._handle = None


__all__ = ["run_local_selena"]
