<#
    Restart the ArkPanel agent on THIS machine.

    Run by the "ArkPanel Agent Restart" scheduled task, never by the agent
    directly. That separation is the whole point: this process is owned by Task
    Scheduler, not by the agent, so stopping the agent cannot kill the thing
    that is supposed to start it again.

    Four earlier attempts at in-process self-restart all failed --
    os.execv left two agents fighting over port 8767, a clean exit made the
    task "completed" rather than "failed" so nothing restarted it, and a
    detached relauncher died with its parent. This does not have that problem.
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'ArkPanel Agent',
    [int]$SettleSeconds = 4,
    # Which install this restart belongs to. The leftover-process sweep below
    # used to kill ANY python running an agent.py, which is fine on a machine
    # with one agent and catastrophic on one with two - the smoke-test
    # instance restarting would have taken the real agent down with it.
    # Defaults to this script's own install, which is always the right answer.
    [string]$InstallDir = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$log = Join-Path (Split-Path -Parent $PSScriptRoot) 'logs\agent-restart.log'
New-Item -ItemType Directory -Path (Split-Path -Parent $log) -Force | Out-Null

function Write-Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Output $line
}

try {
    Write-Log "restart requested for task '$TaskName'"

    # Stop-ScheduledTask first. Start-ScheduledTask on an already-Running task
    # is a silent no-op, which once relaunched nothing and looked like success.
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds $SettleSeconds

    # Belt and braces: if the process outlived the task stop, it still holds
    # the port and the new one would fail to bind. Matched on THIS install's
    # path, so a second agent on the same machine is never touched.
    $target = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
    Write-Log "sweeping leftovers under '$target'"
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$target\agent.py*" } |
        ForEach-Object {
            Write-Log "killing leftover agent PID $($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    Start-Sleep -Seconds 1

    Start-ScheduledTask -TaskName $TaskName
    Write-Log "task started"
} catch {
    Write-Log "FAILED: $($_.Exception.Message)"
    exit 1
}
