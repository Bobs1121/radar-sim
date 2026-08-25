"""Stdlib-only stable launcher for the versioned local radar-sim MCP install."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def _status(message: str) -> None:
    try:
        sys.stderr.write(f"[radar-sim] {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        return


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
    os.execv(str(executable), [str(executable), "-m", "radar_sim_mcp.server", *sys.argv[1:]])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
