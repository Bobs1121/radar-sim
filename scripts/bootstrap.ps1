<#
.SYNOPSIS
    radar-sim Windows one-click installer.

.DESCRIPTION
    Installs one of two explicit product modes and persists the connection once.
    The current sprint keeps loopback-only ``full + local`` free of login/token
    setup; authentication remains required for every Linux control plane:

      light - local Selena compile + Runtime Bundle/data upload only.  Simulation
              always continues on Cluster; this mode never enables local simulation.
      full  - Windows full Agent.  ControlPlane=local starts an offline local-only
              Web/API; ControlPlane=linux connects the full Agent to the central
              Web so one task entry can select either local or Cluster simulation.

    The installer does not ask users to select an internal project.  Code/data/
    Runtime/Adapter/MatFilter bindings are configured later through the unified
    Web/YAML contract.  Re-running the installer updates dependencies but preserves
    the persisted connection configuration unless new values are supplied.

.EXAMPLE
    .\scripts\bootstrap.ps1 -Mode light -ServerUrl http://rsim:8878 `
        -AgentId alice-laptop -AgentToken <agent-token> -ApiToken <user-token> -Start

.EXAMPLE
    .\scripts\bootstrap.ps1 -Mode full -Start

.EXAMPLE
    .\scripts\bootstrap.ps1 -Mode full -ControlPlane linux `
        -ServerUrl http://rsim:8878 -AgentId alice-full `
        -AgentToken <agent-token> -ApiToken <user-token> -Start
#>

[CmdletBinding()]
param(
    [ValidateSet("light", "full")]
    [string]$Mode = "light",
    [ValidateSet("local", "linux")]
    [string]$ControlPlane = "",
    [string]$ServerUrl = "",
    [string]$AgentId = "",
    [string]$AgentToken = "",
    [string]$ApiToken = "",
    [string]$InstallRoot = "",
    [switch]$SkipDeps,
    [switch]$SkipCheck,
    [switch]$RegisterStartup,
    [switch]$Start
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $InstallRoot) {
    $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME "AppData\Local" }
    $InstallRoot = Join-Path $base "radar-sim"
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$ConfigPath = Join-Path $InstallRoot "install.json"
$SecretsPath = Join-Path $InstallRoot "credentials.json"
$AuthPath = Join-Path $InstallRoot "http-auth.json"
$DataRoot = Join-Path $InstallRoot "data"
$VenvDir = Join-Path $RepoRoot ".venv"
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
$StartScript = Join-Path $PSScriptRoot "start_windows.ps1"

function Write-Step($message) { Write-Host "`n==> $message" -ForegroundColor Cyan }
function Write-Ok($message) { Write-Host "    OK  $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "    WARN $message" -ForegroundColor Yellow }
function Fail($message) { Write-Host "    ERR  $message" -ForegroundColor Red; exit 1 }

function Stop-ConnectorProcessTree([int]$RootPid) {
    # Stop deepest children first.  Stopping only the PowerShell supervisor can
    # orphan its Python Agent, and a reinstall would then run the same task
    # twice under the same logical node identity.
    $snapshot = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $ordered = New-Object System.Collections.Generic.List[int]
    $pending = New-Object System.Collections.Generic.Stack[int]
    $pending.Push($RootPid)
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        if ($ordered.Contains($current)) { continue }
        $ordered.Add($current)
        foreach ($child in $snapshot | Where-Object { [int]$_.ParentProcessId -eq $current }) {
            $pending.Push([int]$child.ProcessId)
        }
    }
    for ($index = $ordered.Count - 1; $index -ge 0; $index--) {
        Stop-Process -Id $ordered[$index] -Force -ErrorAction SilentlyContinue
    }
}

Set-Location $RepoRoot
Write-Step "1/5 Check Windows and Python"
if (-not $IsWindows -and $env:OS -ne "Windows_NT") {
    Fail "The full/light installer only runs on Windows. Without Windows, use Linux Web/SDK and an existing Runtime Bundle."
}
$Python = $null
foreach ($candidate in @("python", "py")) {
    try {
        $version = & $candidate -c "import sys; print('.'.join(map(str, sys.version_info[:3]))); raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $Python = $candidate; Write-Ok "Python $version ($candidate)"; break }
    } catch { }
}
if (-not $Python) { Fail "Python 3.10+ is required." }

