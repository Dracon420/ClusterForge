<#
    Put the Minecraft server on a port a console will actually find.

    WHY THIS IS NEEDED AT ALL. A Bedrock client discovers LAN servers by
    broadcasting RakNet pings to exactly two ports: 19132 and 19133. Not a
    range, not the port in your config - those two. A server on any other
    port is invisible to LAN discovery no matter how healthy it is, and
    nothing anywhere says so.

    THE FAILURE, 2026-10-29 to 10-31. A child's Xbox stopped showing the
    server under Worlds -> LAN. Everything was up: newest build, correct
    properties, her gamertag on the allow list, world loaded, answering
    pings. The PC could see it. The Xbox could not. Mapping every
    discovery port found the whole story in five lines:

        IPv4 :19132  silent      <- BedrockConnect used to answer here
        IPv4 :19133  silent
        IPv6 :19132  silent
        IPv6 :19133  protocol 2225   the server
        IPv4 :7300   protocol 2225   the server

    The server's IPv4 port was 7300, which is never scanned. Its IPv6 port
    was 19133, which IS scanned - and that is the only reason the PC found
    it, because PCs scan IPv6 too and the console apparently does not. The
    console's IPv4 entry had been BedrockConnect on 19132, which first fell
    behind on protocol and then moved to NetherNet and stopped answering
    RakNet at all. So the IPv4 side went quiet and the Xbox went blind.

    So: move the server's IPv4 port onto 19133 and the console finds the
    REAL server directly, on a matching protocol, with nothing in between.

    IT REVERTS ITSELF if that does not work. server.properties is backed up
    before anything is touched, and if the server does not come back up
    answering on the new port, the old file goes back and the server is
    restarted on it. A child who cannot play is bad; a child who cannot
    play AND a server left in a worse state is worse.

    Run elevated - the server runs as SYSTEM under Task Scheduler:
        .\fix-lan-discovery.ps1 -WhatIf
        .\fix-lan-discovery.ps1
#>

[CmdletBinding()]
param(
    [int]$Port = 19133,
    [string]$PanelDir = 'D:\Servers\ClusterForge',
    [string]$InstallDir = 'D:\Servers\MinecraftBedrock',
    [string]$ConnectDir = 'D:\Servers\BedrockConnect',
    [string]$TaskName = 'ClusterForge minecraft Server',
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$props = Join-Path $InstallDir 'server.properties'
$serversJson = Join-Path $ConnectDir 'servers.json'

function Invoke-Panel([string]$code) {
    $tmp = Join-Path $env:TEMP ("mclan-{0}.py" -f [guid]::NewGuid().ToString('N'))
    [System.IO.File]::WriteAllText($tmp, $code, (New-Object System.Text.UTF8Encoding $false))
    try { & python -I $tmp | Out-Host; return $LASTEXITCODE }
    finally { Remove-Item $tmp -Force -EA SilentlyContinue }
}

function Get-Prop([string]$key) {
    $line = Get-Content $props | Where-Object { $_ -match "^$key=" } | Select-Object -First 1
    if ($line) { ($line -replace "^$key=", '').Trim() } else { '' }
}

function Set-Prop([string]$key, [string]$value) {
    # Written without a BOM: PowerShell 5.1's -Encoding utf8 adds one, and a
    # BOM in server.properties makes Bedrock ignore the first setting.
    $found = $false
    $lines = Get-Content $props | ForEach-Object {
        if ($_ -match "^$key=") { $found = $true; "$key=$value" } else { $_ }
    }
    if (-not $found) { $lines = $lines + "$key=$value" }
    [System.IO.File]::WriteAllLines($props, $lines, (New-Object System.Text.UTF8Encoding $false))
}

# ---- what a console can see right now -----------------------------------
$oldPort = Get-Prop 'server-port'
$portV6 = Get-Prop 'server-portv6'
"Minecraft : $InstallDir"
"  server-port   : $oldPort"
"  server-portv6 : $portV6"
"  target        : $Port  (one of the two ports LAN discovery scans)"
""
"What answers on the ports a console scans:"
$mapPy = @"
import socket, struct, sys
sys.path.insert(0, r'$PanelDir')
MAGIC = bytes.fromhex('00ffff00fefefefefdfdfdfd12345678')
packet = b'\x01' + struct.pack('>q', 0) + MAGIC + struct.pack('>q', 2)

def probe(family, host, port):
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(2)
    try:
        sock.sendto(packet, (host, port))
        data, _ = sock.recvfrom(4096)
        return data[35:].decode('utf-8', 'replace').split(';')
    except Exception:
        return None
    finally:
        sock.close()

visible = False
for label, family, host in (('IPv4', socket.AF_INET, '127.0.0.1'),
                            ('IPv6', socket.AF_INET6, '::1')):
    for port in (19132, 19133):
        fields = probe(family, host, port)
        if fields:
            print(f'  {label} :{port:<6} -> protocol {fields[2]:<6} {fields[1]}')
            if label == 'IPv4':
                visible = True
        else:
            print(f'  {label} :{port:<6} -> silent')
print()
if visible:
    print('  An IPv4 LAN scan finds something. A console should see it.')
else:
    print('  NOTHING answers an IPv4 scan of 19132 or 19133.')
    print('  A console looks at those two ports and nowhere else, so it')
    print('  sees no server here at all - however healthy the server is.')
sys.exit(0 if visible else 4)
"@
$mapRc = Invoke-Panel $mapPy

if ($oldPort -eq "$Port") {
    ""
    "server-port is already $Port. Nothing to change here."
    if ($mapRc -eq 4) {
        "But nothing answered an IPv4 scan - check the server is running."
    }
    return
}

# ---- is the port free? --------------------------------------------------
# Checked before stopping anything. BedrockConnect sits on 19132, so 19133
# is the one left - and a server that cannot bind its port starts, logs a
# line nobody reads, and serves nobody.
$holder = Get-NetUDPEndpoint -LocalPort $Port -EA SilentlyContinue |
          Where-Object { $_.LocalAddress -eq '0.0.0.0' }
if ($holder) {
    # Not $pid - that is a read-only automatic variable in PowerShell and
    # assigning to it throws, which would only ever surface on the one path
    # that matters here: the port being taken.
    $holderPid = $holder.OwningProcess | Select-Object -First 1
    $holderName = (Get-Process -Id $holderPid -EA SilentlyContinue).ProcessName
    if ($holderName -ne 'bedrock_server') {
        throw "UDP $Port (IPv4) is already held by $holderName (pid $holderPid). Pick another of 19132/19133."
    }
}

""
"Change to make:"
"  server-port : $oldPort -> $Port"
if ((Test-Path $serversJson) -and ((Get-Content $serversJson -Raw) -match "\b$oldPort\b")) {
    "  BedrockConnect servers.json : port $oldPort -> $Port"
}

if ($WhatIf) { ""; "-WhatIf: stopping here. Nothing was changed."; return }

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run this elevated - the server runs as SYSTEM under Task Scheduler and cannot be stopped otherwise."
}

