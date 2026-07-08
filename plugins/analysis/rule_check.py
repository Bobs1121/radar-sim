"""
Rule check plugin — evaluate configured rules against signal data.
"""

from __future__ import annotations

from core.analysis_runner import AnalysisPlugin
from core.models import AnalysisContext, PluginResult, RuleResult, SignalData


class RuleCheckPlugin(AnalysisPlugin):
    """Evaluate rules from rules.yaml against signal data."""

    @property
    def name(self) -> str:
        return "rule_check"

    def analyze(self, signals: dict[str, SignalData], context: AnalysisContext) -> PluginResult:
        rules = context.rules_config
        results = []

        for rule in rules:
            name = rule.get("name", "unnamed")
            severity = rule.get("severity", "P1")
            description = rule.get("description", "")
            source = rule.get("source", "signal")

            if source == "signal":
                result = self._check_signal_rule(name, rule, signals, severity, description)
            elif source == "log":
                result = self._check_log_rule(name, rule, context, severity, description)
            elif source == "file":
                result = self._check_file_rule(name, rule, context, severity, description)
            else:
                result = RuleResult(
                    name=name, status="skip", severity=severity,
                    message=f"Unknown source type: {source}",
                )

            results.append(result)

        passed = sum(1 for r in results if r.status == "pass")
        failed = sum(1 for r in results if r.status == "fail")
        skipped = sum(1 for r in results if r.status == "skip")

        return PluginResult(
            plugin_name=self.name,
            success=True,
            data={
                "rules": [
                    {"name": r.name, "status": r.status, "severity": r.severity,
                     "message": r.message, "details": r.details}
                    for r in results
                ]
            },
            summary=f"Rules: {passed} passed, {failed} failed, {skipped} skipped",
        )

    def _check_signal_rule(self, name, rule, signals, severity, description) -> RuleResult:
        """Check a signal-based rule."""
        signal_name = rule.get("signal", "")
        condition = rule.get("condition", "")

        if signal_name not in signals:
            return RuleResult(
                name=name, status="skip", severity=severity,
                message=f"Signal '{signal_name}' not found in MF4",
                details=description,
            )

        sig = signals[signal_name]
        values = sig.values
        if not values:
            return RuleResult(
                name=name, status="skip", severity=severity,
                message=f"Signal '{signal_name}' has no data",
                details=description,
            )

        # Parse condition
        if condition.startswith("reaches value"):
            target = float(condition.split()[-1])
            reached = any(v == target for v in values)
            if reached:
                idx = values.index(target)
                t = sig.timestamps[idx] if idx < len(sig.timestamps) else "?"
                return RuleResult(
                    name=name, status="pass", severity=severity,
                    message=f"{signal_name} reached {target} at t={t}s",
                    details=description,
                )
            return RuleResult(
                name=name, status="fail", severity=severity,
                message=f"{signal_name} did NOT reach {target} (range: {min(values)}-{max(values)})",
                details=description,
            )

        elif condition.startswith("always"):
            if " > " in condition:
                threshold = float(condition.split(">")[-1].strip())
                all_above = all(v > threshold for v in values)
            elif " < " in condition:
                threshold = float(condition.split("<")[-1].strip())
                all_above = all(v < threshold for v in values)
            else:
                all_above = True

            if all_above:
                return RuleResult(
                    name=name, status="pass", severity=severity,
                    message=f"{signal_name} condition met",
                    details=description,
                )
            return RuleResult(
                name=name, status="fail", severity=severity,
                message=f"{signal_name} condition violated",
                details=description,
            )

        else:
            return RuleResult(
                name=name, status="skip", severity=severity,
                message=f"Unknown condition format: {condition}",
                details=description,
            )

    def _check_log_rule(self, name, rule, context, severity, description) -> RuleResult:
        """Check a log-based rule."""
        if not context.log_path:
            return RuleResult(
                name=name, status="skip", severity=severity,
                message="No log path provided",
                details=description,
            )

        condition = rule.get("condition", "")
        if "no [ERROR]" in condition or "no ERROR" in condition:
            try:
                with open(context.log_path, encoding="utf-8") as f:
                    content = f.read()
                if "[ERROR]" in content:
                    return RuleResult(
                        name=name, status="fail", severity=severity,
                        message="Errors found in simulation log",
                        details=description,
                    )
                return RuleResult(
                    name=name, status="pass", severity=severity,
                    message="No errors in simulation log",
                    details=description,
                )
            except OSError:
                return RuleResult(
                    name=name, status="skip", severity=severity,
                    message=f"Cannot read log: {context.log_path}",
                    details=description,
                )

        return RuleResult(
            name=name, status="skip", severity=severity,
            message=f"Unknown log condition: {condition}",
            details=description,
        )

    def _check_file_rule(self, name, rule, context, severity, description) -> RuleResult:
        """Check a file-based rule."""
        condition = rule.get("condition", "")
        if "output_mf4 exists" in condition or "mf4 exists" in condition:
            if not context.mf4_path or not __import__("os").path.exists(context.mf4_path):
                return RuleResult(
                    name=name, status="fail", severity=severity,
                    message=f"MF4 file not found: {context.mf4_path}",
                    details=description,
                )
            size = __import__("os").path.getsize(context.mf4_path)
            if size == 0:
                return RuleResult(
                    name=name, status="fail", severity=severity,
                    message="MF4 file exists but is empty",
                    details=description,
                )
            return RuleResult(
                name=name, status="pass", severity=severity,
                message=f"MF4 file exists ({size:,} bytes)",
                details=description,
            )

        return RuleResult(
            name=name, status="skip", severity=severity,
            message=f"Unknown file condition: {condition}",
            details=description,
        )
