<#
    One-time cleanup: remove stray ArkPanel agent processes and leave exactly
    one, managed by its scheduled task.

    Why this is needed: an earlier version of the agent's /api/admin/reload
    used os.execv. On Windows there is no true exec, so Python spawned a new
    process while the old one could linger holding port 8767. The result was
    several agent processes competing for the port, with a stale one often
    winning -- which looked exactly like deployments silently failing to take.

    The reload endpoint no longer does that (it exits and lets the task restart
    it), so this should only ever need running once.

    Run ELEVATED. It does not touch the ARK servers.
#>

[CmdletBinding()]
param([switch]$WhatIf)

$ErrorActionPreference = 'Stop'

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}
if (-not (Test-Admin)) {
    throw "Run this from an ELEVATED PowerShell (right-click > Run as administrator)."
}

Write-Host "=== ARK servers (must NOT be touched) ===" -ForegroundColor Cyan
Get-Process ArkAscendedServer -ErrorAction SilentlyContinue |
    Select-Object Id, @{n='RAM_GB';e={[math]::Round($_.WorkingSet64/1GB,2)}} |
    Format-Table -AutoSize

Write-Host "=== ArkPanel processes before ===" -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Select-Object ProcessId, Name, CreationDate | Format-Table -AutoSize

if ($WhatIf) { Write-Host "WhatIf: stopping here."; return }

Write-Host "Stopping tasks..." -ForegroundColor Cyan
foreach ($n in @('ArkPanel Agent', 'ArkPanel Web')) {
    Stop-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 3

# Only Python processes. ArkAscendedServer is deliberately never matched.
Write-Host "Killing any remaining Python processes..." -ForegroundColor Cyan
Get-Process python, pythonw -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 4

$left = Get-Process python, pythonw -ErrorAction SilentlyContinue
if ($left) {
    Write-Host "Still running: $($left.Id -join ', ')" -ForegroundColor Yellow
} else {
    Write-Host "All clear." -ForegroundColor Green
}

$port = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object LocalPort -in 8767, 8090
if ($port) {
    Write-Host "Ports still held: $($port.LocalPort -join ', ')" -ForegroundColor Yellow
} else {
    Write-Host "Ports 8767 and 8090 are free." -ForegroundColor Green
}

Write-Host "Starting tasks..." -ForegroundColor Cyan
foreach ($n in @('ArkPanel Agent', 'ArkPanel Web')) {
    if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
        Start-ScheduledTask -TaskName $n
    }
}
Start-Sleep -Seconds 12

Write-Host ""
Write-Host "=== After ===" -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Select-Object ProcessId, CreationDate | Format-Table -AutoSize
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object LocalPort -in 8767, 8090 |
    Select-Object LocalPort, OwningProcess | Format-Table -AutoSize

Write-Host "Expect exactly two pythonw processes on PC1 (agent + panel)," -ForegroundColor Green
Write-Host "or one on PC2 (agent only), each owning its port." -ForegroundColor Green
Write-Host ""
Write-Host "Verify: http://localhost:8767/api/health"
