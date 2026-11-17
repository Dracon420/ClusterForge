<#
    Stage the ClusterForge agent onto PC2 (HOME-SERVER-2, formerly ChaoticControl) over its share.

    This copies files only. It cannot START the agent there -- WinRM is
    disabled on PC2 and the admin share is not reachable -- so the last step
    has to be run at PC2 itself. The script prints exactly what to run.

    The PC2 config is generated rather than copied: the agent must use LOCAL
    paths (C:\ASA-Server-2\...) because it runs on that machine. Pointing it at
    Z: would make PC2 reach back across the network to its own disk.
#>

[CmdletBinding()]
param(
    # UNC by default, NOT the Z: drive letter. A mapped drive belongs to one
    # interactive logon session; the panel runs as a scheduled task in its own
    # session and has no Z: at all, so calling this from the panel failed with
    # "Z: is not available" while running it by hand worked fine.
    # PC2 was renamed from CHAOTICCONTROL to HOME-SERVER-2 on 2026-09-14.
    [string]$Target = '\\HOME-SERVER-2\ASA-Server-2\ClusterForge-Agent',
    [string]$RemoteLocalPath = 'C:\ASA-Server-2\ClusterForge-Agent',
    [string]$FallbackTarget = 'Z:\ClusterForge-Agent'
)

$ErrorActionPreference = 'Stop'
$base = Split-Path -Parent $PSScriptRoot

$root = Split-Path -Parent $Target
if (-not (Test-Path $root)) {
    # Fall back to the mapped drive for anyone running this by hand in a
    # session where the UNC needs credentials it does not have.
    $fallbackRoot = Split-Path -Parent $FallbackTarget
    if (Test-Path $fallbackRoot) {
        "UNC $root unavailable - falling back to $fallbackRoot"
        $Target = $FallbackTarget
        $root = $fallbackRoot
    } else {
        throw "Cannot reach $root or $fallbackRoot - is HOME-SERVER-2 reachable?"
    }
}
New-Item -ItemType Directory -Path $Target -Force | Out-Null
New-Item -ItemType Directory -Path "$Target\core" -Force | Out-Null
New-Item -ItemType Directory -Path "$Target\data\wiki" -Force | Out-Null
New-Item -ItemType Directory -Path "$Target\data\maps" -Force | Out-Null
New-Item -ItemType Directory -Path "$Target\backups" -Force | Out-Null
New-Item -ItemType Directory -Path "$Target\logs" -Force | Out-Null

"Staging to $Target  (PC2 sees this as $RemoteLocalPath)"
""

# --- code ---
Copy-Item "$base\agent.py" $Target -Force
Get-ChildItem "$base\core" -Filter '*.py' | Copy-Item -Destination "$Target\core" -Force
Get-ChildItem "$base\data\wiki" -Filter '*.csv' | Copy-Item -Destination "$Target\data\wiki" -Force
# The agent's map switch reads the catalogue from its OWN data folder. It was
# never shipped, so on PC2 every switch failed with "no map in the catalog.
# Known: none" -- invisible from PC1, where the file has always existed and
# where every test of the switch happened to run.
Copy-Item "$base\data\maps\catalog.json" "$Target\data\maps\catalog.json" -Force
"  agent.py + core\*.py + data\wiki\*.csv + data\maps\catalog.json copied"

# The boot-task installer has to be run AT PC2, elevated, so it ships with the
# agent rather than staying on PC1.
New-Item -ItemType Directory -Path "$Target\scripts" -Force | Out-Null
Copy-Item "$base\scripts\install-task.ps1" "$Target\scripts" -Force
# The on-demand restart task is what lets the panel's "Update agents" button
# restart this machine remotely, so its installer and payload ship too.
Copy-Item "$base\scripts\install-restart-task.ps1" "$Target\scripts" -Force
Copy-Item "$base\scripts\restart-agent.ps1" "$Target\scripts" -Force
# Optional, and it must be run AT the remote machine, so it ships with the agent.
Copy-Item "$base\scripts\enable-remote-admin.ps1" "$Target\scripts" -Force
"  scripts\install-task.ps1 + install-restart-task.ps1 + restart-agent.ps1"
"    + enable-remote-admin.ps1 copied"

# --- secrets: same shared token, so the panel on PC1 is accepted ---
"  (secrets.json written below, BOM-stripped)"

