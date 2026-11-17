<#
    Register the "ClusterForge Warden Restart" task on THIS machine. Run once,
    elevated. After this the panel's "Update agents" button can restart this
    agent remotely and nobody has to walk to the machine again.

    Why a second task rather than the agent restarting itself: the restarter
    must survive the agent being stopped. A task is owned by Task Scheduler, so
    it does. A child process of the agent is not, and does not.

    Reversible:  .\install-restart-task.ps1 -Remove
#>

[CmdletBinding()]
param(
    [string]$TaskName  = 'ClusterForge Warden Restart',
    [string]$AgentTask = 'ClusterForge Warden',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$base   = Split-Path -Parent $PSScriptRoot
$script = Join-Path $base 'scripts\restart-agent.ps1'

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run this elevated (right-click PowerShell, Run as administrator)."
}

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        "Removed '$TaskName'. Remote agent restarts are no longer possible from the panel."
    } else {
        "'$TaskName' was not registered - nothing to remove."
    }
    return
}

if (-not (Test-Path $script)) { throw "Missing $script - deploy the agent files first." }
if (-not (Get-ScheduledTask -TaskName $AgentTask -ErrorAction SilentlyContinue)) {
    throw "The '$AgentTask' task does not exist on this machine. Install the boot task first."
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -TaskName "{1}"' -f $script, $AgentTask)

# No trigger: it exists only to be run on demand. Highest privileges because
# stopping and starting the agent task needs them, and SYSTEM because it must
# work with nobody logged in.
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
    -Settings $settings -Description 'Restarts the ClusterForge agent on request from the panel.' `
    -Force | Out-Null

"Registered '$TaskName'."
"The panel's Update agents button can now restart this agent remotely."
