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
from pathlib import Path
import shutil
import subprocess
import sys

from bootstrap_agent_tools import BootstrapFailure, _default_root, _internal_log, bootstrap


def _terminal_status(message: str) -> None:
    try:
        sys.stderr.write(f"[radar-sim] {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        return


def _run_mcp(command: str, args: list[str], environment: dict[str, str]) -> int:
    """Run the configured MCP while inheriting the Agent's stdio handles."""

    try:
        completed = subprocess.run(
            [command, *args],
            env=environment,
            check=False,
            stdin=None,
            stdout=None,
            stderr=None,
        )
    except OSError:
        _internal_log(_default_root(), "MCP process launch failed")
        print("仿真 MCP 启动失败", file=sys.stderr)
        return 3
    return int(completed.returncode)

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


def _existing_mcp_is_usable(
    existing: tuple[str, list[str], dict[str, str]] | None,
) -> bool:
    """Return whether the local MCP can start without a bootstrap round-trip."""

    if existing is None:
        return False
    command, args, _environment = existing
    command_path = Path(command)
    if not command_path.is_file() and shutil.which(command) is None:
        return False
    if len(args) >= 2 and args[0] == "-m" and args[1] == "radar_sim_mcp.server":
        return True
    # Legacy configurations use a Python command plus the stable launcher.
    # The launcher validates install.json and selects the versioned venv.
    return bool(args) and Path(args[0]).is_file()


def main() -> int:
    existing = _existing_mcp_command()
    if _existing_mcp_is_usable(existing):
        # A valid local install is the normal hot path.  Compatibility/update
        # checks belong to the MCP Skill flow, not every stdio process start.
        _internal_log(_default_root(), "using existing Agent Tools installation")
        command, args, configured_env = existing  # type: ignore[misc]
    else:
        _terminal_status("准备本地仿真服务")
        try:
            bootstrap_result = bootstrap()
        except BootstrapFailure as exc:
            _internal_log(_default_root(), f"bootstrap deferred: {type(exc).__name__}")
            existing = _existing_mcp_command()
            if not _existing_mcp_is_usable(existing):
                print("仿真能力准备失败", file=sys.stderr)
                return 2
            command, args, configured_env = existing  # type: ignore[misc]
        else:
            command, args, configured_env = _existing_mcp_command() or ("", [], {})
            if not _existing_mcp_is_usable((command, args, configured_env)):
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
    return _run_mcp(command, args, environment)


if __name__ == "__main__":
    raise SystemExit(main())
