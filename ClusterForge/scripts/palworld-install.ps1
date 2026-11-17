<#
    Install the Palworld dedicated server on this machine with SteamCMD.

    App 2394010 is free and anonymous - the server does not need a Palworld
    licence. Installed to D: rather than C: because Pocketpair are explicit
    that slow or contended storage risks corrupting saves, and D: is a roomy
    SSD.

    Nothing about the ARK cluster is touched. This only downloads.
#>
[CmdletBinding()]
param(
    [string]$SteamCmd = 'D:\Servers\TheIsland\steamcmd\steamcmd.exe',
    [string]$InstallDir = 'D:\Servers\Palworld',
    [string]$AppId = '2394010'
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $SteamCmd)) { throw "steamcmd not found at $SteamCmd" }
New-Item -ItemType Directory -Force $InstallDir | Out-Null

$before = Get-PSDrive D
"free on D: before: {0} GB" -f [math]::Round($before.Free / 1GB, 1)
"installing app $AppId to $InstallDir (this takes a while)..."

# +app_info_update 1 and a retry, both learned the hard way on PC3: a steamcmd
# that has just updated itself answers the first +app_update with
# "Failed to install app ... (Missing configuration)" because its app cache is
# stale. Refreshing the cache and asking twice fixes it.
foreach ($attempt in 1..3) {
    "attempt $attempt..."
    & $SteamCmd +force_install_dir $InstallDir +login anonymous `
        +app_info_update 1 +app_update $AppId validate +quit
    if (Test-Path (Join-Path $InstallDir 'PalServer.exe')) {
        "  PalServer.exe appeared on attempt $attempt"
        break
    }
    "  no PalServer.exe yet"
    Start-Sleep -Seconds 10
}

$exe = Join-Path $InstallDir 'PalServer.exe'
"`nPalServer.exe present: {0}" -f (Test-Path $exe)
if (Test-Path $InstallDir) {
    $size = (Get-ChildItem $InstallDir -Recurse -File -ErrorAction SilentlyContinue |
             Measure-Object -Sum Length).Sum
    "installed size: {0} GB" -f [math]::Round($size / 1GB, 2)
}
$after = Get-PSDrive D
"free on D: after: {0} GB" -f [math]::Round($after.Free / 1GB, 1)

$default = Join-Path $InstallDir 'DefaultPalWorldSettings.ini'
"DefaultPalWorldSettings.ini present: {0}" -f (Test-Path $default)
