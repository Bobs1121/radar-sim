"""
PlatformBackend abstract interface.

v4: Build + extract + parse. No auto simulation (user runs in VS).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from core.models import (
        BuildOptions,
        BuildResult,
        LogSummary,
        SignalData,
    )


class PlatformBackend(ABC):
    """Abstract interface for radar simulation platform backends."""

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Unique platform identifier (e.g. 'gen5_selena')."""
        ...

    @abstractmethod
    def check_environment(self) -> list[str]:
        """Return list of issues (empty = all OK)."""
        ...

    @abstractmethod
    def build(self, options: BuildOptions) -> BuildResult:
        """Compile the radar codebase."""
        ...

    @abstractmethod
    def extract_signals(
        self,
        output_file: str,
        signal_names: list[str],
    ) -> dict[str, SignalData]:
        """Extract signal time series from output data file."""
        ...

    @abstractmethod
    def parse_log(self, log_file: str) -> LogSummary:
        """Parse simulation log file. Returns LogSummary."""
        ...
