<#
    Store a Discord bot token in secrets.json without it ever appearing on a
    command line, in a screenshot, or in PowerShell history.

    A token pasted as an ARGUMENT ends up in your shell history, in the
    scrollback, and in any screenshot of the window -- which is exactly how the
    first bouncer token was burned. This prompts for it instead: the typing is
    masked, the value never becomes part of a command, and nothing is echoed.

    Usage:
        .\set-bot-token.ps1                       # prompts for which + value
        .\set-bot-token.ps1 -Key discord_bouncer_token
#>

[CmdletBinding()]
param(
    [string]$Key,
    # Resolved in the BODY, not here: $PSScriptRoot is not populated yet while
    # PowerShell evaluates param() defaults, so using it above gives an empty
    # string and Split-Path throws before the script has done anything.
    [string]$SecretsPath
)

$ErrorActionPreference = 'Stop'

if (-not $SecretsPath) {
    $root = $PSScriptRoot
    if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $SecretsPath = Join-Path (Split-Path -Parent $root) 'secrets.json'
}

if (-not (Test-Path $SecretsPath)) { throw "secrets.json not found at $SecretsPath" }

$known = @(
    'discord_bouncer_token',
    'discord_guardian_token',
    'discord_watcher_token'
)

if (-not $Key) {
    "Which token are you setting?"
    for ($i = 0; $i -lt $known.Count; $i++) { "  [$($i+1)] $($known[$i])" }
    $choice = Read-Host "Number (or type a key name)"
    if ($choice -match '^\d+$' -and [int]$choice -ge 1 -and [int]$choice -le $known.Count) {
        $Key = $known[[int]$choice - 1]
    } else {
        $Key = $choice.Trim()
    }
}
if (-not $Key) { throw "No key given." }

""
"Paste the token for '$Key' and press Enter."
"Nothing will be shown as you type or paste - that is expected."
$secure = Read-Host "Token" -AsSecureString

$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if ([string]::IsNullOrWhiteSpace($plain)) { throw "Nothing entered - aborted." }
$plain = $plain.Trim()

# A Discord bot token is three dot-separated parts. Catching a mistyped paste
# here beats a bot that simply never connects with no explanation.
if ($plain -notmatch '^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$') {
    Write-Warning "That does not look like a Discord bot token (expected three dot-separated parts)."
    if ((Read-Host "Save it anyway? (y/N)") -notmatch '^[Yy]') { throw "Aborted." }
}

$json = Get-Content $SecretsPath -Raw | ConvertFrom-Json
$json | Add-Member -NotePropertyName $Key -NotePropertyValue $plain -Force

# Keep a copy before overwriting: secrets.json holds every other credential too.
Copy-Item $SecretsPath "$SecretsPath.bak" -Force

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($SecretsPath, ($json | ConvertTo-Json -Depth 10), $utf8NoBom)

$plain = $null
[System.GC]::Collect()

""
"Saved '$Key' to secrets.json ($($json.PSObject.Properties.Name.Count) keys total)."
"The value was never printed. Restart the panel to pick it up:"
"  Stop-ScheduledTask -TaskName 'ArkPanel Web'; Start-Sleep 3; Start-ScheduledTask -TaskName 'ArkPanel Web'"