Write-Step "2/5 Install $Mode dependencies"
if (-not (Test-Path $VenvPy)) {
    & $Python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Fail "Failed to create .venv." }
}
if (-not $SkipDeps) {
    & $VenvPy -m pip install --quiet --upgrade pip
    $extra = if ($Mode -eq "full") { ".[v5,full]" } else { ".[sdk]" }
    & $VenvPy -m pip install --quiet -e $extra
    if ($LASTEXITCODE -ne 0) { Fail "Failed to install $Mode dependencies." }
}
Write-Ok "$Mode Python environment is ready."

Write-Step "3/5 Persist the one-time connection configuration"
New-Item -ItemType Directory -Force -Path $InstallRoot, $DataRoot | Out-Null
$existing = $null
if (Test-Path $ConfigPath) {
    try { $existing = Get-Content -Raw -Encoding UTF8 $ConfigPath | ConvertFrom-Json } catch { }
}
if (-not $AgentId) {
    $AgentId = if ($existing.agent_id) { [string]$existing.agent_id } else { "agent-$env:USERNAME-$env:COMPUTERNAME" }
}
if (-not $ControlPlane) {
    if ($Mode -eq "light") { $ControlPlane = "linux" }
    elseif ($existing.control_plane) { $ControlPlane = [string]$existing.control_plane }
    else { $ControlPlane = "local" }
}
if ($Mode -eq "light" -and $ControlPlane -ne "linux") {
    Fail "Light mode requires -ControlPlane linux and has no local Web or local simulation."
}
$UseLocalControl = ($Mode -eq "full" -and $ControlPlane -eq "local")

if ($UseLocalControl) {
    if (-not $ServerUrl) { $ServerUrl = "http://127.0.0.1:8878" }
    # Local control is bound to loopback and intentionally has no token gate in
    # this sprint.  Do not generate credentials that the user cannot usefully
    # distinguish from ordinary simulation configuration.
    $ApiToken = ""
    $AgentToken = ""
} else {
    if (-not $ServerUrl -and $existing.server_url) { $ServerUrl = [string]$existing.server_url }
    if (-not $ServerUrl) { Fail "$Mode + linux requires the Linux -ServerUrl." }
    $ServerUrl = $ServerUrl.TrimEnd('/')
    $RemoteAuthRequired = $true
    try {
        $health = Invoke-RestMethod -Method Get -Uri "$ServerUrl/api/v1/health" -TimeoutSec 5
        if ($null -ne $health.authentication_required) {
            $RemoteAuthRequired = [bool]$health.authentication_required
        }
    } catch {
        Write-Warn "Could not inspect Linux authentication mode yet: $($_.Exception.Message)"
    }
    if (Test-Path $SecretsPath) {
        $oldSecrets = Get-Content -Raw -Encoding UTF8 $SecretsPath | ConvertFrom-Json
        if (-not $AgentToken) { $AgentToken = [string]$oldSecrets.agent_token }
        if (-not $ApiToken) { $ApiToken = [string]$oldSecrets.api_token }
    }
    if ($RemoteAuthRequired -and (-not $AgentToken -or -not $ApiToken)) {
        Fail "$Mode + linux requires -AgentToken and -ApiToken from the Linux administrator."
    }
    if (-not $RemoteAuthRequired) {
        $AgentToken = ""
        $ApiToken = ""
        Write-Ok "Linux test service currently has authentication disabled; no token is stored."
    }
}

$installConfig = [ordered]@{
    version = 2
    mode = $Mode
    control_plane = $ControlPlane
    server_url = $ServerUrl.TrimEnd('/')
    agent_id = $AgentId
    repo_root = $RepoRoot
    data_root = $DataRoot
    auth_file = ""
    authentication_required = if ($UseLocalControl) { $false } else { $RemoteAuthRequired }
}
$secrets = [ordered]@{ version = 1; agent_token = $AgentToken; api_token = $ApiToken }
$installConfig | ConvertTo-Json | Set-Content -Encoding UTF8 $ConfigPath
$secrets | ConvertTo-Json | Set-Content -Encoding UTF8 $SecretsPath

