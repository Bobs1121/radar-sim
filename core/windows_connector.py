"""Windows connector distribution helpers.

The public installer never asks a user for control-plane vocabulary.  The
Linux service binds the downloaded script to its own public URL and the script
then downloads the matching, versioned application bundle from that service.
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


class WindowsConnectorError(ValueError):
    """The connector cannot be safely generated or distributed."""


_SAFE_HOST = re.compile(r"^[A-Za-z0-9.:-]+$")


def public_server_url(request_base_url: str) -> str:
    """Return a validated public control-plane URL.

    ``RSIM_PUBLIC_URL`` is useful behind a reverse proxy.  Otherwise the URL
    the browser used is the correct zero-configuration choice (including the
    Linux server IP and non-default port).
    """

    candidate = os.environ.get("RSIM_PUBLIC_URL", "").strip() or str(request_base_url).strip()
    candidate = candidate.rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WindowsConnectorError("public server URL must use http or https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WindowsConnectorError("public server URL contains unsupported fields")
    if not _SAFE_HOST.fullmatch(parsed.netloc):
        raise WindowsConnectorError("public server URL host is invalid")
    return candidate


def render_installer(*, template: Path, server_url: str, mode: str) -> str:
    if mode not in {"light", "full"}:
        raise WindowsConnectorError("connector mode must be light or full")
    try:
        source = template.read_text(encoding="utf-8")
    except OSError as exc:
        raise WindowsConnectorError("Windows connector installer template is unavailable") from exc
    encoded_url = base64.b64encode(server_url.encode("utf-8")).decode("ascii")
    rendered = source.replace("__RSIM_SERVER_URL_BASE64__", encoded_url).replace(
        "__RSIM_WINDOWS_MODE__", mode,
    )
    # Windows PowerShell 5.1 requires a BOM to reliably parse non-ASCII user
    # guidance in a downloaded UTF-8 script.
    return rendered if rendered.startswith("\ufeff") else "\ufeff" + rendered


def render_launcher(*, template: Path, server_url: str, mode: str) -> str:
    if mode not in {"light", "full"}:
        raise WindowsConnectorError("connector mode must be light or full")
    try:
        source = template.read_text(encoding="utf-8")
    except OSError as exc:
        raise WindowsConnectorError("Windows connector launcher template is unavailable") from exc
    encoded_url = base64.b64encode(server_url.encode("utf-8")).decode("ascii")
    return source.replace("__RSIM_SERVER_URL_BASE64__", encoded_url).replace(
        "__RSIM_WINDOWS_MODE__", mode,
    )


__all__ = [
    "WindowsConnectorError",
    "public_server_url",
    "render_installer",
    "render_launcher",
]
