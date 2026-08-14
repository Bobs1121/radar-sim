<#
.SYNOPSIS
    radar-sim Windows one-click installer.

.DESCRIPTION
    Installs the single user-facing Windows connector and persists the
    connection once.  The historical light/full switches remain accepted for
    administrator compatibility, but ordinary users always receive the
    unified connector with both local-execution and Cluster-preparation
    capabilities.
    The current sprint keeps loopback-only ``full + local`` free of login/token
    setup; authentication remains required for every Linux control plane:

      unified - one connector for local compile/local simulation and Cluster
                preparation/transfer.  The local Selena/VS environment remains
                user-managed; this installer only installs the control connector.
      light/full - legacy administrator compatibility values.

    The installer does not ask users to select an internal project.  Code/data/
    Runtime/Adapter/MatFilter bindings are configured later through the unified
    Web/YAML contract.  Re-running the installer updates dependencies but preserves
    the persisted connection configuration unless new values are supplied.

.EXAMPLE
    .\scripts\bootstrap.ps1 -ServerUrl http://rsim:8878 -Start

.EXAMPLE
    .\scripts\bootstrap.ps1 -Mode full -Start

.EXAMPLE
    .\scripts\bootstrap.ps1 -Mode full -ControlPlane linux `
        -ServerUrl http://rsim:8878 -AgentId alice-full `
        -AgentToken <agent-token> -ApiToken <user-token> -Start
#>

[CmdletBinding()]
param(
    [ValidateSet("unified", "light", "full")]
    [string]$Mode = "unified",
    [ValidateSet("local", "linux")]
    [string]$ControlPlane = "",
    [string]$ServerUrl = "",
    [string]$AgentId = "",
    [string]$AgentToken = "",
    [string]$ApiToken = "",
    [string]$Owner = "",
    [string]$InstallRoot = "",
    [switch]$ForceRebind,
    [switch]$SkipDeps,
    [switch]$SkipCheck,
    [switch]$RegisterStartup,
    [switch]$Start
)

$ErrorActionPreference = "Stop"
# Internally retain the established full node policy for the public unified
# connector.  Existing light/full installations continue to work unchanged.
$RequestedMode = $Mode
if ($Mode -eq "unified") { $Mode = "full" }
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
$VenvConfigPath = Join-Path $VenvDir "pyvenv.cfg"
$ConnectorWheelDir = Join-Path $RepoRoot "vendor\windows-wheels"
$StartScript = Join-Path $PSScriptRoot "start_windows.ps1"
$WatchdogScript = Join-Path $PSScriptRoot "watch_windows_connector.ps1"
$RunHiddenScript = Join-Path $PSScriptRoot "run_hidden.vbs"

function Write-Step($message) { Write-Host "`n==> $message" -ForegroundColor Cyan }
function Write-Ok($message) { Write-Host "    OK  $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "    WARN $message" -ForegroundColor Yellow }
function Fail($message) { Write-Host "    ERR  $message" -ForegroundColor Red; exit 1 }

function Invoke-CapturedNative([string]$Executable, [string[]]$Arguments) {
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& $Executable @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    [pscustomobject]@{
        ExitCode = $exitCode
        Output = (($output | ForEach-Object { $_.ToString() }) -join " ").Trim()
    }
}

function Get-RemoteHealth([string]$Url, [int]$Attempts = 5) {
    $lastError = $null
    foreach ($attempt in 1..$Attempts) {
        try {
            return Invoke-RestMethod -Method Get -Uri "$Url/api/v1/health" -TimeoutSec 10
        } catch {
            $lastError = $_.Exception.Message
            if ($attempt -lt $Attempts) {
                Write-Warn "Linux service is temporarily unavailable; retrying ($attempt/$Attempts)."
                Start-Sleep -Seconds ([Math]::Min(2 * $attempt, 8))
            }
        }
    }
    throw "Linux service is unreachable after $Attempts attempts: $lastError"
}

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
    # Do not immediately launch the replacement while the previous
    # supervisor still owns the single-instance mutex.  Scheduled Task would
    # otherwise exit successfully and leave the PC disconnected until logon.
    foreach ($processId in $ordered) {
        Wait-Process -Id $processId -Timeout 10 -ErrorAction SilentlyContinue
    }
}

