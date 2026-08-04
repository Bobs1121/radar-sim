"""Cross-platform normalization for user-supplied file paths.

The control plane never opens a Windows path.  It only needs a stable spelling
for matching the same path advertised by a Windows Agent.  This module keeps
that concern separate from node-local ``Path.resolve`` calls:

* accepts either slash direction;
* collapses repeated separators and ``.``/``..`` segments for drive/UNC paths;
* preserves URI schemes such as ``shared://`` and ``dataset://``;
* optionally case-folds only when producing an opaque matching token.
"""

from __future__ import annotations

import ntpath
import posixpath
import re


_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:/")


def normalize_path_text(value: object, *, casefold: bool = False) -> str:
    """Return one separator-stable representation of a path-like value."""

    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""

    # Logical references are not filesystem paths.  Do not run ``..``
    # collapsing over a URI because its authority/path semantics are owned by
    # the corresponding store.
    if _WINDOWS_ABS_RE.match(text) or text.startswith("//"):
        if text.startswith("//"):
            rest = re.sub(r"/+", "/", text[2:]).lstrip("/")
            text = "//" + rest
        else:
            text = re.sub(r"/+", "/", text)
        normalized = ntpath.normpath(text.replace("/", "\\")).replace("\\", "/")
    elif "://" in text:
        scheme, rest = text.split("://", 1)
        normalized = f"{scheme}://{re.sub(r'/+', '/', rest)}"
    else:
        normalized = posixpath.normpath(text)

    return normalized.casefold() if casefold else normalized


def path_token(value: object) -> str:
    """Return the canonical case-insensitive value used for opaque IDs."""

    normalized = normalize_path_text(value, casefold=True)
    if normalized in {"/", "//"}:
        return normalized
    return normalized.rstrip("/")


__all__ = ["normalize_path_text", "path_token"]
