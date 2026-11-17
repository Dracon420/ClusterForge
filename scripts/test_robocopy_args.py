"""
Prove a copy across the network is told to be patient.

PC3 sends its snapshots to PC1's drive over Tailscale. With the local-disk settings
(16 threads, one retry, one second apart) that copy failed with RETRY LIMIT EXCEEDED
and left a half-written snapshot offsite (2026-09-16).

Run:  python scripts\\test_robocopy_args.py
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.backup import robocopy_args                                # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got} (expected {want})")
    if not ok:
        FAILED.append(label)


local = robocopy_args(r"C:\ASA-Server-3\Astraeos", r"C:\backups\astraeos\2026-09-16")
print("Local disk copy - fast and impatient:")
check("many threads", "/MT:16" in local, True)
check("one retry", "/R:1" in local, True)
check("no restartable mode (pure overhead locally)", "/Z" in local, False)

print("\nCopy to another PC over the network:")
remote = robocopy_args(r"C:\ASA-Server-3\Astraeos", r"\\100.64.0.99\I\ASA-Backups\astraeos")
check("threads to hide network latency", "/MT:8" in remote, True)
check("five retries", "/R:5" in remote, True)
check("ten seconds between retries", "/W:10" in remote, True)
check("unbuffered, for the big .ark files", "/J" in remote, True)
check("SMB compression on the wire", "/COMPRESS" in remote, True)
check("no /Z - it cost more than it saved (0.4 MB/s measured)", "/Z" in remote, False)

print("\nReading FROM a share counts as remote too:")
pull = robocopy_args(r"\\HOME-SERVER-2\ASA-Server-2\Svartalfhiem", r"D:\restore")
check("patient settings", "/R:5" in pull and "/MT:8" in pull, True)

print("\nThrottled, for when players are online:")
busy = robocopy_args(r"C:\snap", r"\\100.64.0.99\I\ASA-Backups\astraeos", throttle=True)
check("packet gap added", "/IPG:20" in busy, True)
check("still patient", "/R:5" in busy and "/J" in busy, True)
quiet = robocopy_args(r"C:\snap", r"\\100.64.0.99\I\ASA-Backups\astraeos")
check("no gap when nobody is playing", "/IPG:20" in quiet, False)
local_busy = robocopy_args(r"C:\snap", r"D:\backups\astraeos", throttle=True)
check("local copies never throttled (they share nothing)", "/IPG:20" in local_busy, False)

print("\nMirror and exclusions still work:")
mirror = robocopy_args(r"C:\install", r"\\host\share\mirror", mirror=True,
                       exclude_dirs=["Saved", "Logs"])
check("mirror flag", "/MIR" in mirror, True)
check("both exclusions", mirror.count("/XD"), 2)
check("still patient", "/R:5" in mirror, True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed")