function Get-ConnectorProcessId {
    $pidPath = Join-Path $InstallRoot "connector.pid"
    if (-not (Test-Path $pidPath)) { return 0 }
    try {
        $connectorPid = [int](Get-Content -Raw -Encoding ASCII $pidPath)
        if ($connectorPid -gt 0 -and (Get-Process -Id $connectorPid -ErrorAction SilentlyContinue)) {
            return $connectorPid
        }
    } catch { }
    return 0
}

function Clear-ConnectorPythonCache {
    # Release archives use deterministic timestamps.  An in-place upgrade can
    # therefore leave an old timestamp/size-valid .pyc beside new source.  The
    # Connector must never start until all application bytecode caches are
    # rebuilt from the just-downloaded package.  The reusable .venv is outside
    # these source roots and is intentionally preserved.
    foreach ($sourceRootName in @("cli", "core", "radar_sim_sdk", "radar_sim_web", "platforms", "plugins")) {
        $sourceRoot = Join-Path $RepoRoot $sourceRootName
        if (-not (Test-Path $sourceRoot)) { continue }
        Get-ChildItem -LiteralPath $sourceRoot -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Get-ChildItem -LiteralPath $sourceRoot -Recurse -File -Filter "*.pyc" -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
}

function Get-ConnectorRuntimeContract {
    $result = Invoke-CapturedNative -Executable $VenvPy -Arguments @(
        "-c", "from core.agent_policy import WINDOWS_CONNECTOR_CONTRACT_VERSION as v; print(v)"
    )
    if ($result.ExitCode -ne 0 -or [string]$result.Output -notmatch '^\d+$') {
        Fail "The installed Connector runtime could not report its task contract. Reconnect this PC from Web."
    }
    return [int]([string]$result.Output).Trim()
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

Write-Step "2/5 Prepare the unified connector runtime"
$ConnectorOptionalDependenciesReady = $true
$ConnectorOptionalDependencyError = ""
if (-not (Test-Path $VenvPy)) {
    # Keep the connector environment isolated from project packages, but make
    # the user's already-installed Python packages visible.  This avoids
    # downloading a second copy of the scaffold dependencies on every PC;
    # pip is only used for packages that are genuinely missing or incompatible.
    $venvOutput = @(& $Python -m venv --system-site-packages $VenvDir 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $detail = (($venvOutput | ForEach-Object { $_.ToString() }) -join " ").Trim()
        if ($detail.Length -gt 600) { $detail = $detail.Substring(0, 600) + "..." }
        Fail ("Failed to create .venv." + $(if ($detail) { " $detail" } else { "" }))
    }
}
if (Test-Path $VenvConfigPath) {
    # Existing installations were created before package reuse was enabled.
    # Flip only this venv switch; do not modify the user's global Python.
    $venvConfig = Get-Content -Raw -Encoding ASCII $VenvConfigPath
    if ($venvConfig -notmatch '(?im)^include-system-site-packages\s*=\s*true\s*$') {
        if ($venvConfig -match '(?im)^include-system-site-packages\s*=') {
            $venvConfig = [regex]::Replace(
                $venvConfig,
                '(?im)^include-system-site-packages\s*=.*$',
                'include-system-site-packages = true'
            )
        } else {
            $venvConfig = $venvConfig.TrimEnd() + "`r`ninclude-system-site-packages = true`r`n"
        }
        Set-Content -LiteralPath $VenvConfigPath -Value $venvConfig -Encoding ASCII
    }
}
if ($RequestedMode -eq "light") {
    # A light Agent only polls the control plane and performs local path
    # discovery, hashing and resumable uploads.  Those code paths use the
    # Python standard library; requiring ``pip install -e .[sdk]`` here made
    # a new user depend on a corporate package index (and its credentials)
    # even though no third-party module was needed.  Put the downloaded
    # source tree on the venv import path instead.  This also keeps one-click
    # installation usable on an offline Windows workstation.
    $sitePackages = (& $VenvPy -c "import sysconfig; print(sysconfig.get_paths()['purelib'])").Trim()
    if (-not $sitePackages -or -not (Test-Path $sitePackages)) {
        Fail "Could not locate the light Agent Python site-packages directory."
    }
    $sourcePathFile = Join-Path $sitePackages "radar_sim_source.pth"
    Set-Content -LiteralPath $sourcePathFile -Value $RepoRoot -Encoding ASCII
    & $VenvPy -c "import cli.agent, core.agent_policy, core.progress_parser" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Fail "The light Agent source package is incomplete. Reconnect this PC from the Linux Web entry."
    }
    Write-Ok "legacy light connector uses its bundled standard-library path."
} elseif ($RequestedMode -eq "unified") {
    # The public connector is intentionally thin.  The polling, path binding,
    # hashing and resumable transfer paths use only the Python standard
    # library, so a new user must still be able to connect when a corporate
    # Python index/proxy is unavailable.  YAML/httpx/pydantic are optional
    # extensions used by build/local-simulation/SDK paths and are attempted
    # here with a short bounded pip call; their absence must not block the
    # basic Windows -> Linux connection.
    $sitePackages = (& $VenvPy -c "import sysconfig; print(sysconfig.get_paths()['purelib'])").Trim()
    if (-not $sitePackages -or -not (Test-Path $sitePackages)) {
        Fail "Could not locate the connector Python site-packages directory. The Windows Python installation may be incomplete."
    }
    $sourcePathFile = Join-Path $sitePackages "radar_sim_source.pth"
    Set-Content -LiteralPath $sourcePathFile -Value $RepoRoot -Encoding ASCII
    $baselineOutput = @(& $VenvPy -c "import cli.agent, core.agent_policy, core.progress_parser" 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $detail = (($baselineOutput | ForEach-Object { $_.ToString() }) -join " ").Trim()
        if ($detail.Length -gt 600) { $detail = $detail.Substring(0, 600) + "..." }
        Fail ("The downloaded connector source is incomplete." + $(if ($detail) { " $detail" } else { "" }))
    }

    $dependencyProbeCode = @'
import importlib.util
from importlib.metadata import PackageNotFoundError, version

checks = (("PyYAML", "yaml", "6.0", ">="), ("httpx", "httpx", "0.28.1", "=="), ("pydantic", "pydantic", "2.13.4", "=="))
def parts(value):
    return tuple(int(piece) if piece.isdigit() else 0 for piece in value.split(".")[:3])
missing = []
for distribution, module, wanted, operator in checks:
    if importlib.util.find_spec(module) is None:
        missing.append(distribution)
        continue
    try:
        found = version(distribution)
    except PackageNotFoundError:
        missing.append(distribution)
        continue
    if (operator == "==" and found != wanted) or (operator == ">=" and parts(found) < parts(wanted)):
        missing.append(distribution)
print(",".join(missing))
'@
    $dependencyProbePath = Join-Path $env:TEMP ("radar-sim-dependency-probe-" + [Guid]::NewGuid().ToString("N") + ".py")
    Set-Content -LiteralPath $dependencyProbePath -Value $dependencyProbeCode -Encoding ASCII
    function Invoke-DependencyProbe([string]$PythonPath, [string]$ProbePath) {
        # Passing a multiline Python program directly to a native executable
        # loses embedded quotes under Windows PowerShell 5.1.  Execute a
        # temporary file instead; this keeps version checks identical on PS5.1
        # and PowerShell 7 and avoids leaking a source fragment to the shell.
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $output = @(& $PythonPath $ProbePath 2>&1)
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        [pscustomobject]@{
            ExitCode = $exitCode
            Output = (($output | ForEach-Object { $_.ToString() }) -join " ").Trim()
        }
    }
    try {
        $probe = Invoke-DependencyProbe -PythonPath $VenvPy -ProbePath $dependencyProbePath
        if ($probe.ExitCode -ne 0) {
            $detail = if ($probe.Output) { " $($probe.Output)" } else { "" }
            Fail ("The connector dependency check failed.$detail")
        }
        $missing = [string]$probe.Output
        if ($missing) {
            Write-Host "Optional build/local-simulation dependencies are not installed; attempting a one-time setup..." -ForegroundColor Yellow
            Write-Host "Missing or incompatible scaffold packages: $missing" -ForegroundColor DarkGray
            $pipArguments = @(
                "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                "--timeout", "20", "--retries", "1", "--quiet",
                "PyYAML>=6.0", "httpx==0.28.1", "pydantic==2.13.4"
            )
            $pipSource = "configured package source/proxy"
            $wheelFiles = @()
            if (Test-Path $ConnectorWheelDir) {
                $wheelFiles = @(Get-ChildItem -LiteralPath $ConnectorWheelDir -Filter "*.whl" -File -ErrorAction SilentlyContinue)
            }
            if ($wheelFiles.Count -gt 0) {
                Write-Host "Trying the bundled scaffold wheels before using the configured package source..." -ForegroundColor DarkGray
                $wheelArguments = @(
                    "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-index",
                    "--find-links", $ConnectorWheelDir, "--quiet",
                    "PyYAML>=6.0", "httpx==0.28.1", "pydantic==2.13.4"
                )
                $wheelResult = Invoke-CapturedNative -Executable $VenvPy -Arguments $wheelArguments
                if ($wheelResult.ExitCode -eq 0) {
                    $pipResult = $wheelResult
                    $pipSource = "bundled scaffold wheels"
                } else {
                    $pipResult = Invoke-CapturedNative -Executable $VenvPy -Arguments $pipArguments
                }
            } else {
                $pipResult = Invoke-CapturedNative -Executable $VenvPy -Arguments $pipArguments
            }
            $pipOutput = [string]$pipResult.Output
            $pipExit = [int]$pipResult.ExitCode
            $afterProbe = Invoke-DependencyProbe -PythonPath $VenvPy -ProbePath $dependencyProbePath
            $afterMissing = if ($afterProbe.ExitCode -eq 0) { [string]$afterProbe.Output } else { "dependency probe failed" }
            if ($pipExit -ne 0 -or $afterProbe.ExitCode -ne 0 -or $afterMissing) {
                $ConnectorOptionalDependenciesReady = $false
                $pipDetail = $pipOutput.Trim()
                $ConnectorOptionalDependencyError = @($afterMissing, $missing, $pipDetail) |
                    Where-Object { $_ } |
                    Select-Object -First 1
                if (-not $ConnectorOptionalDependencyError) {
                    $ConnectorOptionalDependencyError = "optional dependencies are unavailable"
                }
                if ($ConnectorOptionalDependencyError.Length -gt 600) {
                    $ConnectorOptionalDependencyError = $ConnectorOptionalDependencyError.Substring(0, 600) + "..."
                }
                Write-Warn "The PC is still connectable. Optional dependencies could not be installed from $pipSource ($ConnectorOptionalDependencyError). Existing Selena + Cluster tasks can continue; build/local-simulation tasks will show the missing dependency before execution."
            } else {
                Write-Ok "Missing scaffold dependencies installed from $pipSource."
            }
        } else {
            Write-Ok "Existing user Python packages satisfy the connector requirements; no duplicate download was needed."
        }
        Write-Host "Selena, Visual Studio and the actual simulation environment remain user-managed." -ForegroundColor DarkGray
    } finally {
        Remove-Item -LiteralPath $dependencyProbePath -Force -ErrorAction SilentlyContinue
    }
} elseif (-not $SkipDeps) {
    & $VenvPy -m pip install --quiet --upgrade pip
    $extra = ".[v5,full]"
    & $VenvPy -m pip install --quiet -e $extra
    if ($LASTEXITCODE -ne 0) { Fail "Failed to install $Mode dependencies. Check the configured Python package index or install the required packages before retrying." }
    Write-Ok "$Mode Python environment is ready."
} else {
    Write-Warn "$Mode dependency installation skipped by the operator."
}

Write-Step "3/5 Persist the one-time connection configuration"
New-Item -ItemType Directory -Force -Path $InstallRoot, $DataRoot | Out-Null
$existing = $null
if (Test-Path $ConfigPath) {
    try { $existing = Get-Content -Raw -Encoding UTF8 $ConfigPath | ConvertFrom-Json } catch { }
}
$existingOwner = if ($existing -and $existing.owner) { [string]$existing.owner } else { "" }
$existingServerUrl = if ($existing -and $existing.server_url) { ([string]$existing.server_url).TrimEnd('/') } else { "" }
if (-not $Owner -and $existingOwner) {
    $Owner = $existingOwner
} elseif (
    $Owner -and $existingOwner -and
    -not [string]::Equals($existingOwner, $Owner, [StringComparison]::OrdinalIgnoreCase) -and
    $existingOwner -match '^(web|sdk)-[0-9a-f]{24,64}$' -and
    $Owner -match '^user-[a-z0-9_.-]+$'
) {
    # Older one-click downloads paired the Connector to a generated browser
    # or machine hash.  A later Web/SDK launch supplies the user's durable
    # no-auth grouping label; migrate only those legacy labels while keeping
    # Agent ID, path bindings and the install root intact.  Never silently
    # replace an existing explicit owner.
    Write-Warn "Migrating the legacy generated Connector owner to the supplied stable user identifier."
} elseif (
    $Owner -and $existingOwner -and
    -not [string]::Equals($existingOwner, $Owner, [StringComparison]::OrdinalIgnoreCase) -and
    -not $ForceRebind
) {
    Fail "This Windows profile is already connected with another Web/SDK identity. Open the service with the original NTID, or ask the administrator to perform an explicit rebind. An update never changes the bound user."
}
$Owner = [string]$Owner.Trim()
if ($Owner) { $env:RSIM_USER = $Owner }
if (-not $AgentId) {
    if ($existing.agent_id -and -not $ForceRebind) {
        $AgentId = [string]$existing.agent_id
    } else {
        $identityBytes = [Text.Encoding]::UTF8.GetBytes($Owner.ToLowerInvariant())
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $identitySuffix = ([BitConverter]::ToString($sha.ComputeHash($identityBytes))).Replace("-", "").Substring(0, 12).ToLowerInvariant()
        } finally {
            $sha.Dispose()
        }
        $AgentId = "agent-$env:USERNAME-$env:COMPUTERNAME-$identitySuffix"
    }
}
if (-not $ControlPlane) {
    if ($RequestedMode -in @("unified", "light")) { $ControlPlane = "linux" }
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
    if (
        $ServerUrl -and $existingServerUrl -and
        -not [string]::Equals($existingServerUrl, $ServerUrl.TrimEnd('/'), [StringComparison]::OrdinalIgnoreCase) -and
        -not $ForceRebind
    ) {
        Fail "This Windows profile is already connected to another radar-sim server. Updating cannot silently change the server; request an explicit rebind."
    }
    if (-not $ServerUrl -and $existing.server_url) { $ServerUrl = [string]$existing.server_url }
    if (-not $ServerUrl) { Fail "$Mode + linux requires the Linux -ServerUrl." }
    $ServerUrl = $ServerUrl.TrimEnd('/')
    $RemoteAuthRequired = $true
    try {
        $health = Get-RemoteHealth -Url $ServerUrl
        if ($null -ne $health.authentication_required) {
            $RemoteAuthRequired = [bool]$health.authentication_required
        }
    } catch {
        Fail $_.Exception.Message
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
    # Persist the public value.  start_windows.ps1 and cli.agent accept
    # ``unified`` and map it to the internal full execution policy.
    mode = $RequestedMode
    internal_mode = $Mode
    control_plane = $ControlPlane
    server_url = $ServerUrl.TrimEnd('/')
    agent_id = $AgentId
    owner = $Owner
    repo_root = $RepoRoot
    data_root = $DataRoot
    auth_file = ""
    authentication_required = if ($UseLocalControl) { $false } else { $RemoteAuthRequired }
    optional_dependencies_ready = [bool]$ConnectorOptionalDependenciesReady
    optional_dependency_error = [string]$ConnectorOptionalDependencyError
}
$secrets = [ordered]@{ version = 1; agent_token = $AgentToken; api_token = $ApiToken }
$RecoveryConfigPath = Join-Path $DataRoot "install.backup.json"
function Save-InstallConfig {
    $json = $installConfig | ConvertTo-Json
    $primaryTemp = "$ConfigPath.tmp"
    $recoveryTemp = "$RecoveryConfigPath.tmp"
    $json | Set-Content -Encoding UTF8 $primaryTemp
    Move-Item -LiteralPath $primaryTemp -Destination $ConfigPath -Force
    $json | Set-Content -Encoding UTF8 $recoveryTemp
    Move-Item -LiteralPath $recoveryTemp -Destination $RecoveryConfigPath -Force
}
Save-InstallConfig
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
Save-InstallConfig

if ($RegisterStartup) {
    Write-Step "Register automatic startup and reconnect"
    $taskName = "RadarSimConnector-$env:USERNAME"
    $watchdogTaskName = "$taskName-Watchdog"
    $taskArgs = "`"$RunHiddenScript`" `"$StartScript`" -InstallRoot `"$InstallRoot`" -Supervise -NoBrowser"
    # A reinstall must replace the running code, not leave the previous
    # supervisor holding the single-instance mutex until the next logon.
    if ($existing -and [string]$existing.startup_method -eq "scheduled_task" -and $existing.startup_name) {
        Stop-ScheduledTask -TaskName ([string]$existing.startup_name) -ErrorAction SilentlyContinue
    }
    if ($existing -and $existing.watchdog_name) {
        Unregister-ScheduledTask -TaskName ([string]$existing.watchdog_name) -Confirm:$false -ErrorAction SilentlyContinue
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
        $action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument $taskArgs
        $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $logonTrigger `
            -Settings $settings -Description "radar-sim Windows connector" -Force | Out-Null
    $watchdogArgs = "`"$RunHiddenScript`" `"$WatchdogScript`" -InstallRoot `"$InstallRoot`" -ConnectorTaskName `"$taskName`""
    $watchdogAction = New-ScheduledTaskAction -Execute "wscript.exe" -Argument $watchdogArgs
        $watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
            -RepetitionInterval (New-TimeSpan -Minutes 2)
        $watchdogSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 1) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $watchdogTaskName -Action $watchdogAction `
            -Trigger $watchdogTrigger -Settings $watchdogSettings `
            -Description "radar-sim Windows connector watchdog" -Force | Out-Null
        $installConfig["startup_method"] = "scheduled_task"
        $installConfig["startup_name"] = $taskName
        $installConfig["watchdog_name"] = $watchdogTaskName
        Write-Ok "This PC will reconnect automatically after sign-in or a process failure."
    } catch {
        Unregister-ScheduledTask -TaskName $watchdogTaskName -Confirm:$false -ErrorAction SilentlyContinue
        $startupDir = [Environment]::GetFolderPath("Startup")
        $startupFile = Join-Path $startupDir "RadarSimConnector.cmd"
        $command = "@echo off`r`npowershell.exe $taskArgs`r`n"
        Set-Content -LiteralPath $startupFile -Value $command -Encoding ASCII
        $installConfig["startup_method"] = "startup_folder"
        $installConfig["startup_name"] = $startupFile
        $installConfig["watchdog_name"] = ""
        Write-Warn "Scheduled Task is blocked; registered the current-user Startup fallback."
    }
    Save-InstallConfig
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
    if ($RequestedMode -eq "unified") {
        Write-Ok "unified connector + Linux: central Web can schedule local simulation and Linux Cluster"
    } else {
        Write-Ok "full + linux: central Web can schedule Windows local simulation and Linux Cluster"
    }
} else {
    if ($RequestedMode -eq "unified") {
        Write-Ok "unified connector + local: local execution is available; Cluster requires the Linux service"
    } else {
        Write-Ok "full + local: offline Web/API, build and local simulation; no Cluster executor"
    }
}

