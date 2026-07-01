# Parameter sweep runner — compares returns across configs
#
# Usage:
#   powershell -File tests/run_param_sweep.ps1
#   powershell -File tests/run_param_sweep.ps1 -Execute
#   powershell -File tests/run_param_sweep.ps1 -Execute -UseCache
#   powershell -File tests/run_param_sweep.ps1 -Execute -UseCache -Group sl_tp

param(
    [switch]$Execute,
    [switch]$UseCache,
    [string]$Group = "",
    [string]$CommandsFile = "",
    [string]$ResultsFile = "",
    [string]$CsvFile = ""
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $CommandsFile) { $CommandsFile = Join-Path $Root "tests\param_commands.txt" }
if (-not $ResultsFile) { $ResultsFile = Join-Path $Root "tests\param_results.txt" }
if (-not $CsvFile) { $CsvFile = Join-Path $Root "tests\param_results.csv" }

Set-Location $Root

function Parse-ReturnPct {
    param([string]$Text)
    if (-not $Text) { return $null }
    $clean = $Text -replace '%', '' -replace ',', ''
    $val = 0.0
    if ([double]::TryParse($clean, [ref]$val)) { return $val }
    return $null
}

function Parse-SummaryMetrics {
    param([string[]]$OutputLines)
    $m = [ordered]@{
        Strategy = ""
        Symbol = ""
        Trades = ""
        Return = ""
        ReturnPct = $null
        MaxDD = ""
        WinRate = ""
        FinalEquity = ""
    }
    foreach ($line in $OutputLines) {
        if ($line -match '^Estrategia:\s*(.+)') { $m.Strategy = $Matches[1].Trim() }
        if ($line -match '^S.mbolo:\s*(.+)') { $m.Symbol = $Matches[1].Trim() }
        if ($line -match '^Capital final:\s*') { $m.FinalEquity = ($line -replace '^Capital final:\s*', '').Trim() }
        if ($line -match '^Retorno total:\s*(.+)') {
            $m.Return = $Matches[1].Trim()
            $m.ReturnPct = Parse-ReturnPct $m.Return
        }
        if ($line -match '^Max drawdown:\s*(.+)') { $m.MaxDD = $Matches[1].Trim() }
        if ($line -match '^Trades:\s*(\d+)') { $m.Trades = $Matches[1].Trim() }
        if ($line -match '^Win rate:\s*(.+)') { $m.WinRate = $Matches[1].Trim() }
    }
    return $m
}

$runs = New-Object System.Collections.Generic.List[object]
Get-Content $CommandsFile -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if ((-not $line) -or $line.StartsWith('#')) { return }
    $parts = $line -split '\|', 4
    if ($parts.Count -lt 4) { return }
    $runs.Add([PSCustomObject]@{
        Id = $parts[0].Trim()
        Group = $parts[1].Trim()
        Command = $parts[2].Trim()
        Label = $parts[3].Trim()
    })
}

$filtered = @($runs | Where-Object { ($Group -eq '') -or ($_.Group -eq $Group) })
$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

Write-Host ""
Write-Host "Borex Parameter Sweep"
Write-Host "Commands: $($filtered.Count) runs"
Write-Host "Results:  $ResultsFile"
Write-Host "CSV:      $CsvFile"
Write-Host ""

if (-not $Execute) {
    foreach ($r in $filtered) {
        $cmd = $r.Command
        if ($UseCache -and ($cmd -notmatch '--use-cache')) { $cmd = "$cmd --use-cache" }
        Write-Host "$($r.Id) [$($r.Group)] $($r.Label)"
        Write-Host "  $cmd"
    }
    Write-Host ""
    Write-Host "Dry-run. Use -Execute -UseCache to run."
    exit 0
}

$rows = New-Object System.Collections.Generic.List[object]

if ($UseCache) {
    Write-Host "Cache mode: ON"
    Write-Host ""
}

"" | Set-Content -Path $ResultsFile -Encoding UTF8
Add-Content -Path $ResultsFile -Value "================================================================================"
Add-Content -Path $ResultsFile -Value "BOREX PARAM SWEEP - $timestamp"
Add-Content -Path $ResultsFile -Value "Runs: $($filtered.Count)"
Add-Content -Path $ResultsFile -Value "================================================================================"

