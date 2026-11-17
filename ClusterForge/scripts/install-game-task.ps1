<#
    Give a game server its own scheduled task, so Windows owns the process.

    Generic: it reads the game's block from ClusterForge's config.json, so it
    works for any game in core/games rather than needing a new script each
    time. Replaces the Palworld-specific install-palworld-task.ps1.

    WHY THIS MATTERS, learned the hard way twice:

      * A server launched as a child of a terminal, a panel, or an agent
        session dies when that thing dies. Palworld was killed mid-life that
        way and its world was corrupted - twice - because an unclean stop
        loses the save.

      * A server launched from a session that cannot CREATE_BREAKAWAY_FROM_JOB
        is worse than dead: Minecraft Bedrock started, logged "Server
        started", and then never opened its game port at all. It looked
        healthy from every angle except the one that mattered.

    A scheduled task is owned by Task Scheduler. Restart the panel, close a
    terminal, end a remote session - the server keeps running, properly.

    Run once per game, elevated:
        .\install-game-task.ps1 -Game minecraft
        .\install-game-task.ps1 -Game palworld
        .\install-game-task.ps1 -Game sdtd

    Reversible:
        .\install-game-task.ps1 -Game minecraft -Remove
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Game,
    [string]$TaskName,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$panelDir = Split-Path -Parent $PSScriptRoot

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this elevated (right-click PowerShell, Run as administrator)." }

$cfg = Get-Content (Join-Path $panelDir 'config.json') -Raw | ConvertFrom-Json
$block = $cfg.$Game
if (-not $block) { throw "config.json has no '$Game' block. Add one first." }
$install = $block.install_dir
if (-not (Test-Path $install)) { throw "install_dir does not exist: $install" }

# Each game's executable and arguments. The one thing that cannot be read
# from config, because it is a property of the game rather than the setup.
$spec = @{
    minecraft = @{ Exe = 'bedrock_server.exe';     Args = @() }
    palworld  = @{ Exe = 'Pal\Binaries\Win64\PalServer-Win64-Shipping.exe'
                   Args = @('Pal') + $block.args }
    bedrockconnect = @{ Exe = 'jre\jdk-21.0.12.1+1-jre\bin\java.exe'
                        Args = @('-jar', 'BedrockConnect.jar', 'nodb=true',
                                 'custom_servers=servers.json') }
    sdtd      = @{ Exe = '7DaysToDieServer.exe'
                   Args = @('-configfile=clusterforge-serverconfig.xml',
                            '-quit', '-batchmode', '-nographics', '-dedicated') }
}
if (-not $spec.ContainsKey($Game)) {
    throw "No launch spec for '$Game'. Add one to this script's `$spec table."
}
$exe = Join-Path $install $spec[$Game].Exe
$launchArgs = $spec[$Game].Args

if (-not $TaskName) {
    $TaskName = if ($block.task_name) { $block.task_name } else { "ClusterForge $Game Server" }
}

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        "Removed '$TaskName'. ClusterForge will fall back to launching it directly."
    } else {
        "'$TaskName' was not registered - nothing to remove."
    }
    return
}

if (-not (Test-Path $exe -PathType Leaf)) { throw "Server binary not found: $exe" }
"game        : $Game"
"executable  : $exe"
"arguments   : $($launchArgs -join ' ')"
"task name   : $TaskName"

# The task runs the server through run-server-logged.ps1 rather than directly.
#
# Scheduled tasks discard stdout, which left a Minecraft server with no record
# of anything - no connections, no errors, nothing to read when it misbehaved.
# The wrapper redirects output to a rotated log and waits, so the task still
# reflects the server's lifetime and ending it still stops the server.
$wrapper = Join-Path $PSScriptRoot 'run-server-logged.ps1'
if (-not (Test-Path $wrapper -PathType Leaf)) { throw "Missing $wrapper" }
$logPath = Join-Path $install "logs\$Game.log"
"log file    : $logPath"

$wrapperArgs = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" ' +
               '-Exe "{1}" -WorkDir "{2}" -LogPath "{3}"'
$wrapperArgs = $wrapperArgs -f $wrapper, $exe, $install, $logPath
if ($launchArgs -and $launchArgs.Count -gt 0) {
    # Quoted as one argument so the wrapper receives the whole string.
    $wrapperArgs += ' -ServerArgs "{0}"' -f ($launchArgs -join ' ')
}
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -WorkingDirectory $install -Argument $wrapperArgs
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
# ExecutionTimeLimit 0 = never killed for running too long. The default is
# three days, which would eventually terminate a game server MID-SAVE.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
        -Settings $settings -Force `
        -Description "$Game dedicated server, owned by Task Scheduler so it survives panel restarts and terminal sessions." | Out-Null
} catch {
    throw "Could not register '$TaskName': $($_.Exception.Message)"
}

""
"--- verifying ---"
$problems = @()
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    "  OK    task '$TaskName' is registered"
} else {
    "  FAIL  task '$TaskName' is NOT registered"; $problems += 'task missing'
}
if (Test-Path $exe -PathType Leaf) { "  OK    $exe exists" }
else { "  FAIL  $exe missing"; $problems += 'binary missing' }

# Record the task name so the panel uses it rather than launching directly.
if (-not $problems.Count) {
    $cfg.$Game.task_name = $TaskName
    # Written WITHOUT a byte-order mark, deliberately.
    #
    # Windows PowerShell 5.1's "-Encoding utf8" writes a BOM, and a BOM in
    # config.json stops ClusterForge.py opening at all: it reads plain utf-8 and
    # json.load dies with "Unexpected UTF-8 BOM". The panel and the agent
    # read utf-8-sig so they never noticed, which made it look like the
    # desktop app alone had broken.
    $json = $cfg | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText((Join-Path $panelDir 'config.json'), $json,
                                   (New-Object System.Text.UTF8Encoding $false))
    "  OK    config.json updated: $Game.task_name = '$TaskName' (no BOM)"
}

""
if ($problems.Count) { "INSTALL INCOMPLETE: $($problems -join '; ')"; exit 1 }
"Done. ClusterForge will now start $Game through this task."
"Restart the panel so it picks up the new config."
