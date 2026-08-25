#!/usr/bin/env python3
"""Start the local radar-sim stdio MCP with first-run auto-bootstrap.

An Agent host can register this script as the stdio command immediately after
installing only the Skill.  It downloads/updates the source-free MCP bundle
before the MCP protocol starts, and therefore never writes bootstrap logs to
stdout.  Stdout is reserved for MCP JSON-RPC traffic.
"""

from __future__ import annotations

import json
import os
import sys

from bootstrap_agent_tools import BootstrapFailure, _default_root, _internal_log, bootstrap


def _terminal_status(message: str) -> None:
    try:
        sys.stderr.write(f"[radar-sim] {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        return


def _existing_mcp_command() -> tuple[str, list[str], dict[str, str]] | None:
    path = _default_root() / "mcp-config.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    command = str(value.get("command") or "").strip()
    args = value.get("args")
    env = value.get("env")
    if not command or not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        return None
    configured_env = dict(env) if isinstance(env, dict) else {}
    return command, list(args), {str(key): str(item) for key, item in configured_env.items()}


def main() -> int:
    _terminal_status("准备本地仿真服务")
    try:
        bootstrap_result = bootstrap()
    except BootstrapFailure as exc:
        _internal_log(_default_root(), f"bootstrap deferred: {type(exc).__name__}")
        existing = _existing_mcp_command()
        if existing is None:
            print("仿真能力准备失败", file=sys.stderr)
            return 2
        command, args, configured_env = existing
    else:
        command, args, configured_env = _existing_mcp_command() or ("", [], {})
        if not command:
            _internal_log(_default_root(), "bootstrap completed without a usable MCP command")
            print("仿真能力启动失败", file=sys.stderr)
            return 2
        if bootstrap_result.get("restart_required"):
            _internal_log(_default_root(), "local Agent Tools release activated")

    environment = dict(os.environ)
    environment.update(configured_env)
    environment.setdefault("RADAR_SIM_ALLOW_AGENT_TOOLS_UPDATE", "1")
    environment.setdefault("RADAR_SIM_ALLOW_CONNECTOR_INSTALL", "1")
    environment.setdefault("RADAR_SIM_AUTO_PREPARE", "1")
    _terminal_status("本地仿真服务已就绪")
    try:
        os.execvpe(command, [command, *args], environment)
    except OSError:
        _internal_log(_default_root(), "MCP process launch failed")
        print("仿真 MCP 启动失败", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
