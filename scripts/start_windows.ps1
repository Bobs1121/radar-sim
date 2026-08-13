<# Start a persisted radar-sim Windows connector (legacy full/light compatible). #>
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
    throw "Not installed. Run .\scripts\bootstrap.ps1 once to connect this PC."
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
$env:RSIM_USER = [string]$config.owner
$env:RSIM_OWNER_BOUND = "1"
$env:RSIM_AGENT_TOKEN = [string]$secrets.agent_token
$env:RSIM_API_TOKEN = [string]$secrets.api_token
$serverUrl = ([string]$config.server_url).TrimEnd('/')
$serverHost = ([Uri]$serverUrl).Host
$bypass = @($env:NO_PROXY -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($bypass -notcontains $serverHost) { $bypass += $serverHost }
$env:NO_PROXY = ($bypass -join ',')
$env:no_proxy = $env:NO_PROXY
$unifiedMode = [string]$config.mode -in @("unified", "full")
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

if ($unifiedMode -and $controlPlane -eq "local") {
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
        Write-Host "Local Web/API is already running: $serverUrl/" -ForegroundColor Green
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
    if ($server) { Write-Host "Local Web/API started: $serverUrl/ (PID $($server.Id))." -ForegroundColor Green }
    if (-not $NoBrowser) { Start-Process "$serverUrl/" }
} else {
    $controlPlaneHealthy = $false
    try {
        Invoke-RestMethod -Method Get -Uri "$serverUrl/api/v1/health" -TimeoutSec 5 | Out-Null
        $controlPlaneHealthy = $true
    } catch {
        if (-not $Supervise) {
            throw "Linux control plane is unavailable: $serverUrl"
        }
        Write-Warning "Linux control plane is temporarily unavailable: $serverUrl. The supervisor will keep retrying."
    }
    if ($controlPlaneHealthy) {
        Write-Host "Windows connector will use Linux control plane: $serverUrl/" -ForegroundColor Green
    } else {
        Write-Host "Windows connector is starting in reconnect mode for Linux control plane: $serverUrl/" -ForegroundColor Yellow
    }
    if (-not $NoBrowser -and $controlPlaneHealthy) { Start-Process "$serverUrl/" }
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
    # Task Scheduler can regard a console-close exit as terminal while a
    # child Python launcher remains orphaned.  A later repair trigger then
    # owns the mutex but would otherwise start a duplicate Agent with the same
    # logical id.  Once this supervisor owns the mutex, remove only Agent
    # processes launched from this exact installation before starting one.
    $entryPath = [IO.Path]::GetFullPath($RsimEntry).ToLowerInvariant()
    $orphanAgentPids = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $line = [string]$_.CommandLine
                $normalized = $line.Replace('/', '\').ToLowerInvariant()
                $normalized.Contains($entryPath.Replace('/', '\')) -and
                    $normalized -match '\sagent(?:\s|$)'
            } |
            Select-Object -ExpandProperty ProcessId
    )
    if ($orphanAgentPids.Count -gt 0) {
        Stop-Process -Id $orphanAgentPids -Force -ErrorAction SilentlyContinue
        foreach ($orphanPid in $orphanAgentPids) {
            Wait-Process -Id $orphanPid -Timeout 5 -ErrorAction SilentlyContinue
        }
    }
    $connectorPidPath = Join-Path $InstallRoot "connector.pid"
    $logRoot = Join-Path $InstallRoot "logs"
    $agentLog = Join-Path $logRoot "connector.log"
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
    if ((Test-Path $agentLog) -and (Get-Item $agentLog).Length -gt 2097152) {
        Move-Item -LiteralPath $agentLog -Destination ($agentLog + ".1") -Force
    }
    Set-Content -LiteralPath $connectorPidPath -Value ([string]$PID) -Encoding ASCII
    Add-Content -LiteralPath $agentLog -Encoding UTF8 `
        -Value ("{0:o} Connector supervisor started (PID {1})." -f (Get-Date), $PID)
    try {
        while ($true) {
            try {
                # Windows PowerShell converts a native process' stderr into a
                # NativeCommandError record.  With the script-wide
                # ErrorActionPreference=Stop, harmless library diagnostics
                # (for example NumExpr INFO lines) used to abort the Agent
                # invocation and make the supervisor restart the same Stage
                # every few seconds.  Native stderr belongs in the durable
                # connector log; only the actual process exit code decides
                # whether the child stopped.
                $savedErrorActionPreference = $ErrorActionPreference
                try {
                    $ErrorActionPreference = "Continue"
                    & $venvPy @agentArgs *>> $agentLog
                    $exitCode = $LASTEXITCODE
                } finally {
                    $ErrorActionPreference = $savedErrorActionPreference
                }
                Add-Content -LiteralPath $agentLog -Encoding UTF8 `
                    -Value ("{0:o} Connector child stopped with exit {1}; restarting." -f (Get-Date), $exitCode)
                Write-Warning "The connector stopped (exit $exitCode); reconnecting in 5 seconds."
            } catch {
                Add-Content -LiteralPath $agentLog -Encoding UTF8 `
                    -Value ("{0:o} Connector child start failed: {1}" -f (Get-Date), $_.Exception.Message)
                Write-Warning "The connector could not start: $($_.Exception.Message); reconnecting in 5 seconds."
            }
            Start-Sleep -Seconds 5
        }
    } finally {
        Add-Content -LiteralPath $agentLog -Encoding UTF8 `
            -Value ("{0:o} Connector supervisor is exiting." -f (Get-Date))
        try {
            if ((Get-Content -Raw -Encoding ASCII $connectorPidPath -ErrorAction SilentlyContinue).Trim() -eq [string]$PID) {
                Remove-Item -LiteralPath $connectorPidPath -Force -ErrorAction SilentlyContinue
            }
        } catch { }
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
}

Write-Host "Windows connector is running; press Ctrl+C to stop." -ForegroundColor Cyan
& $venvPy @agentArgs
exit $LASTEXITCODE
