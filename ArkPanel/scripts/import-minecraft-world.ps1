<#
    Put an existing Minecraft Bedrock world onto the server.

    For when someone has already built something they care about somewhere
    else and the server's own empty world is not where they want to be.

    WHAT THIS ACCEPTS:
      *  a .mcworld file   - what "Export World" produces on PC, phone or
                             tablet, and what a Realm download gives you
      *  a .zip            - the same thing by another name
      *  a world FOLDER    - as found under a PC client's minecraftWorlds,
                             or already unpacked

    WHAT IT CANNOT DO: pull a world off an Xbox, PlayStation or Switch.
    Consoles cannot export a world at all - there is no file to copy. The
    way round it is to upload the world to a Realm from the console, then
    download that Realm from a PC, phone or tablet, which does produce a
    .mcworld. Only the Realm's OWNER can download it.

    NOTHING IS DELETED. The existing world folder stays exactly where it is;
    this imports alongside it and repoints the server's level-name. If the
    new world is wrong, point level-name back and the old one is untouched.

    Run elevated - the server belongs to Task Scheduler as SYSTEM:
        .\import-minecraft-world.ps1 -Source "C:\Users\me\Downloads\Her World.mcworld"
        .\import-minecraft-world.ps1 -Source "D:\copies\My World" -Name "HerWorld"
        .\import-minecraft-world.ps1 -ListOnly          # just show what is there
#>

