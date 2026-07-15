"""Post-simulation run manifest and webhook notification (PRD §1.7.5).

On SUCCESS the system produces a detailed archive manifest (JSON) listing the
absolute UNC/Linux paths of every physical artifact the run produced, and
optionally pushes a notification card to a configured webhook.

The manifest is the single source of truth returned to Web/API consumers:
  - output MF4 absolute path
  - applied Config.cfg snapshot path + the Runtime.xml that was used
  - simulation logs (CRlog.log + missing-signal probe log)
  - analysis report HTML path
  - core KPI check verdict (e.g. "FCTA Check: PASS") when rule results exist

Stdlib-only. Webhook delivery is best-effort: a network failure degrades to a
logged warning and never raises — the simulation already succeeded.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional


def _abs(path: str) -> str:
    """Normalize to an absolute path; '' for falsy."""
    if not path:
        return ""
    return os.path.normpath(os.path.abspath(str(path)))


def build_run_manifest(
    config: dict[str, Any],
    *,
    output_mf4: str = "",
    run_result: Optional[Any] = None,
    analysis_result: Optional[Any] = None,
    status: str = "success",
    error: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    """Build the post-simulation archive manifest (PRD §1.7.5).

    Pulls paths from the layered sim config and the run/analysis result
    objects. Accepts duck-typed result objects (RunResult / AnalysisResult
    shapes) so callers don't have to adapt. Returns a JSON-serializable dict.
    """
    sim = config.get("simulation", {}) or {}
    assets = config.get("assets", {}) or {}
    meta = config.get("_meta", {}) or {}
    project = meta.get("project") or config.get("project", {}).get("name", "") or ""

    # Output MF4: explicit arg first, then run_result, then sim config.
    out_mf4 = output_mf4 or getattr(run_result, "output_mf4", "") or sim.get("output_mf4", "")

    # Config snapshot + Runtime.xml actually applied this run.
    config_cfg = _abs(sim.get("paramconfig_path") or assets.get("fixed_config_path", ""))
    runtime_xml = _abs(sim.get("runtime_xml") or assets.get("runtime_xml", ""))

    # Logs.
    log_file = _abs(sim.get("log_file", ""))
    missing_signals_log = ""
    if log_file:
        # Selena's missing-signal probe sits beside CRlog.log.
        cand = Path(log_file + "_MissingSignals.txt")
        if cand.exists():
            missing_signals_log = str(cand)

    # Analysis report HTML.
    report_html = _abs(getattr(analysis_result, "report_path", "") or "") if analysis_result else ""
    output_dir = _abs(getattr(analysis_result, "output_dir", "") or "") if analysis_result else ""

    # KPI core-check verdict (e.g. "FCTA Check: PASS").
    kpi_verdict = _summarize_kpi(analysis_result)

    success = status == "success" and not error
    manifest = {
        "run_id": run_id or meta.get("_run_id", ""),
        "project": project,
        "status": "success" if success else status,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "artifacts": {
            "output_mf4": _abs(out_mf4),
            "config_snapshot": config_cfg,
            "runtime_xml": runtime_xml,
            "crlog": log_file,
            "missing_signals_log": missing_signals_log,
            "report_html": report_html,
            "analysis_output_dir": output_dir,
        },
        "kpi": kpi_verdict,
        "error": error,
    }
    if getattr(run_result, "duration_sec", None):
        manifest["duration_sec"] = round(float(run_result.duration_sec), 1)
    return manifest


def _summarize_kpi(analysis_result: Optional[Any]) -> dict[str, Any]:
    """Distill rule results into a {name: PASS|FAIL} verdict card."""
    if not analysis_result:
        return {}
    rules = getattr(analysis_result, "rule_results", None) or []
    if not rules:
        return {}
    verdict: dict[str, str] = {}
    for r in rules:
        name = getattr(r, "name", None) or getattr(r, "rule_name", None) or "?"
        passed = bool(getattr(r, "passed", getattr(r, "success", False)))
        verdict[str(name)] = "PASS" if passed else "FAIL"
    all_pass = all(v == "PASS" for v in verdict.values()) if verdict else False
    return {"checks": verdict, "all_pass": all_pass}


def write_manifest(manifest: dict[str, Any], dest_dir: str) -> str:
    """Atomically write the manifest JSON to ``<dest_dir>/run_manifest.json``.

    Returns the absolute path written. Used so the Web/API layer can return a
    stable file URL alongside the in-memory dict.
    """
    out = Path(dest_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "run_manifest.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return str(path)


def notify_webhook(
    manifest: dict[str, Any],
    notifications_cfg: Optional[dict[str, Any]],
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """POST the manifest (as a notification card) to the configured webhook.

    ``notifications_cfg`` is the project's ``notifications`` block, e.g.::
        notifications:
          webhook:
            url: "https://hooks.example.com/services/..."
            on_success: true
            on_failure: true

    Best-effort: returns ``{"ok": bool, "status": int, "error": str}``. Never
    raises — a webhook outage must not break a successful simulation.
    """
    cfg = (notifications_cfg or {}).get("webhook") or {}
    url = str(cfg.get("url") or "").strip()
    if not url:
        return {"ok": False, "status": 0, "error": "no webhook url configured"}

    status = manifest.get("status", "success")
    if status == "success" and not cfg.get("on_success", True):
        return {"ok": False, "status": 0, "error": "on_success disabled"}
    if status != "success" and not cfg.get("on_failure", True):
        return {"ok": False, "status": 0, "error": "on_failure disabled"}

    payload = json.dumps({"event": "simulation_run", "manifest": manifest}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": 200 <= resp.status < 300, "status": resp.status, "error": ""}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": str(exc)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "status": 0, "error": str(exc)}
