# Phonos Windows Dependency Helper (Python 3.12+ & Optional Docker Desktop)
# Uses Microsoft's official winget package manager only (avoids unsigned %TEMP% EXE downloads).

param(
    [switch]$InstallPython,
    [switch]$InstallDocker
)

$ErrorActionPreference = "Continue"
$LogDir = Join-Path $env:LOCALAPPDATA "Phonos\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "install-deps.log"

function Write-Log([string]$Message) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    "$ts  $Message" | Out-File -FilePath $LogFile -Append -Encoding utf8
    Write-Host $Message
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Test-Python311Plus {
    foreach ($candidate in @("python", "python3", "py")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $argsList = if ($candidate -eq "py") { @("-3", "--version") } else { @("--version") }
            $out = & $cmd.Source @argsList 2>&1 | Out-String
            if ($out -match "Python\s+(\d+)\.(\d+)") {
                $maj = [int]$Matches[1]; $min = [int]$Matches[2]
                if ($maj -gt 3 -or ($maj -eq 3 -and $min -ge 11)) {
                    Write-Log "Python already installed: $($out.Trim())"
                    return $true
                }
            }
        } catch {}
    }
    return $false
}

function Test-DockerInstalled {
    if (Get-Command docker -ErrorAction SilentlyContinue) { return $true }
    if (Test-Path "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe") { return $true }
    return $false
}

$Winget = Get-Command winget -ErrorAction SilentlyContinue

if ($InstallPython -and -not (Test-Python311Plus)) {
    if ($Winget) {
        Write-Log "Installing Python 3.12 via winget..."
        & $Winget.Source install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements --scope user | Out-Null
        if ($LASTEXITCODE -ne 0) {
            & $Winget.Source install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
        }
        Refresh-ProcessPath
    } else {
        Write-Log "winget not found. Please install Python 3.12+ from https://www.python.org/downloads/windows/"
    }
}

if ($InstallDocker -and -not (Test-DockerInstalled)) {
    if ($Winget) {
        Write-Log "Installing Docker Desktop via winget..."
        & $Winget.Source install --id Docker.DockerDesktop -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
        Refresh-ProcessPath
    } else {
        Write-Log "winget not found. Please install Docker Desktop from https://www.docker.com/products/docker-desktop/"
    }
}

exit 0
