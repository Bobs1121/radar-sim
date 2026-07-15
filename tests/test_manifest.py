"""Tests for the post-simulation manifest + webhook (PRD §1.7.5)."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import manifest as manifest_mod
from core.manifest import build_run_manifest, notify_webhook, write_manifest


# ---------------------------------------------------------------------------
# Duck-typed result objects (avoid importing full RunResult/AnalysisResult)
# ---------------------------------------------------------------------------

def _run_result(*, success=True, output_mf4="", duration_sec=12.3):
    return SimpleNamespace(success=success, output_mf4=output_mf4, duration_sec=duration_sec)


def _analysis_result(*, report_path="", output_dir="", rules=None):
    return SimpleNamespace(report_path=report_path, output_dir=output_dir, rule_results=rules or [])


def _rule(name, passed):
    return SimpleNamespace(name=name, passed=passed)


def _config(tmp_path, *, log_file=None, runtime_xml=None, paramconfig=None):
    return {
        "_meta": {"project": "bydod25", "_run_id": "run_123"},
        "project": {"name": "bydod25"},
        "simulation": {
            "output_mf4": str(tmp_path / "out.MF4"),
            "log_file": str(log_file or (tmp_path / "CRlog.log")),
            "runtime_xml": str(runtime_xml or (tmp_path / "runtime.xml")),
            "paramconfig_path": str(paramconfig or (tmp_path / "Config.cfg")),
        },
        "assets": {},
    }


# ---------------------------------------------------------------------------
# build_run_manifest
# ---------------------------------------------------------------------------

def test_manifest_captures_all_artifact_paths(tmp_path):
    cfg = _config(tmp_path)
    # Create the missing-signal probe log so it's picked up.
    probe = Path(cfg["simulation"]["log_file"] + "_MissingSignals.txt")
    probe.write_text("none", encoding="utf-8")

    m = build_run_manifest(
        cfg,
        run_result=_run_result(output_mf4=str(tmp_path / "out.MF4")),
        analysis_result=_analysis_result(
            report_path=str(tmp_path / "report.html"),
            output_dir=str(tmp_path),
            rules=[_rule("FCTA Check", True), _rule("no_error", True)],
        ),
    )
    art = m["artifacts"]
    assert art["output_mf4"].endswith("out.MF4")
    assert art["config_snapshot"].endswith("Config.cfg")
    assert art["runtime_xml"].endswith("runtime.xml")
    assert art["crlog"].endswith("CRlog.log")
    assert art["missing_signals_log"].endswith("CRlog.log_MissingSignals.txt")
    assert art["report_html"].endswith("report.html")
    assert m["status"] == "success"
    assert m["project"] == "bydod25"
    assert m["run_id"] == "run_123"
    assert m["kpi"]["all_pass"] is True
    assert m["kpi"]["checks"]["FCTA Check"] == "PASS"


def test_manifest_kpi_reports_fail_when_any_rule_fails(tmp_path):
    m = build_run_manifest(
        _config(tmp_path),
        analysis_result=_analysis_result(rules=[_rule("FCTA Check", False)]),
    )
    assert m["kpi"]["all_pass"] is False
    assert m["kpi"]["checks"]["FCTA Check"] == "FAIL"


def test_manifest_reflects_failure_status(tmp_path):
    m = build_run_manifest(
        _config(tmp_path), status="failed", error="selena.exe crashed",
    )
    assert m["status"] == "failed"
    assert m["error"] == "selena.exe crashed"


def test_manifest_omits_missing_signals_log_when_absent(tmp_path):
    m = build_run_manifest(_config(tmp_path))
    assert m["artifacts"]["missing_signals_log"] == ""


def test_manifest_serializable_json(tmp_path):
    m = build_run_manifest(_config(tmp_path))
    # Must round-trip through json (returned to Web/API consumers).
    json.dumps(m)


# ---------------------------------------------------------------------------
# write_manifest
# ---------------------------------------------------------------------------

def test_write_manifest_atomic(tmp_path):
    m = build_run_manifest(_config(tmp_path))
    path = write_manifest(m, str(tmp_path / "results"))
    assert Path(path).name == "run_manifest.json"
    assert Path(path).exists()
    assert not (Path(path).parent / "run_manifest.json.tmp").exists()
    loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    assert loaded["run_id"] == "run_123"


# ---------------------------------------------------------------------------
# notify_webhook
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    last_payload: dict[str, Any] = {}

    def do_POST(self):  # noqa: N802 (stdlib naming)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _Handler.last_payload = json.loads(body.decode("utf-8"))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):  # silence test stderr
        pass


@pytest.fixture
def webhook_server():
    _Handler.last_payload = {}
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    import threading
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}/hook"
    srv.shutdown()
    t.join(timeout=2)


def test_notify_webhook_success(webhook_server):
    m = build_run_manifest(_config(Path(".")), status="success")
    res = notify_webhook(m, {"webhook": {"url": webhook_server, "on_success": True}})
    assert res["ok"] is True
    assert res["status"] == 200
    assert _Handler.last_payload["event"] == "simulation_run"
    assert _Handler.last_payload["manifest"]["run_id"] == "run_123"


def test_notify_webhook_no_url_returns_gracefully():
    res = notify_webhook({}, {"webhook": {}})
    assert res["ok"] is False
    assert "no webhook url" in res["error"]


def test_notify_webhook_respects_on_success_disabled(webhook_server):
    m = build_run_manifest(_config(Path(".")), status="success")
    res = notify_webhook(m, {"webhook": {"url": webhook_server, "on_success": False}})
    assert res["ok"] is False
    assert "on_success disabled" in res["error"]
    assert _Handler.last_payload == {}  # never called


def test_notify_webhook_on_failure_sends_when_enabled(webhook_server):
    m = build_run_manifest(_config(Path(".")), status="failed", error="boom")
    res = notify_webhook(m, {"webhook": {"url": webhook_server, "on_failure": True}})
    assert res["ok"] is True
    assert _Handler.last_payload["manifest"]["status"] == "failed"


def test_notify_webhook_never_raises_on_network_error():
    # Point at a closed port; must not raise — best-effort only.
    m = build_run_manifest(_config(Path(".")))
    res = notify_webhook(
        m, {"webhook": {"url": "http://127.0.0.1:1/nope", "on_success": True}}, timeout=1.0,
    )
    assert res["ok"] is False
    assert res["error"]  # populated, not raised
