"""Public, path-free distribution metadata for Agent Tools.

The Linux service may be configured with a prebuilt ZIP containing the SDK
wheel, MCP wheel, dependency wheels and Skill package.  This module reads only
the deployment-owned sidecar metadata and exposes a deliberately small public
manifest; physical server paths and build details never leave the service.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class AgentToolsDistributionError(ValueError):
    """The deployment does not have a valid Agent Tools release."""

    code = "agent_tools_unavailable"


def render_agent_tools_installer(*, template: Path, base_url: str) -> str:
    """Render the source-free Windows bootstrap with only a public URL."""

    try:
        source = template.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentToolsDistributionError("Agent Tools installer is unavailable") from exc
    value = str(base_url or "").rstrip("/")
    if not value.startswith(("http://", "https://")) or any(char in value for char in ("\r", "\n", '"')):
        raise AgentToolsDistributionError("Agent Tools public URL is invalid")
    rendered = source.replace("__RSIM_SERVER_URL__", value)
    return rendered if rendered.startswith("\ufeff") else "\ufeff" + rendered


@dataclass(frozen=True)
class AgentToolsDistribution:
    bundle_path: Path
    release_version: str
    sdk_version: str
    mcp_version: str
    skill_version: str
    mcp_tool_contract_version: str
    mcp_dependency_version: str
    api_version: str
    config_schema_version: str
    connector_contract_version: str
    python_requires: str
    bundle_sha256: str
    bundle_size: int

    @classmethod
    def from_files(
        cls,
        bundle_path: str | Path,
        manifest_path: str | Path | None = None,
    ) -> "AgentToolsDistribution":
        bundle = Path(bundle_path).expanduser().resolve()
        if not bundle.is_file():
            raise AgentToolsDistributionError("Agent Tools package is not available")
        sidecar = (
            Path(manifest_path).expanduser().resolve()
            if manifest_path
            else bundle.with_suffix(".json")
        )
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AgentToolsDistributionError("Agent Tools manifest is unavailable") from exc
        if not isinstance(payload, Mapping):
            raise AgentToolsDistributionError("Agent Tools manifest is invalid")
        try:
            actual_size = int(bundle.stat().st_size)
            if "bundle_size" not in payload:
                raise AgentToolsDistributionError("Agent Tools manifest is invalid")
            expected_size = int(payload["bundle_size"])
            expected_sha256 = str(payload.get("bundle_sha256") or "").strip().lower()
            if expected_size <= 0 or len(expected_sha256) != 64:
                raise AgentToolsDistributionError("Agent Tools manifest is invalid")
            actual_sha256 = _sha256(bundle)
            if expected_size != actual_size or expected_sha256 != actual_sha256:
                raise AgentToolsDistributionError("Agent Tools package checksum is invalid")
            values = {
                "release_version": str(payload["release_version"]),
                "sdk_version": str(payload["sdk_version"]),
                "mcp_version": str(payload["mcp_version"]),
                "skill_version": str(payload["skill_version"]),
                "mcp_tool_contract_version": str(payload.get("mcp_tool_contract_version") or "1.0"),
                "mcp_dependency_version": str(payload.get("mcp_dependency_version") or ""),
                "api_version": str(payload.get("api_version") or "v1"),
                "config_schema_version": str(payload.get("config_schema_version") or "2.0"),
                "connector_contract_version": str(payload.get("connector_contract_version") or ""),
                "python_requires": str(payload.get("python_requires") or ">=3.10"),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentToolsDistributionError("Agent Tools manifest is invalid") from exc
        return cls(
            bundle_path=bundle,
            bundle_sha256=actual_sha256,
            bundle_size=actual_size,
            **values,
        )

    def public_manifest(self, *, base_url: str) -> dict[str, Any]:
        """Return only stable public metadata and same-origin download URLs."""

        root = str(base_url or "").rstrip("/")
        return {
            "schema_version": "radar-sim.agent-tools/1.0",
            "release_version": self.release_version,
            "sdk_version": self.sdk_version,
            "mcp_version": self.mcp_version,
            "skill_version": self.skill_version,
            "mcp_tool_contract_version": self.mcp_tool_contract_version,
            "mcp_dependency_version": self.mcp_dependency_version,
            "api_version": self.api_version,
            "config_schema_version": self.config_schema_version,
            "connector_contract_version": self.connector_contract_version,
            "python_requires": self.python_requires,
            "bundle": {
                "size": self.bundle_size,
                "sha256": "sha256:" + self.bundle_sha256,
                "download_url": root + "/api/v1/agent-tools/package.zip",
            },
            "installer": {
                "windows_powershell_url": root + "/api/v1/agent-tools/install.ps1",
                "python_url": root + "/api/v1/agent-tools/install.py",
            },
            "actions": {
                "update": "Download the current bundle, verify sha256, install the new MCP/Skill release, then restart the MCP process.",
            },
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "AgentToolsDistribution",
    "AgentToolsDistributionError",
    "render_agent_tools_installer",
]
