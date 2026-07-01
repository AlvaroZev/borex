# Borex backtest suite runner
# Logs results to tests/results.txt (plain text, appended per run)
#
# Usage:
#   powershell -File tests/run_suite.ps1
#   powershell -File tests/run_suite.ps1 -Execute
#   powershell -File tests/run_suite.ps1 -Execute -Suite A
#   powershell -File tests/run_suite.ps1 -Execute -From BT-050 -To BT-060

param(
    [switch]$Execute,
    [switch]$UseCache,
    [string]$Suite = "",
    [string]$From = "",
    [string]$To = "",
    [string]$ResultsFile = "",
    [string]$CommandsFile = ""
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $CommandsFile) { $CommandsFile = Join-Path $Root "tests\commands.txt" }
if (-not $ResultsFile) { $ResultsFile = Join-Path $Root "tests\results.txt" }

Set-Location $Root

function Write-LogLine {
    param([string]$Line)
    Add-Content -Path $ResultsFile -Value $Line -Encoding UTF8
    Write-Host $Line
}

function Parse-SummaryMetrics {
    param([string[]]$OutputLines)
    $m = @{
        Strategy = ""
        Symbol = ""
        Trades = ""
        Return = ""
        MaxDD = ""
        WinRate = ""
        FinalEquity = ""
    }
    foreach ($line in $OutputLines) {
        if ($line -match '^Estrategia:\s*(.+)') { $m.Strategy = $Matches[1].Trim() }
        if ($line -match '^S.mbolo:\s*(.+)') { $m.Symbol = $Matches[1].Trim() }
        if ($line -match '^Capital final:\s*') { $m.FinalEquity = ($line -replace '^Capital final:\s*', '').Trim() }
        if ($line -match '^Retorno total:\s*(.+)') { $m.Return = $Matches[1].Trim() }
        if ($line -match '^Max drawdown:\s*(.+)') { $m.MaxDD = $Matches[1].Trim() }
        if ($line -match '^Trades:\s*(\d+)') { $m.Trades = $Matches[1].Trim() }
        if ($line -match '^Win rate:\s*(.+)') { $m.WinRate = $Matches[1].Trim() }
    }
    return $m
}

function Test-InIdRange {
    param([string]$Id, [string]$FromId, [string]$ToId)
    if ((-not $FromId) -and (-not $ToId)) { return $true }
    $num = [int]($Id -replace 'BT-', '')
    if ($FromId) {
        $fromNum = [int]($FromId -replace 'BT-', '')
        if ($num -lt $fromNum) { return $false }
    }
    if ($ToId) {
        $toNum = [int]($ToId -replace 'BT-', '')
        if ($num -gt $toNum) { return $false }
    }
    return $true
}

function Get-TestStatus {
    param([int]$ExitCode, [string]$Expect, [string]$Output)
    $dataError = $Output -match 'Error cargando datos|No se encontraron datos|Datos insuficientes'
    if ($Expect -eq 'fail') {
        if ($ExitCode -ne 0) { return 'PASS' }
        return 'FAIL'
    }
    if ($Expect -eq 'skip_ok') {
        if ($ExitCode -eq 0) { return 'PASS' }
        if ($dataError) { return 'SKIP' }
        return 'FAIL'
    }
    if ($ExitCode -eq 0) { return 'PASS' }
    if ($dataError) { return 'SKIP' }
    return 'FAIL'
}

if (-not (Test-Path $CommandsFile)) {
    Write-Error "Commands file not found: $CommandsFile"
    exit 1
}

$tests = New-Object System.Collections.Generic.List[object]
Get-Content $CommandsFile -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if ((-not $line) -or $line.StartsWith('#')) { return }
    $parts = $line -split '\|', 5
    if ($parts.Count -lt 5) { return }
    $tests.Add([PSCustomObject]@{
        Id = $parts[0].Trim()
        Suite = $parts[1].Trim()
        Command = $parts[2].Trim()
        Description = $parts[3].Trim()
        Expect = $parts[4].Trim()
    })
}

$filtered = @($tests | Where-Object {
    (($Suite -eq '') -or ($_.Suite -eq $Suite)) -and (Test-InIdRange -Id $_.Id -FromId $From -ToId $To)
})

$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$modeLabel = 'EXECUTE'
if (-not $Execute) { $modeLabel = 'DRY-RUN' }

