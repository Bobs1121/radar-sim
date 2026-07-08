"""Cluster V2.0 packaging and submission helpers.

The Cluster web page is a status surface. Automation should stage a job folder
on the shared workspace and submit the generated Config.cfg through client.py.
"""

from __future__ import annotations

import json
import html
import os
import re
import shutil
import sys
import socket
import subprocess
import time
import urllib.request
import xmlrpc.client
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from core.config import resolve_selena_executable
from core.data import (
    DataFile,
    check_data_access,
    copy_input_data,
    is_input_mf4,
    iter_mf4_inputs,
    looks_local_windows_path,
    scan_data_file,
    scan_segments,
)
from core.simulation import get_simulation_config

# Backwards-compatible alias: external callers (web API, tests) may reference
# ClusterDataFile. It is now the shared DataFile from core.data.
ClusterDataFile = DataFile


DEFAULT_WORKSPACE_ROOT = r"\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster"
DEFAULT_SOFTWARE_PATHS = [
    r"\\szhradar01\cluster_software",
    r"\\szhradar01\_cluster_software",
]

RUNTIME_COPY_EXCLUDES = {".pdb", ".ilk", ".exp", ".lib"}
RUNTIME_COPY_NAMES = {
    "selena.exe",
    "selena_dll.dll",
    "selena_core.dll",
    "selena_gui.dll",
    "Mdf4Lib_x64.dll",
    "MdfLibSort_x64.dll",
    "MDFSort_x64.dll",
    "Qt5Core.dll",
    "Qt5Xml.dll",
    "XmlParser_x64.dll",
}


@dataclass
class CheckItem:
    name: str
    ok: bool
    detail: str
    severity: str = "error"   # error | warning | info
    category: str = ""        # repo | selena | runtime | data | cluster | profile
    repair_hint: str = ""     # human-readable repair guidance
    auto_repairable: bool = False  # can sim fix this automatically?
    repair_action: str = ""   # switch_branch | build_selena | run_env_script


@dataclass
class ClusterJobPackage:
    run_id: str
    profile: str
    job_dir: str
    config_path: str
    simulation_script: str
    manifest_path: str
    datafile_path: str
    output_hint: str
    submit_command: list[str]
    warnings: list[str]


@dataclass
class PythonCandidate:
    path: str
    ok: bool
    detail: str


@dataclass
class SubmitResult:
    mode: str
    dry_run: bool
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


def get_cluster_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return cluster config with conservative defaults."""
    cluster = dict(config.get("cluster") or {})
    cluster.setdefault("workspace_root", DEFAULT_WORKSPACE_ROOT)
    cluster.setdefault("project_folder", "radar-sim")
    cluster.setdefault("software_path", _first_existing(DEFAULT_SOFTWARE_PATHS) or DEFAULT_SOFTWARE_PATHS[0])
    if cluster.get("software_path") and not Path(str(cluster["software_path"])).exists():
        fallback = _first_existing(DEFAULT_SOFTWARE_PATHS)
        if fallback:
            cluster["software_path"] = fallback
    cluster.setdefault("client_py", "client.py")
    cluster.setdefault("submit_mode", "auto")
    cluster.setdefault("manager_host", "SZHRADAR01")
    cluster.setdefault("manager_port", 8123)
    configured_python = str(cluster.get("python_path") or "").strip()
    detected_python = detect_python2_path(configured_python)
    if configured_python:
        cluster["python_path"] = configured_python if _python_config_looks_usable(configured_python) else (detected_python or configured_python)
    else:
        cluster["python_path"] = detected_python or r"C:\Python27\python.exe"
    cluster.setdefault("kill_password", "1234")
    cluster.setdefault("group", "Radar")
    cluster.setdefault("subgroup", "PSS2")
    cluster.setdefault("python_version", "*")
    cluster.setdefault("simulation_prio", 4)
    cluster.setdefault("simulation_type", 0)
    cluster.setdefault("timeout_min", 120)
    cluster.setdefault("extension", "*.MF4,*MF4.zip")
    cluster.setdefault("skip_dir", "Failed")
    cluster.setdefault("skip_filename", "")
    cluster.setdefault("filter", 0)
    cluster.setdefault("finalstep", 0)
    cluster.setdefault("send_email", 0)
    cluster.setdefault("send_netsend", 0)
    cluster.setdefault("copy_data", False)
    cluster.setdefault("copy_selena", False)
    cluster.setdefault("use_local_worker_temp", True)
    cluster.setdefault("required_input_signals", ["g_Golf_Fct_Hmi_RunnableHmi_internalstates"])
    cluster.setdefault(
        "dependency_paths",
        [
            r"\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\DA\Radar\02_GEN5\00_Simulation\matlab_r2018b\bin\win64",
            r"\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\DA\Radar\02_GEN5\00_Simulation\boost_1_63_0",
            r"\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\DA\Radar\02_GEN5\00_Simulation\qt_5_8_0",
        ],
    )
    return cluster


def list_cluster_profiles(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return cluster profiles in the legacy flat shape (CLI/web compatibility).

    Delegates to core.profiles.list_profiles and flattens the nested
    selena/data blocks back into top-level fields that existing callers
    (cli/cluster.py profiles, web console) expect.
    """
    from core.profiles import list_profiles

    flat: list[dict[str, Any]] = []
    for profile in list_profiles(config):
        selena = profile.get("selena") or {}
        data = profile.get("data") or {}
        item = {
            "name": profile.get("name"),
            "description": profile.get("description", ""),
            "backend": profile.get("backend", "local"),
            "selena_exe": str(selena.get("exe") or resolve_selena_executable(config) or ""),
            "selena_source": str(selena.get("source") or "build"),
            "runtime_xml": profile.get("runtime_xml", ""),
            "matfilefilter": profile.get("matfilefilter", ""),
            "adapter_file": profile.get("adapter_file", ""),
            "config_template": profile.get("config_template", ""),
            "source": profile.get("source", ""),
            "mounting_position": profile.get("mounting_position", ""),
            "required_input_signals": list(data.get("required_signals") or []),
            "data_copy": bool(data.get("copy", False)),
        }
        if profile.get("cluster"):
            item["cluster"] = profile["cluster"]
        flat.append(item)
    return flat


