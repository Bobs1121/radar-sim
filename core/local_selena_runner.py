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
from core.progress_parser import parse_sim_progress
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
    log_files: list[Path] = []
    progress_paths: list[Path] = []
    try:
        controlled_work.mkdir(parents=True, exist_ok=True)
        paramconfig = controlled_work / f"paramconfig-{request.item_index:04d}.txt"
        private_log = controlled_work / f"selena-{request.item_index:04d}.log"
        sim_base = config.setdefault("simulation", {})
        sim_base["paramconfig_dir"] = str(controlled_work)
        sim_base["paramconfig_path"] = str(paramconfig)
        sim_base["log_file"] = str(controlled_work / f"CRlog-{request.item_index:04d}.log")
        log_files = [private_log, Path(sim_base["log_file"])]
        sim_base["input_mf4"] = str(request.input_mf4)
        sim_base["output_mf4"] = str(request.output_mf4)
        config.setdefault("assets", {})["fixed_config_path"] = str(paramconfig)
        config.setdefault("paths", {})["input_mf4"] = str(request.input_mf4)
        config["paths"]["output_mf4"] = str(request.output_mf4)

        # V2 uses one Selena invocation contract. Product recipes must not
        # mutate runtime arguments or make Adapter/MatFilter product-specific.
        sim = get_simulation_config(config)
        config = apply_simulation_to_config(config, sim)
        sim = build_effective_simulation(
            config,
            str(request.input_mf4),
            output_mf4=str(request.output_mf4),
        )
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
        rendered_log = str((config.get("simulation") or {}).get("log_file") or "").strip()
        if rendered_log:
            rendered_log_path = Path(rendered_log)
            if _contained(lease_root, rendered_log_path):
                log_files.append(rendered_log_path)
        progress_paths = list(dict.fromkeys(log_files))
    except Exception as exc:
        # The lease layer redacts physical paths before publishing diagnostics.
        # Preserve the exception class/message here so a framework-owned
        # template or parameter adaptation problem is actionable instead of
        # collapsing into an opaque ``paramconfig_failed`` code.
        diagnostic = f"Paramconfig preparation failed: {type(exc).__name__}: {exc}"
        return LocalRunOutcome(1, "paramconfig_failed", (diagnostic,))

    timeout = max(0, int(request.timeout_seconds))
    started = time.monotonic()
    process = None
    job = None
    progress_offsets: dict[Path, int] = {}
    progress_buffers: dict[Path, str] = {}
    last_progress = -1.0
    last_progress_report_at = 0.0
    fatal_engine_line = ""

    def report_simulation_progress() -> None:
        """Read only newly appended engine lines and report real progress."""

        nonlocal last_progress, last_progress_report_at, fatal_engine_line
        callback = request.progress_callback
        for log_path in progress_paths:
            try:
                size = int(log_path.stat().st_size)
                offset = int(progress_offsets.get(log_path, 0))
                if size < offset:
                    offset = 0
                    progress_buffers.pop(log_path, None)
                if size == offset:
                    continue
                with log_path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                progress_offsets[log_path] = offset + len(chunk)
                text = progress_buffers.get(log_path, "") + chunk.decode(
                    "utf-8", errors="replace"
                )
                lines = text.splitlines(keepends=True)
                if lines and not lines[-1].endswith(("\n", "\r")):
                    progress_buffers[log_path] = lines.pop()
                else:
                    progress_buffers.pop(log_path, None)
                for raw_line in lines:
                    if _is_fatal_engine_line(raw_line) and not fatal_engine_line:
                        fatal_engine_line = raw_line.strip()[:2000]
                    parsed = parse_sim_progress(raw_line)
                    if parsed is None:
                        continue
                    done, total = parsed
                    percentage = min(100.0, max(0.0, done / total * 100.0))
                    value = min(0.99, percentage / 100.0)
                    if callback is None:
                        continue
                    now = time.monotonic()
                    if value <= last_progress and now - last_progress_report_at < 2.0:
                        continue
                    last_progress = max(last_progress, value)
                    last_progress_report_at = now
                    callback(
                        value,
                        f"Selena simulation {percentage:.1f}% ({done}/{total})",
                    )
            except (OSError, UnicodeError):
                # The engine log is advisory.  Process liveness and the final
                # exit code remain authoritative if a log handle is transiently
                # unavailable on Windows.
                continue

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
                report_simulation_progress()
                if fatal_engine_line:
                    # Selena has already reported a terminal engine error but
                    # may keep the process alive while a recorder/descendant
                    # drains. Do not wait for an arbitrary wall-clock timeout;
                    # terminate this known-failed engine run and surface the
                    # diagnostic immediately.
                    job.terminate(1)
                    outcome = LocalRunOutcome(
                        1, "selena_failed", (fatal_engine_line,)
                    )
                    break
                if cancel_requested():
                    job.terminate(130)
                    outcome = LocalRunOutcome(130, "cancelled")
                    break
                if timeout > 0 and time.monotonic() - started >= timeout:
                    job.terminate(124)
                    outcome = LocalRunOutcome(124, "runtime_timeout")
                    break
                time.sleep(0.25)
            else:
                report_simulation_progress()
                if fatal_engine_line:
                    job.terminate(1)
                    outcome = LocalRunOutcome(
                        1, "selena_failed", (fatal_engine_line,)
                    )
                else:
                    returncode = int(process.returncode or 0)
                    outcome = LocalRunOutcome(
                        returncode,
                        _selena_error_code(returncode),
                    )
    except (OSError, subprocess.SubprocessError):
        if job is not None:
            job.terminate(1)
        elif process is not None and process.poll() is None:
            process.kill()
        outcome = LocalRunOutcome(1, "selena_launch_failed")
    finally:
        if job is not None:
            job.close()
    return _with_private_logs(outcome, log_files)


