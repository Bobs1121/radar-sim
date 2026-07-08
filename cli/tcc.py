"""rsim tcc - TCC (Tool Collection) bootstrap/install/auto-repair.

Exposes core/tcc.py as subcommands so the control-plane agent can schedule
TCC tasks the same way it schedules build/sim (via `rsim tcc <action>`).
"""

from __future__ import annotations

import sys

from core.tcc import (
    auto_repair_environment,
    ensure_itc2,
    install_toolcollection,
    read_required_toolcollection,
)


def register(subparsers):
    parser = subparsers.add_parser("tcc", help="TCC toolchain bootstrap / install / auto-repair")
    sub = parser.add_subparsers(dest="tcc_command", help="TCC commands")

    sub.add_parser("bootstrap-itc2", help="Ensure itc2.exe is available (bootstrap from ITO if missing)")

    install = sub.add_parser("install", help="Install a toolcollection (e.g. IF:BTC-7.0.0)")
    install.add_argument("toolcollection", nargs="?", default="",
                         help="Toolcollection name; omit to read from ip_if/tcc_toolversion_itc2.txt")

    sub.add_parser("auto-repair", help="One-stop: ensure itc2 + derive + install toolcollection")

    status = sub.add_parser("status", help="Show itc2 + toolcollection detection (read-only)")
    status.add_argument("--toolcollection", default="", help="Toolcollection name to check")


def run(args, config):
    command = getattr(args, "tcc_command", "") or ""
    if command == "bootstrap-itc2":
        return _run_bootstrap_itc2(config)
    if command == "install":
        return _run_install(config, getattr(args, "toolcollection", "") or "")
    if command == "auto-repair":
        return _run_auto_repair(config)
    if command == "status":
        return _run_status(config, getattr(args, "toolcollection", "") or "")
    print("Missing tcc command. Use: rsim tcc bootstrap-itc2|install|auto-repair|status")
    return 1


def _log(message: str) -> None:
    print(message, flush=True)


def _run_bootstrap_itc2(config: dict) -> int:
    status = ensure_itc2(config, log=_log)
    if status.installed:
        _log(f"[OK] itc2 ready: {status.exe_path} (v{status.version})")
        return 0
    _log(f"[FAIL] itc2 unavailable: {status.detail}")
    return 1


def _run_install(config: dict, toolcollection: str) -> int:
    tc = toolcollection or read_required_toolcollection(config)
    if not tc:
        _log("[FAIL] no toolcollection specified and none derivable from ip_if/tcc_toolversion_itc2.txt")
        return 1
    result = install_toolcollection(config, tc, log=_log)
    if result.ok:
        _log(f"[OK] toolcollection {tc} installed")
        return 0
    _log(f"[FAIL] install failed: {result.detail}")
    return 1


def _run_auto_repair(config: dict) -> int:
    report = auto_repair_environment(config, log=_log)
    _log(f"[RESULT] {report.summary}")
    for step in report.steps:
        _log(f"  [{step.name}] {'OK' if step.ok else 'FAIL'}: {step.detail}")
    return 0 if report.ok else 1


def _run_status(config: dict, toolcollection: str) -> int:
    from core.tcc import check_toolcollection, detect_itc2

    itc2 = detect_itc2(config)
    _log(f"itc2: installed={itc2.installed} exe={itc2.exe_path} version={itc2.version}")
    _log(f"  detail: {itc2.detail}")
    tc = toolcollection or read_required_toolcollection(config)
    if tc:
        st = check_toolcollection(config, tc)
        _log(f"toolcollection {tc}: installed={st.installed} detail={st.detail}")
    else:
        _log("toolcollection: none derivable from config")
    return 0
