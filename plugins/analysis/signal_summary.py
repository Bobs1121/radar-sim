"""
Signal summary plugin — extract statistics for each signal.
"""

from __future__ import annotations

import statistics
from core.analysis_runner import AnalysisPlugin
from core.models import AnalysisContext, PluginResult, SignalData


class SignalSummaryPlugin(AnalysisPlugin):
    """Compute summary statistics for each signal."""

    @property
    def name(self) -> str:
        return "signal_summary"

    def analyze(self, signals: dict[str, SignalData], context: AnalysisContext) -> PluginResult:
        summary = {}
        for name, sig in signals.items():
            if not sig.values:
                summary[name] = {"error": "No data"}
                continue

            stats = {
                "min": min(sig.values),
                "max": max(sig.values),
                "mean": statistics.mean(sig.values),
                "count": len(sig.values),
                "first": sig.values[0],
                "last": sig.values[-1],
                "unit": sig.unit,
            }

            if len(sig.timestamps) > 1:
                stats["duration"] = sig.timestamps[-1] - sig.timestamps[0]

            # Detect transitions (value changes)
            transitions = sum(1 for i in range(1, len(sig.values)) if sig.values[i] != sig.values[i-1])
            stats["transitions"] = transitions

            # Detect peak
            peak_idx = sig.values.index(max(sig.values))
            stats["peak_time"] = sig.timestamps[peak_idx] if sig.timestamps else None

            summary[name] = stats
            sig.summary = stats

        total = len(signals)
        return PluginResult(
            plugin_name=self.name,
            success=True,
            data=summary,
            summary=f"Analyzed {total} signals",
        )
