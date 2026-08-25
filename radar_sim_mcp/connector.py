"""Local Windows Connector installation and exact-device verification.

This module is intentionally outside ``radar_sim_sdk``: the SDK is a safe
HTTP/data-plane client and never launches local installers.  The MCP adapter
may opt into this local mutation because it runs beside the user's Agent, but
it still requires both process policy and per-call confirmation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any

from radar_sim_sdk import RadarSimClient


class ConnectorInstallError(RuntimeError):
    """A local Connector install/update could not reach the desired state."""

    code = "connector_install_failed"


def default_install_root() -> Path:
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base) / "radar-sim"


def read_install_state(install_root: str | Path | None = None) -> dict[str, Any]:
    """Read non-secret local install metadata, if present."""

    root = Path(install_root or default_install_root()).expanduser()
    path = root / "install.json"
    if not path.is_file():
        return {"installed": False, "install_root": str(root)}
    try:
        # Windows PowerShell installers may emit UTF-8 with a BOM.  The
        # metadata is machine-generated and not user text; accepting both
        # UTF-8 variants avoids falsely treating a completed installation as
        # unreadable while remaining strict about JSON structure below.
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConnectorInstallError("Connector install metadata is unreadable") from exc
    if not isinstance(raw, dict):
        raise ConnectorInstallError("Connector install metadata is invalid")
    # Never expose credentials.json or arbitrary local metadata to MCP.
    return {
        "installed": True,
        "install_root": str(root),
        "agent_id": str(raw.get("agent_id") or ""),
        "owner": str(raw.get("owner") or ""),
        "server_url": str(raw.get("server_url") or ""),
        "mode": str(raw.get("mode") or ""),
        "connector_contract_version": raw.get("connector_contract_version"),
    }


def check_connector(
    client: RadarSimClient,
    *,
    install_root: str | Path | None = None,
) -> dict[str, Any]:
    """Combine local install metadata with exact server-side device status."""

    state = read_install_state(install_root)
    capabilities = client.capabilities()
    result: dict[str, Any] = {
        "installed": bool(state.get("installed")),
        "available": False,
        "contract_current": False,
        "update_required": bool(
            ((capabilities.get("capabilities") or {}).get("windows_connector") or {}).get(
                "update_required", False
            )
        ),
        "reason": "not_installed" if not state.get("installed") else "not_registered",
        "capabilities": capabilities,
    }
    agent_id = str(state.get("agent_id") or "").strip()
    if agent_id:
        status = client.windows_connector_status(agent_id)
        result.update(
            {
                "available": bool(status.get("available")),
                "contract_current": bool(status.get("contract_current")),
                "reason": str(status.get("reason") or ""),
                "update_required": not bool(status.get("contract_current")),
            }
        )
    return result


def install_or_update_connector(
    client: RadarSimClient,
    *,
    install_root: str | Path | None = None,
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    """Run the same signed/current launcher used by Web and verify exact device.

    The launcher is run in a new visible Windows console because the installer
    may need to show Python/package/Task Scheduler diagnostics to the user.
    No shell command is assembled from user text; only the downloaded launcher
    path is passed as an argument to ``cmd.exe /c``.
    """

    if os.name != "nt":
        raise ConnectorInstallError("Automatic Connector installation requires Windows")
    timeout = max(10.0, float(timeout_seconds))
    poll_interval = max(0.5, float(poll_interval_seconds))
    with tempfile.TemporaryDirectory(prefix="radar-sim-mcp-connector-") as temporary:
        launcher = client.download_windows_connector(Path(temporary))
        creationflags = int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        try:
            completed = subprocess.run(
                ["cmd.exe", "/c", str(launcher)],
                cwd=str(Path(temporary)),
                check=False,
                timeout=timeout,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConnectorInstallError("Connector installer exceeded its time limit") from exc
        except OSError as exc:
            raise ConnectorInstallError("Could not start the Connector installer") from exc
        if completed.returncode != 0:
            raise ConnectorInstallError(
                f"Connector installer exited with code {completed.returncode}"
            )

    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() <= deadline:
        last = check_connector(client, install_root=install_root)
        if last.get("available") and last.get("contract_current"):
            return last
        time.sleep(min(poll_interval, max(0.1, deadline - time.monotonic())))
    raise ConnectorInstallError(
        "Connector installer finished but this exact computer did not become available"
    )


__all__ = [
    "ConnectorInstallError",
    "check_connector",
    "default_install_root",
    "install_or_update_connector",
    "read_install_state",
]