Write-Step "5/5 Basic verification"
if (-not $SkipCheck) {
    Clear-ConnectorPythonCache
    $ConnectorRuntimeContract = Get-ConnectorRuntimeContract
    $policySource = Get-Content -Raw -Encoding UTF8 (Join-Path $RepoRoot "core\agent_policy.py")
    $policyMatch = [regex]::Match($policySource, '(?m)^WINDOWS_CONNECTOR_CONTRACT_VERSION\s*=\s*(\d+)\s*$')
    if (-not $policyMatch.Success) {
        Fail "The downloaded Connector package does not declare a task contract."
    }
    $ExpectedConnectorContract = [int]$policyMatch.Groups[1].Value
    if ($ConnectorRuntimeContract -ne $ExpectedConnectorContract) {
        Fail "The Connector runtime loaded contract $ConnectorRuntimeContract but the downloaded source requires $ExpectedConnectorContract. Old Python cache was not replaced."
    }
    Write-Ok "Connector task contract $ConnectorRuntimeContract was loaded from the downloaded runtime."
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
            if ($Owner) { $headers["X-Rsim-User"] = $Owner }
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
                    connector_contract_version = $ConnectorRuntimeContract
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
        else { Write-Ok "Unified Connector central registration check passed." }
    }
} else {
    Write-Warn "Remote connectivity verification skipped."
}

