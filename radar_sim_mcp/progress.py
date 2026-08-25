"""Human-readable stderr progress for the local MCP process.

MCP stdout is reserved for JSON-RPC.  This module provides a small, safe
terminal renderer for users who choose to watch the hidden/local MCP terminal.
It never returns progress through the MCP tool envelope and never raises when
stderr is unavailable.
"""

from __future__ import annotations

import sys
from typing import TextIO


def render_progress(
    percent: float | int | None,
    *,
    label: str,
    stage: str = "",
    job_id: str = "",
    width: int = 24,
) -> str:
    try:
        value = max(0.0, min(100.0, float(percent if percent is not None else 0.0)))
    except (TypeError, ValueError):
        value = 0.0
    size = max(10, int(width))
    filled = int(round(size * value / 100.0))
    bar = "█" * filled + "░" * (size - filled)
    suffix = f" | {stage}" if str(stage or "").strip() else ""
    short_job = str(job_id or "").strip()
    if len(short_job) > 18:
        short_job = short_job[:18]
    job_suffix = f" | {short_job}" if short_job else ""
    return f"[radar-sim] {label} [{bar}] {value:5.1f}%{suffix}{job_suffix}"


def emit_progress(
    percent: float | int | None,
    *,
    label: str,
    stage: str = "",
    job_id: str = "",
    stream: TextIO | None = None,
) -> None:
    try:
        output = stream or sys.stderr
        output.write(render_progress(percent, label=label, stage=stage, job_id=job_id) + "\n")
        output.flush()
    except (OSError, ValueError):
        return


__all__ = ["emit_progress", "render_progress"]