def apply_cluster_profile(config: dict[str, Any], profile_name: str = "") -> dict[str, Any]:
    """Return a config copy with the selected profile overlaid (cluster-compatible).

    Delegates to core.profiles.apply_profile and additionally records
    ``cluster.active_profile`` so cluster-side checks that read it directly
    keep working.
    """
    from core.profiles import apply_profile

    updated = apply_profile(config, profile_name)
    name = str(profile_name or "").strip() or "default"
    updated["cluster"] = dict(updated.get("cluster") or {})
    updated["cluster"]["active_profile"] = name
    return updated


def check_cluster_environment(config: dict[str, Any], *, profile: str = "") -> list[CheckItem]:
    """Check local visibility of the server Cluster integration points."""
    config = apply_cluster_profile(config, profile)
    cluster = get_cluster_config(config)
    software_path = Path(cluster["software_path"])
    workspace_root = Path(cluster["workspace_root"])
    python_path = Path(cluster["python_path"])
    client_path = _client_path(cluster)
    manager = _manager_item(cluster)
    submit_mode = _resolve_submit_mode(cluster)
    python_ok = _python_config_looks_usable(str(cluster.get("python_path") or ""))
    python_detail = _python_detail(str(python_path))
    if not python_ok and submit_mode == "xmlrpc" and manager.ok:
        python_detail = f"{python_detail}; optional because XML-RPC submit path is reachable"

    items = [
        _path_item("Cluster software path", software_path, must_be_dir=True),
        _path_item("client.py", client_path),
        _path_item("manager.py", software_path / "manager.py"),
        _path_item("worker.py", software_path / "worker.py"),
        _path_item("database.py", software_path / "database.py"),
        _path_item("simulation_runtime.py", software_path / "simulation_runtime.py"),
        _path_item("Cluster workspace root", workspace_root, must_be_dir=True),
        CheckItem("Python for client.py", python_ok or (submit_mode == "xmlrpc" and manager.ok), python_detail),
        manager,
        CheckItem("Submit path", (python_ok and submit_mode == "client") or (submit_mode == "xmlrpc" and manager.ok), submit_mode),
    ]

    writable_detail = "not checked"
    writable_ok = False
    try:
        probe_dir = workspace_root / cluster["project_folder"] / "_rsim_probe"
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe_file = probe_dir / "write_probe.txt"
        probe_file.write_text("radar-sim cluster probe\n", encoding="utf-8")
        writable_ok = probe_file.exists()
        writable_detail = str(probe_file)
    except Exception as exc:  # pragma: no cover - depends on network state
        writable_detail = str(exc)
    items.append(CheckItem("Workspace write probe", writable_ok, writable_detail))

    for dep in cluster.get("dependency_paths", []) or []:
        items.append(_path_item(f"Worker dependency path: {dep}", Path(dep), must_be_dir=True))
    active_profile = str(cluster.get("active_profile") or profile or "default")
    items.append(CheckItem("Cluster profile", True, active_profile))
    selena_exe = str(cluster.get("selena_exe") or resolve_selena_executable(config) or "")
    if selena_exe:
        items.append(_path_item("Profile Selena executable", Path(selena_exe)))
    sim = get_simulation_config(config)
    runtime_xml = str(sim.get("runtime_xml") or "")
    if runtime_xml:
        items.append(_path_item("Profile runtime XML", Path(runtime_xml)))

    return items


def detect_python2_candidates(configured: str = "") -> list[PythonCandidate]:
    """Return possible Python2 executables for client.py submission."""
    raw_candidates = [
        configured,
        r"C:\Python27\python.exe",
        r"C:\Python27_64\python.exe",
        r"C:\Tools\Python27\python.exe",
        r"C:\TCC\Tools\Python27\python.exe",
    ]
    seen = set()
    candidates: list[PythonCandidate] = []
    for raw in raw_candidates:
        if not raw:
            continue
        path = os.path.normpath(str(raw))
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(_probe_python2(path))

    py_launcher = shutil.which("py")
    if py_launcher:
        candidates.append(_probe_python2_launcher(py_launcher))
    python_on_path = shutil.which("python2") or shutil.which("python")
    if python_on_path:
        normalized = os.path.normpath(python_on_path)
        if normalized.lower() not in seen:
            candidates.append(_probe_python2(normalized))
    return candidates


def detect_python2_path(configured: str = "") -> str:
    for candidate in detect_python2_candidates(configured):
        if candidate.ok and candidate.path != "py -2":
            return candidate.path
        if candidate.ok and candidate.path == "py -2":
            return candidate.path
    return ""


