<#
    Show the ArkPanel boot task configuration, and optionally start it now.

    Kept as a real .ps1 rather than PowerShell embedded in a .bat -- escaping
    a script of this size inside a batch file is what produced a screenful of
    parser errors on the first attempt.
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'ArkPanel Agent',
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "TASK NOT FOUND: $TaskName" -ForegroundColor Red
    Write-Host "Run install-boot-task.bat as administrator first."
    exit 1
}

Write-Host "=== $TaskName ===" -ForegroundColor Cyan
Write-Host ("  State     : {0}" -f $task.State)
Write-Host ("  RunLevel  : {0}   (Highest = elevated)" -f $task.Principal.RunLevel)
Write-Host ("  UserId    : {0}" -f $task.Principal.UserId)
Write-Host ("  LogonType : {0}   (Password = starts without anyone logging in)" -f $task.Principal.LogonType)

$trigger = $task.Triggers[0]
Write-Host ("  Trigger   : {0}  delay={1}" -f $trigger.CimClass.CimClassName, $trigger.Delay)

$action = $task.Actions[0]
Write-Host ("  Runs      : {0} {1}" -f $action.Execute, $action.Arguments)
Write-Host ("  WorkDir   : {0}" -f $action.WorkingDirectory)

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ("  Last run  : {0}  (result {1})" -f $info.LastRunTime, $info.LastTaskResult)
Write-Host "  Note: result 267009 means 'currently running', not an error."

if ($StartNow) {
    Write-Host ""
    Write-Host "Restarting the agent via its task..." -ForegroundColor Cyan

    # Stop-ScheduledTask FIRST, not just Stop-Process. Killing the process
    # leaves the task believing it is still running, and Start-ScheduledTask on
    # a "running" task is a no-op -- which silently relaunches the OLD code and
    # looks exactly like a successful restart.
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2

    # Best-effort only. Once the agent runs elevated via the task, an
    # unelevated shell cannot terminate it -- and does not need to, because
    # Stop-ScheduledTask already did.
    Get-Process python, pythonw -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3

    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Host "  task state before start: $state"

    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 8
}

Write-Host ""
Write-Host "=== Agent processes now running ===" -ForegroundColor Cyan
$procs = Get-Process pythonw, python -ErrorAction SilentlyContinue
if ($procs) {
    $procs | Select-Object Id, ProcessName, StartTime | Format-Table -AutoSize
    Write-Host "A pythonw process means the boot task started it (no console window)." -ForegroundColor Green
} else {
    Write-Host "No agent process running." -ForegroundColor Yellow
    Write-Host "Re-run with -StartNow, or just reboot - the task fires 90s after boot."
}

Write-Host ""
Write-Host "Check it answers:  http://localhost:8767/api/health"
