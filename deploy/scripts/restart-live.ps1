#Requires -Version 5.1
<#
.SYNOPSIS
  Restart alexg5 live after Drone deploys the ci-cd branch.

.DESCRIPTION
  Modes (env BOREX_DEPLOY_MODE):
    native (default) — run mt5service.py with local Windows Python + MT5 terminal
    docker           — docker compose pull/up using the image built by Drone

  Required on deploy host:
    - Git checkout of this repo on branch ci-cd at $DEPLOY_PATH
    - deploy/borex_live/.env with DATABASE_URL + MT5_* (native/live)
    - For native: Python 3.11 venv at deploy/borex_live/.venv311 (or $BOREX_PYTHON)
    - For docker: Docker Engine + compose plugin, image pull credentials if private
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$LiveRoot = Join-Path $RepoRoot "deploy\borex_live"
$ComposeFile = Join-Path $RepoRoot "deploy\docker-compose.yml"
$Mode = if ($env:BOREX_DEPLOY_MODE) { $env:BOREX_DEPLOY_MODE.ToLowerInvariant() } else { "native" }
$LogDir = Join-Path $LiveRoot "logs"
$PidFile = Join-Path $LogDir "alexg5-live.pid"
$OutLog = Join-Path $LogDir "alexg5-live.out.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Stop-NativeService {
    if (Test-Path $PidFile) {
        $oldPid = Get-Content $PidFile -ErrorAction SilentlyContinue
        if ($oldPid) {
            $proc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "Stopping native alexg5-live PID $oldPid"
                Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 2
            }
        }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match "mt5service\.py" -and $_.CommandLine -match "alexg5" } |
        ForEach-Object {
            Write-Host "Stopping leftover mt5service.py PID $($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

function Start-NativeService {
    $envFile = Join-Path $LiveRoot ".env"
    if (-not (Test-Path $envFile)) {
        throw "Missing $envFile — copy from .env.example and fill MT5 + DATABASE_URL"
    }

    $python = $env:BOREX_PYTHON
    if (-not $python) {
        $candidates = @(
            (Join-Path $LiveRoot ".venv311\Scripts\python.exe"),
            (Join-Path $LiveRoot ".venv\Scripts\python.exe")
        )
        foreach ($c in $candidates) {
            if (Test-Path $c) { $python = $c; break }
        }
    }
    if (-not $python -or -not (Test-Path $python)) {
        throw "Python 3.11 not found. Create deploy/borex_live/.venv311 or set BOREX_PYTHON"
    }

    $env:BOREX_MAIN_ROOT = "$RepoRoot"
    $argList = @(
        "mt5service.py",
        "--demo",
        "--strategy", "alexg5",
        "--leverage", "5000",
        "--rr-factor", "2.5",
        "--host", "127.0.0.1",
        "--port", "8790"
    )

    $ErrLog = Join-Path $LogDir "alexg5-live.err.log"
    Write-Host "Starting native alexg5-live with $python"
    $proc = Start-Process -FilePath $python `
        -ArgumentList $argList `
        -WorkingDirectory $LiveRoot `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog `
        -PassThru `
        -WindowStyle Hidden

    Set-Content -Path $PidFile -Value $proc.Id -Encoding ascii
    Write-Host "alexg5-live started PID $($proc.Id) — logs: $OutLog / $ErrLog"
}

function Restart-DockerService {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "docker not found — install Docker or use BOREX_DEPLOY_MODE=native"
    }
    if (-not $env:BOREX_DOCKER_REPO) {
        Write-Warning "BOREX_DOCKER_REPO unset; compose may use placeholder image name"
    }
    Push-Location (Join-Path $RepoRoot "deploy")
    try {
        docker compose -f $ComposeFile pull
        docker compose -f $ComposeFile up -d --force-recreate
        docker compose -f $ComposeFile ps
    }
    finally {
        Pop-Location
    }
}

Write-Host "Repo: $RepoRoot"
Write-Host "Mode: $Mode"

switch ($Mode) {
    "docker" { Restart-DockerService }
    "native" {
        Stop-NativeService
        Start-NativeService
    }
    default { throw "Unknown BOREX_DEPLOY_MODE=$Mode (use native|docker)" }
}

Write-Host "restart-live.ps1 done"