# Remote token persistence is required for unattended reconnect.  Local mode
# writes an empty compatibility document; no local access token is generated.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $InstallRoot /inheritance:r /grant:r "${identity}:(OI)(CI)F" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Warn "Could not restrict ACL automatically. Ensure only this user can read $InstallRoot." }
if ($UseLocalControl) {
    Write-Ok "Config: $ConfigPath. Local loopback access does not require a token in this sprint."
} else {
    Write-Ok "Config: $ConfigPath. Credentials stay in the restricted folder, never in simulation YAML."
}

Write-Step "4/5 Verify deployment-mode boundaries"
$vsCompilers = @()
$vs2015 = "${env:ProgramFiles(x86)}\Microsoft Visual Studio 14.0\VC\bin\amd64\cl.exe"
if (Test-Path $vs2015) { $vsCompilers += "Visual Studio 2015 (v140)" }
foreach ($candidate in @(
    "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2017\*\VC\Tools\MSVC\*\bin\Hostx64\x64\cl.exe",
    "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2019\*\VC\Tools\MSVC\*\bin\Hostx64\x64\cl.exe",
    "$env:ProgramFiles\Microsoft Visual Studio\2022\*\VC\Tools\MSVC\*\bin\Hostx64\x64\cl.exe"
)) {
    if (Get-Item $candidate -ErrorAction SilentlyContinue) { $vsCompilers += $candidate }
}
if ($vsCompilers.Count -eq 0) {
    Write-Warn "No supported Visual Studio C++ compiler found. Install Visual Studio yourself before submitting a Selena build."
} else {
    Write-Ok "User-managed Visual Studio detected: $($vsCompilers -join ', ')"
}
$installConfig["visual_studio_detected"] = ($vsCompilers.Count -gt 0)
$installConfig | ConvertTo-Json | Set-Content -Encoding UTF8 $ConfigPath

if ($RegisterStartup) {
    Write-Step "Register automatic startup and reconnect"
    $taskName = "RadarSimConnector-$env:USERNAME"
    $taskArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`" -InstallRoot `"$InstallRoot`" -Supervise -NoBrowser"
    # A reinstall must replace the running code, not leave the previous
    # supervisor holding the single-instance mutex until the next logon.
    if ($existing -and [string]$existing.startup_method -eq "scheduled_task" -and $existing.startup_name) {
        Stop-ScheduledTask -TaskName ([string]$existing.startup_name) -ErrorAction SilentlyContinue
    }
    $connectorPidPath = Join-Path $InstallRoot "connector.pid"
    if (Test-Path $connectorPidPath) {
        try {
            $connectorPid = [int](Get-Content -Raw -Encoding ASCII $connectorPidPath)
            if ($connectorPid -gt 0 -and $connectorPid -ne $PID) {
                Stop-ConnectorProcessTree $connectorPid
            }
        } catch { }
        Remove-Item -LiteralPath $connectorPidPath -Force -ErrorAction SilentlyContinue
    }
    try {
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $taskArgs
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Description "radar-sim Windows connector" -Force | Out-Null
        $installConfig["startup_method"] = "scheduled_task"
        $installConfig["startup_name"] = $taskName
        Write-Ok "This PC will reconnect automatically after sign-in or a process failure."
    } catch {
        $startupDir = [Environment]::GetFolderPath("Startup")
        $startupFile = Join-Path $startupDir "RadarSimConnector.cmd"
        $command = "@echo off`r`npowershell.exe $taskArgs`r`n"
        Set-Content -LiteralPath $startupFile -Value $command -Encoding ASCII
        $installConfig["startup_method"] = "startup_folder"
        $installConfig["startup_name"] = $startupFile
        Write-Warn "Scheduled Task is blocked; registered the current-user Startup fallback."
    }
    $installConfig | ConvertTo-Json | Set-Content -Encoding UTF8 $ConfigPath
}

$policyCheck = @'
from core.agent_policy import default_capabilities_for_mode
import sys
mode = sys.argv[1]
caps = set(default_capabilities_for_mode(mode))
forbidden = {'simulation.local', 'simulation.cluster', 'cluster.gateway', 'cluster.run', 'result.collect'}
if mode == 'light' and caps & forbidden:
    raise SystemExit('light mode exposes forbidden runtime capabilities')
