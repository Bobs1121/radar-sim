<#
.SYNOPSIS
    radar-sim one-click deploy (Mode B: Windows local repo, full local+cluster).

.DESCRIPTION
    Runs the post-clone setup that takes a fresh checkout of this repo on a
    Windows machine to a working rsim install:

      1. Preflight  : Python 3.9+ present, repo layout sane.
      2. venv       : create .venv if missing, upgrade pip.
      3. Deps       : pip install -r requirements.txt + pip install -e .
                     (if third_party/python-wheels/ has wheels, install offline)
      4. local.yaml : rsim config init (copy template) if local.yaml missing.
      5. Diagnostics: rsim doctor (system-level) + rsim check (config-level).

    Does NOT auto-install VS/MATLAB/Qt/Boost (too heavy / license-bound) —
    doctor reports what's missing and points at docs/environment-setup.md.

    Re-runnable: each step is idempotent (skips work already done).

.PARAMETER Project
    Project name under config/projects/ to configure (default: ovrs25).

.PARAMETER SkipDeps
    Skip the pip install step (use if deps already installed).

.PARAMETER SkipCheck
    Skip the final doctor/check step.

.EXAMPLE
    .\scripts\bootstrap.ps1 -Project ovrs25
#>

[CmdletBinding()]
param(
    [string]$Project = "ovrs25",
    [switch]$SkipDeps,
    [switch]$SkipCheck
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    OK  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    WARN $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "    ERR  $msg" -ForegroundColor Red }

Set-Location $RepoRoot

# --------------------------------------------------------------------------
# Step 1: Preflight — Python 3.9+
# --------------------------------------------------------------------------
Write-Step "Step 1/5: preflight checks"

$pyExe = $null
foreach ($candidate in @("python", "py")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $ver -match "Python (\d+)\.(\d+)") {
            $maj = [int]$Matches[1]; $min = [int]$Matches[2]
            if ($maj -ge 3 -and $min -ge 9) {
                $pyExe = $candidate
                Write-Ok "Python $maj.$min found ($candidate)"
                break
            }
        }
    } catch { }
}
if (-not $pyExe) {
    Write-Err "Python 3.9+ not found on PATH."
    Write-Err "Install Python 3.9+ from https://www.python.org/ and re-run."
    exit 1
}

$projectDir = Join-Path $RepoRoot "config/projects/$Project"
if (-not (Test-Path $projectDir)) {
    Write-Err "Project dir not found: $projectDir"
    Write-Err "Available: $((Get-ChildItem (Join-Path $RepoRoot 'config/projects') -Directory).Name -join ', ')"
    exit 1
}
Write-Ok "Project '$Project' config present"

# --------------------------------------------------------------------------
# Step 2: venv
# --------------------------------------------------------------------------
Write-Step "Step 2/5: virtual environment (.venv)"

$venvDir = Join-Path $RepoRoot ".venv"
if (Test-Path (Join-Path $venvDir "Scripts/python.exe")) {
    Write-Ok ".venv already exists, reusing"
} else {
    & $pyExe -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Err "venv creation failed"; exit 1 }
    Write-Ok ".venv created"
}

$venvPy = Join-Path $venvDir "Scripts/python.exe"
& $venvPy -m pip install --upgrade pip --quiet
Write-Ok "pip upgraded"

# --------------------------------------------------------------------------
# Step 3: dependencies
# --------------------------------------------------------------------------
if (-not $SkipDeps) {
    Write-Step "Step 3/5: install Python dependencies"

    $wheelsDir = Join-Path $RepoRoot "third_party/python-wheels"
    if ((Test-Path $wheelsDir) -and ((Get-ChildItem $wheelsDir -Filter *.whl -ErrorAction SilentlyContinue).Count -gt 0)) {
        Write-Ok "Using offline wheels from third_party/python-wheels/"
        & $venvPy -m pip install --no-index --find-links $wheelsDir -r requirements.txt
        if ($LASTEXITCODE -ne 0) { Write-Err "offline pip install failed"; exit 1 }
    } else {
        & $venvPy -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "pip install -r requirements.txt failed (asammdf C-extension needs a compiler or prebuilt wheel)."
            Write-Warn "Try: pip install asammdf separately, or prepare third_party/python-wheels/ (see docs/environment-setup.md)."
        }
    }

    & $venvPy -m pip install -e .
    if ($LASTEXITCODE -ne 0) { Write-Err "pip install -e . failed"; exit 1 }
    Write-Ok "rsim installed in editable mode"
} else {
    Write-Step "Step 3/5: install Python dependencies (skipped)"
}

# --------------------------------------------------------------------------
# Step 4: local.yaml
# --------------------------------------------------------------------------
Write-Step "Step 4/5: local config (local.yaml)"

$localYaml = Join-Path $projectDir "local.yaml"
if (Test-Path $localYaml) {
    Write-Ok "local.yaml already exists, leaving as-is"
} else {
    & $venvPy rsim.py config init --project $Project 2>$null
    if (-not (Test-Path $localYaml)) {
        # Fallback: copy the example template directly.
        $example = Join-Path $projectDir "local.example.yaml"
        if (Test-Path $example) { Copy-Item $example $localYaml }
    }
    if (Test-Path $localYaml) {
        Write-Ok "local.yaml created from template"
        Write-Warn "Edit $localYaml environment.* paths for your machine, then re-run to verify."
    } else {
        Write-Warn "Could not create local.yaml; create it manually from local.example.yaml"
    }
}

# --------------------------------------------------------------------------
# Step 5: diagnostics
# --------------------------------------------------------------------------
if (-not $SkipCheck) {
    Write-Step "Step 5/5: diagnostics (rsim doctor + rsim check)"

    Write-Host "`n  --- rsim doctor (system-level) ---" -ForegroundColor DarkGray
    & $venvPy rsim.py --project $Project doctor
    $doctorRc = $LASTEXITCODE

    Write-Host "`n  --- rsim check (config-level) ---" -ForegroundColor DarkGray
    & $venvPy rsim.py --project $Project check
    $checkRc = $LASTEXITCODE

    Write-Host ""
    if ($doctorRc -eq 0 -and $checkRc -eq 0) {
        Write-Ok "All diagnostics passed. rsim is ready."
    } else {
        Write-Warn "Some diagnostics reported issues (see above)."
        Write-Warn "Fix environment paths in local.yaml and re-run: .\scripts\bootstrap.ps1 -Project $Project -SkipDeps"
        Write-Warn "Full guide: docs/environment-setup.md"
    }
} else {
    Write-Step "Step 5/5: diagnostics (skipped)"
}

Write-Host "`nBootstrap complete.`n" -ForegroundColor Cyan
Write-Host "Next steps:"
Write-Host "  - Activate venv:  .\.venv\Scripts\Activate.ps1"
Write-Host "  - Build selena:   rsim --project $Project build selena"
Write-Host "  - Run sim:        rsim --project $Project run <input.mf4>"
Write-Host "  - Cluster run:    rsim --project $Project cluster run --dataset <name>"
Write-Host "  - Web console:    rsim --project $Project web"
