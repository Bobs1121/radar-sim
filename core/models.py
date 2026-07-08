"""
Data models for radar-sim v4.

Covers: build, analysis plugins, signal data, diff results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ============================================================
# Build
# ============================================================

@dataclass
class BuildOptions:
    """Options for build."""
    build_type: str = "selena"  # "hex" | "selena" | "all"
    build_config: str = ""
    build_mode: str = "RelWithDebInfo"
    clean: bool = False
    vs_version: Optional[str] = None


@dataclass
class BuildResult:
    """Result of a compilation step."""
    success: bool
    build_type: str = ""
    executable_path: Optional[str] = None
    log_path: Optional[str] = None
    duration_sec: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    interrupted: bool = False

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {
            "success": self.success,
            "build_type": self.build_type,
            "duration_sec": self.duration_sec,
            "errors": self.errors,
            "warnings": self.warnings,
            "interrupted": self.interrupted,
        }


# ============================================================
# Signal Data
# ============================================================

@dataclass
class SignalData:
    """Time series data for a single signal."""
    name: str
    timestamps: list[float]
    values: list[float]
    unit: str = ""
    source_mf4: str = ""
    summary: dict = field(default_factory=dict)


# ============================================================
# Log Parsing
# ============================================================

@dataclass
class LogEntry:
    """Single parsed log entry."""
    timestamp: str
    level: str
    message: str
    source: str = ""


@dataclass
class LogSummary:
    """Parsed summary of a simulation log file."""
    version: str = ""
    runnables_loaded: int = 0
    connections: int = 0
    errors: list[LogEntry] = field(default_factory=list)
    warnings: list[LogEntry] = field(default_factory=list)
    duration_sec: float = 0.0
    raw_path: str = ""


# ============================================================
# Rule Check
# ============================================================

@dataclass
class RuleResult:
    """Result of a single rule check."""
    name: str
    status: str  # pass / fail / warn / skip
    severity: str  # P0 / P1 / P2
    message: str
    details: str = ""


# ============================================================
# Analysis Plugin
# ============================================================

@dataclass
class AnalysisContext:
    """Context passed to analysis plugins."""
    mf4_path: str
    project: str
    platform: str
    timestamp: datetime
    signals_config: list[dict]
    rules_config: list[dict]
    log_path: Optional[str] = None
    user_context: Optional[str] = None
    output_dir: str = ""


@dataclass
class PluginResult:
    """Result from one analysis plugin."""
    plugin_name: str
    success: bool
    data: dict = field(default_factory=dict)
    summary: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """Full result of analyzing an MF4 file."""
    id: str
    timestamp: datetime
    project: str
    mf4_path: str
    signals: dict[str, SignalData] = field(default_factory=dict)
    log_summary: Optional[LogSummary] = None
    rule_results: list[RuleResult] = field(default_factory=list)
    plugin_results: list[PluginResult] = field(default_factory=list)
    output_dir: str = ""
    report_path: Optional[str] = None
    success: bool = False
    error: Optional[str] = None


# ============================================================
# Diff / Comparison
# ============================================================

@dataclass
class DiffSignal:
    """Comparison of one signal between two results."""
    signal: str
    base_value: Optional[float]
    current_value: Optional[float]
    change_pct: Optional[float]
    interpretation: str = ""


@dataclass
class DiffResult:
    """Full diff between two analysis results."""
    base_dir: str
    current_dir: str
    signals: list[DiffSignal] = field(default_factory=list)
    summary: str = ""
