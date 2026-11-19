#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Build a Pico11 ISO from an official Windows 11 ISO using debloat_iso.py.

.PREREQUISITES
  - Windows 10/11, running as Administrator
  - Python 3.8+ on PATH (python --version)
  - Windows ADK "Deployment Tools" (provides oscdimg.exe)
    https://learn.microsoft.com/windows-hardware/get-started/adk-install
  - An official Windows 11 ISO from https://www.microsoft.com/software-download/windows11
  - About 20 GB of free disk space on the work drive (default work folder: C:\isowork)

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File build_pico11.ps1 -Iso .\Win11.iso -Out .\Pico11.iso
#>
param(
    [Parameter(Mandatory = $true)][string]$Iso,
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Locale = "en-GB",
    [string]$Label = "PICO11",
    [string]$HeliumSha256 = ""
)

$ErrorActionPreference = "Stop"

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# --- Prerequisite checks ---
if (-not (Test-Path $Iso)) { Fail "Input ISO not found: $Iso" }

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { Fail "Python not found on PATH. Install Python 3.8+ first." }

$oscdimg = Get-Command oscdimg -ErrorAction SilentlyContinue
if (-not $oscdimg) {
    $adkPath = "C:\Program Files (x86)\Windows Kits\10\Assessment and Deployment Kit\Deployment Tools\amd64\Oscdimg\oscdimg.exe"
    if (-not (Test-Path $adkPath)) { Fail "oscdimg not found. Install the Windows ADK (Deployment Tools)." }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$debloat = Join-Path $scriptDir "debloat_iso.py"
if (-not (Test-Path $debloat)) { Fail "debloat_iso.py not found next to this script." }

Write-Host "Building Pico11 from $Iso ..." -ForegroundColor Cyan
$extraArgs = @()
if ($HeliumSha256 -ne "") { $extraArgs += @("--helium-sha256", $HeliumSha256) }
python $debloat -i $Iso -o $Out --locale $Locale --label $Label @extraArgs
if ($LASTEXITCODE -ne 0) { Fail "debloat_iso.py exited with code $LASTEXITCODE" }

# --- Checksum for release notes / verification ---
$hash = (Get-FileHash $Out -Algorithm SHA256).Hash.ToLower()
Write-Host ""
Write-Host "Done: $Out" -ForegroundColor Green
Write-Host "SHA-256: $hash" -ForegroundColor Green
Write-Host "Publish this checksum with the ISO so users can verify their download."
