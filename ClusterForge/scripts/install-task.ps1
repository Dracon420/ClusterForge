<#
    Register the ClusterForge agent (and optionally the panel) to start at boot,
    elevated, without anyone logging in.

    Run this ELEVATED on each PC.

    Why a Scheduled Task and not a Windows service: a service runs in session 0
    isolation, which suits ARK's child-process behaviour badly and loses the
    server console entirely. A task with "run whether user is logged on or not"
    plus "highest privileges" gives silent elevation at boot with none of that.

    Why a named account and not SYSTEM: SYSTEM cannot reach SMB shares or mapped
    drives, which the backup phase needs, and it forces session 0 again.

    Undo with:  install-task.ps1 -Remove
#>

[CmdletBinding()]
param(
    [string]$TaskName   = 'ClusterForge Warden',
    [string]$PanelTask  = 'ClusterForge Web',
    [int]$DelaySeconds  = 90,
    [switch]$IncludePanel,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    throw "This must be run from an ELEVATED PowerShell (right-click > Run as administrator)."
}

$base = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    foreach ($n in @($TaskName, $PanelTask)) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            "removed scheduled task: $n"
        } else { "not present: $n" }
    }
    return
}

$python = (Get-Command python).Source
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

if (-not (Test-Path (Join-Path $base 'agent.py'))) { throw "agent.py not found in $base" }

"ClusterForge boot task installer"
"  folder : $base"
"  python : $pythonw"
"  delay  : ${DelaySeconds}s after boot"
""

# The task needs the password of the account it runs as, so it can start
# before anyone logs in. Windows stores it; it is not written to disk here.
$cred = Get-Credential -Message "Account for the ClusterForge task to run as (e.g. $env:COMPUTERNAME\$env:USERNAME)" `
                       -UserName "$env:COMPUTERNAME\$env:USERNAME"
$user = $cred.UserName
$pass = $cred.GetNetworkCredential().Password

function New-ArkTask {
    param([string]$Name, [string]$Script, [string]$Description)

    $action = New-ScheduledTaskAction -Execute $pythonw `
        -Argument ('"{0}"' -f (Join-Path $base $Script)) -WorkingDirectory $base

    # At startup, not At-LogOn: PatchRaptor's existing At-LogOn task has a known
    # race where it fires before the network is ready after a reboot.
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = "PT${DelaySeconds}S"

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Settings $settings -Description $Description `
        -User $user -Password $pass -RunLevel Highest | Out-Null

    "  registered: $Name"
}

New-ArkTask -Name $TaskName -Script 'agent.py' `
    -Description 'ClusterForge agent - controls the ARK servers on this machine.'

if ($IncludePanel -and (Test-Path (Join-Path $base 'panel.py'))) {
    New-ArkTask -Name $PanelTask -Script 'panel.py' `
        -Description 'ClusterForge web UI.'
}

""
"Verifying..."
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State |
    Format-Table -AutoSize
$info = Get-ScheduledTaskInfo -TaskName $TaskName
"  last run   : $($info.LastRunTime)"
"  next run   : at next boot (+${DelaySeconds}s)"
""
"Start it now without rebooting:  Start-ScheduledTask -TaskName '$TaskName'"
"Undo everything:                 install-task.ps1 -Remove"
