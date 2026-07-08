"""Stable public API for programmatic radar-sim usage.

Other software should import from ``core.api`` only. Functions in other
``core.*`` modules are internal and may change between minor versions; the
signatures exported here are stable within a minor version.

Quick start:

    from core.api import check_environment, prepare_simulation, run_local

    report = check_environment("ovrs25", profile="local-build", backend="local")
    if not report.ok:
        for err in report.errors:
            print(err)

    prepared = prepare_simulation("ovrs25", profile="local-build", input_path="D:/data/case.MF4")
    result = run_local(prepared, dry_run=True)   # dry-run first
    result = run_local(prepared)                 # real run
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.cluster import (
    SubmitResult,
    package_to_dict,
    prepare_cluster_job,
    submit_cluster_job,
)
from core.config import get_default_project, list_projects, load_config
from core.environment import CheckReport, check_for_backend
from core.profiles import apply_profile, list_profiles as _list_profiles
from core.simulation import get_simulation_config

__all__ = [
    "PreparedRun",
    "RunResult",
    "SubmitResult",
    "CheckReport",
    "load_project",
    "list_projects",
    "list_profiles",
    "prepare_simulation",
    "run_local",
    "submit_cluster",
    "check_environment",
]

API_VERSION = "1.0"


@dataclass
class PreparedRun:
    """A simulation ready to run on either backend.

    ``input_files`` is a list of resolved MF4 paths. For a single-file run it
    has one entry; for a dataset/directory run it has all selected files.
    """

    config: dict
    sim: dict
    selena_exe: str
    input_files: list[str]
    backend: str
    profile: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    """Result of a local simulation run."""

    success: bool
    return_code: int
    output_mf4: str = ""
    duration_sec: float = 0.0
    stdout: str = ""
    stderr: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_project(project: str = "", *, path: str = "") -> dict:
    """Load merged config by project name OR local.yaml path. Path takes precedence."""
    if path:
        from core.config import load_config_from_path
        return load_config_from_path(path)
    return load_config(project or get_default_project())


def list_profiles(project: str = "") -> list[dict]:
    """List unified profiles (local + cluster) for a project."""
    return _list_profiles(load_project(project))


def check_environment(
    project: str = "",
    *,
    profile: str = "",
    backend: str = "",
) -> CheckReport:
    """Run unified environment checks. Returns a CheckReport."""
    config = load_project(project)
    return check_for_backend(config, backend, profile=profile)


def prepare_simulation(
    project: str = "",
    *,
    profile: str = "",
    input_path: str = "",
    dataset: str = "",
    backend: str = "",
) -> PreparedRun:
    """Apply profile, resolve inputs, locate selena.exe. Does not run.

    For backend=cluster the returned PreparedRun carries the same config; call
    ``submit_cluster`` to package and submit it.
    """
    config = load_project(project)
    if profile:
        config = apply_profile(config, profile)
    target_backend = (backend or config.get("active_backend") or "local").strip().lower()

    sim = get_simulation_config(config)
    input_files, warnings = _resolve_inputs(sim, input_path=input_path, dataset=dataset)
    selena_exe = _resolve_selena(config)

    return PreparedRun(
        config=config,
        sim=sim,
        selena_exe=selena_exe,
        input_files=input_files,
        backend=target_backend,
        profile=str(config.get("active_profile") or profile or "default"),
        warnings=warnings,
    )


def run_local(
    prepared: PreparedRun,
    *,
    dry_run: bool = False,
    timeout: int = 3600,
    output_mf4: str = "",
    input_mf4: str = "",
) -> RunResult:
    """Execute a local Selena simulation.

    Runs ``selena.exe --paramconfig`` via the ``rsim run`` command (subprocess
    isolation keeps a selena crash from taking down the caller). For inline
    execution without a subprocess, call ``cli.run`` directly (less stable).

    For batch runs, pass ``input_mf4`` for each file in ``prepared.input_files``
    so the run uses the current file instead of always the first one.
    """
    if not prepared.input_files:
        return RunResult(False, 1, errors=["no input files in PreparedRun"])
    if not prepared.selena_exe or not Path(prepared.selena_exe).exists():
        return RunResult(False, 1, errors=[f"selena.exe not found: {prepared.selena_exe}"])

    rsim_py = str(Path(__file__).resolve().parent.parent / "rsim.py")
    # Use the explicitly-passed file for batch runs; default to the first for single-file callers.
    current_input = input_mf4 or prepared.input_files[0]
    cmd = [sys.executable, rsim_py, "--project", prepared.config.get("_meta", {}).get("project", ""), "run", current_input]
    if prepared.profile and prepared.profile != "default":
        cmd.extend(["--profile", prepared.profile])
    if output_mf4:
        cmd.extend(["--output-mf4", output_mf4])
    if timeout:
        cmd.extend(["--timeout", str(timeout)])
    if dry_run:
        cmd.append("--dry-run")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 120 if timeout else None)
    except subprocess.TimeoutExpired as exc:
        return RunResult(False, -1, errors=[f"timed out: {exc}"], stdout=exc.stdout or "", stderr=exc.stderr or "")

    success = result.returncode == 0
    return RunResult(
        success=success,
        return_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        errors=[] if success else [f"rsim run exit {result.returncode}"],
    )


def submit_cluster(
    prepared: PreparedRun,
    *,
    run_id: str = "",
    copy_data: Optional[bool] = None,
    copy_selena: Optional[bool] = None,
    dry_run: bool = True,
    input_path: str = "",
    dataset: str = "",
) -> "SubmitResult":
    """Prepare a cluster job package and submit it.

    When ``dry_run`` is True (default), only prepares the package and returns
    the submit command without calling the manager. Set ``dry_run=False`` to
    actually submit via XML-RPC.

    When copy_data/copy_selena are None, they are auto-decided from the
    Selena source and data path type (UNC data → no copy; local data → copy;
    source=build → copy selena runtime).
    """
    if not input_path and prepared.input_files:
        input_path = prepared.input_files[0]
    policy = _auto_copy_policy(prepared.config, input_path)
    if copy_data is None:
        copy_data = policy["copy_data"]
    if copy_selena is None:
        copy_selena = policy["copy_selena"]
    package = prepare_cluster_job(
        prepared.config,
        input_path=input_path,
        dataset=dataset,
        run_id=run_id,
        profile=prepared.profile if prepared.profile != "default" else "",
        copy_data=copy_data,
        copy_selena=copy_selena,
    )
    result = submit_cluster_job(package.config_path, prepared.config, dry_run=dry_run)
    return result


def _auto_copy_policy(config: dict, data_path: str) -> dict[str, bool]:
    """Backwards-compatible dict wrapper over core.policy.derive_run_policy.

    Returns {"copy_data": bool, "copy_selena": bool} so existing callers/tests
    keep working. New code should call core.policy.policy_from_config directly
    to get the full RunPolicy (with output_local, rationale, ...).
    """
    from core.policy import policy_from_config
    p = policy_from_config(config, data_path)
    return {"copy_data": p.copy_data, "copy_selena": p.copy_selena}


def _resolve_inputs(sim: dict, *, input_path: str, dataset: str) -> tuple[list[str], list[str]]:
    """Resolve input MF4 paths from an explicit path or a named dataset."""
    from core.data import iter_mf4_inputs

    warnings: list[str] = []
    if input_path:
        p = Path(input_path)
        if p.is_file():
            return [str(p)], warnings
        if p.is_dir():
            return [str(x) for x in iter_mf4_inputs(p)], warnings
        return [], [f"input path not found: {input_path}"]

    if dataset:
        for item in sim.get("datasets", []) or []:
            if item.get("name") != dataset:
                continue
            input_dir = str(item.get("input_mf4") or item.get("input_dir") or "")
            if not input_dir:
                return [], [f"dataset '{dataset}' has no input_dir/input_mf4"]
            return [str(x) for x in iter_mf4_inputs(Path(input_dir))], warnings
        return [], [f"dataset '{dataset}' not found"]

    return [], ["no input_path or dataset provided"]


def _resolve_selena(config: dict) -> str:
    """Resolve selena.exe from cluster.selena_exe or build_output."""
    cluster_exe = str((config.get("cluster") or {}).get("selena_exe") or "")
    if cluster_exe and Path(cluster_exe).exists():
        return cluster_exe
    from core.config import resolve_selena_executable
    exe = resolve_selena_executable(config)
    if exe and Path(exe).exists():
        return exe
    return cluster_exe or exe or ""
