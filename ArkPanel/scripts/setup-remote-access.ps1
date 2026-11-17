<#
    Reach ArkPanel from anywhere, over real HTTPS, without opening a port.

    WHY TAILSCALE RATHER THAN PORT FORWARDING

    Forwarding 8090 to the internet would put a control surface that can stop
    and wipe game servers on the public internet, behind nothing but HTTP Basic
    auth sent in CLEARTEXT. Every scanner on the internet would find it within
    hours. Do not do it.

    Tailscale builds a private encrypted network between YOUR devices only.
    Nothing is exposed publicly, no router changes, and it solves a second
    problem at the same time: `tailscale serve` terminates real HTTPS with a
    genuine Let's Encrypt certificate, which is what Android Chrome requires
    before it will offer "Install app". Over plain http:// it never will.

    So this one step gives you both:
      * the panel installable as a real app on Android
      * access from mobile data, work, anywhere

    THE PANEL'S OWN PASSWORD STILL APPLIES. Tailscale controls which devices
    can reach it; Basic auth controls who can use it. Keep both.

    NEVER run `tailscale funnel` on this. Funnel is the opposite of serve --
    it publishes to the whole internet.
#>

[CmdletBinding()]
param(
    [int]$Port = 8090,
    [switch]$Install,
    [switch]$Status,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

function Find-Tailscale {
    foreach ($p in @("$env:ProgramFiles\Tailscale\tailscale.exe",
                     "${env:ProgramFiles(x86)}\Tailscale\tailscale.exe")) {
        if (Test-Path $p) { return $p }
    }
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$ts = Find-Tailscale

if (-not $ts) {
    if (-not $Install) {
        @"
Tailscale is not installed on this machine.

Install it (either works):

  winget install --id Tailscale.Tailscale
      or download from  https://tailscale.com/download/windows

Then sign in (a free personal account covers this easily), install the
Tailscale app on your PHONE and sign in with the SAME account, and re-run:

  .\setup-remote-access.ps1

Re-run with -Install to have this script invoke winget for you.
"@
        return
    }

    "Installing Tailscale via winget ..."
    winget install --id Tailscale.Tailscale --accept-package-agreements --accept-source-agreements
    $ts = Find-Tailscale
    if (-not $ts) { throw "winget finished but tailscale.exe was still not found. Install manually." }
}

"Using $ts"
""

if ($Remove) {
    & $ts serve --https=443 off
    "Stopped serving the panel over Tailscale HTTPS."
    "Tailscale itself is still installed and connected; run 'tailscale down' to disconnect."
    return
}

# --- is this machine signed in? ---
$state = (& $ts status --json 2>$null) | ConvertFrom-Json
if (-not $state -or $state.BackendState -ne 'Running') {
    @"
Tailscale is installed but not connected (state: $(if ($state) { $state.BackendState } else { 'unknown' })).

Run this, sign in in the browser window it opens, then re-run this script:

  & "$ts" up
"@
    return
}

$dnsName = $state.Self.DNSName.TrimEnd('.')
"Signed in as this machine: $dnsName"
""

if ($Status) {
    & $ts serve status
    return
}

# --- publish the panel over HTTPS inside the tailnet ---
# serve, NOT funnel: reachable by your devices only, never the public internet.
"Publishing http://127.0.0.1:$Port over Tailscale HTTPS ..."
& $ts serve --bg --https=443 "http://127.0.0.1:$Port"

""
"Done. From any of your signed-in devices, anywhere:"
""
"    https://$dnsName/"
""
@"
On your phone:
  1. Install the Tailscale app and sign in with the same account.
  2. Open the URL above in Chrome (Android) or Safari (iOS).
  3. Log in with your ArkPanel username and password.
  4. Android: Chrome menu -> "Install app".  iOS: Share -> "Add to Home Screen".

Because that URL is real HTTPS with a valid certificate, Android will now
offer a proper install rather than just a shortcut.

If it says HTTPS is not enabled for your tailnet, switch it on once in the
admin console under DNS -> HTTPS Certificates, then re-run this script.

Turn it off again with:  .\setup-remote-access.ps1 -Remove
"@
