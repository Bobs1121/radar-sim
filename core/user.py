"""Per-user isolation helpers for the control plane.

Each user gets a separate SQLite control DB (``_control_<user>.db``) so jobs,
agents, and logs are fully isolated between users on a shared server. The user
identity is taken from ``RSIM_USER`` (explicit) or the OS username, and flows
through the HTTP layer via the ``X-Rsim-User`` request header.
"""

from __future__ import annotations

import getpass
import os
import re
from pathlib import Path

USER_HEADER = "X-Rsim-User"
_SAFE_USER = re.compile(r"[^A-Za-z0-9_.-]")


def current_user() -> str:
    """Return the current user identity.

    Priority: ``RSIM_USER`` env var > OS login user > ``default``.
    The value is sanitized to a filename-safe token for DB path construction.
    """
    raw = os.environ.get("RSIM_USER", "").strip()
    if not raw:
        try:
            raw = getpass.getuser()
        except Exception:
            raw = ""
    raw = (raw or "default").strip()
    safe = _SAFE_USER.sub("_", raw)
    return safe or "default"


def control_db_path_for_user(user: str | None = None) -> Path:
    """Return the control DB path for a user (follows RSIM_HOME)."""
    from core.control_service import _data_root

    user = user or current_user()
    results_dir = _data_root() / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    name = "_control.db" if user == "default" else f"_control_{user}.db"
    return results_dir / name
