<#
    Apply one of the panel's Minecraft presets from the command line.

    Supersedes apply-minecraft-easier.ps1, which carried its own hardcoded
    list of gamerules. Two copies of "what gentle means" is one too many -
    they drift, and the one you are not looking at is the one that is wrong.
    This reads the SAME presets the panel's settings screen offers, out of
    core/minecraft_schema.py, and applies them through the SAME adapter code
    path. If a preset is right here it is right in the panel, and vice versa.

    It exists at all because the panel runs as a scheduled task and the
    server it controls runs as SYSTEM: applying settings needs elevation,
    and sometimes you want it done without waiting for a panel restart.

    WHY A RESTART IS INVOLVED: Bedrock reads server.properties once, at
    startup, so a setting written while it runs is correct on disk and has
    no effect - which looks exactly like a broken save. Gamerules are worse:
    they live inside the world and can only be set through the server's
    stdin, which a detached server does not have. Both are handled by
    stopping, applying, and starting again.

        .\apply-minecraft-preset.ps1                      # list the presets
        .\apply-minecraft-preset.ps1 -Preset creative_child
        .\apply-minecraft-preset.ps1 -Preset creative_child -WhatIf
#>

[CmdletBinding()]
param(
    [string]$Preset,
    [string]$PanelDir = 'D:\Servers\ArkPanel',
    [string]$TaskName = 'ArkPanel minecraft Server',
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'

function Invoke-Panel([string]$code) {
    $tmp = Join-Path $env:TEMP ("mcpreset-{0}.py" -f [guid]::NewGuid().ToString('N'))
    [System.IO.File]::WriteAllText($tmp, $code, (New-Object System.Text.UTF8Encoding $false))
    # Out-Host, not plain output: anything written to the pipeline here would
    # be returned alongside the exit code and the caller would compare a
    # whole transcript against 0.
    try { & python -I $tmp | Out-Host; return $LASTEXITCODE }
    finally { Remove-Item $tmp -Force -EA SilentlyContinue }
}

$header = @"
import json, sys
sys.path.insert(0, r'$PanelDir')
from core import games, minecraft_gamerules as gr
from core import minecraft_schema as schema
cfg = json.load(open(r'$PanelDir\config.json', encoding='utf-8-sig'))
mc = cfg['minecraft']
game = games.build('minecraft', cfg, {}, log=print)

"@
# A here-string does not end in a newline, so without this the first line of
# every block below would be glued onto the end of the import block.
$header = $header + "`n"

# ---- no preset named: list them and stop -------------------------------
if (-not $Preset) {
    Invoke-Panel ($header + @"
current = game.read_settings(mc)
print('Presets available:')
for pid, p in schema.PRESETS.items():
    vals = p['values']
    rules = sum(1 for k in vals if k.startswith(gr.PREFIX))
    already = sum(1 for k, v in vals.items() if str(current.get(k)) == str(v))
    print()
    print(f"  {pid}")
    print(f"    {p['label']}")
    print(f"    {len(vals)} settings ({len(vals) - rules} properties, {rules} gamerules), "
          f"{already} already match")
    print(f"    {p['why']}")
print()
print('Apply one with:  .\\apply-minecraft-preset.ps1 -Preset <name>')
"@) | Out-Null
    return
}

# ---- show the diff, always, before doing anything ----------------------
"Preset: $Preset"
""
$code = $header + @"
pid = '$Preset'
preset = schema.PRESETS.get(pid)
if not preset:
    print('No such preset. Run without -Preset to list them.')
    sys.exit(2)
current = game.read_settings(mc)
changes = {}
for key, want in preset['values'].items():
    have = str(current.get(key, '(absent)'))
    if have != str(want):
        changes[key] = want
        print(f'  {key:42} {have:12} -> {want}')
if not changes:
    print('  nothing to change - every setting already matches.')
print()
print(f'{len(changes)} of {len(preset["values"])} settings would change.')
json.dump(changes, open(r'$env:TEMP\mcpreset-changes.json', 'w'))
sys.exit(0 if changes else 3)
"@
$rc = Invoke-Panel $code
if ($rc -eq 2) { exit 1 }
if ($rc -eq 3) { ""; "Nothing to do."; return }
if ($rc -ne 0) { throw "Could not work out what to change (exit $rc)." }

if ($WhatIf) { ""; "-WhatIf: stopping here. Nothing was changed."; return }

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run this elevated - the server runs as SYSTEM under Task Scheduler and cannot be stopped otherwise."
}

# ---- stop ---------------------------------------------------------------
# Two Bedrock servers on one world both write the same LevelDB, which is how
# a world gets corrupted. Nothing is written until this one is gone.
""
"Stopping '$TaskName'..."
schtasks /End /TN $TaskName 2>&1 | Out-Null
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process bedrock_server -EA SilentlyContinue)) { break }
    Start-Sleep -Seconds 2
}
if (Get-Process bedrock_server -EA SilentlyContinue) {
    throw "bedrock_server is still running after 90s. Not writing settings under a live server."
}
"  stopped."

# ---- apply --------------------------------------------------------------
# write_settings handles both sources: the properties file directly, and
# gamerules through a temporary attached console instance.
""
"Applying..."
$applyRc = Invoke-Panel ($header + @"
changes = json.load(open(r'$env:TEMP\mcpreset-changes.json'))
try:
    applied = game.write_settings(mc, changes)
except Exception as exc:
    print('FAILED:', exc)
    sys.exit(1)
for key, (before, after) in sorted(applied.items()):
    flag = '' if str(after) == str(changes.get(key, after)) else '   <-- DID NOT TAKE'
    print(f'  {key:42} {before} -> {after}{flag}')
print()
print(f'{len(applied)} settings written.')
"@)

# ---- start again, whatever happened ------------------------------------
# A server left off because applying settings failed is a worse outcome
# than the old values staying put.
""
"Starting '$TaskName' again..."
schtasks /Run /TN $TaskName 2>&1 | Out-Null
Start-Sleep -Seconds 12
if (Get-Process bedrock_server -EA SilentlyContinue) { "  running." }
else { "  WARNING: it did not come back - start it from the panel and check the log." }

if ($applyRc -ne 0) { ""; "Applying FAILED - see above. The server is back up on its old settings."; exit 1 }

# ---- prove it, from the files rather than from the commands -------------
""
"Verifying (read back from server.properties and the world file):"
Invoke-Panel ($header + @"
changes = json.load(open(r'$env:TEMP\mcpreset-changes.json'))
current = game.read_settings(mc)
bad = 0
for key, want in sorted(changes.items()):
    have = str(current.get(key, '(absent)'))
    ok = have == str(want)
    if not ok:
        bad += 1
    print(f"  {'OK  ' if ok else 'FAIL'}  {key:42} {have}")
print()
print('All settings verified.' if not bad else f'{bad} setting(s) did not stick.')
sys.exit(1 if bad else 0)
"@) | Out-Null
Remove-Item (Join-Path $env:TEMP 'mcpreset-changes.json') -Force -EA SilentlyContinue
