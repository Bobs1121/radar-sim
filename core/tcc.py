"""TCC (Tool Collection) integration: itc2 bootstrap + toolcollection install.

Selena builds depend on the Bosch-internal TCC mechanism. The
``jenkins_selena_build.bat`` reads ``ip_if/tcc_toolversion_itc2.txt`` to get a
toolcollection name (e.g. ``IF:BTC-7.0.0``), then ``itc2.exe install`` downloads
it and ``init.bat`` sets ``TCCPATH_*`` env vars (boost/python3/cmake/mingw64/
selena_environment ...). MATLAB/Boost/Qt are all provided by TCC — users do
not install them manually.

This module lets radar-sim bootstrap itc2 itself (copy from an ITO share) and
detect/install the required toolcollection, so a fresh machine can be readied
without manual TCC setup.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_TCC_ROOT = r"C:\TCC"
DEFAULT_ITC2_EXE = r"C:\TCC\itc2\itc2.exe"
DEFAULT_TCC_INIT_DIR = r"C:\TCC\Tools\tcc_init"

# Active ITO mirrors (from ItoConfig.json availableItoLinks). Suzhou first
# (APAC default), then by region. Override via config.tcc.ito_share.
ITO_MIRRORS = [
    r"\\szhccfile.apac.bosch.com\ito",   # Suzhou/China
    r"\\bmh2fs01.apac.bosch.com\ito",    # Bangalore/India
    r"\\cob0fs01.apac.bosch.com\ito",    # Coimbatore/India
    r"\\yh0vm019.apac.bosch.com\ito",    # Yokohama/Japan
    r"\\cl-vm009.apac.bosch.com\ito",    # Clayton/Australia
    r"\\abtv1000.de.bosch.com\ito",      # Abstatt/Germany
    r"\\bx-isminst.de.bosch.com\ito",    # Boxberg/Germany
    r"\\brh1fs01.de.bosch.com\ito",      # Breidenberg/Germany
    r"\\plyism02.us.bosch.com\ito",      # Plymouth/US
    r"\\bauism01.us.bosch.com\ito",      # Baudette/US
    r"\\ao-vism.de.bosch.com\ito",       # Arjeplog/Sweden
    r"\\hc10fs01.apac.bosch.com\ito",    # Ho Chi Minh/Vietnam
    r"\\CAVMISMPROFILER.br.bosch.com\ito",  # Campinas/Brasil
    r"\\brgsrvism.brg.emea.bosch.com\ito",  # Braga/Portugal
]

LogFn = Optional[Callable[[str], None]]


@dataclass
class Itc2Status:
    installed: bool
    exe_path: str = ""
    version: str = ""
    ito_reachable: bool = False
    ito_share: str = ""
    detail: str = ""


@dataclass
class ToolCollectionStatus:
    name: str
    installed: bool = False
    init_bat_present: bool = False
    init_bat_path: str = ""
    sample_tool_path: str = ""
    detail: str = ""


@dataclass
class InstallResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    detail: str = ""


# ------------------------------------------------------------------
# Config helpers
# ------------------------------------------------------------------

def _itc2_exe(config: Optional[dict]) -> str:
    tcc = (config or {}).get("tcc") or {}
    return str(tcc.get("itc2_exe") or DEFAULT_ITC2_EXE)


def _ito_share(config: Optional[dict]) -> str:
    tcc = (config or {}).get("tcc") or {}
    return str(tcc.get("ito_share") or "")


# ------------------------------------------------------------------
# itc2 detection + bootstrap
# ------------------------------------------------------------------

def detect_itc2(config: Optional[dict] = None) -> Itc2Status:
    """Check whether itc2.exe is installed and read its version."""
    exe = _itc2_exe(config)
    if not Path(exe).exists():
        return Itc2Status(installed=False, exe_path=exe, detail=f"not found: {exe}")
    version = _read_itc2_version(exe)
    return Itc2Status(installed=True, exe_path=exe, version=version, detail=f"v{version}")


def _read_itc2_version(exe: str) -> str:
    version_json = Path(exe).parent / "version.json"
    try:
        data = json.loads(version_json.read_text(encoding="utf-8"))
        v = data.get("version") or {}
        return f"{v.get('major','?')}.{v.get('minor','?')}.{v.get('revision','?')}"
    except Exception:
        return "?"


def detect_ito_share(config: Optional[dict] = None) -> tuple[bool, str]:
    """Probe ITO mirrors; return (reachable, share_path). Config override first."""
    configured = _ito_share(config)
    candidates = [configured] + [m for m in ITO_MIRRORS if m != configured] if configured else ITO_MIRRORS
    for mirror in candidates:
        if not mirror:
            continue
        if _ito_mirror_has_itc2(mirror):
            return True, mirror
    return False, ""


def _ito_mirror_has_itc2(mirror: str) -> bool:
    """Quick reachability check for an ITO mirror's itc2 package."""
    # Prefer a fast filesystem check, but guard against UNC hangs with a socket
    # probe of the SMB host (port 445) first.
    host = mirror.lstrip("\\").split("\\")[0]
    if not _port_open(host, 445, timeout=2.0):
        return False
    try:
        return Path(mirror, "TCC", "itc2", "itc2.exe").exists()
    except OSError:
        return False


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_itc2(config: Optional[dict] = None, log: LogFn = None) -> Itc2Status:
    """Ensure itc2 is available; bootstrap from ITO if missing."""
    status = detect_itc2(config)
    if status.installed:
        if log:
            log(f"itc2 already installed: {status.exe_path} (v{status.version})")
        return status

    reachable, share = detect_ito_share(config)
    status.ito_reachable = reachable
    status.ito_share = share
    if not reachable:
        status.detail = "itc2 not found and no ITO mirror reachable — connect to the Bosch intranet/VPN and retry."
        if log:
            log(status.detail)
        return status

    if log:
        log(f"Bootstrapping itc2 from {share} ...")
    result = bootstrap_itc2_from_ito(share, _itc2_exe(config), log=log)
    if not result.ok:
        status.detail = f"bootstrap failed: {result.detail}"
        return status

    # Re-detect after install.
    status = detect_itc2(config)
    status.ito_reachable = True
    status.ito_share = share
    if log:
        log(f"itc2 installed: {status.exe_path} (v{status.version})")
    return status


