<#
.SYNOPSIS  Start the Onboarding Worker console in the background and open the browser.
.EXAMPLE   .\start.ps1            # real model from .env
.EXAMPLE   .\start.ps1 -Demo      # offline scripted model, sample files, no key
.EXAMPLE   .\start.ps1 -Port 9000
#>
param([int]$Port = 8765, [switch]$Demo, [switch]$NoOpen)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$state = Join-Path $PSScriptRoot ".worker_state"
New-Item -ItemType Directory -Force $state | Out-Null
$pidFile = Join-Path $state "server.pid"
$log = Join-Path $PSScriptRoot ".logs"
New-Item -ItemType Directory -Force $log | Out-Null

if (Test-Path $pidFile) {
    $old = [int](Get-Content $pidFile -ErrorAction SilentlyContinue)
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
        Write-Host "Already running (PID $old) at http://127.0.0.1:$Port. Run .\stop.ps1 first to restart."
        if (-not $NoOpen) { Start-Process "http://127.0.0.1:$Port" }
        exit 0
    }
}
$busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($busy) { Write-Error "Port $Port is already in use (PID $($busy[0].OwningProcess)). Use -Port or free it."; exit 1 }

$cmd = if ($Demo) { "demo" } else { "serve" }
$argList = @("run", "onboarding-worker", $cmd, "--port", $Port)
if ($Demo) { $argList += "--no-open" }
$p = Start-Process -FilePath "uv" -ArgumentList $argList -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $log "server.out.log") -RedirectStandardError (Join-Path $log "server.err.log")
$p.Id | Set-Content $pidFile

Write-Host "Starting ($cmd) ..." -NoNewline
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    if ($p.HasExited) { break }
    try { Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 2 | Out-Null; $ok = $true; break } catch { Write-Host "." -NoNewline }
}
Write-Host ""
if (-not $ok) {
    Write-Host "Did not start. Last log lines:" -ForegroundColor Red
    Get-Content (Join-Path $log "server.err.log") -Tail 15 -ErrorAction SilentlyContinue
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "Running at http://127.0.0.1:$Port   (stop with .\stop.ps1)" -ForegroundColor Green
if (-not $NoOpen) { Start-Process "http://127.0.0.1:$Port" }
