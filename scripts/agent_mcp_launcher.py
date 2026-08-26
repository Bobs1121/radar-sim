"""Stdlib-only stable launcher for the versioned local radar-sim MCP install."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


def _status(message: str) -> None:
    try:
        sys.stderr.write(f"[radar-sim] {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        return


def _run_server(executable: Path, arguments: list[str]) -> int:
    """Run the real MCP as a child so Windows stdio handles are inherited."""

    command = [str(executable), "-m", "radar_sim_mcp.server", *arguments]
    try:
        # Explicit None means inherit the stdio handles supplied by the Agent
        # host.  Do not replace this process with os.execv on Windows: VS Code
        # stdio sessions can lose the handshake channel during replacement.
        completed = subprocess.run(
            command,
            check=False,
            stdin=None,
            stdout=None,
            stderr=None,
        )
    except OSError:
        _status("本地仿真服务启动失败")
        return 3
    return int(completed.returncode)

def main() -> int:
    root = Path(__file__).resolve().parent
    _status("启动本地仿真服务")
    state_path = root / "install.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        executable = Path(str(state["venv_python"])).expanduser().resolve()
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(f"radar-sim MCP install state is unavailable: {exc}")
    if not executable.is_file():
        raise SystemExit("radar-sim MCP virtual environment is unavailable; run the Agent Tools installer")
    _status("本地仿真服务已就绪")
    return _run_server(executable, sys.argv[1:])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
