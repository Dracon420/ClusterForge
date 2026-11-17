"""Back up a machine's irreplaceable files to another PC's drive.

Separate from backup.py, which is about ARK saves: tiered snapshots every few
hours, pruned on a schedule. This is the other problem -- somebody's PC, whose
valuable part is a user profile and a handful of configs, wanted weekly.

Three decisions worth stating, because they are the ones that make this useful
rather than merely present:

  * NO /MIR. A mirror deletes from the backup whatever left the source, which
    means anything that wipes his files gets to wipe the copy of them too, one
    scheduled run later. This project exists because an AI with admin rights
    made a mess of a PC; a backup that faithfully reproduces the mess is not a
    backup. Files are added and updated here, never removed.

  * GAMES ARE NOT BACKED UP. Steam, Xbox and Ubisoft installs were 373 GB of
    the 619 GB on his drive and every byte is redownloadable. What cannot be
    redownloaded is his profile, the ARK saves, and the knowledge of WHICH
    games were installed -- so that last one is written down instead.

  * The inventory is part of the backup. Restoring a PC is not only files: it
    is knowing the Windows edition, the hardware, the installed programs and
    the game list. That fits in a few KB of text and is the difference between
    a folder of files and something you can rebuild from.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime

from core.backup import robocopy_args

# Directories never worth carrying across a network, in any profile. Caches
# rebuild themselves; a browser cache alone can be gigabytes of nothing.
SKIP_DIRS = [
    "Temp", "Cache", "Cache2", "CacheStorage", "Code Cache", "GPUCache",
    "ShaderCache", "DXCache", "D3DSCache", "CrashDumps", "Crashpad",
    "INetCache", "WebCache", "Service Worker", "GrShaderCache",
    "Package Cache", "Downloaded Installations", "WER",
    "$RECYCLE.BIN", "System Volume Information",
    # AppData\Local\Microsoft\WindowsApps holds zero-byte app execution aliases
    # -- APPEXECLINK reparse points that launch Store apps. They are not files
    # and cannot be read, which /XJ does not help with because it only covers
    # junctions and mount points. 52 of them accounted for 312 of the 324
    # "failures" in the first clean run. The Store recreates them.
    "WindowsApps",
]

# Files a copy of a RUNNING Windows cannot have, and does not want.
#
# NTUSER.DAT and UsrClass.dat are the per-user registry hives: Windows holds
# them open for as long as the account is logged in, so robocopy can never read
# them, and a half-written hive would be useless to restore anyway. The journal
# and temp files are databases mid-write - they exist when robocopy lists the
# folder and are gone when it reaches them.
#
# Left out of the backup deliberately, rather than failing every run over them.
SKIP_FILES = [
    "NTUSER.DAT*", "UsrClass.dat*", "ntuser.ini",
    "*.sqlite3-journal", "*-journal", "*.tmp", "*.etl", "hiberfil.sys",
    "pagefile.sys", "swapfile.sys",
    # Held open for as long as the browser or Explorer is running. Cookies in
    # particular are a live SQLite database and a session's worth of logins --
    # not something a file copy can take, nor something worth carrying to
    # somebody else's drive.
    "WebCacheLock.dat", "Cookies", "Cookies-journal", "*.lock", "lockfile",
]


def _run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(args, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def copy_tree(src: str, dst: str, exclude_dirs=None, throttle=False) -> dict:
    """One tree, copied without deleting anything at the destination.

    robocopy exit codes below 8 all mean success of some flavour (8 is the
    first real failure); anything at or above 8 is reported with its code so a
    half-done copy is never mistaken for a good one.
    """
    if not os.path.isdir(src):
        return {"src": src, "dst": dst, "ok": False, "detail": "source missing",
                "skipped": True}

    os.makedirs(dst, exist_ok=True)
    args = robocopy_args(src, dst, mirror=False,
                         exclude_dirs=(exclude_dirs or []) + SKIP_DIRS,
                         throttle=throttle, exclude_junctions=True)
    for pattern in SKIP_FILES:
        args += ["/XF", pattern]
    # /XA:O leaves OneDrive's online-only placeholders alone. Without it
    # robocopy asks for each one, which both fails when the sync client is not
    # running AND would pull his entire OneDrive down over his own connection
    # to copy it back across the same link. Those files are already in the
    # cloud; that is what OneDrive is.
    args.append("/XA:O")
    started = datetime.now()
    code, out = _run(args)
    seconds = round((datetime.now() - started).total_seconds(), 1)

    # robocopy's exit code is a bitmask: 1 copied, 2 extra, 4 mismatched,
    # 8 some files could not be copied, 16 fatal. Only 8 and above is failure.
    ok = code < 8
    # robocopy writes its failures as "2026/09/16 23:20:29 ERROR 362 (0x16A)
    # Copying File C:\..." -- a TIMESTAMP first, then ERROR. An earlier version
    # of this looked for lines starting with "ERROR" and so found only the
    # "ERROR: RETRY LIMIT EXCEEDED" summaries, reporting a failed copy with no
    # word of which file failed.
    errors = [line.strip() for line in out.splitlines()
              if re.search(r"\bERROR \d+", line)]
    detail = f"robocopy exit {code} in {seconds}s"
    if errors:
        # The count is the useful number. Two locked files out of forty
        # thousand is a healthy backup; four hundred is something to look at.
        detail += (f" - {len(errors)} file(s) could not be read: "
                   + " | ".join(e[:120] for e in errors[:3]))
    return {"src": src, "dst": dst, "ok": ok, "code": code,
            "seconds": seconds, "detail": detail, "skipped": False,
            "unreadable": len(errors), "errors": errors}


def inventory(root: str) -> str:
    """Write down what a person would need in order to rebuild this PC.

    Deliberately excludes the Windows product key. On any machine of this age
    the key lives in firmware and a reinstall picks it up by itself, so writing
    it into a folder on someone else's drive would add risk and no capability.
    """
    lines = [f"HUBMASTER inventory - {datetime.now():%Y-%m-%d %H:%M}", ""]

    def section(title, args):
        lines.append(f"===== {title} =====")
        code, out = _run(args)
        lines.extend((out or "").rstrip().splitlines() or ["(nothing reported)"])
        lines.append("")

    section("WINDOWS", ["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_OperatingSystem | "
                        "Select-Object Caption,Version,BuildNumber,InstallDate,OSArchitecture | "
                        "Format-List"])
    section("HARDWARE", ["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,"
                         "TotalPhysicalMemory | Format-List; "
                         "Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores | Format-List; "
                         "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion | Format-List; "
                         "Get-Disk | Select-Object Number,FriendlyName,"
                         "@{n='GB';e={[math]::Round($_.Size/1GB)}} | Format-Table -AutoSize"])
    section("INSTALLED PROGRAMS", ["powershell", "-NoProfile", "-Command",
                                   "Get-ItemProperty "
                                   "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
                                   "'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
                                   "'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*' "
                                   "-ErrorAction SilentlyContinue | "
                                   "Where-Object DisplayName | Select-Object DisplayName,DisplayVersion | "
                                   "Sort-Object DisplayName -Unique | Format-Table -AutoSize"])
    # The whole reason game folders are not copied: this list is what makes
    # skipping 373 GB safe.
    section("GAMES INSTALLED (reinstall these)",
            ["powershell", "-NoProfile", "-Command",
             "Get-ChildItem 'C:\\Program Files (x86)\\Steam\\steamapps\\common',"
             "'C:\\XboxGames','C:\\Program Files (x86)\\Ubisoft\\Ubisoft Game Launcher\\games' "
             "-Directory -ErrorAction SilentlyContinue | "
             "Select-Object @{n='Game';e={$_.Name}},@{n='Where';e={$_.Parent.FullName}} | "
             "Sort-Object Game | Format-Table -AutoSize"])
    section("NETWORK", ["powershell", "-NoProfile", "-Command",
                        "Get-NetIPConfiguration | Select-Object InterfaceAlias,IPv4Address,"
                        "IPv4DefaultGateway | Format-List"])

    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "INVENTORY.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def run_jobs(config: dict, log=print, throttle=False) -> dict:
    """Run every configured copy job, then refresh the inventory.

    A failing job never stops the others: losing his pictures because the ARK
    save folder was locked would be a poor trade.
    """
    destination = config.get("destination")
    if not destination:
        return {"ok": False, "error": "no destination configured", "jobs": []}

    parent = os.path.dirname(destination.rstrip("\\/")) or destination
    if not (os.path.isdir(destination) or os.path.isdir(parent)):
        return {"ok": False, "error": f"destination not reachable: {destination}",
                "jobs": []}

    results, started = [], datetime.now()
    for job in config.get("jobs", []):
        name = job.get("name") or os.path.basename(job.get("source", "job"))
        target = os.path.join(destination, name)
        log(f"[filebackup] {name}: {job.get('source')} -> {target}")
        result = copy_tree(job.get("source", ""), target,
                           exclude_dirs=job.get("exclude_dirs"),
                           throttle=throttle)
        result["name"] = name
        results.append(result)
        log(f"[filebackup] {name}: {result['detail']}")

        # The list of what could NOT be copied belongs next to what was, not
        # buried in a log on the other machine. If a file ever matters, this is
        # where you find out it was never there.
        listing = os.path.join(destination, f"_unreadable-{name}.txt")
        if not result.get("errors"):
            # A clean run must clear last run's list, or the folder keeps
            # accusing itself of a problem that has been fixed.
            try:
                if os.path.exists(listing):
                    os.remove(listing)
            except OSError:
                pass
        if result.get("errors"):
            try:
                with open(listing, "w", encoding="utf-8") as fh:
                    fh.write(f"{len(result['errors'])} file(s) could not be read "
                             f"during the backup of {job.get('source')}\n"
                             f"{datetime.now():%Y-%m-%d %H:%M}\n\n")
                    fh.write("\n".join(result["errors"]))
            except OSError as exc:
                log(f"[filebackup] could not write the unreadable list: {exc}")

    inventory_path = None
    try:
        inventory_path = inventory(destination)
        log(f"[filebackup] inventory written to {inventory_path}")
    except Exception as exc:  # noqa: BLE001 - an inventory must not fail a backup
        log(f"[filebackup] inventory failed: {exc}")

    done = [r for r in results if not r["skipped"]]
    return {
        "ok": all(r["ok"] for r in done) if done else False,
        "unreadable": sum(r.get("unreadable", 0) for r in done),
        "destination": destination,
        "inventory": inventory_path,
        "seconds": round((datetime.now() - started).total_seconds(), 1),
        "jobs": results,
        "finished": datetime.now().isoformat(timespec="seconds"),
    }


