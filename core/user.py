"""Per-user isolation helpers for the control plane.

Each user gets a separate SQLite control DB (``_control_<user>.db``) so jobs,
agents, and logs are fully isolated between users on a shared server. The user
identity is taken from ``RSIM_USER`` (explicit) or the OS username, and flows
through the HTTP layer via the ``X-Rsim-User`` request header.

The Web/SDK stable no-auth convention is ``user-<lowercase-id>``; the header
still remains a trusted-intranet label and is never a substitute for Bearer
authentication.
"""

from __future__ import annotations

import getpass
import os
import re
from pathlib import Path

USER_HEADER = "X-Rsim-User"
_SAFE_USER = re.compile(r"[^A-Za-z0-9_.-]")


def normalize_user(user: str | None) -> str:
    """Return a filename-safe user token for DB routing and metadata.

    This is not authentication; it only prevents path traversal and guarantees
    every per-user DB path stays directly under ``RSIM_HOME/results``.
    """
    raw = str(user or "").strip()
    if not raw:
        return "default"
    safe = _SAFE_USER.sub("_", raw).strip(" .")
    while ".." in safe:
        safe = safe.replace("..", ".")
    safe = safe.strip(" .")
    if not safe or safe in {".", ".."}:
        return "default"
    return safe


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
    return normalize_user(raw.casefold())


def stable_user_identity(user: str | None) -> str:
    """Return the cross-client no-auth owner format.

    The Web asks for a human-entered NTID while the SDK/Connector can read an
    OS login.  Lower-casing both and adding the ``user-`` namespace gives the
    same owner without exposing machine fingerprints.  Explicit legacy
    ``X-Rsim-User`` labels remain untouched by the HTTP adapters for backward
    compatibility; callers opting into the stable identity should use this
    helper (the SDK default does so automatically).
    """

    value = normalize_user(str(user or "").strip().casefold())
    if value.startswith("user-"):
        return value
    return f"user-{value}"


def connector_owner_identity() -> str:
    """Return the owner a long-running Connector must use on every request.

    ``RSIM_USER`` is written by the server-generated installer and is already
    the control-plane owner.  Treat it as an opaque binding: applying
    ``stable_user_identity`` a second time changes legacy ``web-*``/``sdk-*``
    owners and splits registration from Web capability checks.  Only a bare
    developer launch without an explicit binding derives ``user-<os-login>``.
    """

    configured = os.environ.get("RSIM_USER", "").strip()
    if configured and os.environ.get("RSIM_OWNER_BOUND", "").strip() == "1":
        return normalize_user(configured)
    return stable_user_identity(current_user())


def control_db_path_for_user(user: str | None = None) -> Path:
    """Return the control DB path for a user (follows RSIM_HOME)."""
    from core.control_service import _data_root

    user = normalize_user(user or current_user())
    results_dir = _data_root() / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    name = "_control.db" if user == "default" else f"_control_{user}.db"
    return results_dir / name
