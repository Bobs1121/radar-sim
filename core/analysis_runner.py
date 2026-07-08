"""
Analysis plugin interface and runner.

Plugins are loaded dynamically, executed against MF4 data, and produce results.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from core.models import (
    AnalysisContext,
    AnalysisResult,
    PluginResult,
    RuleResult,
    SignalData,
)

logger = logging.getLogger(__name__)


# ============================================================
# Plugin interface
# ============================================================

class AnalysisPlugin(ABC):
    """Base class for all analysis plugins."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Plugin name (e.g., 'signal_summary', 'rule_check')."""

    @abstractmethod
    def analyze(self, signals: dict[str, SignalData], context: AnalysisContext) -> PluginResult:
        """Execute analysis on extracted signal data."""

    def ask(self, question: str, signals: dict[str, SignalData], context: AnalysisContext) -> str:
        """Optional: answer user questions about the analysis.
        Default returns a message that this plugin doesn't support QA."""
        return f"Plugin '{self.name}' does not support Q&A."


# ============================================================
# Plugin loader
# ============================================================

def discover_plugins() -> dict[str, type[AnalysisPlugin]]:
    """Discover all analysis plugins in plugins/analysis/."""
    plugins_dir = Path(__file__).resolve().parent.parent / "plugins" / "analysis"
    if not plugins_dir.exists():
        return {}

    registry = {}
    for py_file in plugins_dir.glob("*.py"):
        if py_file.name.startswith("_"):
            continue

        module_name = f"plugins.analysis.{py_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                # Find all classes that subclass AnalysisPlugin
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (isinstance(attr, type) and
                        issubclass(attr, AnalysisPlugin) and
                        attr is not AnalysisPlugin):
                        instance = attr()
                        plugin_name = instance.name
                        registry[plugin_name] = attr
        except Exception as e:
            logger.warning(f"Failed to load plugin from {py_file}: {e}")

    return registry


def load_plugins(names: Optional[list[str]] = None) -> list[AnalysisPlugin]:
    """Load analysis plugins by name. If names is None, use defaults."""
    registry = discover_plugins()

    if names is None:
        # Use default plugins from global config
        from core.config import load_global_defaults
        global_cfg = load_global_defaults()
        default_plugins = global_cfg.get("analysis", {}).get("default_plugins", [
            "signal_summary", "rule_check", "default_report",
        ])
        names = default_plugins

    loaded = []
    for name in names:
        if name in registry:
            loaded.append(registry[name]())
            logger.info(f"Loaded plugin: {name}")
        else:
            logger.warning(f"Plugin '{name}' not found. Available: {list(registry.keys())}")

    return loaded


# ============================================================
# Analysis runner
# ============================================================

