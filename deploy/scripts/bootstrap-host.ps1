#Requires -Version 5.1
<#
.SYNOPSIS
  One-time bootstrap of the Windows MT5 deploy host for alexg7 (ci-cd branch).

.EXAMPLE
  # From an empty folder, or pass -RepoDir:
  .\bootstrap-host.ps1 -RepoDir C:\borex -RepoUrl https://github.com/AlvaroZev/borex.git

  Then edit deploy\borex_live\.env and run:
  .\deploy\scripts\restart-live.ps1
#>

param(
    [string]$RepoDir = "C:\borex",
    [string]$RepoUrl = "https://github.com/AlvaroZev/borex.git",
    [string]$Branch = "ci-cd",
    [string]$PythonLauncher = "py"
)

$ErrorActionPreference = "Stop"

function Assert-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing command: $Name"
    }
}

Write-Host "=== Borex deploy host bootstrap ==="
Write-Host "RepoDir=$RepoDir Branch=$Branch"

Assert-Command git
Assert-Command $PythonLauncher

if (-not (Test-Path (Join-Path $RepoDir ".git"))) {
    $parent = Split-Path -Parent $RepoDir
    if ($parent -and -not (Test-Path $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    Write-Host "Cloning $RepoUrl -> $RepoDir"
    git clone --branch $Branch --single-branch $RepoUrl $RepoDir
}
else {
    Write-Host "Repo exists; fetching $Branch"
    Push-Location $RepoDir
    try {
        git fetch origin $Branch
        git checkout $Branch
        git pull --ff-only origin $Branch
    }
    finally {
        Pop-Location
    }
}

$LiveRoot = Join-Path $RepoDir "deploy\borex_live"
$ReqLive = Join-Path $LiveRoot "requirements.txt"
$ReqRoot = Join-Path $RepoDir "requirements.txt"
$VenvPython = Join-Path $LiveRoot ".venv311\Scripts\python.exe"
$EnvExample = Join-Path $LiveRoot ".env.example"
$EnvFile = Join-Path $LiveRoot ".env"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating Python 3.11 venv at deploy\borex_live\.venv311"
    Push-Location $LiveRoot
    try {
        & $PythonLauncher -3.11 -m venv .venv311
    }
    finally {
        Pop-Location
    }
}

Write-Host "Installing dependencies"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r $ReqLive
if (Test-Path $ReqRoot) {
    & $VenvPython -m pip install -r $ReqRoot
}

if (-not (Test-Path $EnvFile)) {
    Copy-Item $EnvExample $EnvFile
    Write-Host "Created $EnvFile — fill DATABASE_URL + MT5_* before starting"
}
else {
    Write-Host ".env already present"
}

Write-Host ""
Write-Host "Next:"
Write-Host "  1. Edit $EnvFile"
Write-Host "  2. Open MT5, enable Algo Trading, log into demo"
Write-Host "  3. Run:  cd $RepoDir ; .\deploy\scripts\restart-live.ps1"
Write-Host "  4. Point Drone secret deploy_path at: $RepoDir"
Write-Host "  5. Ensure OpenSSH Server is running so Drone can SSH in"
Write-Host "Done."
