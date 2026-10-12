<#
.SYNOPSIS  Stop the Onboarding Worker console and anything it started (model runs, sandbox subprocesses).
#>
param([int]$Port = 8765)
Set-Location $PSScriptRoot
$pidFile = Join-Path $PSScriptRoot ".worker_state\server.pid"
$stopped = 0

function Stop-Tree([int]$id) {
    if (Get-Process -Id $id -ErrorAction SilentlyContinue) {
        & taskkill /PID $id /T /F | Out-Null
        return 1
    }
    return 0
}

if (Test-Path $pidFile) {
    $id = [int](Get-Content $pidFile -ErrorAction SilentlyContinue)
    if ($id) { $stopped += Stop-Tree $id }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}
# Anything still listening on the port that is ours (matched by command line, not by port alone).
Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | ForEach-Object {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)" -ErrorAction SilentlyContinue
    if ($proc -and $proc.CommandLine -match 'onboarding[-_]worker|worker\.(cli|web)') {
        $stopped += Stop-Tree $_.OwningProcess
    }
}
if ($stopped) { Write-Host "Stopped." -ForegroundColor Green } else { Write-Host "Nothing was running." }
