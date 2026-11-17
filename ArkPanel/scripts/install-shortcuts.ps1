<#
    Creates Desktop and Start Menu shortcuts for ArkPanel.

    Uses pythonw.exe so no console window flashes up. Fully reversible --
    run with -Remove to delete both shortcuts again. Nothing else on the
    system is touched: no registry keys, no services, no file associations.
#>

[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$base      = Split-Path -Parent $PSScriptRoot
$launcher  = Join-Path $base 'ArkPanel.py'
$icon      = Join-Path $base 'static\arkpanel.ico'
$desktop   = Join-Path ([Environment]::GetFolderPath('Desktop')) 'ArkPanel.lnk'
$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'ArkPanel.lnk'

if ($Remove) {
    foreach ($p in @($desktop, $startMenu)) {
        if (Test-Path $p) { Remove-Item $p -Force; "removed $p" }
        else { "not present: $p" }
    }
    return
}

if (-not (Test-Path $launcher)) { throw "Launcher not found: $launcher" }

# pythonw.exe runs without a console window.
$python = (Get-Command python).Source
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$shell = New-Object -ComObject WScript.Shell
foreach ($path in @($desktop, $startMenu)) {
    $sc = $shell.CreateShortcut($path)
    $sc.TargetPath       = $pythonw
    $sc.Arguments        = '"{0}"' -f $launcher
    $sc.WorkingDirectory = $base
    $sc.Description      = 'ArkPanel - DraconCluster server management'
    if (Test-Path $icon) { $sc.IconLocation = $icon }
    $sc.Save()
    "created $path"
}

"`nTarget : $pythonw"
"Args   : $launcher"
"Icon   : $icon"
"`nRun with -Remove to undo."
