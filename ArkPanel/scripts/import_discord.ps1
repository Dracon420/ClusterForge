<#
    Import the existing Discord bot identities into ArkPanel's secrets.json,
    so the same bots keep their names, avatars and history in the server.

    Sources:
      The Island Watcher    D:\Servers\PatchRaptor\config.json          (bot_token)
      Svartalfhiem Watcher  Z:\Svartalfhiem\PatchRaptor-src\config.json (bot_token)
      Guardian              DSM's user.config                           (botToken)

    Tokens are copied between files and never printed.
#>

[CmdletBinding()]
param([switch]$WhatIf)

$ErrorActionPreference = 'Stop'
$noBom = New-Object System.Text.UTF8Encoding($false)

function Get-DsmSetting {
    param([string]$Name)
    $cfg = Get-ChildItem "$env:LOCALAPPDATA\ASADedicatedManager" -Recurse -Filter 'user.config' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $cfg) { return $null }
    $lines = Get-Content $cfg.FullName
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "name=`"$Name`"") {
            for ($j = $i; $j -le [Math]::Min($i + 3, $lines.Count - 1); $j++) {
                if ($lines[$j] -match '<value>(.*?)</value>') { return $Matches[1] }
            }
        }
    }
    return $null
}

$islandToken = (Get-Content 'D:\Servers\PatchRaptor\config.json' -Raw | ConvertFrom-Json).bot_token
$islandChan  = (Get-Content 'D:\Servers\PatchRaptor\config.json' -Raw | ConvertFrom-Json).channel_id

$svartToken = $null; $svartChan = $null
$svartCfg = 'Z:\Svartalfhiem\PatchRaptor-src\config.json'
if (Test-Path $svartCfg) {
    $c = Get-Content $svartCfg -Raw | ConvertFrom-Json
    $svartToken = $c.bot_token; $svartChan = $c.channel_id
}

$guardianToken = Get-DsmSetting 'botToken'
$guardianChan  = Get-DsmSetting 'discordChannelId'
$guardianUser  = Get-DsmSetting 'discordUserId'

"Discovered identities (tokens not shown):"
"  Island Watcher       token {0}  channel {1}" -f $(if($islandToken){"len $($islandToken.Length)"}else{'MISSING'}), $islandChan
"  Svartalfhiem Watcher token {0}  channel {1}" -f $(if($svartToken){"len $($svartToken.Length)"}else{'MISSING'}), $svartChan
"  Guardian             token {0}  channel {1}  owner {2}" -f $(if($guardianToken){"len $($guardianToken.Length)"}else{'MISSING'}), $guardianChan, $guardianUser
""

if ($WhatIf) { "WhatIf: nothing written."; return }

# --- PC1: the Island watcher and Guardian both live here ---
$p1 = 'D:\Servers\ArkPanel\secrets.json'
$s1 = Get-Content $p1 -Raw | ConvertFrom-Json
$s1 | Add-Member -NotePropertyName discord_watcher_token   -NotePropertyValue $islandToken   -Force
$s1 | Add-Member -NotePropertyName discord_watcher_channel -NotePropertyValue $islandChan    -Force
$s1 | Add-Member -NotePropertyName discord_guardian_token  -NotePropertyValue $guardianToken -Force
$s1 | Add-Member -NotePropertyName discord_guardian_channel -NotePropertyValue $guardianChan -Force
$s1 | Add-Member -NotePropertyName discord_owner_id        -NotePropertyValue $guardianUser  -Force
[System.IO.File]::WriteAllText($p1, ($s1 | ConvertTo-Json -Depth 6), $noBom)
"wrote PC1 secrets.json  (Island watcher + Guardian)"

# --- PC2: only its own watcher ---
$p2 = 'Z:\ArkPanel-Agent\secrets.json'
if ((Test-Path $p2) -and $svartToken) {
    $s2 = Get-Content $p2 -Raw | ConvertFrom-Json
    $s2 | Add-Member -NotePropertyName discord_watcher_token   -NotePropertyValue $svartToken -Force
    $s2 | Add-Member -NotePropertyName discord_watcher_channel -NotePropertyValue $svartChan  -Force
    [System.IO.File]::WriteAllText($p2, ($s2 | ConvertTo-Json -Depth 6), $noBom)
    "wrote PC2 secrets.json  (Svartalfhiem watcher)"
}

""
"NOTE: a Discord bot token can only be connected once at a time."
"The PatchRaptor watchers must be stopped before ArkPanel uses these tokens,"
"or the two will fight over the same bot session."
