<#
    Give the Palworld server its own scheduled task, so Windows owns it.

    Why this exists, in one line: a Palworld server launched as a child of
    anything dies when that thing dies - and being killed rather than shut
    down corrupts the world.

    It happened twice. The server was started from a terminal session back in
    September; Windows tears down a session's whole process tree on exit, so
    the server was killed mid-life. Level.sav holds bases, structures and
    container contents, and it was damaged both times, while the per-player
    .sav files came through fine. The symptom was "all my structures and my
    inventory are gone but my levels are still there".

    A scheduled task is owned by Task Scheduler. Restart the panel, close a
    terminal, end a remote session - the server keeps running. Nothing short
    of stopping the task (or the machine) touches it.

    Run once, elevated:
        .\install-palworld-task.ps1

    Reversible:
        .\install-palworld-task.ps1 -Remove
#>

[CmdletBinding()]
param(
    [string]$TaskName   = 'Palworld Server',
    [string]$InstallDir = 'D:\Servers\Palworld',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this elevated (right-click PowerShell, Run as administrator)." }

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        "Removed '$TaskName'. ArkPanel will fall back to launching the server itself."
    } else {
        "'$TaskName' was not registered - nothing to remove."
    }
    return
}

$exe = Join-Path $InstallDir 'Pal\Binaries\Win64\PalServer-Win64-Shipping.exe'
if (-not (Test-Path $exe -PathType Leaf)) { throw "Server binary not found: $exe" }

# Read the launch arguments from ArkPanel's config so there is ONE source of
# truth. A task started with different flags than the panel expects is a very
# quiet way to lose -publiclobby and wonder why the Xbox cannot see the server.
$panelDir = Split-Path -Parent $PSScriptRoot
$cfg = Get-Content (Join-Path $panelDir 'config.json') -Raw | ConvertFrom-Json
$launchArgs = @('Pal') + $cfg.palworld.args
"launch arguments (from config.json): $($launchArgs -join ' ')"

# No trigger: ArkPanel runs it on demand. RunLevel Highest and SYSTEM so it
# works with nobody logged in - the save lives under the install directory,
# not in a user profile, so SYSTEM is fine.
$action = New-ScheduledTaskAction -Execute $exe -Argument ($launchArgs -join ' ') `
    -WorkingDirectory $InstallDir
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
# ExecutionTimeLimit 0 = never kill it for running too long. The default is
# three days, which would eventually terminate the server MID-SAVE - exactly
# the failure this task exists to prevent.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
    -Settings $settings -Force `
    -Description 'Palworld dedicated server. Owned by Task Scheduler so it survives panel restarts and terminal sessions - being killed corrupts the world save.' | Out-Null

"Registered '$TaskName'."
""
"--- verifying ---"
$problems = @()
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    "  OK    task '$TaskName' is registered"
} else {
    "  FAIL  task '$TaskName' is NOT registered"; $problems += 'task missing'
}
if (Test-Path $exe -PathType Leaf) { "  OK    $exe exists" }
else { "  FAIL  $exe is missing"; $problems += 'binary missing' }

""
if ($problems.Count) {
    "INSTALL INCOMPLETE: $($problems -join '; ')"
    exit 1
}
"Done. ArkPanel will now start Palworld through this task."
"Stop the server from the panel (or Discord) so it saves and shuts down properly -"
"never End Task it, and never kill the process."
