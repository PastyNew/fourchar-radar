<#
.SYNOPSIS
    Bootstrap fourchar-radar on Windows.

.DESCRIPTION
    Creates a virtualenv (no admin rights, no conflict with other packages),
    installs the radar, and puts a fourchar-radar.cmd launcher on your PATH.

    Run from PowerShell:
        irm https://raw.githubusercontent.com/PastyNew/fourchar-radar/main/install.ps1 | iex

    Or, from a checkout:
        powershell -ExecutionPolicy Bypass -File install.ps1

.EXAMPLE
    fourchar-radar run https://discord.com/api/webhooks/123/abc
#>

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Info($msg) { Write-Host "    $msg" -ForegroundColor DarkGray }
function Die($msg) {
    Write-Host "error: $msg" -ForegroundColor Red
    exit 1
}

# --------------------------------------------------------------- python
function Find-Python {
    # The py launcher is the reliable entry point on Windows; fall back to
    # whatever python is on PATH, then to the version-specific names.
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $candidates += @("py -3.13", "py -3.12", "py -3.11", "py -3")
    }
    foreach ($name in @("python3.13", "python3.12", "python3.11", "python3", "python")) {
        if (Get-Command $name -ErrorAction SilentlyContinue) { $candidates += $name }
    }

    foreach ($candidate in $candidates) {
        $parts = $candidate.Split(" ")
        $exe = $parts[0]
        $argList = @()
        if ($parts.Count -gt 1) { $argList += $parts[1] }
        $argList += @("-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)")

        try {
            & $exe @argList 2>$null
            if ($LASTEXITCODE -eq 0) { return $exe }
        } catch { continue }
    }
    return $null
}

Write-Step "Looking for Python 3.11+"
$python = Find-Python
if (-not $python) {
    Die @"
Python 3.11 or newer is required.

  winget install Python.Python.3.12
  or download from https://www.python.org/downloads/

During install, tick 'Add python.exe to PATH'.
"@
}
Write-Info "using $python"

# ------------------------------------------------------------------ venv
$home_ = if ($env:FOURCHAR_RADAR_HOME) { $env:FOURCHAR_RADAR_HOME }
         else { Join-Path $env:LOCALAPPDATA "fourchar-radar" }
$venv = Join-Path $home_ "venv"
$venvScripts = Join-Path $venv "Scripts"
$exe = Join-Path $venvScripts "fourchar-radar.exe"

Write-Step "Creating a virtualenv in $venv"
New-Item -ItemType Directory -Force -Path $home_ | Out-Null
& $python -m venv $venv
if ($LASTEXITCODE -ne 0) { Die "venv creation failed. Re-run with: $python -m venv $venv" }

$venvPython = Join-Path $venvScripts "python.exe"
if (-not (Test-Path $venvPython)) { Die "virtualenv is missing its interpreter: $venvPython" }

# ------------------------------------------------------------------ pip
Write-Step "Installing fourchar-radar"
& $venvPython -m pip install --quiet --upgrade pip
$source = ""
if (Test-Path "pyproject.toml") { $source = "."; Write-Info "installing from this directory" }

& $venvPython -m pip install --quiet $(if ($source) { $source } else { "fourchar-radar" })
if ($LASTEXITCODE -ne 0) { Die "install failed" }

# ------------------------------------------------------------------ link
Write-Step "Adding the launcher to your PATH"
$binDir = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps"
if (-not (Test-Path $binDir)) { $binDir = $home_ }
$cmdPath = Join-Path $binDir "fourchar-radar.cmd"
"@echo off`r`n`"$venvScripts\fourchar-radar.exe`" %*" | Set-Content -Path $cmdPath -Encoding ASCII

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$binDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$binDir", "User")
    Write-Info "added to your user PATH — open a new terminal for it to take effect"
}

Write-Host ""
Write-Host "fourchar-radar installed." -ForegroundColor Green
Write-Host ""
Write-Host "  database:  $venv\..\radar.db" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Start it with your webhook URL - no config file needed:"
Write-Host ""
Write-Host "    fourchar-radar run https://discord.com/api/webhooks/ID/TOKEN" -ForegroundColor White
Write-Host ""
Write-Host "Get that URL in Discord: channel -> Edit Channel -> Integrations ->"
Write-Host "Webhooks -> New Webhook -> Copy Webhook URL."
Write-Host ""
Write-Host "Try it without alerts first:  fourchar-radar run --dry-run --limit 20" -ForegroundColor White
Write-Host ""