print(','.join(sorted(caps)))
'@
$capabilities = $policyCheck | & $VenvPy - $Mode
if ($LASTEXITCODE -ne 0) { Fail "Agent mode policy check failed." }
if ($Mode -eq "light") {
    Write-Ok "light only allows local build/upload/data staging; simulation continues on Cluster"
} elseif ($ControlPlane -eq "linux") {
    Write-Ok "full + linux: central Web can schedule Windows local simulation and Linux Cluster"
} else {
    Write-Ok "full + local: offline Web/API, build and local simulation; no Cluster executor"
}

Write-Step "5/5 Basic verification"
if (-not $SkipCheck) {
    $env:RSIM_AGENT_TOKEN = $AgentToken
    $env:RSIM_API_TOKEN = $ApiToken
    $checkSucceeded = $true
    if ($UseLocalControl) {
        & $VenvPy rsim.py server serve-v1 --help | Out-Null
        $checkSucceeded = ($LASTEXITCODE -eq 0)
    } else {
        try {
            Invoke-RestMethod -Method Get -Uri "$ServerUrl/api/v1/health" -TimeoutSec 5 | Out-Null
            $headers = @{}
            if ($AgentToken) { $headers.Authorization = "Bearer $AgentToken" }
            $registration = @{
                name = "$env:COMPUTERNAME-installer-check"
                agent_id = $AgentId
                hostname = $env:COMPUTERNAME
                platform = "Windows"
                # This is only an endpoint/identity probe.  Empty capabilities
                # prevent it from appearing as an online execution node before
                # the persistent connector process has really started.
                capabilities = @()
                metadata = @{
                    node_kind = if ($Mode -eq "full") { "windows_full" } else { "windows_agent" }
                    windows_mode = $Mode
                    installer_check = $true
                }
            } | ConvertTo-Json -Depth 4
            Invoke-RestMethod -Method Post -Uri "$ServerUrl/api/agents/register" `
                -Headers $headers -ContentType "application/json" -Body $registration -TimeoutSec 10 | Out-Null
        } catch {
            Write-Warn "Remote verification failed: $($_.Exception.Message)"
            $checkSucceeded = $false
        }
    }
    if (-not $checkSucceeded) {
        Write-Warn "Initial verification failed. Check URL, tokens, and network before starting."
    } else {
        if ($UseLocalControl) { Write-Ok "Local serve-v1 command check passed." }
        else { Write-Ok "$Mode Agent central registration check passed." }
    }
} else {
    Write-Warn "Remote connectivity verification skipped."
}

Write-Host "`nInstallation complete." -ForegroundColor Cyan
Write-Host "Mode: $Mode / control plane: $ControlPlane"
Write-Host "Visual Studio is user-managed; every build task validates and adapts the Selena script to the installed version."
Write-Host "Start: .\scripts\start_windows.ps1"
Write-Host "Background: .\scripts\start_windows.ps1 -Background"
if ($Mode -eq "light") {
    Write-Host "light has no local simulation. After upload, Linux continues Cluster scheduling without this PC."
} elseif ($ControlPlane -eq "linux") {
    Write-Host "full + linux Web: $ServerUrl/ (one entry for local or Cluster simulation)"
} else {
    Write-Host "full + local Web: $ServerUrl/ (offline local only; use -ControlPlane linux for Cluster)"
    Write-Host "Local loopback Web does not require an access token in this sprint."
}
if ($Start) {
    if ($RegisterStartup -and $installConfig["startup_method"] -eq "scheduled_task") {
        Start-ScheduledTask -TaskName ([string]$installConfig["startup_name"])
        Write-Ok "The connector startup task is running."
    } else {
        & $StartScript -InstallRoot $InstallRoot -Background -NoBrowser
    }
    if (-not $UseLocalControl) {
        $capabilityName = if ($Mode -eq "full") { "windows_full" } else { "windows_light" }
        $connected = $false
        foreach ($attempt in 1..30) {
            try {
                $snapshot = Invoke-RestMethod -Method Get -Uri "$ServerUrl/api/v1/capabilities" -TimeoutSec 5
                if ([bool]$snapshot.capabilities.$capabilityName.available) {
                    $connected = $true
                    break
                }
            } catch { }
            Start-Sleep -Seconds 1
        }
        if (-not $connected) {
            Fail "The background connector did not become available within 30 seconds."
        }
        Write-Ok "Linux confirmed this PC is available for task scheduling."
    }
}
