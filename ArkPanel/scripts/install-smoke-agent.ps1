<#
    Set up a THROWAWAY agent beside the real one, for testing self-update.

    It is a genuinely separate install: its own folder, its own port, its own
    config, its own pair of scheduled tasks. What it is NOT is a copy that
    stays in sync by hand - it gets agent.py and core\ once, and from then on
    it updates itself from the panel, which is the whole point.

    It manages no servers. If the update path breaks it, nothing is lost.

    Run once, elevated:
        .\install-smoke-agent.ps1

    Then, from the panel folder, as often as you like:
        python scripts\smoke_selfupdate.py --agent http://127.0.0.1:8768

    Reversible:  .\install-smoke-agent.ps1 -Remove
#>

[CmdletBinding()]
param(
    [string]$InstallDir = 'D:\Servers\ArkPanel-SmokeAgent',
    [int]$Port          = 8768,
    [string]$PanelUrl   = 'http://127.0.0.1:8090',
    [string]$TaskName   = 'ArkPanel Smoke Agent',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$panelDir    = Split-Path -Parent $PSScriptRoot
$restartTask = "$TaskName Restart"

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this elevated (right-click PowerShell, Run as administrator)." }

if ($Remove) {
    foreach ($t in @($restartTask, $TaskName)) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask   -TaskName $t -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $t -Confirm:$false
            "removed task '$t'"
        }
    }
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$InstallDir\agent.py*" } |
        ForEach-Object { "stopping pid $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    if (Test-Path $InstallDir) {
        "left $InstallDir in place - delete it by hand if you want it gone"
    }
    return
}

# ---------------------------------------------------------------- the files
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
New-Item -ItemType Directory -Path "$InstallDir\logs" -Force | Out-Null
New-Item -ItemType Directory -Path "$InstallDir\data" -Force | Out-Null

Copy-Item "$panelDir\agent.py" $InstallDir -Force
Copy-Item "$panelDir\core"    $InstallDir -Recurse -Force