Write-Host "`nInstallation complete." -ForegroundColor Cyan
$PublicModeLabel = if ($RequestedMode -eq "unified") { "unified connector" } else { $RequestedMode }
Write-Host "Mode: $PublicModeLabel / control plane: $ControlPlane"
Write-Host "Visual Studio is user-managed; every build task validates and adapts the Selena script to the installed version."
Write-Host "Start: .\scripts\start_windows.ps1"
Write-Host "Background: .\scripts\start_windows.ps1 -Background"
if ($Mode -eq "light") {
    Write-Host "light has no local simulation. After upload, Linux continues Cluster scheduling without this PC."
} elseif ($ControlPlane -eq "linux") {
    if ($RequestedMode -eq "unified") {
        Write-Host "Unified connector + Linux Web: $ServerUrl/ (one entry for local or Cluster simulation)"
    } else {
        Write-Host "full + linux Web: $ServerUrl/ (one entry for local or Cluster simulation)"
    }
} else {
    if ($RequestedMode -eq "unified") {
        Write-Host "Unified connector + local Web: $ServerUrl/ (local execution; use Linux for Cluster)"
    } else {
        Write-Host "full + local Web: $ServerUrl/ (offline local only; use -ControlPlane linux for Cluster)"
    }
    Write-Host "Local loopback Web does not require an access token in this sprint."
}
if ($Start) {
    if ($RegisterStartup -and $installConfig["startup_method"] -eq "scheduled_task") {
        Start-ScheduledTask -TaskName ([string]$installConfig["startup_name"])
    } else {
        & $StartScript -InstallRoot $InstallRoot -Background -NoBrowser
    }
    $connectorPid = 0
    foreach ($attempt in 1..30) {
        $connectorPid = Get-ConnectorProcessId
        if ($connectorPid -gt 0) { break }
        # A just-replaced task can exit once if the old mutex is still being
        # released.  Retry the same registered task without user action.
        if (
            $RegisterStartup -and
            $installConfig["startup_method"] -eq "scheduled_task" -and
            ($attempt % 5) -eq 0
        ) {
            $task = Get-ScheduledTask -TaskName ([string]$installConfig["startup_name"]) -ErrorAction SilentlyContinue
            if ($task -and [string]$task.State -ne "Running") {
                Start-ScheduledTask -TaskName ([string]$installConfig["startup_name"])
            }
        }
        Start-Sleep -Seconds 1
    }
    if ($connectorPid -le 0) {
        Fail "The background connector did not stay running. Re-run this installer or contact the service administrator."
    }
    Write-Ok "The connector is running (PID $connectorPid)."
    if ($RegisterStartup -and $installConfig["startup_method"] -eq "scheduled_task") {
        $watchdog = Get-ScheduledTask -TaskName ([string]$installConfig["watchdog_name"]) -ErrorAction SilentlyContinue
        if (-not $watchdog) {
            Fail "The background watchdog was not registered. Re-run this installer or contact the service administrator."
        }
        Write-Ok "The independent reconnect watchdog is registered."
    }
    if (-not $UseLocalControl) {
        $capabilityHeaders = @{}
        if ($Owner) { $capabilityHeaders["X-Rsim-User"] = $Owner }
        $connected = $false
        $connectionReason = "not_registered"
        $encodedAgentId = [Uri]::EscapeDataString([string]$AgentId)
        foreach ($attempt in 1..30) {
            try {
                $snapshot = Invoke-RestMethod -Method Get `
                    -Uri "$ServerUrl/api/v1/windows-connector/status?agent_id=$encodedAgentId" `
                    -Headers $capabilityHeaders -TimeoutSec 5
                $connectionReason = [string]$snapshot.reason
                if ([bool]$snapshot.available -and [bool]$snapshot.contract_current) {
                    $connected = $true
                    break
                }
            } catch { }
            Start-Sleep -Seconds 1
        }
        if (-not $connected) {
            if ($connectionReason -eq "connector_owner_mismatch") {
                Fail "The connector process started, but this Windows profile is bound to a different Web/SDK identity. Reopen the Web with the original NTID or request an explicit rebind."
            }
            if ($connectionReason -eq "windows_connector_update_required") {
                Fail "The connector registered, but its task contract is outdated. Download the current one-click update from this Web service."
            }
            Fail "Linux did not confirm this exact PC within 30 seconds (state: $connectionReason). The installer preserved the local diagnostics for the service administrator."
        }
        Write-Ok "Linux confirmed this exact PC and user binding are available for task scheduling."
    }
}
