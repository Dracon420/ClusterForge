<#
    Run a game server and keep its console output in a log file.

    Scheduled tasks throw stdout away. That is fine until something goes
    wrong: a Minecraft server launched by its task left no record at all, so
    when we needed to know which accounts had connected there was nothing to
    read. A server you cannot see the output of is one you can only guess at.

    This sits between the task and the server: it rotates the old log, starts
    the server with output redirected, and WAITS - so the task stays running
    for as long as the server does, and ending the task still stops it.

    Two details that are not decoration:

      * stdin comes from NUL. Minecraft Bedrock stalls at startup with no
        stdin - it printed "NO LOG FILE! - setting up server logging..." and
        then sat there, never opening the world, never binding its port. A
        NUL handle gives its console reader a clean EOF instead.

      * Output is APPENDED, and rotated by size rather than truncated, so a
        crash loop cannot erase the evidence of the first crash.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [Parameter(Mandatory = $true)][string]$WorkDir,
    [Parameter(Mandatory = $true)][string]$LogPath,
    [string]$ServerArgs = '',
    [int]$MaxLogMB = 20,
    [int]$KeepLogs = 5,
    [string]$ConsoleFile = ''
)

$ErrorActionPreference = 'Continue'

$logDir = Split-Path -Parent $LogPath
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

# Rotate if the current log has grown past the limit.
if (Test-Path $LogPath) {
    $sizeMB = (Get-Item $LogPath).Length / 1MB
    if ($sizeMB -ge $MaxLogMB) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        Move-Item $LogPath "$LogPath.$stamp" -Force
        # Keep only the most recent few, oldest first out.
        Get-ChildItem "$logDir" -Filter "$(Split-Path -Leaf $LogPath).*" -EA SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -Skip $KeepLogs |
            Remove-Item -Force -EA SilentlyContinue
    }
}

# [IO.File] rather than Add-Content: PowerShell 5.1's -Encoding utf8 writes a
# BOM, and a BOM part-way through a log file is noise at best. Same reason
# config.json is written this way - see scripts/test_config_encoding.py.
$banner = "=== ArkPanel starting $(Split-Path -Leaf $Exe) at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===`r`n"
[System.IO.File]::AppendAllText($LogPath, $banner, (New-Object System.Text.UTF8Encoding $false))

# Console mode (2026-11-12): Minecraft Bedrock's only clean shutdown is typing
# "stop" into its console. With stdin from NUL nobody could, so every panel
# stop fell through to ending the task, which kills it. In console mode the
# server's stdin is a pipe this script holds, and any line written to
# $ConsoleFile is typed into it (core/minecraft.py send_console). The file is
# replaced atomically by the writer, read here, deleted, then typed.
# "$ConsoleFile.ready" holds this script's PID while the pipe is open, so the
# panel knows the console is there. Bedrock gets it automatically; other
# servers keep stdin from NUL unless -ConsoleFile is passed.
$consoleMode = [bool]$ConsoleFile -or ((Split-Path -Leaf $Exe) -ieq 'bedrock_server.exe')
if ($consoleMode -and -not $ConsoleFile) { $ConsoleFile = Join-Path $WorkDir 'console-in.txt' }

$command = '"' + $Exe + '"'
if ($ServerArgs) { $command += ' ' + $ServerArgs }
$command += ' >> "' + $LogPath + '" 2>&1'

Set-Location $WorkDir
if (-not $consoleMode) {
    # cmd does the redirect because it handles "< NUL" cleanly; PowerShell's own
    # redirection operators do not give the child a real NUL stdin handle.
    & cmd.exe /c ($command + ' < NUL')
    $code = $LASTEXITCODE
} else {
    $ready = "$ConsoleFile.ready"
    Remove-Item -LiteralPath $ConsoleFile, $ready -Force -EA SilentlyContinue
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = Join-Path $env:SystemRoot 'System32\cmd.exe'
    $psi.Arguments = '/s /c "' + $command + '"'
    $psi.WorkingDirectory = $WorkDir
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.CreateNoWindow = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $stdin = $p.StandardInput
    $stdin.AutoFlush = $true
    [System.IO.File]::WriteAllText($ready, "$PID", (New-Object System.Text.UTF8Encoding $false))
    try {
        while (-not $p.HasExited) {
            if (Test-Path -LiteralPath $ConsoleFile) {
                try {
                    $lines = [System.IO.File]::ReadAllLines($ConsoleFile)
                    Remove-Item -LiteralPath $ConsoleFile -Force
                } catch {
                    Start-Sleep -Milliseconds 200      # writer still busy - next pass
                    continue
                }
                foreach ($line in $lines) {
                    $line = $line.Trim()
                    if ($line) { $stdin.WriteLine($line) }
                }
            }
            Start-Sleep -Milliseconds 500
        }
    } finally {
        Remove-Item -LiteralPath $ready -Force -EA SilentlyContinue
    }
    $p.WaitForExit()
    $code = $p.ExitCode
}

[System.IO.File]::AppendAllText($LogPath,
    "=== exited with code $code at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===`r`n",
    (New-Object System.Text.UTF8Encoding $false))
exit $code
