# Phonos One-Time Setup Script (Windows PowerShell)
#
# Usage:
#   .\scripts\setup.ps1
#   .\scripts\setup.ps1 -AdminUser admin -AdminPassword "MyStrongPassword"

param(
    [ValidateSet("both", "playground", "production")]
    [string]$Mode = "both",
    [string]$AdminUser = "",
    [string]$AdminPassword = "",
    [string]$LabName = "Phonos Lab",
    [switch]$SkipDocker
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "===================================================================="
Write-Host "  Phonos - One-Time Setup (Playground :5040 + Production Lab :5041)"
Write-Host "===================================================================="

function Refresh-EnvPath {
    $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($machinePath) { $parts += $machinePath }
    if ($userPath) { $parts += $userPath }
    foreach ($p in @(
        "$env:LOCALAPPDATA\Programs\Python\Python312",
        "$env:LOCALAPPDATA\Programs\Python\Python312\Scripts",
        "$env:ProgramFiles\Python312",
        "$env:ProgramFiles\Python312\Scripts"
    )) {
        if ((Test-Path $p) -and ($parts -notcontains $p)) { $parts += $p }
    }
    $env:Path = ($parts -join ";")
}

$DepsScript = Join-Path $Root "packaging\windows\install-deps.ps1"
$NeedPython = -not (Get-Command python -ErrorAction SilentlyContinue)
if (-not $NeedPython) {
    try {
        $pyVer = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -ne 0 -or $pyVer -notmatch '^3\.(1[1-9]|[2-9]\d)$') {
            $NeedPython = $true
        }
    } catch {
        $NeedPython = $true
    }
}

if ($NeedPython -and (Test-Path $DepsScript)) {
    Write-Host "[0/3] Installing Python 3.12 via official Microsoft winget..."
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $DepsScript -InstallPython
    Refresh-EnvPath
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3.11+ is required. Install from https://www.python.org/downloads/windows/"
}

Write-Host "[1/3] Setting up Python virtual environment (.venv) & installing Phonos..."
if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .\.venv\Scripts\python.exe -m pip install -e . --quiet

Write-Host "[2/3] Initializing isolated Playground sandbox (:5040)..."
& .\.venv\Scripts\phonos.exe setup --mode playground

if ($Mode -eq "production" -or $Mode -eq "both") {
    Write-Host "[3/3] Initializing Production lab environment (:5041)..."
    if ($AdminUser -and $AdminPassword) {
        & .\.venv\Scripts\phonos.exe setup --mode production --lab-name $LabName --admin-user $AdminUser --admin-password $AdminPassword
    } else {
        & .\.venv\Scripts\phonos.exe setup --mode production --lab-name $LabName
    }
}

Write-Host ""
Write-Host "Setup complete! Start Phonos on Windows with:"
Write-Host "  Playground (:5040):  .\.venv\Scripts\phonos.exe playground"
Write-Host "  Production (:5041):  .\.venv\Scripts\phonos.exe production"
Write-Host "  Manage Accounts:     .\.venv\Scripts\phonos.exe accounts list"
Write-Host "  Check Updates:       .\.venv\Scripts\phonos.exe update --check"
