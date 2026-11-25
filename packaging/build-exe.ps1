# Build Windows Phonos standalone bundle (Phonos.exe --onedir) and optional
# Inno Setup UI Installer (Phonos-Setup-x64.exe).
#
# Usage:
#   .\packaging\build-exe.ps1
#   .\packaging\build-exe.ps1 -SkipInno
#   .\packaging\build-exe.ps1 -OnlyInno

param(
    [switch]$SkipInno,
    [switch]$OnlyInno
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$BundleDir = "$Root\packaging\dist\phonos"

Push-Location $Root
try {
    if (-not $OnlyInno) {
        Write-Host "==> 1/2 Installing Phonos + PyInstaller"
        python -m pip install --upgrade pip
        python -m pip install . "pyinstaller>=6.10"

        Write-Host "==> 2/2 Building Phonos Windows bundle (--onedir)"
        $env:PHONOS_PYINSTALLER_ONEDIR = "1"
        python -m PyInstaller `
            --noconfirm `
            --clean `
            --distpath "$Root\packaging\dist" `
            --workpath "$Root\packaging\build" `
            "$Root\packaging\phonos.spec"

        if (-not (Test-Path "$BundleDir\Phonos.exe")) {
            throw "Expected PyInstaller --onedir binary at $BundleDir\Phonos.exe"
        }

        @"
@echo off
"%~dp0Phonos.exe" --mode playground %*
"@ | Set-Content -Encoding ASCII "$BundleDir\Start-Phonos-Playground.bat"

        @"
@echo off
"%~dp0Phonos.exe" --mode production %*
"@ | Set-Content -Encoding ASCII "$BundleDir\Start-Phonos-Production.bat"
    }

    if (-not $SkipInno) {
        $IsccPath = $null
        $IsccCmd = Get-Command iscc -ErrorAction SilentlyContinue
        if ($IsccCmd) {
            $IsccPath = $IsccCmd.Source
        } elseif (Test-Path "C:\Program Files (x86)\Inno Setup 6\ISCC.exe") {
            $IsccPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
        }
        if ($IsccPath) {
            $VerLine = Select-String -Path "$Root\phonos_app\version.py" -Pattern 'APP_VERSION\s*=\s*"([^"]+)"'
            $AppVer = if ($VerLine) { $VerLine.Matches[0].Groups[1].Value } else { "1.0.0" }
            Write-Host "==> Building Windows UI Installer (Phonos-Setup-x64.exe) via Inno Setup ($AppVer)"
            & $IsccPath "/DAppVersion=$AppVer" "$Root\packaging\windows\phonos-setup.iss"
            if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
        } else {
            Write-Host "Note: Inno Setup (iscc) not found; skipped Phonos-Setup-x64.exe."
        }
    }
} finally {
    Pop-Location
}
