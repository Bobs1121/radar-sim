<# Start a persisted radar-sim Windows full/light deployment. #>
[CmdletBinding()]
param(
    [string]$InstallRoot = "",
    [switch]$Background,
    [switch]$NoBrowser,
    [switch]$Supervise
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $InstallRoot) {
    $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME "AppData\Local" }
    $InstallRoot = Join-Path $base "radar-sim"
}
$configPath = Join-Path $InstallRoot "install.json"
$secretsPath = Join-Path $InstallRoot "credentials.json"
if (-not (Test-Path $configPath)) {
    throw "Not installed. Run .\scripts\bootstrap.ps1 -Mode full|light first."
}
$config = Get-Content -Raw -Encoding UTF8 $configPath | ConvertFrom-Json
$secrets = if (Test-Path $secretsPath) {
    Get-Content -Raw -Encoding UTF8 $secretsPath | ConvertFrom-Json
} else {
    [pscustomobject]@{ agent_token = ""; api_token = "" }
}
$venvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "Python environment is missing; rerun bootstrap.ps1." }
$RsimEntry = Join-Path $RepoRoot "rsim.py"
if (-not (Test-Path $RsimEntry)) { throw "radar-sim entry point is missing; reconnect this PC from Web." }

$env:RSIM_HOME = [string]$config.data_root
$env:RSIM_AGENT_TOKEN = [string]$secrets.agent_token
$env:RSIM_API_TOKEN = [string]$secrets.api_token
$serverUrl = ([string]$config.server_url).TrimEnd('/')
$serverHost = ([Uri]$serverUrl).Host
$bypass = @($env:NO_PROXY -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($bypass -notcontains $serverHost) { $bypass += $serverHost }
$env:NO_PROXY = ($bypass -join ',')
$env:no_proxy = $env:NO_PROXY
$controlPlane = if ($config.control_plane) { [string]$config.control_plane }
    elseif ([string]$config.mode -eq "full") { "local" } else { "linux" }
if ([string]$config.mode -eq "light" -and $controlPlane -ne "linux") {
    throw "Light mode requires the Linux control plane. Rerun bootstrap.ps1."
}
$agentArgs = @(
    $RsimEntry, "agent", "--server-url", $serverUrl, "--api-url", $serverUrl,
    "--agent-id", [string]$config.agent_id, "--windows-mode", [string]$config.mode
)

function Quote-ProcessArgument([string]$value) {
    return '"' + $value.Replace('"', '\"') + '"'
}

if ([string]$config.mode -eq "full" -and $controlPlane -eq "local") {
    $uri = [Uri]$serverUrl
    if (-not $uri.IsLoopback) { throw "Full local control plane requires a loopback ServerUrl." }
    $serverArgs = @(
        $RsimEntry, "server", "serve-v1", "--host", "127.0.0.1",
        "--port", [string]$uri.Port, "--no-cluster-executor"
    )
    $ready = $false
    try {
        Invoke-RestMethod -Method Get -Uri "$serverUrl/api/v1/health" -TimeoutSec 2 | Out-Null
        $ready = $true
        Write-Host "Full Web/API is already running: $serverUrl/" -ForegroundColor Green
    } catch { }
    if (-not $ready) {
        $serverArgumentLine = ($serverArgs | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join ' '
        $server = Start-Process -FilePath $venvPy -ArgumentList $serverArgumentLine -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
    }
    foreach ($attempt in 1..30) {
        if ($ready) { break }
        try {
            Invoke-RestMethod -Method Get -Uri "$serverUrl/api/v1/health" -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $ready) { throw "Local serve-v1 failed to start." }
    if ($server) { Write-Host "Full Web/API started: $serverUrl/ (PID $($server.Id))." -ForegroundColor Green }
    if (-not $NoBrowser) { Start-Process "$serverUrl/" }
} else {
    try {
        Invoke-RestMethod -Method Get -Uri "$serverUrl/api/v1/health" -TimeoutSec 5 | Out-Null
    } catch {
        throw "Linux control plane is unavailable: $serverUrl"
    }
    Write-Host "$($config.mode) Agent will use Linux control plane: $serverUrl/" -ForegroundColor Green
    if (-not $NoBrowser) { Start-Process "$serverUrl/" }
}

if ($Background) {
    $self = $MyInvocation.MyCommand.Path
    $supervisorArgs = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $self,
        "-InstallRoot", $InstallRoot, "-Supervise", "-NoBrowser"
    )
    $supervisorArgumentLine = ($supervisorArgs | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join ' '
    $supervisor = Start-Process -FilePath "powershell.exe" -ArgumentList $supervisorArgumentLine `
        -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru
    Write-Host "This PC is connecting in the background (PID $($supervisor.Id))." -ForegroundColor Green
    return
}

if ($Supervise) {
    $created = $false
    $mutexName = "Local\RadarSimConnector-" + ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
    $mutex = New-Object Threading.Mutex($true, $mutexName, [ref]$created)
    if (-not $created) {
        Write-Host "This PC is already connected." -ForegroundColor Green
        return
    }
    $connectorPidPath = Join-Path $InstallRoot "connector.pid"
    Set-Content -LiteralPath $connectorPidPath -Value ([string]$PID) -Encoding ASCII
    try {
        while ($true) {
            try {
                & $venvPy @agentArgs
                $exitCode = $LASTEXITCODE
                Write-Warning "The connector stopped (exit $exitCode); reconnecting in 5 seconds."
            } catch {
                Write-Warning "The connector could not start: $($_.Exception.Message); reconnecting in 5 seconds."
            }
            Start-Sleep -Seconds 5
        }
    } finally {
        try {
            if ((Get-Content -Raw -Encoding ASCII $connectorPidPath -ErrorAction SilentlyContinue).Trim() -eq [string]$PID) {
                Remove-Item -LiteralPath $connectorPidPath -Force -ErrorAction SilentlyContinue
            }
        } catch { }
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
}

Write-Host "$($config.mode) Agent is running; press Ctrl+C to stop." -ForegroundColor Cyan
& $venvPy @agentArgs
exit $LASTEXITCODE
