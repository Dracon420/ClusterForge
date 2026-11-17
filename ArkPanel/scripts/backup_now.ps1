<#
    Point-in-time backup of the irreplaceable data for both servers.

    Copies saves, configs, the DSM Profile folder and the start script. It does
    NOT copy the server binaries or mods -- those are ~26 GB each and are
    reproducible with steamcmd, whereas the save files are not reproducible at
    all. The install mirror is a separate job (Phase 5, Tier B) and does not
    need the servers to be down.

    Run this with the servers STOPPED. Copying a live .ark while the server is
    mid-write can produce a torn file that restores to nothing useful.
#>

[CmdletBinding()]
param(
    [string]$Destination = 'I:\ASA-Backups\ArkPanel',
    [switch]$Force   # skip the running-server check
)

$ErrorActionPreference = 'Stop'
$stamp = Get-Date -Format 'yyyy-MM-dd_HHmm'

$servers = @(
    @{ Name = 'island'; Display = 'The Island'
       Saves  = 'D:\Servers\TheIsland\ShooterGame\Saved\SavedArks\TheIsland_WP'
       Config = 'D:\Servers\TheIsland\ShooterGame\Saved\Config\WindowsServer'
       Profile= 'D:\Servers\TheIsland\Profile'
       Bat    = 'D:\Servers\TheIsland\StartServer.bat' }
    @{ Name = 'svartalfheim'; Display = 'Svartalfheim'
       Saves  = 'Z:\Svartalfhiem\ShooterGame\Saved\SavedArks\Svartalfheim'
       Config = 'Z:\Svartalfhiem\ShooterGame\Saved\Config\WindowsServer'
       Profile= 'Z:\Svartalfhiem\Profile'
       Bat    = 'Z:\StartServer.bat' }
)

if (-not $Force) {
    $running = Get-Process ArkAscendedServer -ErrorAction SilentlyContinue
    if ($running) {
        throw ("ArkAscendedServer is running (PID {0}). Stop the servers first, " +
               "or pass -Force to accept possibly-torn save files." -f ($running.Id -join ', '))
    }
}

"ArkPanel backup  $stamp"
"destination: $Destination"
""

$results = @()
foreach ($s in $servers) {
    $root = Join-Path $Destination ("{0}\{1}" -f $s.Name, $stamp)
    "===== $($s.Display) ====="

    if (-not (Test-Path $s.Saves)) {
        "  UNREACHABLE: $($s.Saves)  -- SKIPPED"
        $results += [PSCustomObject]@{ Server=$s.Display; Status='skipped'; GB=0; Files=0; Path='' }
        continue
    }

    New-Item -ItemType Directory -Path $root -Force | Out-Null

    foreach ($part in @(
        @{ Label='SavedArks'; Src=$s.Saves },
        @{ Label='Config';    Src=$s.Config },
        @{ Label='Profile';   Src=$s.Profile })) {

        if (-not (Test-Path $part.Src)) { "  $($part.Label): not present, skipped"; continue }
        $dst = Join-Path $root $part.Label
        # /E all subdirs, /R:1 /W:1 fail fast rather than retrying for minutes
        # over SMB, /MT parallel copy, /NFL /NDL quiet file listing.
        $null = robocopy $part.Src $dst /E /R:1 /W:1 /MT:16 /NFL /NDL /NJH /NJS /NP
        if ($LASTEXITCODE -ge 8) { "  $($part.Label): ROBOCOPY ERROR $LASTEXITCODE" }
        else {
            $f = Get-ChildItem $dst -Recurse -File -ErrorAction SilentlyContinue
            "  {0,-10} {1,5} files  {2,7:N2} GB" -f $part.Label, $f.Count, (($f|Measure-Object Length -Sum).Sum/1GB)
        }
    }

    if (Test-Path $s.Bat) { Copy-Item $s.Bat (Join-Path $root 'StartServer.bat') -Force; "  StartServer.bat copied" }

    $all = Get-ChildItem $root -Recurse -File -ErrorAction SilentlyContinue
    $gb  = [math]::Round((($all | Measure-Object Length -Sum).Sum)/1GB, 2)
    "  TOTAL: $($all.Count) files, $gb GB -> $root"
    ""
    $results += [PSCustomObject]@{ Server=$s.Display; Status='ok'; GB=$gb; Files=$all.Count; Path=$root }
}

"===== SUMMARY ====="
$results | Format-Table -AutoSize
$free = (Get-PSDrive I).Free / 1GB
"I: free after backup: {0:N1} GB" -f $free
