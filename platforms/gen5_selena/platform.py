"""
Gen5Platform — unified PlatformBackend implementation for gen5_selena.

v4: Build + MF4 extraction + log parsing. No simulation engine.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from core.platform import PlatformBackend
from core.models import BuildOptions, BuildResult, LogSummary, SignalData

from .builder import Gen5Builder
from .mf4_reader import Gen5Mf4Reader
from .log_parser import Gen5LogParser


class Gen5Platform(PlatformBackend):
    """Gen5 Selena platform backend — build + extract + parse."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.builder = Gen5Builder(config)
        self.mf4_reader = Gen5Mf4Reader(config)
        self.log_parser = Gen5LogParser(config)

    @property
    def platform_name(self) -> str:
        return "gen5_selena"

    def check_environment(self) -> list[str]:
        """Check all environment prerequisites."""
        issues = self.builder.check_environment()
        return issues

    def build(self, options: BuildOptions) -> BuildResult:
        return self.builder.build(options)

    def extract_signals(
        self,
        output_file: str,
        signal_names: list[str],
    ) -> dict[str, SignalData]:
        return self.mf4_reader.extract(output_file, signal_names)

    def parse_log(self, log_file: str) -> LogSummary:
        return self.log_parser.parse(log_file)

    def open_vs(self, config: dict) -> bool:
        """Open VS solution (stub — handled by cli/open_vs.py)."""
        sln = config.get("compile", {}).get("vs_sln", "")
        build_output = config.get("paths", {}).get("build_output", "")
        if not sln:
            sln = os.path.join(build_output, "selena.sln")
        return os.path.exists(sln)


# Auto-register
from platforms import register
register(Gen5Platform)
