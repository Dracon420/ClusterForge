<#
    Allow the ArkPanel web UI (TCP 8090) through Windows Firewall on this PC,
    scoped to the local network only.

    Run ELEVATED on PC1 (the machine running panel.py).

    The panel now requires a username and password, so opening it to the LAN
    exposes an authenticated surface rather than an open one. Before that auth
    existed, anything on the network could have started, stopped and
    reconfigured the servers -- which is why the rule is scoped to the local
    subnet rather than "any".

    Undo with:  allow-panel-firewall.ps1 -Remove
#>

[CmdletBinding()]
param(
    [string]$RuleName = 'ArkPanel Web UI',
    [int]$Port = 8090,
    [string]$Scope = '192.168.1.0/24',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}
if (-not (Test-Admin)) {
    throw "Run this from an ELEVATED PowerShell (right-click > Run as administrator)."
}

if ($Remove) {
    Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule
    "removed firewall rule: $RuleName"
    return
}

Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule

New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $Port -RemoteAddress $Scope `
    -Description 'ArkPanel web UI - LAN only, password protected' | Out-Null

"Created firewall rule '$RuleName'"
"  port   : TCP $Port inbound"
"  scope  : $Scope only (not the whole internet)"
""
Get-NetFirewallRule -DisplayName $RuleName |
    Select-Object DisplayName, Enabled, Direction, Action | Format-Table -AutoSize

$ips = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' } |
    Select-Object -ExpandProperty IPAddress
foreach ($ip in $ips) {
    "Reach the panel at:  http://${ip}:$Port"
}
""
"Sign in with the panel_user / panel_password from secrets.json."
"Undo with: allow-panel-firewall.ps1 -Remove"