# --- config: rewritten for PC2's own filesystem ---
$cfg = Get-Content "$base\config.json" -Raw | ConvertFrom-Json

# The save FOLDER follows the map, and a map switch changes it -- the switch
# rewrites save_dir in the panel's config. Hardcoding the folder below meant every
# deploy quietly reset it to the ORIGINAL map's folder, so after switching PC2's
# second server from Nyrandil to Amissa the next deploy would have pointed its
# backups back at SavedArks\Nyrandil. Take the folder name from the panel config.
function Get-SaveFolder($server, $fallback) {
    if ($server.save_dir) { return (Split-Path $server.save_dir -Leaf) }
    return $fallback
}

foreach ($s in $cfg.servers) {
    if ($s.name -eq 'svartalfheim') {
        # This agent owns Svartalfheim, and must address it locally.
        $s.local = $true
        $s.install_dir = 'C:\ASA-Server-2\Svartalfhiem'
        $s.start_bat   = 'C:\ASA-Server-2\StartServer.bat'
        $s.config_dir  = 'C:\ASA-Server-2\Svartalfhiem\ShooterGame\Saved\Config\WindowsServer'
        $s.log_dir     = 'C:\ASA-Server-2\Svartalfhiem\ShooterGame\Saved\Logs'
        $s.save_dir    = 'C:\ASA-Server-2\Svartalfhiem\ShooterGame\Saved\SavedArks\' + (Get-SaveFolder $s 'Svartalfheim')
        $s.rcon_ip     = '127.0.0.1'
        $s.agent_url   = 'http://127.0.0.1:8767'
    }
    elseif ($s.name -eq 'nyrandil') {
        # PC2's SECOND server, on its own install. Without this block it fell
        # through to the else below and was marked as PC1's -- PC2's agent would
        # never have managed it, and PC1's would have looked for it and failed.
        $s.local = $true
        $s.install_dir = 'C:\ASA-Server-2\Nyrandil'
        $s.start_bat   = 'C:\ASA-Server-2\Nyrandil\StartServer.bat'
        $s.config_dir  = 'C:\ASA-Server-2\Nyrandil\ShooterGame\Saved\Config\WindowsServer'
        $s.log_dir     = 'C:\ASA-Server-2\Nyrandil\ShooterGame\Saved\Logs'
        $s.save_dir    = 'C:\ASA-Server-2\Nyrandil\ShooterGame\Saved\SavedArks\' + (Get-SaveFolder $s 'Nyrandil')
        $s.rcon_ip     = '127.0.0.1'
        $s.agent_url   = 'http://127.0.0.1:8767'
    }
    else {
        # The Island is not this machine's responsibility.
        $s.local = $false
        $s.agent_url = 'http://192.168.1.10:8767'
    }
}
# patchraptor_path is gone: ClusterForge speaks RCON itself now (core/rcon.py), so
# there is no external tool to point at.
if ($cfg.PSObject.Properties.Name -contains 'patchraptor_path') {
    $cfg.PSObject.Properties.Remove('patchraptor_path')
}

# Self-update: this agent pulls its own code from the panel. Set HERE rather
# than by hand on PC2, because this script REGENERATES config.json on every
# deploy - a hand-edit survives until the next deploy and then silently
# disappears, which is exactly how an agent ends up on old code again.
$selfupdate = [ordered]@{
    _comment      = 'The agent fetches its own code from the panel instead of waiting to be pushed to. Never fetches config.json or secrets.json, never updates mid-job, and restarts through the ClusterForge Warden Restart task once files verify.'
    enabled       = $true
    panel_url     = 'http://192.168.1.10:8090'
    check_minutes = 15
}
$cfg | Add-Member -NotePropertyName selfupdate -NotePropertyValue $selfupdate -Force

# I: on PC1 is now shared, so PC2 writes its external copy straight there over
# UNC rather than PC1 pulling afterwards. One less moving part, and the offsite
# copy lands at backup time instead of whenever a sync runs.
$cfg.backup.local_root    = 'C:\ASA-Server-2\ClusterForge-Agent\backups'
$cfg.backup.external_root = '\\HOME-SERVER-1\I\ASA-Backups\ClusterForge'
$cfg.backup.mirror_root   = '\\HOME-SERVER-1\I\ASA-Backups\ClusterForge-Mirror'
$cfg.backup.mirror_on_scheduled_restart = $true

