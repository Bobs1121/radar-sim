"""
Default report plugin — generate HTML report from analysis results.
"""

from __future__ import annotations

import os
from datetime import datetime
from core.analysis_runner import AnalysisPlugin
from core.models import AnalysisContext, PluginResult, SignalData


class DefaultReportPlugin(AnalysisPlugin):
    """Generate HTML report combining all analysis results."""

    @property
    def name(self) -> str:
        return "default_report"

    def analyze(self, signals: dict[str, SignalData], context: AnalysisContext) -> PluginResult:
        output_dir = context.output_dir or "."
        report_path = os.path.join(output_dir, "report.html")

        html = self._generate_html(signals, context)

        os.makedirs(output_dir, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html)

        return PluginResult(
            plugin_name=self.name,
            success=True,
            data={"report_path": report_path},
            summary=f"Report saved to {report_path}",
        )

    def _generate_html(self, signals: dict[str, SignalData], context: AnalysisContext) -> str:
        """Generate HTML report."""
        ts = context.timestamp.strftime("%Y-%m-%d %H:%M:%S")

        # Build signal table rows
        signal_rows = ""
        for name, sig in signals.items():
            if not sig.values:
                continue
            s = sig.summary or {}
            signal_rows += f"""\
            <tr>
                <td>{name}</td>
                <td>{s.get('min', '')}</td>
                <td>{s.get('max', '')}</td>
                <td>{s.get('mean', '')}</td>
                <td>{s.get('transitions', 0)}</td>
                <td>{sig.unit}</td>
            </tr>\n"""

        html = f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>radar-sim Analysis Report</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; margin: 2rem; background: #f8f9fa; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        h1 {{ color: #212529; border-bottom: 2px solid #0d6efd; padding-bottom: 0.5rem; }}
        h2 {{ color: #495057; margin-top: 2rem; }}
        .meta {{ color: #6c757d; margin-bottom: 1rem; }}
        table {{ width: 100%; border-collapse: collapse; background: white; box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
        th {{ background: #0d6efd; color: white; padding: 0.5rem; text-align: left; }}
        td {{ padding: 0.5rem; border-bottom: 1px solid #dee2e6; }}
        tr:hover {{ background: #f1f3f5; }}
        .status-pass {{ color: #198754; font-weight: bold; }}
        .status-fail {{ color: #dc3545; font-weight: bold; }}
        .status-skip {{ color: #ffc107; }}
        .card {{ background: white; padding: 1rem; margin: 1rem 0; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
    </style>
</head>
<body>
    <div class="container">
        <h1>radar-sim Analysis Report</h1>
        <div class="meta">
            <p><strong>Project:</strong> {context.project}</p>
            <p><strong>MF4:</strong> {context.mf4_path}</p>
            <p><strong>Time:</strong> {ts}</p>
            <p><strong>Signals:</strong> {len(signals)}</p>
        </div>

        <h2>Signal Summary</h2>
        <table>
            <thead>
                <tr>
                    <th>Signal</th>
                    <th>Min</th>
                    <th>Max</th>
                    <th>Mean</th>
                    <th>Transitions</th>
                    <th>Unit</th>
                </tr>
            </thead>
            <tbody>
                {signal_rows}
            </tbody>
        </table>

        <h2>Conclusion</h2>
        <div class="card">
            <p>Extracted <strong>{len(signals)}</strong> signals from {context.mf4_path}.</p>
            <p>Report generated at {ts}.</p>
            <p>Open in browser for detailed analysis, or use <code>rsim ask</code> for Q&A.</p>
        </div>
    </div>
</body>
</html>
"""
        return html
