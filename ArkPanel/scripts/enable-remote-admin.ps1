<#
    Open THIS machine to remote administration from the ArkPanel host, and
    nothing else.

    Why: PC1 can copy files to PC2 over the share, but cannot run anything
    there. Every task installation, firewall change or diagnostic has meant
    physically walking to PC2. With this, PC1 can run commands remotely and
    that stops being true.

    WHAT THIS ACTUALLY GRANTS -- read before running:

      Anyone who can authenticate as an administrator FROM the allowed IP gets
      full remote control of this machine. The protections are (a) the Windows
      firewall rules below, scoped to one IP, and (b) Windows account
      passwords. There is no extra layer.

      -AllowLocalAccounts sets LocalAccountTokenFilterPolicy=1, which is
      required for a local (non-domain) admin account to hold admin rights
      over the network. It also removes a UAC restriction that normally blocks
      exactly that. On a workgroup home LAN this is the standard trade; on a
      machine reachable from the internet it would be a bad idea.

    This is reversible:  .\enable-remote-admin.ps1 -Remove

    Run ONCE, elevated, at the machine being opened up (e.g. PC2).
#>

[CmdletBinding()]
param(
    # The ArkPanel host that is allowed in. Nothing else is.
    [string]$PanelIP = '192.168.1.10',
    [switch]$AllowLocalAccounts,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run this elevated (right-click PowerShell, Run as administrator)."
}

$policyPath = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
$ruleGroups = @(
    'Windows Remote Management',
    'Remote Scheduled Tasks Management',
    'Remote Service Management',
    'Remote Event Log Management'
)

if ($Remove) {
    "Closing remote administration on $env:COMPUTERNAME ..."
    try { Disable-PSRemoting -Force -ErrorAction Stop; "  PSRemoting disabled" }
    catch { "  PSRemoting: $($_.Exception.Message)" }

    foreach ($g in $ruleGroups) {
        try {
            Set-NetFirewallRule -DisplayGroup $g -Enabled False -ErrorAction Stop
            "  firewall group disabled: $g"
        } catch { "  (no rules for '$g')" }
    }
    try {
        if (Test-Path $policyPath) {
            New-ItemProperty -Path $policyPath -Name 'LocalAccountTokenFilterPolicy' `
                -PropertyType DWord -Value 0 -Force | Out-Null
            "  LocalAccountTokenFilterPolicy reset to 0"
        }
    } catch { "  LocalAccountTokenFilterPolicy: $($_.Exception.Message)" }
    ""
    "Done. Remote admin is off. The ArkPanel agent on :8767 is unaffected."
    return
}

"Opening remote administration on $env:COMPUTERNAME to $PanelIP only ..."
""

# --- 1. WinRM / PowerShell Remoting -------------------------------------
# SkipNetworkProfileCheck because a home LAN often shows as Public, which
# would otherwise make Enable-PSRemoting refuse.
Enable-PSRemoting -Force -SkipNetworkProfileCheck | Out-Null
"  PowerShell Remoting enabled"

# --- 2. Scope every remote-admin rule to the panel host ------------------
# This is the part that matters. Enable-PSRemoting opens WinRM to the whole
# local subnet by default; narrowing it to one address is what makes this
# acceptable on a shared network.
$scoped = 0
foreach ($g in $ruleGroups) {
    try {
        $rules = Get-NetFirewallRule -DisplayGroup $g -ErrorAction Stop
    } catch {
        "  (no rules for '$g' on this Windows edition - skipped)"
        continue
    }
    foreach ($r in $rules) {
        Set-NetFirewallRule -Name $r.Name -Enabled True -RemoteAddress $PanelIP
        $scoped++
    }
    "  firewall group scoped to ${PanelIP}: $g"
}
"  $scoped rule(s) restricted to $PanelIP"

# --- 3. Local admin accounts over the network ----------------------------
if ($AllowLocalAccounts) {
    # New-Item -Force on this key tries to RECREATE it, which Windows refuses
    # with UnauthorizedAccessException even elevated -- the Policies\System key
    # is protected. It already exists on every Windows install, so only create
    # it in the impossible case that it is missing, and write the value with
    # New-ItemProperty -Force, which overwrites without touching the key.
    if (-not (Test-Path $policyPath)) {
        New-Item -Path $policyPath -Force | Out-Null
    }
    New-ItemProperty -Path $policyPath -Name 'LocalAccountTokenFilterPolicy' `
        -PropertyType DWord -Value 1 -Force | Out-Null
    "  LocalAccountTokenFilterPolicy = 1 (local admin accounts work remotely)"
} else {
    "  LocalAccountTokenFilterPolicy NOT set."
    "    If remote commands fail with 'Access is denied' using a local account,"
    "    re-run this with -AllowLocalAccounts. Read the header first."
}

""
"Now run these ONCE on the panel host ($PanelIP), elevated:"
""
"  # TrustedHosts is a CLIENT setting, but writing it still needs the local"
"  # WinRM service running. Starting the service does NOT open that machine to"
"  # inbound remoting -- that would need Enable-PSRemoting, which it does not"
"  # need and should not have. Do not use 'winrm quickconfig' here: it creates"
"  # an inbound listener as well."
"  Set-Service WinRM -StartupType Automatic"
"  Start-Service WinRM"
"  Set-Item WSMan:\localhost\Client\TrustedHosts -Value '$($env:COMPUTERNAME)' -Force"
""
"Then test it from there:"
""
"  Invoke-Command -ComputerName $($env:COMPUTERNAME) -Credential (Get-Credential) -ScriptBlock { hostname }"
""
"Undo any time with:  .\enable-remote-admin.ps1 -Remove"
