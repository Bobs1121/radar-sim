"""Regression tests for the Windows stdio MCP launch path."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import pytest

from scripts import agent_mcp_launcher


def test_stable_launcher_runs_server_as_child_with_inherited_stdio(monkeypatch, tmp_path: Path):
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"python")
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Completed:
        returncode = 17

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    monkeypatch.setattr(agent_mcp_launcher.subprocess, "run", fake_run)

    result = agent_mcp_launcher._run_server(executable, ["--probe"])

    assert result == 17
    assert calls == [
        (
            [str(executable), "-m", "radar_sim_mcp.server", "--probe"],
            {"check": False, "stdin": None, "stdout": None, "stderr": None},
        )
    ]


def test_start_mcp_and_stable_launcher_do_not_replace_the_stdio_process():
    launcher = Path(__file__).parents[1] / "scripts" / "agent_mcp_launcher.py"
    start_mcp = Path(__file__).parents[1] / "skills" / "radar-sim-simulation" / "scripts" / "start_mcp.py"

    assert "os.execv(" not in launcher.read_text(encoding="utf-8")
    assert "os.execvpe(" not in start_mcp.read_text(encoding="utf-8")


def test_installer_registers_the_versioned_server_directly():
    installer = Path(__file__).parents[1] / "scripts" / "install_radar_sim_agent.py.in"
    source = installer.read_text(encoding="utf-8")

    assert '"command": str(venv_python)' in source
    assert '"args": ["-m", "radar_sim_mcp.server"]' in source
    assert '"args": [str(launcher)]' not in source


def test_versioned_server_completes_mcp_initialize_over_stdio():
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exercise() -> None:
        environment = dict(os.environ)
        environment["RADAR_SIM_BASE_URL"] = "http://127.0.0.1:1"
        environment["RADAR_SIM_MCP_TRANSPORT"] = "stdio"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "radar_sim_mcp.server"],
            env=environment,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                result = await session.initialize()
                assert result.serverInfo.name == "radar-sim"

    asyncio.run(exercise())