Write-Host ""
Write-Host "Borex Test Suite - $modeLabel"
Write-Host "Project:  $Root"
Write-Host "Commands: $CommandsFile"
Write-Host "Results:  $ResultsFile"
Write-Host "Tests:    $($filtered.Count) / $($tests.Count)"
Write-Host ""

$passCount = 0
$failCount = 0
$skipCount = 0
$dryCount = 0

if ($Execute) {
    Write-LogLine ""
    Write-LogLine "================================================================================"
    Write-LogLine "BOREX BACKTEST SUITE - $timestamp - mode: $modeLabel"
    Write-LogLine "Tests selected: $($filtered.Count)"
    Write-LogLine "================================================================================"
}

if ($Execute -and $UseCache) {
    Write-Host "Cache mode: ON (--use-cache appended to each command)"
    Write-Host "Ensure data exists: python download_cache.py --suite"
    Write-Host ""
}

foreach ($t in $filtered) {
    $label = $t.Id + ' [' + $t.Suite + '] ' + $t.Description
    $runCommand = $t.Command
    if ($UseCache -and ($runCommand -notmatch '--csv') -and ($runCommand -notmatch '--use-cache')) {
        $runCommand = $runCommand + ' --use-cache'
    }

    if (-not $Execute) {
        Write-Host "DRY  $label"
        Write-Host "     $runCommand"
        Write-Host "     expect: $($t.Expect)"
        Write-Host ""
        $dryCount++
        continue
    }

    Write-Host "Running $($t.Id)..."
    $sw = [System.Diagnostics.Stopwatch]::StartNew()

    Push-Location $Root
    $output = Invoke-Expression ($runCommand + ' 2>&1') | Out-String
    $exitCode = $LASTEXITCODE
    Pop-Location

    if ($null -eq $exitCode) { $exitCode = 0 }

    $sw.Stop()
    $seconds = [math]::Round($sw.Elapsed.TotalSeconds, 2)
    $status = Get-TestStatus -ExitCode $exitCode -Expect $t.Expect -Output $output

    if ($status -eq 'PASS') { $passCount++ }
    elseif ($status -eq 'FAIL') { $failCount++ }
    else { $skipCount++ }

    $lines = @()
    if ($output) { $lines = $output -split "`n" }
    $metrics = Parse-SummaryMetrics -OutputLines $lines

    Write-LogLine ""
    Write-LogLine "--------------------------------------------------------------------------------"
    Write-LogLine "ID:          $($t.Id)"
    Write-LogLine "Suite:       $($t.Suite)"
    Write-LogLine "Status:      $status"
    Write-LogLine "Expect:      $($t.Expect)"
    Write-LogLine "Exit code:   $exitCode"
    Write-LogLine "Duration:    ${seconds}s"
    Write-LogLine "Description: $($t.Description)"
    Write-LogLine "Command:     $runCommand"
    if ($metrics.Strategy) { Write-LogLine "Strategy:    $($metrics.Strategy)" }
    if ($metrics.Symbol) { Write-LogLine "Symbol:      $($metrics.Symbol)" }
    if ($metrics.Trades) { Write-LogLine "Trades:      $($metrics.Trades)" }
    if ($metrics.Return) { Write-LogLine "Return:      $($metrics.Return)" }
    if ($metrics.MaxDD) { Write-LogLine "Max DD:      $($metrics.MaxDD)" }
    if ($metrics.WinRate) { Write-LogLine "Win rate:    $($metrics.WinRate)" }
    if ($metrics.FinalEquity) { Write-LogLine "Final eq:    $($metrics.FinalEquity)" }

    if ($status -eq 'FAIL') {
        Write-LogLine "--- output (last 30 lines) ---"
        $tail = $lines | Select-Object -Last 30
        foreach ($ln in $tail) { Write-LogLine $ln }
    }
}

Write-Host ""
if ($Execute) {
    Write-LogLine ""
    Write-LogLine "================================================================================"
    Write-LogLine "SUMMARY - $timestamp"
    Write-LogLine "PASS: $passCount  FAIL: $failCount  SKIP: $skipCount  TOTAL: $($filtered.Count)"
    Write-LogLine "================================================================================"
    Write-Host "Done. PASS=$passCount FAIL=$failCount SKIP=$skipCount"
    Write-Host "Results written to: $ResultsFile"
    if ($failCount -gt 0) { exit 1 }
}
else {
    Write-Host "Dry-run complete. $dryCount commands listed."
    Write-Host "Run with -Execute to execute and log to tests/results.txt"
}
