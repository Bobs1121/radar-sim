"""Proxy routing tests for source-free Agent Tools bootstrap/update."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from radar_sim_mcp.agent_tools import _append_service_no_proxy


def _load_skill_bootstrap():
    path = (
        Path(__file__).parents[1]
        / "skills"
        / "radar-sim-simulation"
        / "scripts"
        / "bootstrap_agent_tools.py"
    )
    spec = importlib.util.spec_from_file_location("radar_sim_skill_bootstrap_proxy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mcp_update_adds_only_private_service_to_no_proxy():
    environment = {"NO_PROXY": "existing.example", "no_proxy": "existing.example"}

    _append_service_no_proxy(environment, "http://10.190.171.44:8877")

    assert environment["NO_PROXY"].split(",")[-1] == "10.190.171.44"
    assert environment["no_proxy"] == environment["NO_PROXY"]


def test_mcp_update_keeps_proxy_for_public_service():
    environment = {"NO_PROXY": "existing.example"}

    _append_service_no_proxy(environment, "https://sim.example.com")

    assert environment == {"NO_PROXY": "existing.example"}


def test_skill_bootstrap_bypasses_private_literal_host_only():
    module = _load_skill_bootstrap()

    assert module._bypass_proxy_for_url("http://10.190.171.44:8877") is True
    assert module._bypass_proxy_for_url("http://127.0.0.1:8877") is True
    assert module._bypass_proxy_for_url("https://sim.example.com") is False