def _selena_error_code(returncode: int) -> str:
    """Classify Windows loader failures without interpreting engine errors."""

    if returncode == 0:
        return ""
    windows_code = int(returncode) & 0xFFFFFFFF
    return {
        0xC0000135: "selena_dependency_missing",
        0xC000007B: "selena_dependency_invalid_architecture",
        0xC0000142: "selena_dependency_initialization_failed",
    }.get(windows_code, "selena_failed")


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


def _with_private_logs(outcome: LocalRunOutcome, log_files: list[Path]) -> LocalRunOutcome:
    """Attach a bounded tail of Selena output to the logical run outcome.

    The full log remains inside the Agent lease.  Only a small tail is carried
    to the control plane so a failed internal simulation is diagnosable without
    turning Linux into a log-file proxy or leaking an Agent-local path.
    """
    lines: list[str] = []
    seen: set[Path] = set()
    for log_file in log_files:
        try:
            resolved = log_file.resolve(strict=False)
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            with resolved.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 256 * 1024), os.SEEK_SET)
                text = handle.read().decode("utf-8", errors="replace")
            lines.extend(line.rstrip() for line in text.splitlines() if line.strip())
        except (OSError, UnicodeError):
            continue
    lines = lines[-200:]
    if not lines:
        return outcome
    return LocalRunOutcome(outcome.exit_code, outcome.error_code, lines)


def _safe_extra_arg(value: str) -> bool:
    text = str(value or "")
    return bool(text) and len(text) <= 256 and "\x00" not in text and "\r" not in text and "\n" not in text


def _is_fatal_engine_line(line: str) -> bool:
    """Recognize explicit Selena terminal-error diagnostics only."""

    text = str(line or "").casefold()
    return any(
        marker in text
        for marker in (
            "no signal found in channel cache",
            "simulation was failed",
            "return value was incorrect",
            "selena returned a non-zero result",
        )
    )


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
