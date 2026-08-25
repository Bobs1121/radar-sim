"""Contract tests for the thin radar-sim MCP adapter."""

from __future__ import annotations

import asyncio
import importlib.util
import json

import httpx
import pytest

from radar_sim_sdk import RadarSimClient
from radar_sim_mcp.connector import read_install_state


mcp_available = importlib.util.find_spec("mcp") is not None
pytestmark = pytest.mark.skipif(not mcp_available, reason="MCP extra is not installed")


def _server():
    from radar_sim_mcp.server import RadarSimMcpServer

    client = RadarSimClient(
        "http://testserver",
        user="alice",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
        trust_env=False,
    )
    return RadarSimMcpServer(client)


def test_mcp_registers_the_agent_surface_without_a_second_scheduler():
    server = _server()
    tools = asyncio.run(server.app.list_tools())
    names = {tool.name for tool in tools}

    assert {
        "get_simulation_schema",
        "get_simulation_readiness",
        "get_simulation_capabilities",
        "check_agent_tools",
        "update_agent_tools",
        "check_windows_connector",
        "install_or_update_windows_connector",
        "validate_simulation",
        "submit_simulation",
        "get_simulation",
        "get_simulation_events",
        "wait_simulation",
        "get_simulation_transfer",
        "resume_simulation_transfer",
        "cancel_simulation",
        "retry_simulation_stage",
        "retry_failed_inputs",
        "diagnose_simulation",
        "get_simulation_manifest",
        "download_simulation_result",
    }.issubset(names)


def test_mcp_tool_call_returns_the_common_success_envelope():
    server = _server()

    _content, result = asyncio.run(server.app.call_tool("get_simulation_capabilities", {}))

    assert result["ok"] is True
    assert result["data"] == {}


def test_mcp_agent_tools_update_is_confirmation_gated():
    server = _server()

    _content, result = asyncio.run(
        server.app.call_tool("update_agent_tools", {"confirm": False})
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "confirmation_required"


def test_mcp_official_skill_install_can_prepare_without_repeated_confirmation(monkeypatch):
    server = _server()
    monkeypatch.setenv("RADAR_SIM_AUTO_PREPARE", "1")
    monkeypatch.delenv("RADAR_SIM_ALLOW_AGENT_TOOLS_UPDATE", raising=False)

    _content, result = asyncio.run(
        server.app.call_tool("update_agent_tools", {"confirm": False})
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "policy_blocked"


def test_mcp_config_input_requires_exactly_one_source():
    from radar_sim_mcp.server import _config_input

    with pytest.raises(ValueError, match="exactly one"):
        _config_input()
    with pytest.raises(ValueError, match="exactly one"):
        _config_input(yaml_text="schema_version: '2.0'", config={})


def test_mcp_api_error_is_structured_without_a_traceback():
    from radar_sim_mcp.server import _error_payload
    from radar_sim_sdk import RadarSimApiError

    error = _error_payload(
        RadarSimApiError(
            "cluster_readiness_unavailable",
            "Cluster is unavailable",
            status_code=503,
            actions=[{"type": "retry_readiness"}],
            request_id="req-1",
        )
    )

    assert error["type"] == "api_error"
    assert error["code"] == "cluster_readiness_unavailable"
    assert error["retryable"] is True
    assert error["request_id"] == "req-1"
    assert "Traceback" not in error["message"]


def test_connector_install_state_accepts_windows_utf8_bom(tmp_path):
    state = {
        "agent_id": "agent-1",
        "owner": "user-alice",
        "server_url": "http://testserver",
        "mode": "unified",
        "connector_contract_version": 16,
    }
    install_root = tmp_path / "radar-sim"
    install_root.mkdir()
    (install_root / "install.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps(state).encode("utf-8")
    )

    result = read_install_state(install_root)

    assert result["installed"] is True
    assert result["agent_id"] == "agent-1"
    assert result["connector_contract_version"] == 16