[CmdletBinding()]
param(
    [string]$Source,
    [string]$Name,
    [string]$InstallDir = 'D:\Servers\MinecraftBedrock',
    [string]$TaskName = 'ArkPanel minecraft Server',
    [switch]$ListOnly,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$worldsDir = Join-Path $InstallDir 'worlds'
$propsPath = Join-Path $InstallDir 'server.properties'

function Get-WorldSizeMB($path) {
    $sum = (Get-ChildItem $path -Recurse -File -EA SilentlyContinue |
            Measure-Object Length -Sum).Sum
    if (-not $sum) { return 0 }
    [math]::Round($sum / 1MB, 1)
}

function Show-Worlds {
    $current = (Get-Content $propsPath | Where-Object { $_ -match '^level-name=' }) -replace '^level-name=', ''
    "Worlds on the server (level-name is currently '$current'):"
    Get-ChildItem $worldsDir -Directory -EA SilentlyContinue | ForEach-Object {
        $tag = if ($_.Name -eq $current.Trim()) { ' <-- in use' } else { '' }
        $label = Join-Path $_.FullName 'levelname.txt'
        $inner = if (Test-Path $label) { (Get-Content $label -Raw).Trim() } else { '?' }
        "   {0,-26} {1,8} MB   in-game name: {2}{3}" -f $_.Name, (Get-WorldSizeMB $_.FullName), $inner, $tag
    }
}

if ($ListOnly) { Show-Worlds; return }

if (-not $Source) { throw "Give -Source (a .mcworld, a .zip, or a world folder), or -ListOnly." }
if (-not (Test-Path $Source)) { throw "Source not found: $Source" }

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this elevated - the server runs as SYSTEM under Task Scheduler." }

# --- unpack somewhere empty ---------------------------------------------
# A .mcworld is a zip from outside this machine, so it is extracted into its
# own fresh directory rather than anywhere it could overwrite something.
$staging = Join-Path $env:TEMP ("mcworld-{0}" -f [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $staging -Force | Out-Null
$cleanup = $staging

try {
    $item = Get-Item $Source
    if ($item.PSIsContainer) {
        "Source is a folder."
        $worldRoot = $item.FullName
    } else {
        "Source is a file ($($item.Extension)). Unpacking..."
        $zip = Join-Path $staging 'world.zip'
        Copy-Item $item.FullName $zip
        Expand-Archive -Path $zip -DestinationPath (Join-Path $staging 'unpacked') -Force
        Remove-Item $zip -Force
        $worldRoot = Join-Path $staging 'unpacked'
        # Some exports wrap everything in one extra folder.
        if (-not (Test-Path (Join-Path $worldRoot 'level.dat'))) {
            $inner = Get-ChildItem $worldRoot -Directory
            if ($inner.Count -eq 1 -and (Test-Path (Join-Path $inner[0].FullName 'level.dat'))) {
                $worldRoot = $inner[0].FullName
            }
        }
    }

    # --- is this actually a Bedrock world? ------------------------------
    # Checked before the server is touched. An unrecognised folder copied
    # into worlds/ produces a server that starts and generates a brand new
    # empty world, which looks exactly like the import having eaten
    # everything.
    foreach ($needed in @('level.dat', 'db')) {
        if (-not (Test-Path (Join-Path $worldRoot $needed))) {
            throw "That does not look like a Bedrock world - no '$needed' in $worldRoot. (A Java Edition world will not work: different format entirely.)"
        }
    }
    $labelFile = Join-Path $worldRoot 'levelname.txt'
    $inGameName = if (Test-Path $labelFile) { (Get-Content $labelFile -Raw).Trim() } else { 'unknown' }

    if (-not $Name) {
        $Name = if ($inGameName -ne 'unknown') { $inGameName } else { 'Imported' }
    }
    # Folder names end up in server.properties and on disk.
    $Name = ($Name -replace '[^\w\-. ]', '').Trim()
    if (-not $Name) { $Name = 'Imported' }

    $target = Join-Path $worldsDir $Name
    if ((Test-Path $target) -and -not $Force) {
        throw "worlds\$Name already exists. Pass -Name something-else, or -Force to replace it."
    }

    "in-game name : $inGameName"
    "folder name  : $Name"
    "size         : $(Get-WorldSizeMB $worldRoot) MB"
    ""

    # --- stop the server ------------------------------------------------
    # Two servers on one world both write the same LevelDB, which is how a
    # world gets corrupted. Nothing is copied until this one is gone.
    "Stopping '$TaskName'..."
    schtasks /End /TN $TaskName 2>&1 | Out-Null
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process bedrock_server -EA SilentlyContinue)) { break }
        Start-Sleep -Seconds 2
    }
    if (Get-Process bedrock_server -EA SilentlyContinue) {
        throw "bedrock_server is still running after 90s. Not copying a world under a live server."
    }
    "  stopped."

    # --- copy it in -----------------------------------------------------
    # An existing folder being replaced is MOVED ASIDE, never deleted.
    if (Test-Path $target) {
        $aside = Join-Path $worldsDir ("_Cleanup-{0}-{1}" -f $Name, (Get-Date -Format 'yyyyMMdd-HHmmss'))
        "Moving the existing worlds\$Name aside to $(Split-Path -Leaf $aside) (not deleted)..."
        Move-Item $target $aside
    }
    "Copying the world in..."
    Copy-Item $worldRoot $target -Recurse
    if (-not (Test-Path (Join-Path $target 'level.dat'))) {
        throw "Copy finished but worlds\$Name\level.dat is missing. Leaving level-name alone."
    }
    "  copied $(Get-WorldSizeMB $target) MB."

    # --- point the server at it -----------------------------------------
    # Written without a BOM: PowerShell 5.1's -Encoding utf8 adds one, and a
    # BOM in server.properties makes Bedrock ignore the first setting.
    $old = ''
    $lines = Get-Content $propsPath | ForEach-Object {
        if ($_ -match '^level-name=') { $old = $_ -replace '^level-name=', ''; "level-name=$Name" }
        else { $_ }
    }
    [System.IO.File]::WriteAllLines($propsPath, $lines, (New-Object System.Text.UTF8Encoding $false))
    "level-name: '$($old.Trim())' -> '$Name'"
    "  the old world is still on disk under worlds\$($old.Trim()) - nothing was deleted."

} finally {
    # --- always bring the server back ------------------------------------
    ""
    "Starting '$TaskName' again..."
    schtasks /Run /TN $TaskName 2>&1 | Out-Null
    Start-Sleep -Seconds 12
    if (Get-Process bedrock_server -EA SilentlyContinue) { "  running." }
    else { "  WARNING: it did not come back - start it from the panel and check the log." }
    if ($cleanup -and (Test-Path $cleanup)) {
        Remove-Item $cleanup -Recurse -Force -EA SilentlyContinue
    }
}

""
Show-Worlds
""
"If the world did not load, check D:\Servers\MinecraftBedrock\logs\minecraft.log."
"The commonest cause is a world saved by a NEWER Minecraft than the server runs -"
"a world can move forwards between versions but never backwards."
