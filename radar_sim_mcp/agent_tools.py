"""Local version checks and restart-safe Agent Tools updates."""

from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit

from radar_sim_sdk import RadarSimClient


class AgentToolsUpdateError(RuntimeError):
    code = "agent_tools_update_failed"


def _private_or_local_service_host(base_url: str) -> str:
    """Return a literal private/local host that must bypass enterprise proxies."""

    hostname = (urlsplit(str(base_url or "")).hostname or "").strip()
    if not hostname or hostname.casefold() == "localhost":
        return hostname if hostname.casefold() == "localhost" else ""
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return ""
    return hostname if address.is_private or address.is_loopback or address.is_link_local else ""


def _append_service_no_proxy(environment: dict[str, str], base_url: str) -> None:
    """Make the installer use the same direct path as the SDK for lab hosts."""

    hostname = _private_or_local_service_host(base_url)
    if not hostname:
        return
    entries: list[str] = []
    for key in ("NO_PROXY", "no_proxy"):
        entries.extend(
            item.strip()
            for item in str(environment.get(key) or "").split(",")
            if item.strip()
        )
    lowered_host = hostname.casefold()
    covered = any(
        item == "*"
        or item.casefold() == lowered_host
        or (item.startswith(".") and lowered_host.endswith(item.casefold()))
        for item in entries
    )
    if not covered:
        entries.append(hostname)
    merged = ",".join(dict.fromkeys(entries))
    environment["NO_PROXY"] = merged
    environment["no_proxy"] = merged


def default_agent_tools_root() -> Path:
    override = os.environ.get("RADAR_SIM_MCP_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip() or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME", "").strip() or str(Path.home() / ".local" / "share")
    return (Path(base) / "radar-sim-mcp").resolve()


def _local_state() -> dict[str, Any]:
    path = default_agent_tools_root() / "install.json"
    if not path.is_file():
        return {"installed": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentToolsUpdateError("local Agent Tools state is unreadable") from exc
    return dict(value) if isinstance(value, dict) else {"installed": False}


def check_agent_tools(client: RadarSimClient) -> dict[str, Any]:
    """Compare local MCP/Skill state with the server's public manifest."""

    remote = client.agent_tools_manifest()
    local = _local_state()
    remote_release = str(remote.get("release_version") or "")
    local_release = str(local.get("release_version") or "")
    return {
        "installed": bool(local.get("installed", True) and local.get("version_key")),
        "current_release_version": local_release,
        "available_release_version": remote_release,
        "current_sdk_version": str(local.get("sdk_version") or ""),
        "current_mcp_version": str(local.get("mcp_version") or ""),
        "current_skill_version": str(local.get("skill_version") or ""),
        "current_mcp_tool_contract_version": str(local.get("mcp_tool_contract_version") or ""),
        "current_mcp_dependency_version": str(local.get("mcp_dependency_version") or ""),
        "current_bundle_sha256": str(local.get("bundle_sha256") or ""),
        "available_bundle_sha256": str(((remote.get("bundle") or {}).get("sha256") or "")),
        "update_available": bool(
            remote_release
            and (
                remote_release != local_release
                or str(local.get("bundle_sha256") or "")
                != str(((remote.get("bundle") or {}).get("sha256") or ""))
            )
        ),
        "sdk_version": str(remote.get("sdk_version") or ""),
        "mcp_version": str(remote.get("mcp_version") or ""),
        "skill_version": str(remote.get("skill_version") or ""),
        "mcp_tool_contract_version": str(remote.get("mcp_tool_contract_version") or ""),
        "mcp_dependency_version": str(remote.get("mcp_dependency_version") or ""),
        "skill_registry_configured": bool(local.get("skill_registry_path")),
        "api_version": str(remote.get("api_version") or ""),
        "config_schema_version": str(remote.get("config_schema_version") or ""),
        "restart_required": False,
    }


def update_agent_tools(
    client: RadarSimClient,
    *,
    timeout_seconds: float = 600.0,
) -> dict[str, Any]:
    """Install the current version side-by-side and activate its pointer.

    The bootstrap creates a new versioned virtual environment and switches the
    stable launcher only after import validation. A running MCP process is not
    modified in place; its next restart uses the new release.
    """

    before = check_agent_tools(client)
    with tempfile.TemporaryDirectory(prefix="radar-sim-agent-update-") as temporary:
        script = client.download_agent_tools_bootstrap(Path(temporary))
        environment = dict(os.environ)
        environment.update(client._agent_tools_bootstrap_environment())
        _append_service_no_proxy(environment, str(client._client.base_url))
        environment.setdefault("RADAR_SIM_BASE_URL", "")
        completed = subprocess.run(
            [sys.executable, str(script)],
            env=environment,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=max(30.0, min(float(timeout_seconds), 1800.0)),
        )
        if completed.returncode != 0:
            raise AgentToolsUpdateError("Agent Tools bootstrap failed; the previous installation was preserved")
    result = check_agent_tools(client)
    if not result.get("installed") or not result.get("available_release_version"):
        raise AgentToolsUpdateError("Agent Tools update did not produce a valid local installation")
    result["restart_required"] = bool(
        before.get("current_release_version") != result.get("current_release_version")
        or before.get("current_bundle_sha256") != result.get("current_bundle_sha256")
    )
    result["skill_updated"] = bool(result["restart_required"])
    return result


__all__ = ["AgentToolsUpdateError", "check_agent_tools", "default_agent_tools_root", "update_agent_tools"]
