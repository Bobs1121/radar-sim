<# Independent watchdog for the persisted radar-sim Windows Connector. #>
[CmdletBinding()]
param(
    [string]$InstallRoot = "",
    [string]$ConnectorTaskName = ""
)

$ErrorActionPreference = "Stop"
if (-not $InstallRoot) {
    $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME "AppData\Local" }
    $InstallRoot = Join-Path $base "radar-sim"
}
if (-not $ConnectorTaskName) { $ConnectorTaskName = "RadarSimConnector-$env:USERNAME" }
$PidPath = Join-Path $InstallRoot "connector.pid"
$StartScript = Join-Path $InstallRoot "app\scripts\start_windows.ps1"
$LogRoot = Join-Path $InstallRoot "logs"
$LogPath = Join-Path $LogRoot "watchdog.log"
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null

function Write-WatchdogLog([string]$Message) {
    if ((Test-Path $LogPath) -and (Get-Item $LogPath).Length -gt 1048576) {
        Move-Item -LiteralPath $LogPath -Destination ($LogPath + ".1") -Force
    }
    Add-Content -LiteralPath $LogPath -Encoding UTF8 `
        -Value ("{0:o} {1}" -f (Get-Date), $Message)
}

function Test-ConnectorSupervisor {
    if (-not (Test-Path $PidPath)) { return $false }
    try {
        $connectorPid = [int](Get-Content -Raw -Encoding ASCII $PidPath)
        if ($connectorPid -le 0) { return $false }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$connectorPid" -ErrorAction Stop
        if (-not $process) { return $false }
        $line = ([string]$process.CommandLine).Replace('/', '\').ToLowerInvariant()
        return (
            $line.Contains(([IO.Path]::GetFullPath($StartScript)).Replace('/', '\').ToLowerInvariant()) -and
            $line.Contains('-supervise')
        )
    } catch {
        return $false
    }
}

try {
    if (Test-ConnectorSupervisor) { exit 0 }
    Write-WatchdogLog "Connector supervisor is absent; requesting restart."
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
    $task = Get-ScheduledTask -TaskName $ConnectorTaskName -ErrorAction Stop
    if ([string]$task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $ConnectorTaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    Start-ScheduledTask -TaskName $ConnectorTaskName
    foreach ($attempt in 1..20) {
        Start-Sleep -Seconds 1
        if (Test-ConnectorSupervisor) {
            Write-WatchdogLog "Connector supervisor restarted successfully."
            exit 0
        }
    }
    Write-WatchdogLog "Connector restart was requested but no supervisor appeared."
    exit 1
} catch {
    Write-WatchdogLog ("Watchdog failed: " + $_.Exception.Message)
    exit 1
}