# WriteAllText with UTF8Encoding($false) writes NO BOM. Set-Content -Encoding
# utf8 on Windows PowerShell 5.1 emits one, which Python's json.load rejects.
$noBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText("$Target\config.json", ($cfg | ConvertTo-Json -Depth 10), $noBom)
"  config.json generated with PC2-local paths (no BOM)"

# Secrets are MERGED, never copied wholesale.
#
# Copying PC1's secrets.json over PC2's overwrote PC2's Discord watcher token
# with the Island one, so PC2's agent connected as "The Island Watcher" and
# posted Svartalfheim's status under the wrong bot. Worse, it happened again on
# every deploy, silently undoing the fix.
#
# Shared values (the agent token, the RCON password) come from PC1. Anything
# machine-specific -- this PC's own bot identity -- is preserved.
$src = Get-Content "$base\secrets.json" -Raw | ConvertFrom-Json

$dstPath = "$Target\secrets.json"
$dst = if (Test-Path $dstPath) {
    Get-Content $dstPath -Raw | ConvertFrom-Json
} else {
    [PSCustomObject]@{}
}

# Cluster-wide, safe to push from PC1.
foreach ($k in @('agent_token', 'rcon_password', 'discord_webhook', 'discord_owner_id')) {
    if ($null -ne $src.$k) {
        $dst | Add-Member -NotePropertyName $k -NotePropertyValue $src.$k -Force
    }
}

# Never pushed: PC2 runs its own watcher and no Guardian at all.
foreach ($k in @('discord_guardian_token', 'discord_guardian_channel',
                 'panel_user', 'panel_password')) {
    if ($dst.PSObject.Properties.Name -contains $k) {
        $dst.PSObject.Properties.Remove($k)
    }
}

[System.IO.File]::WriteAllText($dstPath, ($dst | ConvertTo-Json -Depth 6), $noBom)

$hasWatcher = $null -ne $dst.discord_watcher_token
"  secrets.json merged (shared keys pushed; this PC's bot identity preserved)"
"     watcher token on PC2: " + $(if ($hasWatcher) { "present, len $($dst.discord_watcher_token.Length)" } else { "MISSING - run set-svartalfheim-watcher.ps1" })

# --- helper the user runs at PC2 ---
$start = @"
@echo off
REM Start the ClusterForge agent on HOME-SERVER-2 (PC2).
REM Leave this window open; close it to stop the agent.
cd /d "$RemoteLocalPath"
echo Checking Python dependencies...
python -c "import flask, waitress, psutil" 2>nul
if errorlevel 1 (
  echo Installing missing packages...
  python -m pip install flask waitress psutil requests
)
echo.
echo Starting ClusterForge agent on port 8767...
python agent.py
pause
"@
Set-Content "$Target\start-agent.bat" $start -Encoding ascii
"  start-agent.bat written"

$fw = @"
REM Run as Administrator on HOME-SERVER-2 (PC2).
REM Allows only PC1 (192.168.1.10) to reach the agent.
netsh advfirewall firewall delete rule name="ClusterForge Warden" >nul 2>&1
netsh advfirewall firewall add rule name="ClusterForge Warden" dir=in action=allow protocol=TCP localport=8767 remoteip=192.168.1.10
echo Firewall rule added for TCP 8767 from 192.168.1.10 only.
pause
"@
Set-Content "$Target\allow-firewall.bat" $fw -Encoding ascii
"  allow-firewall.bat written"

""
"Files staged: " + (Get-ChildItem $Target -Recurse -File).Count
""
"Files are staged. They do NOT take effect until that agent restarts."
""
"The panel's Update agents button handles the restart by itself, once the"
"on-demand restart task exists on that machine. If it does not yet, run this"
"ONCE, elevated, at the remote PC - after that no more trips are needed:"
"  powershell -ExecutionPolicy Bypass -File $RemoteLocalPath\scripts\install-restart-task.ps1"
""
"First-time setup only (a brand new agent machine):"
"  $RemoteLocalPath\allow-firewall.bat   (right-click, Run as administrator)"
"  $RemoteLocalPath\scripts\install-task.ps1   (elevated, for the boot task)"