def bootstrap_itc2_from_ito(ito_share: str, target_exe: str = DEFAULT_ITC2_EXE, log: LogFn = None) -> InstallResult:
    """Copy itc2 package from an ITO mirror to the local TCC tree."""
    src_dir = Path(ito_share, "TCC", "itc2")
    dst_dir = Path(target_exe).parent
    if not src_dir.exists():
        return InstallResult(False, 1, "", "", f"ITO itc2 source not found: {src_dir}")
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        if log:
            log(f"Copying {src_dir} -> {dst_dir} ...")
        # copytree with dirs_exist_ok so a partial prior install is merged.
        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
    except OSError as exc:
        return InstallResult(False, 1, "", str(exc), f"copy failed: {exc}")

    # Generate ItoConfig.json if itc2 needs it (itc2.exe setup does the probe).
    exe = str(dst_dir / "itc2.exe")
    try:
        proc = subprocess.run([exe, "setup"], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0 and log:
            log(f"itc2 setup returned {proc.returncode} (may be ok if config already exists)")
    except Exception as exc:
        if log:
            log(f"itc2 setup skipped: {exc}")
    return InstallResult(ok=Path(exe).exists(), returncode=0, stdout="", stderr="", detail=f"bootstrapped to {exe}")


# ------------------------------------------------------------------
# Toolcollection detection + install
# ------------------------------------------------------------------

def read_required_toolcollection(config: dict) -> str:
    """Read the toolcollection name from <repo>/ip_if/tcc_toolversion_itc2.txt.

    File content looks like ``IF:BTC-7.0.0``. Returns "" if the file is absent
    (legacy/unconfigured projects).
    """
    repos = config.get("repos") or {}
    repo_root = repos.get("inner_repo_root") or repos.get("outer_repo_root") or config.get("project_root", "")
    if not repo_root:
        return ""
    version_file = Path(repo_root) / "ip_if" / "tcc_toolversion_itc2.txt"
    if not version_file.exists():
        return ""
    try:
        text = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    # Normalize: the file may contain "IF:BTC-7.0.0" possibly with extra tokens.
    for token in text.split():
        if ":" in token:
            return token
    return text


def check_toolcollection(config: Optional[dict], toolcollection: str) -> ToolCollectionStatus:
    """Check if a toolcollection is installed via ``itc2.exe get-toolpath``."""
    if not toolcollection:
        return ToolCollectionStatus(name="", installed=False, detail="no toolcollection configured")
    exe = _itc2_exe(config)
    if not Path(exe).exists():
        return ToolCollectionStatus(name=toolcollection, installed=False, detail=f"itc2 not found: {exe}")
    # boost is a stable sentinel tool present in every toolcollection.
    proc = subprocess.run(
        [exe, "get-toolpath", toolcollection, "boost"],
        capture_output=True, text=True, timeout=30,
    )
    # itc2 is a node app — stderr may carry DeprecationWarning noise; judge by
    # returncode + stdout only.
    sample = ""
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line and (":\\" in line or line.startswith("\\\\")):
            sample = line
            break
    installed = proc.returncode == 0 and bool(sample)
    init_path = get_init_bat_path(toolcollection)
    return ToolCollectionStatus(
        name=toolcollection,
        installed=installed,
        init_bat_present=bool(init_path and Path(init_path).exists()),
        init_bat_path=init_path,
        sample_tool_path=sample,
        detail=f"sample boost={sample or '(none)'}; init.bat={init_path or '(not found)'}",
    )


def get_init_bat_path(toolcollection: str) -> str:
    """Find the init.bat for a toolcollection by fuzzy-matching the version segment.

    ``IF:BTC-7.0.0`` may map to ``C:\\TCC\\Tools\\tcc_init\\TCC_IF_Windows_BTC-7.0.0\\init.bat``.
    The exact prefix is not stable, so glob on the version segment.
    """
    if not toolcollection:
        return ""
    # Extract the version segment after ':' (e.g. "BTC-7.0.0" from "IF:BTC-7.0.0").
    version_seg = toolcollection.split(":", 1)[-1] if ":" in toolcollection else toolcollection
    patterns = [
        os.path.join(DEFAULT_TCC_INIT_DIR, f"*{version_seg}*", "init.bat"),
        os.path.join(DEFAULT_TCC_INIT_DIR, f"*{version_seg.replace('-', '_')}*", "init.bat"),
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return ""


def install_toolcollection(config: Optional[dict], toolcollection: str, log: LogFn = None) -> InstallResult:
    """Run ``itc2.exe install <toolcollection>``. Long task — caller should run in a thread."""
    if not toolcollection:
        return InstallResult(False, 1, "", "", "no toolcollection specified")
    exe = _itc2_exe(config)
    if not Path(exe).exists():
        return InstallResult(False, 1, "", "", f"itc2 not found: {exe}")
    if log:
        log(f"Installing toolcollection {toolcollection} via {exe} ...")
    try:
        proc = subprocess.Popen(
            [exe, "install", toolcollection],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        out_lines: list[str] = []
        for line in iter(proc.stdout.readline, ""):
            out_lines.append(line.rstrip())
            if log:
                log(line.rstrip())
        proc.stdout.close()
        proc.wait()
        stdout = "\n".join(out_lines)
        ok = proc.returncode == 0
        return InstallResult(ok=ok, returncode=proc.returncode, stdout=stdout, stderr="",
                             detail=f"install {toolcollection} {'ok' if ok else 'failed'} (exit {proc.returncode})")
    except Exception as exc:
        return InstallResult(False, 1, "", str(exc), f"install failed: {exc}")


def ensure_environment(config: dict, log: LogFn = None) -> tuple[Itc2Status, ToolCollectionStatus]:
    """One-stop: ensure itc2, then ensure the required toolcollection is installed."""
    itc2 = ensure_itc2(config, log=log)
    if not itc2.installed:
        return itc2, ToolCollectionStatus(name="", installed=False, detail="itc2 unavailable")
    tc_name = read_required_toolcollection(config)
    if not tc_name:
        if log:
            log("No toolcollection configured (ip_if/tcc_toolversion_itc2.txt missing) — skipping TC check")
        return itc2, ToolCollectionStatus(name="", installed=True, detail="no toolcollection required")
    tc = check_toolcollection(config, tc_name)
    if tc.installed:
        if log:
            log(f"toolcollection {tc_name} ready (boost={tc.sample_tool_path})")
        return itc2, tc
    if log:
        log(f"toolcollection {tc_name} not installed — installing ...")
    result = install_toolcollection(config, tc_name, log=log)
    if not result.ok:
        tc.detail = f"install failed: {result.detail}"
        return itc2, tc
    return itc2, check_toolcollection(config, tc_name)


# ------------------------------------------------------------------
# Dependency derivation from build scripts (static analysis, no execution)
# ------------------------------------------------------------------

def derive_dependencies_from_build_script(config: dict) -> list[dict]:
    """Statically parse the env build script (cmake_build.bat) for dependencies.

    Returns a structured list: [{"kind": "toolcollection", "name": "IF:BTC-7.0.0"},
    {"kind": "init_bat", "path": "..."}, {"kind": "env_var", "name": "BOOST_ROOT", ...}].
    Never executes the script (it is interactive) — pure regex text analysis.
    Falls back to the selena build script if env_build_script is absent.
    """
    import re

    build = config.get("build") or {}
    script_path = str(build.get("env_build_script") or build.get("selena_build_script") or "")
    if not script_path or not Path(script_path).exists():
        return []
    try:
        text = Path(script_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    deps: list[dict] = []
    source = str(script_path)

    # 1. toolcollection name: itc2 install <name> > set TOOLCOLLECTION= > set /p TOOLCOLLECTION=<file>
    install_match = re.search(r"itc2\.exe\s+install\s+(\S+)", text, flags=re.IGNORECASE)
    if install_match:
        deps.append({"kind": "toolcollection", "name": install_match.group(1), "source": source})
    else:
        set_matches = re.findall(r"set\s+TOOLCOLLECTION=([^\r\n]+)", text, flags=re.IGNORECASE)
        for m in set_matches:
            m = m.strip().strip('"')
            if m and not m.startswith("<"):  # skip "<...txt" file-redirect, handled below
                deps.append({"kind": "toolcollection", "name": m, "source": source})
        # set /p TOOLCOLLECTION=<file.txt> → resolve via read_required_toolcollection
        prompt_match = re.search(r"set\s+/p\s+TOOLCOLLECTION=<([^\r\n]+)", text, flags=re.IGNORECASE)
        if prompt_match and not any(d["kind"] == "toolcollection" for d in deps):
            tc = read_required_toolcollection(config)
            if tc:
                deps.append({"kind": "toolcollection", "name": tc, "source": f"{source} -> {prompt_match.group(1).strip()}"})

    # 2. init.bat calls
    init_matches = re.findall(r"call\s+([^\r\n]*tcc_init[^\r\n]*init\.bat)", text, flags=re.IGNORECASE)
    for m in init_matches:
        deps.append({"kind": "init_bat", "path": m.strip(), "source": source})

    # 3. TCCPATH_* env vars referenced (boost/python3/selena_environment/cmake/mingw64 ...)
    tccpath_vars = set(re.findall(r"%(TCCPATH_\w+)%", text))
    for var in sorted(tccpath_vars):
        deps.append({"kind": "env_var", "name": var, "value": f"%{var}%", "source": source})

    # 4. R2D2 / python3 build entry
    if re.search(r"python3\s+.*R2D2\.py\b", text, flags=re.IGNORECASE):
        deps.append({"kind": "build_entry", "name": "R2D2.py", "source": source})

    return deps


# ------------------------------------------------------------------
# One-stop auto repair: ensure itc2 → derive toolcollection → install
# ------------------------------------------------------------------

@dataclass
class RepairStep:
    name: str            # ensure_itc2 | derive_toolcollection | install_toolcollection
    ok: bool
    detail: str
    toolcollection: str = ""


@dataclass
class RepairReport:
    ok: bool
    steps: list[RepairStep] = field(default_factory=list)
    toolcollection: str = ""
    summary: str = ""


def auto_repair_environment(config: dict, log: LogFn = None) -> RepairReport:
    """One-stop environment repair: ensure itc2, derive toolcollection, install if missing.

    Never runs cmake_build.bat (it is interactive) — only statically parses it via
    derive_dependencies_from_build_script. itc2 install is non-interactive and safe.
    """
    steps: list[RepairStep] = []

    # 1. ensure itc2 (bootstrap from ITO if missing)
    itc2 = ensure_itc2(config, log=log)
    steps.append(RepairStep("ensure_itc2", itc2.installed, itc2.detail))
    if not itc2.installed:
        return RepairReport(False, steps, summary=f"itc2 unavailable: {itc2.detail}")

    # 2. derive toolcollection name: build script (itc2 install / set TOOLCOLLECTION) first,
    #    fall back to ip_if/tcc_toolversion_itc2.txt.
    deps = derive_dependencies_from_build_script(config)
    tc_names = [d["name"] for d in deps if d["kind"] == "toolcollection"]
    tc_name = tc_names[0] if tc_names else read_required_toolcollection(config)
    steps.append(RepairStep("derive_toolcollection", bool(tc_name), f"derived: {tc_name or '(none)'}", tc_name))
    if not tc_name:
        return RepairReport(False, steps, toolcollection="",
                            summary="no toolcollection derivable (配置 code_path 或 env_build_script 以推导)")

    # 3. check + install if missing
    tc = check_toolcollection(config, tc_name)
    if tc.installed:
        steps.append(RepairStep("install_toolcollection", True,
                                f"{tc_name} already installed (boost={tc.sample_tool_path})", tc_name))
        return RepairReport(True, steps, tc_name, summary=f"{tc_name} ready")
    if log:
        log(f"toolcollection {tc_name} not installed — installing ...")
    result = install_toolcollection(config, tc_name, log=log)
    steps.append(RepairStep("install_toolcollection", result.ok, result.detail, tc_name))
    return RepairReport(result.ok, steps, tc_name,
                        summary=f"{tc_name} {'installed' if result.ok else 'install failed: ' + result.detail}")
