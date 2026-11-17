<#
    Move the Bedrock server between RakNet and NetherNet, and prove it came
    back up.

    WHY, AND WHY WE RESISTED. Mojang deprecated RakNet in 1.26 and made
    NetherNet the default. On 2026-10-20 we measured NetherNet here and
    rejected it, for four recorded reasons: joining by address died because
    the server opened a RANDOM udp port known only to signalling;
    BedrockConnect broke because its menu entries are addresses and there
    was no longer an address; the panel lost its player count; and the Xbox
    found the world under Worlds -> LAN either way, so NetherNet bought us
    nothing. That decision is written up in core/minecraft_transport.py.

    WHAT CHANGED, 2026-10-31. Three of those four reasons have expired:

      * server-udp-ports now exists. It pins NetherNet's UDP port, or maps a
        public port to a local one. The "random port" objection is dead -
        the property is sitting commented out in our own server.properties.
      * BedrockConnect 1.72.1 moved to NetherNet itself. Its log now reads
        "[ OK ] [ NETHERNET ] Server is now running". It is no longer a
        RakNet thing we would be stranding.
      * RakNet stopped working. Not deprecated - stopped. In 1.26.61 the
        server still ANSWERS pings over RakNet, so it looks alive and shows
        up in LAN lists, but clients cannot complete a connection. A PC that
        could see the world got an error joining it; an Xbox saw nothing.

    That last one is the trap, and it caught me. The server log prints
    "NetherNet is the only supported transport type" and has printed it
    since 1.26.52, when it was premature - a child played for three days
    with that error on screen. The text never changed when it became TRUE,
    so the one signal that would have saved an evening looked exactly like
    the noise it had been all week. The lesson is not "read the log". It is
    that a warning which cries wolf for six days is indistinguishable from
    one that means it, and only a connection test tells them apart.

    A PING IS NOT A CONNECTION. That is the whole moral. This script
    verifies by looking for the listening sockets and a clean startup, and
    it still cannot tell you a human can join - so it says so and asks.

    Run elevated - the server runs as SYSTEM under Task Scheduler:
        .\switch-minecraft-transport.ps1 -To nethernet -WhatIf
        .\switch-minecraft-transport.ps1 -To nethernet
        .\switch-minecraft-transport.ps1 -To raknet          # go back
#>

[CmdletBinding()]
param(
    [ValidateSet('nethernet', 'raknet')]
    [string]$To = 'nethernet',
    # The signalling socket. Under NetherNet this is TCP, not UDP, and it
    # must not collide with BedrockConnect, which holds TCP 19132.
    [int]$SignalPort = 19133,
    # Pinned so it is knowable and forwardable. Leaving it empty hands the
    # choice to the OS ephemeral range, which is what made NetherNet
    # unusable for us a fortnight ago.
    [int]$UdpPort = 19134,
    [int]$RaknetPort = 19133,
    [string]$InstallDir = 'D:\Servers\MinecraftBedrock',
    [string]$TaskName = 'ClusterForge minecraft Server',
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$props = Join-Path $InstallDir 'server.properties'
$logFile = Join-Path $InstallDir 'logs\minecraft.log'

function Get-Prop([string]$key) {
    $line = Get-Content $props | Where-Object { $_ -match "^$key=" } | Select-Object -First 1
    if ($line) { ($line -replace "^$key=", '').Trim() } else { '' }
}

function Set-Prop([string]$key, [string]$value) {
    # No BOM: PowerShell 5.1's -Encoding utf8 writes one, and a BOM in
    # server.properties makes Bedrock ignore the first setting in the file.
    $found = $false
    $lines = Get-Content $props | ForEach-Object {
        if ($_ -match "^$key=") { $found = $true; "$key=$value" } else { $_ }
    }
    if (-not $found) { $lines = $lines + "$key=$value" }
    [System.IO.File]::WriteAllLines($props, $lines, (New-Object System.Text.UTF8Encoding $false))
}

$from = Get-Prop 'transport'
"Minecraft : $InstallDir"
"  transport now : $from"
"  transport to  : $To"
if ($from -eq $To) { ""; "Already on $To. Nothing to change."; return }

if ($To -eq 'nethernet') {
    "  signalling    : TCP $SignalPort   (server-port)"
    "  gameplay udp  : UDP $UdpPort      (server-udp-ports, pinned)"
    # server-portv6 is ignored under NetherNet - one dual-stack socket
    # serves both families - so it is left alone rather than tidied away.
    # Putting it back is then just a matter of flipping transport.
    $busy = Get-NetTCPConnection -LocalPort $SignalPort -State Listen -EA SilentlyContinue
    if ($busy) {
        $holderPid = $busy.OwningProcess | Select-Object -First 1
        $holderName = (Get-Process -Id $holderPid -EA SilentlyContinue).ProcessName
        throw "TCP $SignalPort is already held by $holderName (pid $holderPid). BedrockConnect sits on 19132 - pick a third port."
    }
} else {
    "  gameplay      : UDP $RaknetPort  (server-port, one of the two LAN-scanned ports)"
}

if ($WhatIf) { ""; "-WhatIf: stopping here. Nothing was changed."; return }

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run this elevated - the server runs as SYSTEM under Task Scheduler and cannot be stopped otherwise."
}