def list_cluster_jobs(config: dict[str, Any], *, limit: int = 20) -> list[dict[str, Any]]:
    cluster = get_cluster_config(config)
    project_key = str(config.get("_meta", {}).get("project") or config.get("project", {}).get("name") or "default")
    root = Path(cluster["workspace_root"]) / cluster["project_folder"] / project_key
    if not root.exists():
        return []
    jobs = []
    for manifest in root.glob("*/manifest.json"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            data = {"run_id": manifest.parent.name, "job_dir": str(manifest.parent)}
        status = inspect_cluster_job(str(manifest.parent))
        data["status"] = status
        jobs.append(data)
    jobs.sort(key=lambda item: str(item.get("created_at") or item.get("run_id") or ""), reverse=True)
    return jobs[:limit]


def get_cluster_web_status(config: dict[str, Any], job: str) -> dict[str, Any]:
    """Read the official Cluster V2.0 web status page for a job id or job path."""
    cluster = get_cluster_config(config)
    base_url = str(cluster.get("web_url") or "http://szhradar01/cluster/").rstrip("/") + "/"
    job_text = str(job or "").strip()
    result: dict[str, Any] = {"query": job_text, "base_url": base_url, "job_id": "", "found": False, "tasks": [], "error": ""}
    try:
        job_id = job_text if job_text.isdigit() else _find_web_job_id_by_path(base_url, job_text)
        result["job_id"] = job_id
        if not job_id:
            result["error"] = "job id not found on official jobs page"
            return result
        task_html = _read_url(f"{base_url}?page=tasks&jobid={job_id}")
        result["url"] = f"{base_url}?page=tasks&jobid={job_id}"
        result["tasks"] = _parse_task_page(task_html)
        result["found"] = True
        if result["tasks"]:
            states = [str(task.get("simulation_state") or "") for task in result["tasks"]]
            result["state"] = "finished" if all(state == "finished" for state in states) else ",".join(sorted(set(states)))
            result["worker_hosts"] = sorted({str(task.get("worker_host") or "") for task in result["tasks"] if task.get("worker_host")})
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _find_web_job_id_by_path(base_url: str, job_path: str) -> str:
    if not job_path:
        return ""
    jobs_html = _read_url(f"{base_url}?page=jobs")
    normalized = job_path.replace("/", "\\")
    index = jobs_html.find(normalized)
    if index < 0:
        # The official page usually stores the Config.cfg path, not just the folder.
        config_path = str(Path(normalized) / "Config.cfg")
        index = jobs_html.find(config_path)
    if index < 0:
        return ""
    window = jobs_html[max(0, index - 2000) : min(len(jobs_html), index + 2000)]
    matches = re.findall(r"(?:changeprio|killjob|resetjob|pausejob)\('(\d+)'", window)
    return matches[0] if matches else ""


def _read_url(url: str, *, timeout: int = 20) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        raw = response.read()
        content_type = response.headers.get("content-type", "")
    encoding = "iso-8859-1" if "iso-8859-1" in content_type.lower() else "utf-8"
    return raw.decode(encoding, errors="replace")


def _parse_task_page(text: str) -> list[dict[str, str]]:
    tasks = []
    for block in re.split(r"<table class=\"table2\" width=\"100%\">", text)[1:]:
        end = block.find("</table>")
        task_html = block[:end] if end >= 0 else block
        task = {}
        for key, value in re.findall(r"<tr><td>(.*?)</td><td[^>]*>(.*?)</td></tr>", task_html, flags=re.IGNORECASE | re.DOTALL):
            clean_key = _strip_html(key)
            clean_value = _strip_html(value)
            if clean_key:
                task[clean_key] = _cluster_state_label(clean_value) if clean_key == "simulation_state" else clean_value
        # Extended rows contain stable DB fields such as time_* and python_version.
        extended_match = re.search(r"<table class='table2' id='task_extended_[^']+'[^>]*>(.*?)</table>", block, flags=re.IGNORECASE | re.DOTALL)
        if extended_match:
            for key, value in re.findall(r"<tr><td>(.*?)</td><td>(.*?)</td></tr>", extended_match.group(1), flags=re.IGNORECASE | re.DOTALL):
                clean_key = _strip_html(key)
                clean_value = _strip_html(value)
                if clean_key:
                    if clean_key == "simulation_state" and "simulation_state" in task:
                        task["simulation_state_code"] = clean_value
                    elif clean_key == "simulation_state":
                        task[clean_key] = _cluster_state_label(clean_value)
                    else:
                        task[clean_key] = clean_value
        if task:
            tasks.append(task)
    return tasks


def _cluster_state_label(value: str) -> str:
    states = {
        "0": "free",
        "1": "assigned",
        "2": "copying",
        "3": "simulating",
        "4": "finished",
        "5": "paused",
    }
    return states.get(str(value).strip(), value)


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def scan_cluster_data(
    config: dict[str, Any],
    *,
    input_path: str = "",
    dataset: str = "",
    profile: str = "",
    required_signals: list[str] | None = None,
    limit: int = 20,
    max_read_mb: int = 8,
) -> dict[str, Any]:
    """List candidate MF4 inputs and optionally scan for required signal names.

    This intentionally uses bounded byte scanning instead of opening each large
    MF4 with asammdf; network-hosted BYD_SR files can be hundreds of MB to GB.
    """
    config = apply_cluster_profile(config, profile)
    cluster = get_cluster_config(config)
    sim = get_simulation_config(config)
    source = _resolve_datafile_path(sim, input_path=input_path, dataset=dataset)
    signals = required_signals if required_signals is not None else list(cluster.get("required_input_signals") or [])
    limit = max(1, int(limit or 20))
    max_bytes = max(0, int(max_read_mb or 0)) * 1024 * 1024
    files = []
    warnings = []
    if not source:
        warnings.append("No input path or dataset was provided.")
    else:
        for path in iter_mf4_inputs(Path(source), limit=limit):
            files.append(scan_data_file(path, signals, max_bytes=max_bytes))
            if len(files) >= limit:
                break
    return {
        "source": source,
        "dataset": dataset,
        "profile": str(cluster.get("active_profile") or profile or "default"),
        "required_signals": signals,
        "limit": limit,
        "max_read_mb": max_read_mb,
        "files": [asdict(item) for item in files],
        "warnings": warnings,
    }


def inspect_cluster_job(job_dir: str, *, max_files: int = 500) -> dict[str, Any]:
    root = Path(job_dir)
    output_dirs = _discover_output_dirs(root)
    files = []
    output_mf4 = []
    logs = []
    result_files = []
    task_results = []
    total_bytes = 0
    truncated = False
    for out in output_dirs:
        if not out.exists():
            continue
        for path in out.rglob("*"):
            if not path.is_file():
                continue
            if len(files) >= max_files:
                truncated = True
                break
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            rel = str(path.relative_to(root)) if _is_relative_to(path, root) else str(path)
            item = {"path": str(path), "relative_path": rel, "size": size, "mtime": path.stat().st_mtime if path.exists() else 0}
            files.append(item)
            total_bytes += size
            suffix = path.suffix.lower()
            if suffix == ".mf4":
                output_mf4.append(item)
            elif suffix in {".log", ".txt"} or path.name.lower() in {"result.ini", "robocopy.txt"}:
                logs.append(item)
            if path.name.lower() == "result.ini":
                result_files.append(item)
                task_results.append(_read_result_ini(path))
        if truncated:
            break
    state = "prepared"
    success_count = sum(1 for item in task_results if item.get("successfull") == "1")
    fail_count = sum(1 for item in task_results if item.get("successfull") == "0")
    error_summary = _summarize_cluster_errors(task_results, logs)
    if result_files:
        state = "finished-success" if success_count and not fail_count else "finished-failed"
    elif output_mf4:
        state = "output-present"
    elif files:
        state = "running-or-started"
    return {
        "job_dir": str(root),
        "exists": root.exists(),
        "state": state,
        "output_dirs": [str(path) for path in output_dirs if path.exists()],
        "file_count": len(files),
        "total_bytes": total_bytes,
        "output_mf4": output_mf4,
        "logs": logs[:20],
        "result_files": result_files,
        "task_results": task_results[:50],
        "success_count": success_count,
        "fail_count": fail_count,
        "error_summary": error_summary,
        "truncated": truncated,
    }


def _summarize_cluster_errors(task_results: list[dict[str, str]], logs: list[dict[str, Any]]) -> list[str]:
    """Extract the most useful failure lines from Cluster result files/logs."""
    summaries: list[str] = []
    for result in task_results:
        message = str(result.get("error_message") or "").strip()
        if message and message.lower() not in {"unknown", "none"}:
            _append_unique(summaries, f"Cluster result: {message}")
    for log in logs:
        name = Path(str(log.get("path") or "")).name.lower()
        if name not in {"selena.log", "result.ini"} and not name.endswith(".log"):
            continue
        for line in _tail_error_lines(Path(str(log.get("path") or ""))):
            _append_unique(summaries, line)
            if len(summaries) >= 6:
                return summaries
    return summaries[:6]


def _tail_error_lines(path: Path, *, max_chars: int = 65536) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_chars:
                handle.seek(-max_chars, os.SEEK_END)
            text = handle.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    candidates = []
    for raw in text.splitlines()[-400:]:
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(token in lowered for token in ("[error]", "return code", "returncode", "failed", "no signal", "error_message")):
            candidates.append(line)
    return candidates[-5:]


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def fetch_cluster_job(job_dir: str, destination: str, *, overwrite: bool = False) -> dict[str, Any]:
    status = inspect_cluster_job(job_dir)
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for out_dir in status.get("output_dirs", []):
        source_root = Path(out_dir)
        if not source_root.exists():
            continue
        for source in source_root.rglob("*"):
            if not source.is_file():
                continue
            rel = source.relative_to(source_root)
            target = dest / rel
            if target.exists() and not overwrite:
                copied.append({"source": str(source), "target": str(target), "skipped": True})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append({"source": str(source), "target": str(target), "skipped": False})
    return {"job_dir": job_dir, "destination": str(dest), "copied": copied, "status": status}


def prepare_cluster_job(
    config: dict[str, Any],
    *,
    input_path: str = "",
    dataset: str = "",
    run_id: str = "",
    profile: str = "",
    copy_data: bool | None = None,
    copy_selena: bool | None = None,
) -> ClusterJobPackage:
    """Create a self-contained Cluster job folder and return its manifest."""
    config = apply_cluster_profile(config, profile)
    cluster = get_cluster_config(config)
    sim = get_simulation_config(config)
    project_key = str(config.get("_meta", {}).get("project") or config.get("project", {}).get("name") or "default")
    run_id = run_id or time.strftime("%Y%m%d_%H%M%S")

    # On Linux, config paths stay as Windows UNC (cluster workers are Windows
    # and read Config.cfg paths as UNC). linux_mount_map (set in local.yaml)
    # translates UNC -> local mount point so this server can actually write the
    # job folder to disk. _to_local_path() is a no-op on Windows or when no map.
    mount_map = dict(cluster.get("linux_mount_map") or {})
    is_windows = sys.platform.startswith("win")

    def _to_local_path(p: str) -> str:
        """UNC -> mount point for local filesystem writes. No-op on Windows."""
        if not p or is_windows or not mount_map:
            return p
        for unc_prefix, mount in mount_map.items():
            if p.lower().startswith(unc_prefix.lower()):
                return mount + p[len(unc_prefix):].replace("\\", "/")
        return p

    def _to_unc_path(p: str) -> str:
        """Mount point -> UNC for Config.cfg content (workers are Windows). No-op on Windows."""
        if not p or is_windows or not mount_map:
            return p
        for unc_prefix, mount in mount_map.items():
            if p.lower().startswith(mount.lower()):
                return unc_prefix + p[len(mount):].replace("/", "\\")
        return p

    workspace_root = str(cluster.get("workspace_root") or "")
    # The job_dir URL path kept for Config.cfg / submit (UNC so Windows workers
    # and the manager can read it); job_dir_local is where we actually write.
    # On non-Windows, Path() joins with '/' which produces mixed separators in
    # UNC paths (\\host\share/dir/file) that Windows managers can't resolve —
    # force backslashes in the UNC string.
    job_dir_unc_str = workspace_root.rstrip("\\/") + "\\" + str(cluster.get("project_folder") or "").strip("/\\") + "\\" + project_key + "\\" + run_id
    job_dir_local = Path(_to_local_path(job_dir_unc_str))
    assets_dir = job_dir_local / "assets"
    data_dir = job_dir_local / "data"
    output_dir = job_dir_local / "output"
    selena_dir = job_dir_local / "selena"
    # Cross-platform guard: a Windows UNC workspace_root with no mount map on
    # non-Windows would silently create a garbled local dir. Fail loud instead.
    if workspace_root.startswith("\\\\") and not is_windows and not mount_map:
        raise RuntimeError(
            f"cluster.workspace_root is a Windows UNC path ({workspace_root!r}) "
            f"but this machine is {sys.platform}. Mount the SMB share and add a "
            f"cluster.linux_mount_map entry mapping the UNC prefix to a mount "
            f"point (e.g. '\\\\\\\\abtvdfs2.de.bosch.com\\\\ismdfs': '/mnt/cluster') "
            f"in local.yaml."
        )
    for path in (assets_dir, data_dir, output_dir):
        path.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    datafile_path = _resolve_datafile_path(sim, input_path=input_path, dataset=dataset)
    do_copy_data = bool(cluster.get("copy_data")) if copy_data is None else copy_data
    if not datafile_path:
        warnings.append("No input path was provided; Config.cfg datafile_path is empty.")
    elif looks_local_windows_path(datafile_path):
        # Local drive data is invisible to cluster workers.
        if do_copy_data:
            copied = copy_input_data(Path(datafile_path), data_dir)
            datafile_path = str(copied)
        else:
            warnings.append(
                f"Input data path is local to this PC and invisible to workers: {datafile_path}. "
                "Set profile data.copy=true (or --copy-data) to stage it under the shared workspace."
            )
    elif do_copy_data:
        copied = copy_input_data(Path(datafile_path), data_dir)
        datafile_path = str(copied)

    copied_assets_local = _copy_assets(config, assets_dir, warnings, mount_map=mount_map or None)
    # Config.cfg needs UNC paths (workers are Windows); copied_assets_local has
    # mount-point paths from the local write. Translate back to UNC.
    copied_assets = {k: _to_unc_path(v) for k, v in copied_assets_local.items()}
    local_selena = resolve_selena_executable(config)
    selena_exe = str(cluster.get("selena_exe") or "").strip()
    # Selena source adaptivity:
    #  - profile selena.source=build (or no exe configured) → copy local runtime
    #    into the job folder so workers don't need the local build path.
    #  - profile selena.source=path → use the configured shared selena.exe as-is.
    profile_selena_source = str((config.get("_profile_selena_source") or "")).lower()
    build_source = profile_selena_source == "build" or (not selena_exe and bool(local_selena))
    do_copy_selena = bool(cluster.get("copy_selena")) if copy_selena is None else copy_selena
    if build_source and copy_selena is None:
        do_copy_selena = True
    if do_copy_selena:
        if local_selena and Path(local_selena).exists():
            selena_exe = str(_copy_selena_runtime(Path(local_selena).parent, selena_dir))
        else:
            warnings.append(f"Local Selena executable not found, cannot copy runtime: {local_selena}")
    if not selena_exe:
        selena_exe = local_selena
        warnings.append("Selena executable still points to the local build; set cluster.selena_exe or use --copy-selena before real submit.")

    # Radar orientation auto-detection: when source/mounting are auto/unset and
    # the input is a single MF4, infer from the file's metadata so the worker
    # runs with the correct radar source (RadarFL/FR/RL/RR) instead of a default.
    # This mirrors what rsim run does locally via build_effective_simulation.
    source_value = str(sim.get("source", "") or "").strip().lower()
    mounting_value = str(sim.get("mounting_position", "") or "").strip().lower()
    needs_detection = source_value in ("", "auto") or mounting_value in ("", "auto")
    if needs_detection and datafile_path:
        # On Linux the datafile is a UNC string workers read; resolve to the
        # local mount point to stat/read it for orientation detection.
        local_datafile = _to_local_path(datafile_path)
        try:
            is_regular_file = Path(local_datafile).is_file()
        except OSError as exc:
            # SMB/DFS hiccups (connection refused, stale handle) shouldn't crash
            # prepare — orientation detection is best-effort.
            is_regular_file = False
            warnings.append(f"Could not stat datafile for orientation detection: {exc}")
        if is_regular_file:
            try:
                from core.simulation import detect_radar_orientation
                detection = detect_radar_orientation(local_datafile)
                if detection:
                    sim = dict(sim)
                    sim["source"] = detection["source"]
                    sim["mounting_position"] = detection["mounting_position"]
                    sim["radar_detection"] = detection
            except Exception as exc:
                warnings.append(f"Radar orientation auto-detection failed: {exc}")

    script_path = job_dir_local / "SIMULATION_RADAR_SIM.py"
    script_unc = job_dir_unc_str + "\\SIMULATION_RADAR_SIM.py"
    script_path.write_text(
        _render_worker_script(
            software_path=str(cluster["software_path"]),
            job_dir=job_dir_unc_str,
            dependency_paths=list(cluster.get("dependency_paths", []) or []),
        ),
        encoding="utf-8",
    )

    config_path = job_dir_local / "Config.cfg"
    config_unc = job_dir_unc_str + "\\Config.cfg"
    config_path.write_text(
        _render_config_cfg(
            cluster=cluster,
            sim=sim,
            simulation_script=script_unc,
            datafile_path=datafile_path,
            selena_exe=selena_exe,
            runtime_xml=copied_assets.get("runtime_xml", sim.get("runtime_xml", "")),
            matfilefilter=copied_assets.get("matfilefilter", sim.get("matfilefilter", "")),
            adapter_file=copied_assets.get("adapter_file", sim.get("adapter_file", "")),
            config_template=copied_assets.get("config_template", ""),
        ),
        encoding="utf-8",
    )

    # submit_command passes config_path to the Windows manager — must be UNC.
    submit_command = build_submit_command(Path(config_unc), cluster=cluster, username=str(cluster.get("username", "") or ""))
    manifest = {
        "run_id": run_id,
        "project": project_key,
        "profile": str(cluster.get("active_profile") or profile or "default"),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "job_dir": job_dir_unc_str,
        "job_dir_local": str(job_dir_local),
        "config_path": config_unc,
        "simulation_script": script_unc,
        "datafile_path": datafile_path,
        "output_hint": job_dir_unc_str + "\\output",
        "selena_exe": selena_exe,
        "assets": copied_assets,
        "submit_command": submit_command,
        "warnings": warnings,
    }
    manifest_path = job_dir_local / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return ClusterJobPackage(
        run_id=run_id,
        profile=str(cluster.get("active_profile") or profile or "default"),
        job_dir=job_dir_unc_str,
        config_path=config_unc,
        simulation_script=script_unc,
        manifest_path=str(manifest_path),
        datafile_path=datafile_path,
        output_hint=job_dir_unc_str + "\\output",
        submit_command=submit_command,
        warnings=warnings,
    )


def build_submit_command(config_path: Path, *, cluster: dict[str, Any], username: str = "") -> list[str]:
    python_path = str(cluster.get("python_path") or r"C:\Python27\python.exe")
    cmd = _python_command_prefix(python_path)
    cmd.extend([str(_client_path(cluster)), str(config_path), str(cluster.get("kill_password") or "1234")])
    if username:
        cmd.append(username)
    return cmd


def submit_cluster_job(config_path: str, config: dict[str, Any], *, dry_run: bool = True) -> SubmitResult:
    cluster = get_cluster_config(config)
    cmd = build_submit_command(Path(config_path), cluster=cluster, username=str(cluster.get("username", "") or ""))
    mode = _resolve_submit_mode(cluster)
    if dry_run:
        return SubmitResult(mode=mode, dry_run=True, command=cmd, returncode=0, stdout="", stderr="")
    if mode == "client":
        result = subprocess.run(cmd, text=True, capture_output=True)
        return SubmitResult(mode=mode, dry_run=False, command=cmd, returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
    return _submit_via_xmlrpc(config_path, config, cluster)


def submit_cluster_job_legacy(config_path: str, config: dict[str, Any], *, dry_run: bool = True) -> subprocess.CompletedProcess | list[str]:
    result = submit_cluster_job(config_path, config, dry_run=dry_run)
    if dry_run:
        return result.command
    return subprocess.CompletedProcess(result.command, result.returncode, result.stdout, result.stderr)


def _first_existing(paths: list[str]) -> str:
    for path in paths:
        if Path(path).exists():
            return path
    return ""


def _probe_python2(path: str) -> PythonCandidate:
    if not Path(path).exists():
        return PythonCandidate(path=path, ok=False, detail="not found")
    try:
        result = subprocess.run([path, "-c", "import sys; print(sys.version_info[0])"], text=True, capture_output=True, timeout=10)
    except Exception as exc:
        return PythonCandidate(path=path, ok=False, detail=str(exc))
    major = (result.stdout or result.stderr or "").strip()
    return PythonCandidate(path=path, ok=(major == "2"), detail=f"major={major or 'unknown'}")


def _probe_python2_launcher(py_launcher: str) -> PythonCandidate:
    try:
        result = subprocess.run([py_launcher, "-2", "-c", "import sys; print(sys.version_info[0])"], text=True, capture_output=True, timeout=10)
    except Exception as exc:
        return PythonCandidate(path="py -2", ok=False, detail=str(exc))
    major = (result.stdout or result.stderr or "").strip()
    return PythonCandidate(path="py -2", ok=(major == "2"), detail=f"major={major or 'unknown'}")


def _python_detail(path: str) -> str:
    candidates = detect_python2_candidates(path)
    for candidate in candidates:
        if candidate.path == path or (path == "py -2" and candidate.path == "py -2"):
            return f"{candidate.path} ({candidate.detail})"
    return path


def _python_command_prefix(python_path: str) -> list[str]:
    if python_path.strip().lower() == "py -2":
        return ["py", "-2"]
    return [python_path]


def _python_config_looks_usable(python_path: str) -> bool:
    if python_path.strip().lower() == "py -2":
        return any(candidate.path == "py -2" and candidate.ok for candidate in detect_python2_candidates(python_path))
    return Path(python_path).exists()


def _manager_item(cluster: dict[str, Any]) -> CheckItem:
    host = str(cluster.get("manager_host") or "SZHRADAR01")
    port = int(cluster.get("manager_port") or 8123)
    try:
        with socket.create_connection((host, port), timeout=3):
            return CheckItem("Manager XML-RPC port", True, f"{host}:{port}")
    except Exception as exc:
        return CheckItem("Manager XML-RPC port", False, f"{host}:{port} ({exc})")


def _resolve_submit_mode(cluster: dict[str, Any]) -> str:
    mode = str(cluster.get("submit_mode") or "auto").strip().lower()
    if mode in {"client", "xmlrpc"}:
        return mode
    if _python_config_looks_usable(str(cluster.get("python_path") or "")):
        return "client"
    return "xmlrpc"


def _submit_via_xmlrpc(config_path: str, config: dict[str, Any], cluster: dict[str, Any]) -> SubmitResult:
    config_file = Path(config_path)
    # On Linux the config_path is a UNC string (the manager reads it on Windows).
    # _validate_submit_package needs to stat the local file, so resolve via mount map.
    mount_map = dict(cluster.get("linux_mount_map") or {})
    is_windows = sys.platform.startswith("win")
    local_config = config_path
    if not is_windows and mount_map:
        for unc_prefix, mount in mount_map.items():
            if config_path.lower().startswith(unc_prefix.lower()):
                local_config = mount + config_path[len(unc_prefix):].replace("\\", "/")
                break
    validation_errors = _validate_submit_package(Path(local_config), mount_map=mount_map or None)
    command = ["xmlrpc", _manager_url(cluster), "addSimulation", str(config_file)]
    if validation_errors:
        return SubmitResult(
            mode="xmlrpc",
            dry_run=False,
            command=command,
            returncode=1,
            stdout="",
            stderr="\n".join(validation_errors),
        )

    username = str(cluster.get("username") or os.environ.get("USERNAME") or os.environ.get("USER") or "radar-sim")
    longname = str(cluster.get("longname") or username)
    password = str(cluster.get("kill_password") or "1234")
    hostname = socket.getfqdn().split(".")[0]
    try:
        manager = xmlrpc.client.ServerProxy(_manager_url(cluster), allow_none=True)
        manager.is_manager_online()
        value, result_text = manager.addSimulation(hostname, username, longname, str(config_file), password)
        try:
            numeric_value = int(value)
        except Exception:
            numeric_value = -1
        return SubmitResult(
            mode="xmlrpc",
            dry_run=False,
            command=command,
            returncode=0 if numeric_value > 0 else numeric_value,
            stdout=f"value={value}\n{result_text}",
            stderr="",
        )
    except Exception as exc:
        return SubmitResult(mode="xmlrpc", dry_run=False, command=command, returncode=1, stdout="", stderr=str(exc))


def _manager_url(cluster: dict[str, Any]) -> str:
    host = str(cluster.get("manager_host") or "SZHRADAR01")
    port = int(cluster.get("manager_port") or 8123)
    return f"http://{host}:{port}"


def _validate_submit_package(config_path: Path, mount_map: dict[str, str] | None = None) -> list[str]:
    if not config_path.exists():
        return [f"Config.cfg not found: {config_path}"]
    cfg = _parse_simple_cfg(config_path.read_text(encoding="utf-8", errors="replace"))
    errors = []
    required = [
        "simulation",
        "simulation_prio",
        "python_version",
        "datafile_path",
        "extension",
        "skip_dir",
        "skip_filename",
        "finalstep",
        "send_email",
        "send_netsend",
        "group",
        "subgroup",
    ]
    for key in required:
        if key not in cfg:
            errors.append(f"Missing Config.cfg key: {key}")

    def _local(p: str) -> str:
        """Resolve a UNC path to local mount for existence checks (Linux)."""
        if not p or not mount_map or sys.platform.startswith("win"):
            return p
        for unc_prefix, mount in mount_map.items():
            if p.lower().startswith(unc_prefix.lower()):
                return mount + p[len(unc_prefix):].replace("\\", "/")
        return p

    script = cfg.get("simulation", "")
    if script:
        script_path = Path(_local(script))
        if not script_path.exists():
            errors.append(f"Simulation script not found: {script}")
        elif "sys.path.append" not in script_path.read_text(encoding="utf-8", errors="replace"):
            errors.append("Simulation script does not contain sys.path.append for Cluster software path")
    datafile_path = cfg.get("datafile_path", "")
    if datafile_path:
        local_df = _local(datafile_path)
        # A dataset directory is valid as datafile_path; accept dir or file.
        if not Path(local_df).exists():
            errors.append(f"Datafile path not found: {datafile_path}")
    return errors


def _parse_simple_cfg(text: str) -> dict[str, str]:
    result = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().rstrip(";").strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        result[key.strip()] = value
    return result


def _read_result_ini(path: Path) -> dict[str, str]:
    data = {"path": str(path)}
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("[") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    except Exception as exc:
        data["read_error"] = str(exc)
    return data


def _infer_radar_from_path(path: str) -> dict[str, str]:
    upper = str(path or "").upper()
    mapping = {
        "FL": {"source": "RadarFL", "mounting_position": "CFL"},
        "FR": {"source": "RadarFR", "mounting_position": "CFR"},
        "RL": {"source": "RadarRL", "mounting_position": "CRL"},
        "RR": {"source": "RadarRR", "mounting_position": "CRR"},
    }
    for pos in ("FL", "FR", "RL", "RR"):
        tokens = [f"RADAR{pos}", f"_{pos}_", f"{pos}5", f"{pos}CR", f"CR{pos}"]
        if any(token in upper for token in tokens):
            return mapping[pos]
    return {}


def _discover_output_dirs(job_root: Path) -> list[Path]:
    dirs = []
    direct = job_root / "output"
    if direct.exists():
        dirs.append(direct)
    if job_root.exists():
        for child in job_root.iterdir():
            if child.is_dir() and child.name.lower() != "output" and child.name.upper().startswith("OUT"):
                dirs.append(child)
    return dirs


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _path_item(name: str, path: Path, *, must_be_dir: bool = False) -> CheckItem:
    exists = path.is_dir() if must_be_dir else path.exists()
    return CheckItem(name, exists, str(path))


def _client_path(cluster: dict[str, Any]) -> Path:
    client = Path(str(cluster.get("client_py") or "client.py"))
    if client.is_absolute():
        return client
    return Path(str(cluster.get("software_path") or "")) / client


def _resolve_datafile_path(sim: dict[str, Any], *, input_path: str, dataset: str) -> str:
    if input_path:
        return os.path.normpath(input_path)
    if dataset:
        for item in sim.get("datasets", []) or []:
            if item.get("name") == dataset:
                return os.path.normpath(str(item.get("input_mf4") or item.get("input_dir") or ""))
    datasets = sim.get("datasets", []) or []
    if datasets:
        first = datasets[0]
        return os.path.normpath(str(first.get("input_mf4") or first.get("input_dir") or ""))
    return ""


def _copy_assets(config: dict[str, Any], assets_dir: Path, warnings: list[str], mount_map: dict[str, str] | None = None) -> dict[str, str]:
    assets_dir.mkdir(parents=True, exist_ok=True)
    assets = config.get("assets", {}) or {}
    result: dict[str, str] = {}
    is_windows = sys.platform.startswith("win")

    def _local(p: str) -> str:
        if not p or is_windows or not mount_map:
            return p
        for unc_prefix, mount in mount_map.items():
            if p.lower().startswith(unc_prefix.lower()):
                return mount + p[len(unc_prefix):].replace("\\", "/")
        return p

    for key in ("runtime_xml", "matfilefilter", "adapter_file", "config_template"):
        value = str(assets.get(key) or "")
        if not value:
            continue
        source = Path(_local(value))
        if not source.exists():
            warnings.append(f"Asset not found: {key} -> {value}")
            continue
        target = assets_dir / source.name
        shutil.copy2(source, target)
        result[key] = str(target)
    return result


def _copy_selena_runtime(source_dir: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        if item.is_dir():
            continue
        if item.name not in RUNTIME_COPY_NAMES and item.suffix.lower() in RUNTIME_COPY_EXCLUDES:
            continue
        if item.name in RUNTIME_COPY_NAMES or item.suffix.lower() == ".dll":
            shutil.copy2(item, target_dir / item.name)
    return target_dir / "selena.exe"


def _quote_cfg(value: Any) -> str:
    return '"' + str(value).replace('"', '\\"') + '";'


def _render_config_cfg(
    *,
    cluster: dict[str, Any],
    sim: dict[str, Any],
    simulation_script: str,
    datafile_path: str,
    selena_exe: str,
    runtime_xml: str,
    matfilefilter: str,
    adapter_file: str,
    config_template: str,
) -> str:
    source = sim.get("source", "")
    if str(source).lower() == "auto":
        source = ""
    mounting = sim.get("mounting_position", "")
    if str(mounting).lower() == "auto":
        mounting = ""
    inferred = _infer_radar_from_path(datafile_path)
    source = source or inferred.get("source", "")
    mounting = mounting or inferred.get("mounting_position", "")
    lines = [
        "simulation = " + _quote_cfg(simulation_script),
        'simulation_description = "radar-sim batch";',
        f"simulation_type = {int(cluster.get('simulation_type', 0))};",
        f"simulation_prio = {int(cluster.get('simulation_prio', 4))};",
        "python_version = " + _quote_cfg(cluster.get("python_version", "*")),
        "group = " + _quote_cfg(cluster.get("group", "Radar")),
        "subgroup = " + _quote_cfg(cluster.get("subgroup", "PSS2")),
        f"timeout = {int(cluster.get('timeout_min', 120))};",
        "datafile_path = " + _quote_cfg(datafile_path),
        "extension = " + _quote_cfg(cluster.get("extension", "*.MF4,*MF4.zip")),
        "skip_dir = " + _quote_cfg(cluster.get("skip_dir", "")),
        "skip_filename = " + _quote_cfg(cluster.get("skip_filename", "")),
        f"filter = {int(cluster.get('filter', 0))};",
        f"finalstep = {int(cluster.get('finalstep', 0))};",
        f"send_email = {int(cluster.get('send_email', 0))};",
        f"send_netsend = {int(cluster.get('send_netsend', 0))};",
        "selenaPathExe = " + _quote_cfg(selena_exe),
        "runTimeConfigFile = " + _quote_cfg(runtime_xml),
        "matfilefilter = " + _quote_cfg(matfilefilter),
        "adapterFile = " + _quote_cfg(adapter_file),
        "paramconfigTemplate = " + _quote_cfg(config_template),
        "radar = " + _quote_cfg(source),
        "mountingPosition = " + _quote_cfg(mounting),
        "additionalCommand = " + _quote_cfg(" ".join(sim.get("extra_args", []) or [])),
        f"tolerant = {1 if sim.get('tolerant') else 0};",
        f"enable_multibuffer_border = {1 if sim.get('enable_multibuffer_border', True) else 0};",
        f"enable_doorkeeper = {1 if sim.get('enable_doorkeeper', True) else 0};",
        f"disable_sequence_check = {1 if sim.get('disable_sequence_check') else 0};",
    ]
    return "\n".join(lines) + "\n"


def _render_worker_script(*, software_path: str, job_dir: str, dependency_paths: list[str]) -> str:
    deps_repr = repr([str(p) for p in dependency_paths])
    return f'''# -*- coding: iso-8859-1 -*-
import os
import sys
import subprocess
import shutil

sys.path.append(r"{software_path}")
import simulation_runtime

JOB_DIR = r"{job_dir}"
DEPENDENCY_PATHS = {deps_repr}


def _cfg(name, default=""):
    exist, value = simulation_runtime.getConfigfileParameter(name)
    if exist == 0:
        return value
    return default


def _bool_cfg(name, default=False):
    value = str(_cfg(name, "1" if default else "0")).strip().lower()
    return value in ("1", "true", "yes", "on")


def _write_paramconfig(inputfile, outputfile, logfile, outputpath):
    template = _cfg("paramconfigTemplate", "")
    runtime_xml = _cfg("runTimeConfigFile", "")
    matfilefilter = _cfg("matfilefilter", "")
    adapter = _cfg("adapterFile", "")
    radar = _cfg("radar", "")
    mounting = _cfg("mountingPosition", "")
    if template and os.path.isfile(template):
        text = open(template, "r").read()
    else:
        text = "\\n".join([
            "config=__RUNTIME_XML__",
            "input=__INPUT_MF4__",
            "output=__OUTPUT_MF4__",
            "log=__LOG_FILE__",
            "source=__SOURCE__",
            "adapterfile=__ADAPTER_FILE__",
            "matfilefilter=__MATFILEFILTER__",
            "nogui=true",
            "write-mat=true",
            "tolerant=__TOLERANT__",
            "disable-sequence-check=__DISABLE_SEQUENCE_CHECK__",
            "enable-multibuffer-border=__ENABLE_MULTIBUFFER_BORDER__",
            "enable-doorkeeper=__ENABLE_DOORKEEPER__",
            "userparam=mountingPosition=__MOUNTING_POSITION__",
        ]) + "\\n"
    repl = {{
        "__INPUT_MF4__": inputfile,
        "__OUTPUT_MF4__": outputfile,
        "__LOG_FILE__": logfile,
        "__RUNTIME_XML__": runtime_xml,
        "__MATFILEFILTER__": matfilefilter,
        "__ADAPTER_FILE__": adapter,
        "__SOURCE__": radar,
        "__MOUNTING_POSITION__": mounting,
        "__TOLERANT__": "true" if _bool_cfg("tolerant", True) else "false",
        "__NOGUI__": "true",
        "__WRITE_MAT__": "true",
        "__DISABLE_SEQUENCE_CHECK__": "true" if _bool_cfg("disable_sequence_check", False) else "false",
        "__ENABLE_MULTIBUFFER_BORDER__": "true" if _bool_cfg("enable_multibuffer_border", True) else "false",
        "__ENABLE_DOORKEEPER__": "true" if _bool_cfg("enable_doorkeeper", True) else "false",
        "__EXTRA_PARAMCONFIG_LINES__": "",
    }}
    for key, value in repl.items():
        text = text.replace(key, str(value))
        text = text.replace(chr(123) + chr(123) + key.strip("_") + chr(125) + chr(125), str(value))
        text = text.replace(chr(123) + key.strip("_") + chr(125), str(value))
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in ("adapterfile=", "adapterfile=" + chr(123) + chr(123) + "ADAPTER_FILE" + chr(125) + chr(125), "matfilefilter=", "matfilefilter=" + chr(123) + chr(123) + "MATFILEFILTER" + chr(125) + chr(125), "source=", "source=" + chr(123) + chr(123) + "SOURCE" + chr(125) + chr(125), "userparam=mountingPosition=", "userparam=mountingPosition=" + chr(123) + chr(123) + "MOUNTING_POSITION" + chr(125) + chr(125), chr(123) + chr(123) + "EXTRA_PARAMCONFIG_LINES" + chr(125) + chr(125), chr(123) + "EXTRA_PARAMCONFIG_LINES" + chr(125), "__EXTRA_PARAMCONFIG_LINES__"):
            continue
        if "=" in stripped:
            left, right = stripped.split("=", 1)
            if left and right in ("", chr(123) + chr(125), chr(123) + chr(123) + left.upper().replace("-", "_") + chr(125) + chr(125)):
                continue
        lines.append(line)
    paramconfig = os.path.join(outputpath, "radar_sim_paramconfig.txt")
    open(paramconfig, "w").write("\\n".join(lines) + "\\n")
    return paramconfig


def simulation(inputfile, outputpath, infos=None):
    if not os.path.isdir(outputpath):
        os.makedirs(outputpath)
    os.chdir(outputpath)
    dep_path = ";".join([p for p in DEPENDENCY_PATHS if p])
    if dep_path:
        os.environ["PATH"] = dep_path + ";" + os.environ.get("PATH", "")
    base = os.path.splitext(os.path.basename(inputfile))[0]
    task_id = "manual"
    try:
        if infos and infos.get("task") and infos["task"].get("id"):
            task_id = str(infos["task"].get("id"))
    except Exception:
        pass
    temp_root = os.environ.get("TEMP", r"c:\\temp")
    safe_workdir = os.path.join(temp_root, "radar_sim_selena_" + task_id)
    if not os.path.isdir(safe_workdir):
        os.makedirs(safe_workdir)
    outputfile = os.path.join(safe_workdir, base + "out.MF4")
    logfile = os.path.join(safe_workdir, "selena.log")
    paramconfig = _write_paramconfig(inputfile, outputfile, logfile, safe_workdir)
    selena = _cfg("selenaPathExe", "")
    if not selena:
        print("ERROR: selenaPathExe missing in Config.cfg")
        return 0
    cmd = [selena, "--paramconfig", paramconfig]
    extra = str(_cfg("additionalCommand", "")).split()
    cmd.extend(extra)
    print("running: " + " ".join(cmd))
    rc = subprocess.call(cmd)
    print("selena return code: " + str(rc))
    for path in (paramconfig, logfile, outputfile):
        if os.path.isfile(path):
            try:
                shutil.copy2(path, os.path.join(outputpath, os.path.basename(path)))
            except Exception as exc:
                print("copy back failed for " + path + ": " + str(exc))
    return 1 if rc == 0 and os.path.isfile(outputfile) and os.path.getsize(outputfile) > 1024 else 0


def filter(inputfile, outputfile):
    return 1


def finalstep(inputfile, outputpath):
    return 1


simulation_runtime.init(filter, simulation, finalstep)
'''


def package_to_dict(package: ClusterJobPackage) -> dict[str, Any]:
    return asdict(package)
