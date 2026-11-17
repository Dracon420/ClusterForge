<#
    Restore an ArkPanel snapshot over a server's live save data.

    The same restore the Backups page offers, for when the panel UI is not
    available -- a headless box, a browser that will not load, or scripting a
    recovery. The panel button is the everyday path; this is the fallback.

    Both go through the same gated endpoint, and both make you type the word.

    It is still heavily gated underneath: the agent refuses to restore while the
    server is running, and takes a safety snapshot of the CURRENT state first,
    so a restore can itself be undone.

    What a restore costs: the whole map goes back to that moment. Every tame,
    level, structure, item and transfer since that snapshot is gone -- not just
    the thing you are trying to recover. Check nobody has played since.

    Usage:
      .\restore.ps1                                   list Island snapshots
      .\restore.ps1 -Server svartalfheim              list that map's snapshots
      .\restore.ps1 -Server island -Stamp 2026-09-11_114757
      .\restore.ps1 -Server island -Stamp ... -Source external
#>

[CmdletBinding()]
param(
    [string]$Server = 'island',
    [string]$Stamp,
    [ValidateSet('local', 'external')]
    [string]$Source = 'local',
    # Restores the world only. Add Config to also roll back Game.ini /
    # GameUserSettings.ini, which is usually NOT what you want -- rates you
    # have tuned since would be reverted along with the save.
    [string[]]$Parts = @('SavedArks'),
    [switch]$StopServer
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot is empty inside param() defaults, so resolve paths in the body.
$base = Split-Path -Parent $PSScriptRoot
$secrets = Get-Content (Join-Path $base 'secrets.json') -Raw | ConvertFrom-Json
$config = Get-Content (Join-Path $base 'config.json') -Raw | ConvertFrom-Json
$root = "http://127.0.0.1:$($config.panel.port)"

$pair = "$($secrets.panel_user):$($secrets.panel_password)"
$auth = @{ Authorization = 'Basic ' + [Convert]::ToBase64String(
    [Text.Encoding]::ASCII.GetBytes($pair)) }

function Invoke-Panel($Method, $Path, $Body) {
    # Not $args -- that is an automatic variable and assigning to it is asking
    # for trouble.
    $params = @{ Method = $Method; Uri = "$root$Path"; Headers = $auth; TimeoutSec = 120 }
    if ($Body) {
        $params.Body = ($Body | ConvertTo-Json -Depth 5)
        $params.ContentType = 'application/json'
    }
    Invoke-RestMethod @params
}

# Defined up here because PowerShell resolves functions at call time in script
# order -- the stop path below runs before the restore and would not find it.
function Wait-PanelJob($ServerName, $JobId) {
    $seen = 0
    while ($true) {
        Start-Sleep -Seconds 3
        $job = Invoke-Panel GET "/api/server/$ServerName/jobs/$JobId"
        if ($job.steps.Count -gt $seen) {
            foreach ($step in $job.steps[$seen..($job.steps.Count - 1)]) {
                "  [{0,5}s] {1}" -f $step.t, $step.message
            }
            $seen = $job.steps.Count
        }
        if ($job.state -and $job.state -ne 'running') { return $job }
    }
}

# ---------- show what is available ----------

$backups = Invoke-Panel GET "/api/server/$Server/backups"
$list = $backups.$Source.backups
if (-not $list) {
    Write-Host "No $Source snapshots found for $Server." -ForegroundColor Red
    return
}

if (-not $Stamp) {
    Write-Host ""
    Write-Host "Snapshots for $Server ($Source)" -ForegroundColor Cyan
    Write-Host ("-" * 58)
    $now = Get-Date
    foreach ($b in $list) {
        $when = [DateTimeOffset]::FromUnixTimeSeconds([int]$b.created).LocalDateTime
        $age = [math]::Round(($now - $when).TotalHours, 1)
        "{0}   {1,6} GB   {2}  ({3} h ago)" -f $b.stamp, $b.gb, $when.ToString('ddd dd MMM HH:mm'), $age
    }
    Write-Host ""
    Write-Host "Pick the last snapshot from BEFORE the thing you want back was lost:" -ForegroundColor Yellow
    Write-Host "  .\restore.ps1 -Server $Server -Stamp <stamp>"
    return
}

if (-not ($list.stamp -contains $Stamp)) {
    Write-Host "No $Source snapshot '$Stamp' for $Server. Run without -Stamp to list them." -ForegroundColor Red
    return
}

# ---------- confirm ----------

$status = Invoke-Panel GET "/api/cluster/status"
$live = $status.servers | Where-Object { $_.name -eq $Server }

Write-Host ""
Write-Host "RESTORE $Server  ->  $Stamp" -ForegroundColor Yellow
Write-Host "  source    : $Source"
Write-Host "  restoring : $($Parts -join ', ')"
Write-Host "  state     : $($live.state)"
Write-Host ""
Write-Host "The whole map goes back to that moment. Anything done since -- tames," -ForegroundColor Yellow
Write-Host "levels, structures, items, transfers -- is lost, on every player's behalf." -ForegroundColor Yellow
Write-Host "A safety snapshot of the CURRENT state is taken first, so this is undoable."
Write-Host ""

$typed = Read-Host "Type RESTORE to proceed (anything else cancels)"
if ($typed -cne 'RESTORE') {
    Write-Host "Cancelled. Nothing was changed." -ForegroundColor Green
    return
}

# ---------- stop if needed ----------

if ($live.state -eq 'online') {
    if (-not $StopServer) {
        Write-Host ""
        Write-Host "$Server is still running. The restore will be refused --" -ForegroundColor Red
        Write-Host "a live server overwrites the restored files immediately." -ForegroundColor Red
        Write-Host "Stop it in the panel, or re-run with -StopServer." -ForegroundColor Red
        return
    }
    Write-Host "Stopping $Server..." -ForegroundColor Cyan
    $stop = Invoke-Panel POST "/api/server/$Server/stop" @{ }
    if ($stop.job.id) { Wait-PanelJob $Server $stop.job.id }
}

# ---------- restore ----------

Write-Host ""
Write-Host "Restoring..." -ForegroundColor Cyan
$result = Invoke-Panel POST "/api/server/$Server/restore" @{
    stamp   = $Stamp
    confirm = 'RESTORE'
    source  = $Source
    parts   = $Parts
}

if ($result.error) {
    # No null-coalescing in Windows PowerShell 5.1.
    $why = $result.message
    if (-not $why) { $why = $result.error }
    Write-Host "Refused: $why" -ForegroundColor Red
    return
}

$job = Wait-PanelJob $Server $result.job.id
Write-Host ""
if ($job.state -eq 'done') {
    Write-Host "Restore complete." -ForegroundColor Green
    Write-Host "Start $Server in the panel when you are ready, then check the world before" -ForegroundColor Green
    Write-Host "letting anyone in. If it is wrong, the safety snapshot above can be restored." -ForegroundColor Green
} else {
    Write-Host "Restore FAILED: $($job.error)" -ForegroundColor Red
    Write-Host "The server was NOT started. Nothing else was touched." -ForegroundColor Red
}