# ---- back up first ------------------------------------------------------
$quarantine = Join-Path $InstallDir '_Cleanup'
if (-not (Test-Path $quarantine)) { New-Item -ItemType Directory -Path $quarantine -Force | Out-Null }
$backup = Join-Path $quarantine ("server.properties-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
Copy-Item $props $backup
""
"Backed up to _Cleanup\$(Split-Path -Leaf $backup) (not deleted)."

# Remember where the log ends, so the check below reads only THIS startup.
# The transport error has been printing for over a week; searching the whole
# file would find a true-looking hit no matter what we did.
$logMark = if (Test-Path $logFile) { (Get-Item $logFile).Length } else { 0 }

# ---- stop ---------------------------------------------------------------
""
"Stopping '$TaskName'..."
schtasks /End /TN $TaskName 2>&1 | Out-Null
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process bedrock_server -EA SilentlyContinue)) { break }
    Start-Sleep -Seconds 2
}
if (Get-Process bedrock_server -EA SilentlyContinue) {
    throw "bedrock_server is still running after 90s. Nothing was changed."
}
"  stopped."

# ---- apply --------------------------------------------------------------
""
Set-Prop 'transport' $To
if ($To -eq 'nethernet') {
    Set-Prop 'server-port' "$SignalPort"
    Set-Prop 'server-udp-ports' "$UdpPort"
    "transport=nethernet, server-port=$SignalPort, server-udp-ports=$UdpPort"
} else {
    Set-Prop 'server-port' "$RaknetPort"
    "transport=raknet, server-port=$RaknetPort"
}

# ---- start --------------------------------------------------------------
""
"Starting '$TaskName' again..."
schtasks /Run /TN $TaskName 2>&1 | Out-Null
Start-Sleep -Seconds 18

# ---- did it come up, and did it come up on the transport we asked for? --
""
"Checking this startup only (ignoring a week of older log lines):"
$fresh = ''
if (Test-Path $logFile) {
    $stream = [System.IO.File]::Open($logFile, 'Open', 'Read', 'ReadWrite')
    try {
        if ($stream.Length -gt $logMark) {
            $stream.Position = $logMark
            $reader = New-Object System.IO.StreamReader($stream)
            $fresh = $reader.ReadToEnd()
        }
    } finally { $stream.Dispose() }
}

$started = $fresh -match 'Server started\.'
$complained = $fresh -match 'TRANSPORT TYPE ERROR'
$running = [bool](Get-Process bedrock_server -EA SilentlyContinue)

"  process running      : $running"
"  'Server started.'    : $started"
"  transport complaint  : $complained"

if ($To -eq 'nethernet') {
    $tcp = [bool](Get-NetTCPConnection -LocalPort $SignalPort -State Listen -EA SilentlyContinue)
    $udp = [bool](Get-NetUDPEndpoint -LocalPort $UdpPort -EA SilentlyContinue)
    "  TCP $SignalPort signalling : $tcp"
    "  UDP $UdpPort gameplay    : $udp"
    $ok = $running -and $started -and $tcp -and (-not $complained)
} else {
    $udp = [bool](Get-NetUDPEndpoint -LocalPort $RaknetPort -EA SilentlyContinue)
    "  UDP $RaknetPort gameplay    : $udp"
    $ok = $running -and $started -and $udp
}

if (-not $ok) {
    ""
    "That did not come up cleanly. Putting server.properties back..."
    schtasks /End /TN $TaskName 2>&1 | Out-Null
    Start-Sleep -Seconds 8
    Copy-Item $backup $props -Force
    schtasks /Run /TN $TaskName 2>&1 | Out-Null
    Start-Sleep -Seconds 12
    "  reverted to transport=$from and restarted."
    "  See $logFile for why."
    exit 1
}

""
"Up on $To."
""
# Stated plainly because the whole reason this evening went wrong is that
# a healthy-looking server was not a joinable one.
"THIS DOES NOT PROVE ANYONE CAN JOIN. The sockets are open and the server"
"stopped complaining, which is exactly how it looked while nobody could"
"get in. Someone has to actually try it:"
""
"  1. On the PC: Play -> Worlds, look for the LAN entry, and JOIN it."
"     Seeing it listed is not the test. Getting in is the test."
"  2. On the Xbox: Play -> Worlds -> LAN."
if ($To -eq 'nethernet') {
    "  3. Also check the Friends tab - NetherNet servers can surface there."
    ""
    "If nobody can join, go back with:"
    "  .\switch-minecraft-transport.ps1 -To raknet"
} else {
    ""
    "If nobody can join, try NetherNet with:"
    "  .\switch-minecraft-transport.ps1 -To nethernet"
}
