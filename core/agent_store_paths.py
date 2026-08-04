"""Standard-library-only paths shared by lightweight Windows Agent stores."""

from __future__ import annotations

import os
from pathlib import Path


def default_agent_binding_db_path() -> Path:
    """Return the per-user local Agent binding database path.

    Keeping this tiny helper independent from the workspace/artifact modules
    lets the light Agent authorize and upload MF4 data without importing the
    optional YAML/configuration stack.
    """
    rsim_home = os.environ.get("RSIM_HOME", "").strip()
    base = Path(rsim_home).expanduser() / "agent" if rsim_home else Path.home() / ".rsim" / "agent"
    return base / "bindings.db"


__all__ = ["default_agent_binding_db_path"]