# ---- back up before touching anything ----------------------------------
$quarantine = Join-Path $InstallDir '_Cleanup'
if (-not (Test-Path $quarantine)) { New-Item -ItemType Directory -Path $quarantine -Force | Out-Null }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$propsBackup = Join-Path $quarantine "server.properties-$stamp"
Copy-Item $props $propsBackup
$jsonBackup = ''
if (Test-Path $serversJson) {
    $jsonBackup = Join-Path $quarantine "servers.json-$stamp"
    Copy-Item $serversJson $jsonBackup
}
""
"Backed up to _Cleanup\$(Split-Path -Leaf $propsBackup) (not deleted)."

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
Set-Prop 'server-port' "$Port"
"server-port set to $Port."

if ($jsonBackup) {
    $raw = Get-Content $serversJson -Raw
    if ($raw -match "\b$oldPort\b") {
        # BedrockConnect's menu entry points at an address:port. Leaving it on
        # the old port would give a console a menu entry that goes nowhere.
        [System.IO.File]::WriteAllText($serversJson,
            ($raw -replace "\b$oldPort\b", "$Port"),
            (New-Object System.Text.UTF8Encoding $false))
        "BedrockConnect servers.json repointed to $Port."
    }
}

# ---- start --------------------------------------------------------------
""
"Starting '$TaskName' again..."
schtasks /Run /TN $TaskName 2>&1 | Out-Null
Start-Sleep -Seconds 15

# ---- prove a console could see it, or put everything back --------------
""
"Verifying with an IPv4 ping to $Port (what a console actually does):"
$verifyRc = Invoke-Panel @"
import socket, struct, sys, time
MAGIC = bytes.fromhex('00ffff00fefefefefdfdfdfd12345678')
packet = b'\x01' + struct.pack('>q', 0) + MAGIC + struct.pack('>q', 2)
for _ in range(12):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2)
    try:
        sock.sendto(packet, ('127.0.0.1', $Port))
        data, _ = sock.recvfrom(4096)
        fields = data[35:].decode('utf-8', 'replace').split(';')
        print(f'  IPv4 :$Port -> protocol {fields[2]}  {fields[1]}')
        print()
        print('  A console scanning the LAN will now find this directly.')
        sys.exit(0)
    except Exception:
        time.sleep(3)
    finally:
        sock.close()
print('  nothing answered on IPv4 :$Port after 36s.')
sys.exit(1)
"@

if ($verifyRc -ne 0) {
    ""
    "It did not come up answering on $Port. Putting everything back..."
    schtasks /End /TN $TaskName 2>&1 | Out-Null
    Start-Sleep -Seconds 8
    Copy-Item $propsBackup $props -Force
    if ($jsonBackup) { Copy-Item $jsonBackup $serversJson -Force }
    schtasks /Run /TN $TaskName 2>&1 | Out-Null
    Start-Sleep -Seconds 12
    "  reverted to server-port=$oldPort and restarted."
    "  Check $InstallDir\logs\minecraft.log for why it would not bind $Port."
    exit 1
}

""
"Done. Have her open Minecraft on the Xbox and look under Worlds -> LAN."
"If anything else was pointed at port $oldPort, it needs repointing to $Port."