class AnalysisRunner:
    """Execute analysis plugins on an MF4 file."""

    def __init__(self, project: str, config: dict):
        self.project = project
        self.config = config

    def run(
        self,
        mf4_path: str,
        plugins: Optional[list[str]] = None,
        user_context: Optional[str] = None,
        log_path: Optional[str] = None,
    ) -> AnalysisResult:
        """Run analysis pipeline on an MF4 file.

        Args:
            mf4_path: Path to the MF4 output file.
            plugins: List of plugin names to run. None = use defaults.
            user_context: Optional user context (e.g., code changes description).
            log_path: Optional simulation log file path.

        Returns:
            AnalysisResult with all plugin results.
        """
        from core.config import get_results_dir, load_signals, load_rules

        timestamp = datetime.now()
        ts_str = timestamp.strftime("%Y%m%d_%H%M%S")

        # Create output directory
        output_dir = str(get_results_dir(self.project, ts_str))

        # Load project config
        signals_cfg = load_signals(self.project)
        rules_cfg = load_rules(self.project)

        # Build context
        context = AnalysisContext(
            mf4_path=mf4_path,
            project=self.project,
            platform=self.config.get("project", {}).get("platform", "gen5_selena"),
            timestamp=timestamp,
            signals_config=signals_cfg,
            rules_config=rules_cfg,
            log_path=log_path,
            user_context=user_context,
            output_dir=output_dir,
        )

        # Step 1: Extract signals from MF4
        print(f"[1/3] Extracting signals from {mf4_path}...")
        signals = self._extract_signals(mf4_path, signals_cfg)
        print(f"       Extracted {len(signals)} signals")

        # Step 2: Run plugins
        print(f"[2/3] Running analysis plugins...")
        plugin_instances = load_plugins(plugins)
        plugin_results = []
        for plugin in plugin_instances:
            try:
                result = plugin.analyze(signals, context)
                plugin_results.append(result)
                status = "OK" if result.success else "FAIL"
                print(f"       [{status}] {plugin.name}: {result.summary[:80]}")
            except Exception as e:
                logger.error(f"Plugin {plugin.name} failed: {e}")
                plugin_results.append(PluginResult(
                    plugin_name=plugin.name, success=False,
                    errors=[str(e)],
                ))

        # Step 3: Save results
        print(f"[3/3] Saving results to {output_dir}...")
        result = self._save_results(signals, plugin_results, context, ts_str)

        # Print summary
        print()
        success_count = sum(1 for r in plugin_results if r.success)
        print(f"  Analysis complete: {success_count}/{len(plugin_results)} plugins succeeded")
        if result.report_path:
            print(f"  Report: {result.report_path}")
        print(f"  Results: {output_dir}")

        return result

    def _extract_signals(self, mf4_path: str, signals_cfg: list[dict]) -> dict[str, SignalData]:
        """Extract signals from MF4 using platform backend."""
        from platforms import get as get_platform

        platform = get_platform(
            self.config.get("project", {}).get("platform", "gen5_selena"),
            self.config,
        )

        signal_names = [s["name"] for s in signals_cfg]
        if not signal_names:
            signal_names = []  # Extract all available

        return platform.extract_signals(mf4_path, signal_names)

    def _save_results(
        self,
        signals: dict[str, SignalData],
        plugin_results: list[PluginResult],
        context: AnalysisContext,
        ts_str: str,
    ) -> AnalysisResult:
        """Save analysis results to disk."""
        from datetime import datetime

        # Save signals as JSON
        signals_data = {}
        for name, sig in signals.items():
            signals_data[name] = {
                "name": sig.name,
                "unit": sig.unit,
                "num_points": len(sig.values),
                "min": min(sig.values) if sig.values else None,
                "max": max(sig.values) if sig.values else None,
                "mean": sum(sig.values) / len(sig.values) if sig.values else None,
                "last_value": sig.values[-1] if sig.values else None,
                "summary": sig.summary,
            }

        signals_json = os.path.join(context.output_dir, "signals.json")
        with open(signals_json, "w", encoding="utf-8") as f:
            yaml.dump(signals_data, f, default_flow_style=False, allow_unicode=True)

        # Save analysis results
        analysis_data = {
            "id": f"{self.project}_{ts_str}",
            "timestamp": ts_str,
            "project": self.project,
            "mf4_path": context.mf4_path,
            "plugins": [],
        }

        for pr in plugin_results:
            analysis_data["plugins"].append({
                "name": pr.plugin_name,
                "success": pr.success,
                "summary": pr.summary,
                "data": pr.data,
                "errors": pr.errors,
            })

            # Collect rule results from rule_check plugin
            # (will be merged into AnalysisResult)

        analysis_json = os.path.join(context.output_dir, "analysis.json")
        with open(analysis_json, "w", encoding="utf-8") as f:
            yaml.dump(analysis_data, f, default_flow_style=False, allow_unicode=True)

        # Find report path from default_report plugin
        report_path = None
        for pr in plugin_results:
            if pr.plugin_name == "default_report" and pr.data.get("report_path"):
                report_path = pr.data["report_path"]
                break

        # Collect rule results
        rule_results = []
        for pr in plugin_results:
            if pr.plugin_name == "rule_check":
                for item in pr.data.get("rules", []):
                    rule_results.append(RuleResult(**item))

        # Build analysis result
        result = AnalysisResult(
            id=f"{self.project}_{ts_str}",
            timestamp=datetime.now(),
            project=self.project,
            mf4_path=context.mf4_path,
            signals=signals,
            rule_results=rule_results,
            plugin_results=plugin_results,
            output_dir=context.output_dir,
            report_path=report_path,
            success=all(r.success for r in plugin_results),
        )

        return result