# Create the directory FIRST, and copy to the full destination path.
# "Copy-Item file dest\scripts\" when scripts does not exist silently creates
# a FILE called scripts holding the script's contents. That is what happened:
# the restart task pointed at scripts\restart-agent.ps1, a path that could
# never exist, powershell.exe started and exited, and schtasks /Run still
# returned 0 - so the agent logged "restart triggered" once a minute for an
# hour without ever restarting.
New-Item -ItemType Directory -Path "$InstallDir\scripts" -Force | Out-Null
Copy-Item "$panelDir\scripts\restart-agent.ps1" `
          "$InstallDir\scripts\restart-agent.ps1" -Force
if (-not (Test-Path "$InstallDir\scripts\restart-agent.ps1" -PathType Leaf)) {
    throw "restart-agent.ps1 did not land at $InstallDir\scripts\restart-agent.ps1"
}
"copied agent.py, core\ and scripts\restart-agent.ps1 to $InstallDir"

# The token has to match the panel's, or the agent cannot fetch code. Read and
# re-emit it programmatically - never printed, never passed on a command line.
$panelSecrets = Get-Content "$panelDir\secrets.json" -Raw | ConvertFrom-Json
@{ agent_token = $panelSecrets.agent_token } | ConvertTo-Json |
    ForEach-Object { [System.IO.File]::WriteAllText("$InstallDir\secrets.json", $_,
                        (New-Object System.Text.UTF8Encoding $false)) }   # no BOM
"wrote secrets.json (agent_token only)"

# No servers. read_only false so it may write its own files, and a fast poll
# so the smoke test is not mostly waiting.
# The port and task names live under "agent", matching the real config.json.
# Writing `port` at the top level instead made _serve() raise KeyError on
# CONFIG["agent"] the moment start_services() finished - the agent logged a
# normal-looking startup and then vanished without binding anything.
@{
    _comment   = 'Throwaway agent for testing self-update. Manages nothing. Created by scripts/install-smoke-agent.ps1.'
    read_only  = $false
    servers    = @()
    agent      = @{
        # Loopback only. This is a test instance and has no business being
        # reachable from the network.
        host         = '127.0.0.1'
        port         = $Port
        task_name    = $TaskName
        restart_task = $restartTask
    }
    selfupdate = @{
        enabled        = $true
        panel_url      = $PanelUrl
        check_minutes  = 1
        settle_seconds = 15
    }
    monitor    = @{ enabled = $false }
    autostart  = @{ enabled = $false }
    discord    = @{}
} | ConvertTo-Json -Depth 6 |
    ForEach-Object { [System.IO.File]::WriteAllText("$InstallDir\config.json", $_,
                        (New-Object System.Text.UTF8Encoding $false)) }   # no BOM: see test_config_encoding.py
"wrote config.json (127.0.0.1:$Port, polls every 1 min, restart task '$restartTask')"

# ---------------------------------------------------------------- the tasks
$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = 'C:\Users\you\AppData\Local\Programs\Python\Python312\pythonw.exe' }
if (-not (Test-Path $python)) { throw "Could not find pythonw.exe - pass its path in the script." }

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

try {
    Register-ScheduledTask -TaskName $TaskName -Force `
        -Action (New-ScheduledTaskAction -Execute $python `
                    -Argument "`"$InstallDir\agent.py`"" -WorkingDirectory $InstallDir) `
        -Principal $principal -Settings $settings `
        -Description 'Throwaway ArkPanel agent used to smoke-test self-update.' | Out-Null
    "registered '$TaskName'"
} catch {
    throw "Could not register '$TaskName': $($_.Exception.Message)"
}

# Its OWN restarter, pointed at its OWN install - so restarting the smoke
# agent can never touch the real one.
try {
    Register-ScheduledTask -TaskName $restartTask -Force `
        -Action (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
            '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -TaskName "{1}" -InstallDir "{2}"' `
                -f "$InstallDir\scripts\restart-agent.ps1", $TaskName, $InstallDir)) `
        -Principal $principal `
        -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
                                                -MultipleInstances IgnoreNew) `
        -Description 'Restarts the throwaway smoke-test agent.' | Out-Null
    "registered '$restartTask'"
} catch {
    throw "Could not register '$restartTask': $($_.Exception.Message)"
}

Start-ScheduledTask -TaskName $TaskName

# ------------------------------------------------------- prove it, or say so
#
# The first run of this script copied every file, wrote both configs, and
# registered NEITHER task - and said nothing about it. The agent was left
# crash-looping on a bad config with no port bound, which looked from the
# outside exactly like "the installer worked". So the script now checks its
# own work and reports each part separately.
""
"--- verifying ---"
$problems = @()

foreach ($t in @($TaskName, $restartTask)) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        "  OK    task '$t' is registered"
    } else {
        "  FAIL  task '$t' is NOT registered"
        $problems += "task '$t' missing"
    }
}

# Registered is not the same as workable. The restart task's action names a
# script, and a task pointing at a missing file fails in the one way nothing
# reports: powershell starts, finds nothing, exits, and schtasks calls that
# success. Check the file is really where the action says it is.
$restartScript = "$InstallDir\scripts\restart-agent.ps1"
if (Test-Path $restartScript -PathType Leaf) {
    "  OK    $restartScript exists"
} else {
    "  FAIL  $restartScript is MISSING - the restart task would do nothing"
    $problems += "restart script missing"
}

$up = $false
foreach ($attempt in 1..10) {
    Start-Sleep -Seconds 3
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $up = $true; break }
    } catch { }
}
if ($up) {
    "  OK    agent is answering on 127.0.0.1:$Port"
} else {
    "  FAIL  nothing answered on port $Port after 30s"
    $problems += "agent not listening"
    $log = "$InstallDir\logs\agent.log"
    if (Test-Path $log) {
        "        last lines of $log :"
        Get-Content $log -Tail 8 | ForEach-Object { "          $_" }
    }
}

""
if ($problems.Count) {
    "INSTALL INCOMPLETE: $($problems -join '; ')"
    "Fix the above before running the smoke test - it cannot pass like this."
    exit 1
}
"Ready.  Now run:"
"  python scripts\smoke_selfupdate.py --agent http://127.0.0.1:$Port"
