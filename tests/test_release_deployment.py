"""Release-entry and Windows deployment-mode contract tests."""

from __future__ import annotations

import argparse
from pathlib import Path

import cli.web as web_module


ROOT = Path(__file__).resolve().parents[1]


def _parse_web(*argv: str):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    web_module.register(subparsers)
    return parser.parse_args(["web", *argv])


def test_compat_web_embedded_mode_is_explicit_not_hardcoded():
    assert _parse_web().windows_mode == "full"
    assert _parse_web("--windows-mode", "light").windows_mode == "light"


def test_compat_web_reports_local_sim_only_for_windows_full(monkeypatch):
    monkeypatch.setattr(web_module, "_WEB_MODE", "embedded")
    monkeypatch.setattr(web_module.sys, "platform", "win32")
    monkeypatch.setattr(web_module, "_EMBEDDED_WINDOWS_MODE", "light")
    assert web_module._server_info()["local_sim_available"] is False

    monkeypatch.setattr(web_module, "_EMBEDDED_WINDOWS_MODE", "full")
    assert web_module._server_info()["local_sim_available"] is True


def test_linux_and_docker_release_entry_is_unified_serve_v1():
    deploy = (ROOT / "scripts" / "linux_deploy.sh").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "rsim.py server serve-v1" in deploy
    assert "--auth-file" in deploy
    assert "RSIM_INSECURE_NO_AUTH" in deploy
    assert "--insecure-no-auth" in deploy
    assert "rsim web" not in deploy
    assert 'CMD ["sh", "-c", "exec rsim server serve-v1' in dockerfile
    assert "EXPOSE 8878" in dockerfile


def test_windows_installer_persists_mode_and_enforces_light_boundary():
    bootstrap = (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")
    starter = (ROOT / "scripts" / "start_windows.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("light", "full")]' in bootstrap
    assert "default_capabilities_for_mode" in bootstrap
    assert "light mode exposes forbidden runtime capabilities" in bootstrap
    assert "credentials.json" in bootstrap
    assert '[ValidateSet("local", "linux")]' in bootstrap
    assert 'control_plane = $ControlPlane' in bootstrap
    assert '"--windows-mode", [string]$config.mode' in starter
    assert '"--no-cluster-executor"' in starter
    assert '$controlPlane -eq "local"' in starter
    assert '"--auth-file"' not in starter
    assert 'Local loopback access does not require a token' in bootstrap
    assert "Visual Studio is user-managed" in bootstrap
    assert "Visual Studio 2015 (v140)" in bootstrap
    assert "visual_studio_detected" in bootstrap
    assert "authentication_required" in bootstrap
    assert "no token is stored" in bootstrap
    assert "RegisterStartup" in bootstrap
    assert "New-ScheduledTaskAction" in bootstrap
    assert "-Supervise" in starter
    assert "Threading.Mutex" in starter
    assert "connector.pid" in starter
    assert '$RsimEntry = Join-Path $RepoRoot "rsim.py"' in starter
    assert '$RsimEntry, "agent"' in starter
    assert '"rsim.py", "agent"' not in starter
    assert "Stop-Process" in bootstrap
    assert "Stop-ConnectorProcessTree" in bootstrap
    assert "Get-CimInstance Win32_Process" in bootstrap
    assert "Wait-Process" in bootstrap
    assert "Get-ConnectorProcessId" in bootstrap
    assert "The background connector did not stay running" in bootstrap
    assert "NO_PROXY" in starter
    assert "X-Content-SHA256" in (ROOT / "scripts" / "install_windows_connector.ps1.in").read_text(encoding="utf-8")
    assert "Get-FileHash" in (ROOT / "scripts" / "install_windows_connector.ps1.in").read_text(encoding="utf-8")
    assert "/api/v1/capabilities" in bootstrap
    connector = (ROOT / "scripts" / "install_windows_connector.ps1.in").read_text(encoding="utf-8")
    assert "Python.Python.3.12" in connector
    assert "--silent" in connector
    assert "--disable-interactivity" in connector
    assert "Software Center" in connector


def test_linux_release_builds_same_origin_windows_connector_bundle():
    deploy = (ROOT / "scripts" / "linux_deploy.sh").read_text(encoding="utf-8")
    assert "build_windows_connector_bundle.py" in deploy
    assert "rsim-windows-connector.zip" in deploy
