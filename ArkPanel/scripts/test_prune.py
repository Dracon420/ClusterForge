"""
Prove backup retention actually deletes snapshots -- including the read-only
ini files every real snapshot contains.

Retention silently pruned NOTHING for over a week: robocopy preserves the
read-only flag on Game.ini and GameUserSettings.ini, rmtree cannot delete a
read-only file on Windows, and the error was swallowed. 58 snapshots and 103 GB
built up against a keep of 18.

Runs entirely in a temp directory. Never touches real backups.

Run:  python scripts\\test_prune.py
"""

import os
import shutil
import stat
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import backup                                          # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got} (expected {want})")
    if not ok:
        FAILED.append(label)


def make_snapshot(root, server, stamp, readonly=True):
    """A snapshot shaped like the real ones, read-only inis included."""
    config = os.path.join(root, server, stamp, "Config")
    os.makedirs(config)
    os.makedirs(os.path.join(root, server, stamp, "SavedArks"))
    for name in ("Game.ini", "GameUserSettings.ini"):
        path = os.path.join(config, name)
        with open(path, "w") as fh:
            fh.write("[ServerSettings]\n")
        if readonly:
            os.chmod(path, stat.S_IREAD)
    with open(os.path.join(root, server, stamp, "SavedArks", "world.ark"), "w") as fh:
        fh.write("save")


def stamps(root, server):
    return sorted(os.listdir(os.path.join(root, server)))


root = tempfile.mkdtemp(prefix="arkpanel_prune_test_")
try:
    print("Read-only inis (the real-world case) - 10 snapshots, keep 3:")
    for day in range(1, 11):
        make_snapshot(root, "island", f"2026-09-{day:02d}_120000")
    removed, failed = backup.prune(root, "island", keep=3)
    left = stamps(root, "island")
    check("snapshots removed", len(removed), 7)
    check("failures", failed, [])
    check("snapshots left", len(left), 3)
    check("the NEWEST three are kept", left,
          ["2026-09-08_120000", "2026-09-09_120000", "2026-09-10_120000"])

    print("\nUnder the limit - nothing deleted:")
    removed, failed = backup.prune(root, "island", keep=5)
    check("removed when already under keep", removed, [])

    print("\nkeep <= 0 is a no-op, never 'delete everything':")
    removed, failed = backup.prune(root, "island", keep=0)
    check("keep=0 removes nothing", removed, [])
    check("all 3 still there", len(stamps(root, "island")), 3)

    print("\nA snapshot that really cannot be deleted is REPORTED, not swallowed:")
    make_snapshot(root, "svart", "2026-09-01_120000", readonly=False)
    make_snapshot(root, "svart", "2026-09-02_120000", readonly=False)
    real = backup._rmtree
    backup._rmtree = lambda p: (_ for _ in ()).throw(PermissionError("locked by a scan"))
    try:
        removed, failed = backup.prune(root, "svart", keep=1)
    finally:
        backup._rmtree = real
    check("nothing claimed as removed", removed, [])
    check("the failure is returned", len(failed), 1)
    check("failure names the snapshot", failed[0].startswith("2026-09-01_120000"), True)
finally:
    # Clean up even if a check blew up, read-only files and all.
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            os.chmod(os.path.join(dirpath, f), stat.S_IWRITE)
    shutil.rmtree(root, ignore_errors=True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed - no real backup was touched")
