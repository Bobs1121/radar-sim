"""
Gen5 Selena log parser.

Parses CRlog.log format:
    [HH:MM:SS.mmm] (thread PID) [level]: message

Extracts version info, runnable count, connection count, errors, warnings.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from core.models import LogEntry, LogSummary

if TYPE_CHECKING:
    pass


# Log line: [15:32:20.727] (thread 12345) [error]: message
LOG_PATTERN = re.compile(
    r"\[(?P<timestamp>\d{2}:\d{2}:\d{2}\.\d{3})\]\s*"
    r"\(thread\s+\d+\)\s*"
    r"\[(?P<level>\w+)\]:\s*(?P<message>.*)"
)

# Version: Selena 1.18.0 Roberta
VERSION_PATTERN = re.compile(
    r"[Ss]elena\s+(?P<version>[\d.]+)\s*(?P<codename>\w+)?",
)

# Runnable loading: Loading runnable: Xxx / loaded runnable: Xxx
RUNNABLE_PATTERN = re.compile(
    r"(?:Loading|loaded)\s+runnable[:\s]+(?P<name>\w+)", re.IGNORECASE,
)

# Connection count: N connections established / N connections
CONNECTION_PATTERN = re.compile(
    r"(?P<count>\d+)\s+connection", re.IGNORECASE,
)

# Config errors: config errors: N
CONFIG_ERRORS_PATTERN = re.compile(
    r"config errors:\s*(?P<count>\d+)", re.IGNORECASE,
)


class Gen5LogParser:
    """Parses Selena simulation log files into structured summaries."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def parse(self, log_file: str) -> LogSummary:
        """Parse log file and return structured summary.

        Args:
            log_file: Path to .log file (e.g. CRlog.log).

        Returns:
            LogSummary with extracted version, errors, warnings, etc.
        """
        errors: list[LogEntry] = []
        warnings: list[LogEntry] = []
        runnables: set[str] = set()
        connection_count = 0
        version = ""
        duration = 0.0

        first_ts: float | None = None
        last_ts: float | None = None

        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    # Parse structured log lines
                    m = LOG_PATTERN.match(line.strip())
                    if m:
                        ts_str = m.group("timestamp")
                        level = m.group("level").lower()
                        message = m.group("message")

                        # Track duration
                        ts_seconds = self._ts_to_seconds(ts_str)
                        if ts_seconds is not None:
                            if first_ts is None:
                                first_ts = ts_seconds
                            last_ts = ts_seconds

                        # Categorize by level
                        if level in ("error", "fatal", "critical"):
                            errors.append(LogEntry(
                                timestamp=ts_str,
                                level=level.upper(),
                                message=message,
                            ))
                        elif level in ("warning", "warn"):
                            warnings.append(LogEntry(
                                timestamp=ts_str,
                                level="WARNING",
                                message=message,
                            ))
                    else:
                        # Check for patterns in non-structured lines
                        pass

                    # Version extraction
                    vm = VERSION_PATTERN.search(line)
                    if vm and not version:
                        version = vm.group("version")
                        codename = vm.group("codename")
                        if codename:
                            version = f"{version} {codename}"

                    # Runnable extraction
                    rm = RUNNABLE_PATTERN.search(line)
                    if rm:
                        runnables.add(rm.group("name"))

                    # Connection count
                    cm = CONNECTION_PATTERN.search(line)
                    if cm and int(cm.group("count")) > connection_count:
                        connection_count = int(cm.group("count"))

        except FileNotFoundError:
            return LogSummary(
                version="",
                runnables_loaded=0,
                connections=0,
                errors=[LogEntry(
                    timestamp="",
                    level="ERROR",
                    message=f"Log file not found: {log_file}",
                )],
                raw_path=log_file,
            )

        if first_ts is not None and last_ts is not None:
            duration = last_ts - first_ts

        return LogSummary(
            version=version,
            runnables_loaded=len(runnables),
            connections=connection_count,
            errors=errors,
            warnings=warnings,
            duration_sec=duration,
            raw_path=log_file,
        )

    @staticmethod
    def _ts_to_seconds(ts: str) -> float | None:
        """Convert HH:MM:SS.mmm timestamp to total seconds."""
        try:
            parts = ts.split(":")
            hours = int(parts[0])
            minutes = int(parts[1])
            sec_parts = parts[2].split(".")
            seconds = int(sec_parts[0])
            millis = int(sec_parts[1]) if len(sec_parts) > 1 else 0
            return hours * 3600 + minutes * 60 + seconds + millis / 1000
        except (ValueError, IndexError):
            return None
