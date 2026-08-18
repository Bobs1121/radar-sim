"""Release-entry and Windows deployment-mode contract tests."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import cli.web as web_module
import pytest


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


def test_windows_installer_exposes_one_unified_connector_and_keeps_legacy_boundary():
    bootstrap = (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")
    starter = (ROOT / "scripts" / "start_windows.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("unified", "light", "full")]' in bootstrap
    assert '$Mode = "unified"' in bootstrap
    assert 'if ($Mode -eq "unified") { $Mode = "full" }' in bootstrap
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
    assert "radar_sim_source.pth" in bootstrap
    assert "use only the Python standard" in bootstrap
    assert "Optional build/local-simulation dependencies" in bootstrap
    assert "The PC is still connectable" in bootstrap
    assert '"--timeout", "20", "--retries", "1", "--quiet"' in bootstrap
    assert '"PyYAML>=6.0", "httpx==0.28.1", "pydantic==2.13.4"' in bootstrap
    assert "optional_dependencies_ready" in bootstrap
    assert "optional_dependency_error" in bootstrap
    assert "--system-site-packages" in bootstrap
    assert "include-system-site-packages = true" in bootstrap
    assert "Existing user Python packages satisfy" in bootstrap
    assert "radar-sim-dependency-probe-" in bootstrap
    assert "vendor\\windows-wheels" in bootstrap
    assert "--no-index" in bootstrap
    assert "bundled scaffold wheels" in bootstrap
    assert "RegisterStartup" in bootstrap
    assert "New-ScheduledTaskAction" in bootstrap
    assert "New-ScheduledTaskTrigger -AtLogOn" in bootstrap
    assert 'watchdogTaskName = "$taskName-Watchdog"' in bootstrap
    assert "RepetitionInterval (New-TimeSpan -Minutes 2)" in bootstrap
    assert "watch_windows_connector.ps1" in bootstrap
    assert 'RecoveryConfigPath = Join-Path $DataRoot "install.backup.json"' in bootstrap
    assert "function Save-InstallConfig" in bootstrap
    assert 'RunHiddenScript = Join-Path $PSScriptRoot "run_hidden.vbs"' in bootstrap
    assert 'New-ScheduledTaskAction -Execute "wscript.exe"' in bootstrap
    assert "-StartWhenAvailable" in bootstrap
    assert "-RestartCount 999" in bootstrap
    assert "-AllowStartIfOnBatteries" in bootstrap
    assert "-DontStopIfGoingOnBatteries" in bootstrap
    assert "-Supervise" in starter
    assert "Threading.Mutex" in starter
    assert "orphanAgentPids" in starter
    assert "Get-CimInstance Win32_Process" in starter
    assert "connector.pid" in starter
    assert "connector.log" in starter
    watchdog = (ROOT / "scripts" / "watch_windows_connector.ps1").read_text(encoding="utf-8")
    assert "Test-ConnectorSupervisor" in watchdog
    assert "Find-ConnectorSupervisor" in watchdog
    assert "Repair-ConnectorControlFiles" in watchdog
    assert "Recovered deleted install metadata" in watchdog
    assert "Recovered deleted or stale supervisor PID metadata" in watchdog
    assert "Recovered corrupt install metadata" in watchdog
    assert "Corrupt install metadata recovery failed" in watchdog
    assert "Start-ScheduledTask -TaskName $ConnectorTaskName" in watchdog
    assert "watchdog.log" in watchdog
    hidden_launcher = (ROOT / "scripts" / "run_hidden.vbs").read_text(encoding="utf-8")
    assert 'shell.Run command, 0, False' in hidden_launcher
    assert "-WindowStyle Hidden" in hidden_launcher
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
    assert "supervisor will keep retrying" in starter
    assert "-not $Supervise" in starter
    assert "X-Content-SHA256" in (ROOT / "scripts" / "install_windows_connector.ps1.in").read_text(encoding="utf-8")
    connector = (ROOT / "scripts" / "install_windows_connector.ps1.in").read_text(encoding="utf-8")
    assert "Get-Sha256" in connector
    assert "Get-FileHash" not in connector
    assert "Invoke-WithRetry" in connector
    assert "foreach ($attempt in 1..$Attempts)" in connector
    assert "Stop-PreviousConnector" in connector
    assert "Get-OneClickRebindRequired" in connector
    assert "Backup-InstalledState" in connector
    assert "Restore-InstalledState" in connector
    assert "Restore-PreviousConnector" in connector
    assert "previous-app" in connector
    assert "previous-config" in connector
    assert '$DataRoot = Join-Path $InstallRoot "data"' in connector
    assert '$ConfigPath = Join-Path $InstallRoot "install.json"' in connector
    assert '$SecretsPath = Join-Path $InstallRoot "credentials.json"' in connector
    assert "unfinished task(s)" in connector
    assert "automatically rebinding" in connector
    assert '"-ForceRebind"' in connector
    assert "/api/v1/jobs?limit=100" in connector
    assert "restore the previous Connector transaction" in connector
    assert '"-Background", "-NoBrowser"' in connector
    assert "Stop-ProcessTreeIfPresent" in connector
    assert '"not found" means the requested end state was reached' in connector
    assert "Reset-InstalledApplication" in connector
    assert '& taskkill.exe /PID $ProcessId /T /F' in connector
    assert "$normalizedAppRoot" in connector
    assert "Get-CimInstance Win32_Process" in connector
    assert 'if ($entry.Name -ieq ".venv") { continue }' in connector
    assert "Clear-ConnectorPythonCache" in bootstrap
    assert "Get-ConnectorRuntimeContract" in bootstrap
    assert "Old Python cache was not replaced" in bootstrap
    assert "connector_contract_version = $ConnectorRuntimeContract" in bootstrap
    assert "Get-RemoteHealth" in bootstrap
    assert "unreachable after $Attempts attempts" in bootstrap
    assert "/api/v1/windows-connector/status?agent_id=$encodedAgentId" in bootstrap
    assert "$snapshot.available" in bootstrap
    assert "$snapshot.contract_current" in bootstrap
    assert "$capabilityName" not in bootstrap
    launcher = (ROOT / "scripts" / "connect_windows.cmd.in").read_text(encoding="utf-8")
    assert "Python.Python.3.12" in connector
    assert "--silent" in connector
    assert "--disable-interactivity" in connector
    assert "Software Center" in connector
    assert "timeout /t" not in launcher
    assert "127.0.0.1" not in launcher
    assert "foreach($i in 1..5)" in launcher
    assert "temporarily unavailable; retrying" in launcher


def test_hidden_launcher_runs_powershell_and_preserves_spaced_arguments(tmp_path: Path):
    if sys.platform != "win32":
        pytest.skip("VBScript launcher is a Windows-only connector component")
    marker = tmp_path / "folder with spaces" / "marker.txt"
    marker.parent.mkdir()
    probe = tmp_path / "probe script.ps1"
    probe.write_text(
        "param([string]$OutputPath, [string]$Value)\n"
        "[IO.File]::WriteAllText($OutputPath, $Value)\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "cscript.exe",
            "//nologo",
            str(ROOT / "scripts" / "run_hidden.vbs"),
            str(probe),
            str(marker),
            "value with spaces",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.1)
    assert marker.read_text(encoding="utf-8") == "value with spaces"


def test_connector_supervisor_does_not_treat_native_stderr_as_child_crash():
    source = (ROOT / "scripts" / "start_windows.ps1").read_text(encoding="utf-8")

    assert '$savedErrorActionPreference = $ErrorActionPreference' in source
    assert '$ErrorActionPreference = "Continue"' in source
    assert '& $venvPy @agentArgs *>> $agentLog' in source
    assert '$exitCode = $LASTEXITCODE' in source
    assert '$ErrorActionPreference = $savedErrorActionPreference' in source


def test_connector_start_restores_deleted_install_metadata_from_backup(tmp_path: Path):
    if sys.platform != "win32":
        pytest.skip("Connector recovery is a Windows-only behavior")
    install_root = tmp_path / "radar-sim"
    data_root = install_root / "data"
    scripts_root = install_root / "app" / "scripts"
    data_root.mkdir(parents=True)
    scripts_root.mkdir(parents=True)
    starter = ROOT / "scripts" / "start_windows.ps1"
    installed_starter = scripts_root / "start_windows.ps1"
    installed_starter.write_bytes(starter.read_bytes())
    recovery = data_root / "install.backup.json"
    recovery.write_text(
        json.dumps({
            "mode": "unified",
            "control_plane": "linux",
            "server_url": "http://127.0.0.1:1",
            "agent_id": "agent-recovery-test",
            "owner": "user-recovery-test",
            "data_root": str(data_root),
        }),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(installed_starter), "-InstallRoot", str(install_root),
            "-NoBrowser",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode != 0  # the fixture intentionally has no venv
    restored = json.loads((install_root / "install.json").read_text(encoding="utf-8-sig"))
    assert restored["agent_id"] == "agent-recovery-test"


def test_connector_start_restores_corrupt_install_metadata_from_backup(tmp_path: Path):
    if sys.platform != "win32":
        pytest.skip("Connector recovery is a Windows-only behavior")
    install_root = tmp_path / "radar-sim"
    data_root = install_root / "data"
    scripts_root = install_root / "app" / "scripts"
    data_root.mkdir(parents=True)
    scripts_root.mkdir(parents=True)
    starter = ROOT / "scripts" / "start_windows.ps1"
    installed_starter = scripts_root / "start_windows.ps1"
    installed_starter.write_bytes(starter.read_bytes())
    recovery = data_root / "install.backup.json"
    recovery.write_text(
        json.dumps({
            "mode": "unified",
            "control_plane": "linux",
            "server_url": "http://127.0.0.1:1",
            "agent_id": "agent-corrupt-recovery-test",
            "owner": "user-recovery-test",
            "data_root": str(data_root),
        }),
        encoding="utf-8",
    )
    # Truncated/invalid install metadata must not strand the connector.
    (install_root / "install.json").write_text("{broken-json", encoding="utf-8")

    completed = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(installed_starter), "-InstallRoot", str(install_root),
            "-NoBrowser",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode != 0  # the fixture intentionally has no venv
    restored = json.loads((install_root / "install.json").read_text(encoding="utf-8-sig"))
    assert restored["agent_id"] == "agent-corrupt-recovery-test"


def test_windows_connector_bundle_contains_hidden_launcher(tmp_path: Path):
    from scripts.build_windows_connector_bundle import build

    archive, _manifest = build(tmp_path / "connector.zip")
    with zipfile.ZipFile(archive) as bundle:
        assert "scripts/run_hidden.vbs" in bundle.namelist()


def test_windows_connector_bundle_hash_is_deterministic(tmp_path: Path):
    from scripts.build_windows_connector_bundle import build

    first, _ = build(tmp_path / "first.zip")
    second, _ = build(tmp_path / "second.zip")
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()


def test_linux_release_builds_same_origin_windows_connector_bundle():
    deploy = (ROOT / "scripts" / "linux_deploy.sh").read_text(encoding="utf-8")
    assert "build_windows_connector_bundle.py" in deploy
    assert "rsim-windows-connector.zip" in deploy


def test_windows_installer_checks_capability_in_paired_owner_scope():
    installer = (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")
    assert '$capabilityHeaders["X-Rsim-User"] = $Owner' in installer
    assert "-Headers $capabilityHeaders -TimeoutSec 5" in installer


def test_windows_installer_verifies_exact_device_and_never_silently_rebinds():
    installer = (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")
    assert "/api/v1/windows-connector/status?agent_id=$encodedAgentId" in installer
    assert "Linux confirmed this exact PC and user binding" in installer
    assert "An update never changes the bound user" in installer
    assert "Updating cannot silently change the server" in installer
    assert "ForceRebind" in installer