foreach ($r in $filtered) {
    $runCommand = $r.Command
    if ($UseCache -and ($runCommand -notmatch '--use-cache')) {
        $runCommand = "$runCommand --use-cache"
    }

    Write-Host "Running $($r.Id) [$($r.Group)] $($r.Label)..."
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Push-Location $Root
    $output = Invoke-Expression ($runCommand + ' 2>&1') | Out-String
    $exitCode = $LASTEXITCODE
    Pop-Location
    if ($null -eq $exitCode) { $exitCode = 0 }
    $sw.Stop()

    $lines = @()
    if ($output) { $lines = $output -split "`n" }
    $m = Parse-SummaryMetrics -OutputLines $lines
    $ok = ($exitCode -eq 0) -and ($m.ReturnPct -ne $null)

    $row = [PSCustomObject]@{
        Id = $r.Id
        Group = $r.Group
        Label = $r.Label
        Strategy = $m.Strategy
        Symbol = $m.Symbol
        Trades = $m.Trades
        Return = $m.Return
        ReturnPct = $m.ReturnPct
        MaxDD = $m.MaxDD
        WinRate = $m.WinRate
        FinalEquity = $m.FinalEquity
        ExitCode = $exitCode
        DurationSec = [math]::Round($sw.Elapsed.TotalSeconds, 2)
        Ok = $ok
        Command = $runCommand
    }
    $rows.Add($row)

    Add-Content -Path $ResultsFile -Value ""
    Add-Content -Path $ResultsFile -Value "--------------------------------------------------------------------------------"
    Add-Content -Path $ResultsFile -Value "ID: $($r.Id) | Group: $($r.Group) | $($r.Label)"
    Add-Content -Path $ResultsFile -Value "Command: $runCommand"
    if ($ok) {
        Add-Content -Path $ResultsFile -Value "Return: $($m.Return) | Trades: $($m.Trades) | MaxDD: $($m.MaxDD) | WR: $($m.WinRate)"
    } else {
        Add-Content -Path $ResultsFile -Value "FAILED exit=$exitCode"
        $tail = $lines | Select-Object -Last 8
        foreach ($ln in $tail) { Add-Content -Path $ResultsFile -Value $ln }
    }
}

$rows | Export-Csv -Path $CsvFile -NoTypeInformation -Encoding UTF8

Add-Content -Path $ResultsFile -Value ""
Add-Content -Path $ResultsFile -Value "================================================================================"
Add-Content -Path $ResultsFile -Value "RANKED BY GROUP (return desc)"
Add-Content -Path $ResultsFile -Value "================================================================================"

$groups = $rows | Group-Object Group | Sort-Object Name
foreach ($g in $groups) {
    Add-Content -Path $ResultsFile -Value ""
    Add-Content -Path $ResultsFile -Value "### $($g.Name)"
    $sorted = @($g.Group | Where-Object { $_.Ok } | Sort-Object ReturnPct -Descending)
    $failed = @($g.Group | Where-Object { -not $_.Ok })
    foreach ($item in $sorted) {
        $line = "{0,-6} {1,-22} ret={2,8} trades={3,4} dd={4,8} wr={5}" -f `
            $item.Id, $item.Label, $item.Return, $item.Trades, $item.MaxDD, $item.WinRate
        Add-Content -Path $ResultsFile -Value $line
    }
    foreach ($item in $failed) {
        Add-Content -Path $ResultsFile -Value ("{0,-6} {1,-22} FAILED" -f $item.Id, $item.Label)
    }
}

Write-Host ""
Write-Host "Done. $($rows.Count) runs -> $CsvFile"
Write-Host ""
Write-Host "Top returns by group:"
foreach ($g in $groups) {
    Write-Host ""
    Write-Host "[$($g.Name)]"
    $sorted = @($g.Group | Where-Object { $_.Ok } | Sort-Object ReturnPct -Descending | Select-Object -First 5)
    foreach ($item in $sorted) {
        Write-Host ("  {0,-6} {1,-22} {2,8}  trades={3}  dd={4}" -f $item.Id, $item.Label, $item.Return, $item.Trades, $item.MaxDD)
    }
}
