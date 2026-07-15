"""Progress parsers for build/sim stdout (PRD §1.7.4).

Selena build scripts print per-file compile progress like
``[45/120] Compiling main.cpp``; the simulation runner prints frame counters
like ``Frame 1200 / 4500`` to ``CRlog.log``. These parsers turn those lines
into ``(done, total, label)`` tuples the build_runner attaches to the task so
the Web UI can render a real progress bar instead of a "frozen" spinner.

Stdlib-only (regex). Returns ``None`` when a line carries no progress signal
so callers can skip it cheaply.
"""

from __future__ import annotations

import re
from typing import Optional

# [45/120] Compiling main.cpp   |   [ 45 / 120 ] Building CXX object ...
# Also tolerates cmake/ninja-style "[ 45%]" percentage tokens (total unknown).
_BUILD_TOKEN_RE = re.compile(
    r"\[\s*(?P<done>\d+)\s*/\s*(?P<total>\d+)\s*\]\s*(?P<label>.+?)\s*$"
)
_BUILD_PCT_RE = re.compile(r"\[\s*(?P<pct>\d+(?:\.\d+)?)\s*%\]")

# Frame 1200 / 4500   |   Frame:1200/4500   |   frame 1200 of 4500
_FRAME_RE = re.compile(
    r"frame[:\s]*(?P<done>\d+)\s*(?:/|of)\s*(?P<total>\d+)",
    re.IGNORECASE,
)


def parse_build_progress(line: str) -> Optional[tuple[int, int, str]]:
    """Parse a build stdout line into ``(done, total, label)``.

    Matches ``[done/total] <label>`` (cmake/ninja file counters). Returns
    ``None`` if the line has no recognizable progress token.
    """
    if not line:
        return None
    m = _BUILD_TOKEN_RE.search(line)
    if m:
        done = int(m.group("done"))
        total = int(m.group("total"))
        if total <= 0 or done > total:
            return None
        label = m.group("label").strip()
        return done, total, label
    return None


def parse_build_percentage(line: str) -> Optional[float]:
    """Parse a ``[NN%]`` percentage token. Returns the percent or ``None``.

    Used when only a percentage is emitted (no file count) so the UI still
    gets a monotonic progress signal.
    """
    if not line:
        return None
    m = _BUILD_PCT_RE.search(line)
    if not m:
        return None
    return float(m.group("pct"))


def parse_sim_progress(line: str) -> Optional[tuple[int, int]]:
    """Parse a simulation log line into ``(frame_done, frame_total)``.

    Matches ``Frame <done> / <total>`` (CRlog.log frame counters). Returns
    ``None`` when the line carries no frame progress.
    """
    if not line:
        return None
    m = _FRAME_RE.search(line)
    if not m:
        return None
    done = int(m.group("done"))
    total = int(m.group("total"))
    if total <= 0 or done > total:
        return None
    return done, total


def build_progress_pct(done: Optional[int], total: Optional[int], pct: Optional[float]) -> Optional[float]:
    """Coalesce file-count and percentage signals into a 0..100 progress value."""
    if done is not None and total and total > 0:
        return min(100.0, round(done / total * 100.0, 1))
    if pct is not None:
        return min(100.0, pct)
    return None
