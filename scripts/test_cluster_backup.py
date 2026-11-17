"""
Prove the cluster transfer backup keeps the right copies.

Runs entirely in a temp folder - never touches the real ClusterData.

Run:  python scripts\\test_cluster_backup.py
"""

import os
import shutil
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import clusterbackup as cb                               # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got} (expected {want})")
    if not ok:
        FAILED.append(label)


def write(path, data, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


tmp = tempfile.mkdtemp(prefix="clusterforge_clusterbackup_")
try:
    cluster = os.path.join(tmp, "ClusterData")
    store = os.path.join(tmp, "store")
    mirror = os.path.join(tmp, "mirror")
    rel = os.path.join("clusters", "DraconCluster", "00028f47")
    player = os.path.join(cluster, rel)
    quiet = lambda m: None  # noqa: E731

    print("A new upload is copied only once it has settled:")
    write(player, b"X" * 9414, mtime=1_000_000.0)
    b = cb.ClusterBackup(cluster, store, mirror, logger=quiet, keep_versions=3)
    check("first look: waits, copies nothing", b.poll_once(), [])
    check("second look, unchanged: copied", b.poll_once(), [rel])
    check("third look: not copied again", b.poll_once(), [])
    check("one version stored", len(b.versions(rel)), 1)
    check("mirror written too", len(b.versions(rel, mirror)), 1)
    stored = open(b.versions(rel)[0]["path"], "rb").read()
    check("stored bytes are exact", (len(stored), stored == b"X" * 9414), (9414, True))

    print("\nA file still being written is not copied mid-write:")
    write(player, b"Y" * 100, mtime=1_000_100.0)
    b.poll_once()                                     # sees change, waits
    write(player, b"Y" * 5000, mtime=1_000_101.0)     # still changing
    check("changed again before settling: nothing copied", b.poll_once(), [])
    check("settles: now copied", b.poll_once(), [rel])
    check("the COMPLETE file was kept (5000 bytes)", b.versions(rel)[0]["size"], 5000)

    print("\nA restart does not duplicate what is already stored:")
    b2 = cb.ClusterBackup(cluster, store, mirror, logger=quiet, keep_versions=3)
    b2.poll_once()
    check("restarted watcher copies nothing new", b2.poll_once(), [])
    check("still two versions", len(b2.versions(rel)), 2)

    print("\nA run of empty writes cannot prune away the last good character:")
    for n in range(6):                                # ARK emptying the file repeatedly
        write(player, b"", mtime=1_000_200.0 + n)
        b2.poll_once()
        b2.poll_once()
    before = b2.versions(rel)
    check("empty versions were recorded", sum(1 for v in before if v["size"] == 0) >= 6, True)
    b2.prune(rel)
    after = b2.versions(rel)
    non_empty = [v for v in after if v["size"] > 0]
    check("newest non-empty version survives pruning", len(non_empty) >= 1, True)
    check("it is the 5000-byte character", non_empty[0]["size"] if non_empty else None, 5000)
    check("otherwise trimmed to keep_versions (+1 protected)", len(after) <= 4, True)

    print("\nPutting a character back into the transfer folder:")
    write(player, b"", mtime=1_000_300.0)          # ARK emptied it after a download
    b2.poll_once(); b2.poll_once()
    listed = b2.characters()
    check("the character is listed for restoring", [c["id"] for c in listed], ["00028f47"])
    check("its live file is empty right now", listed[0]["live_size"], 0)
    out = b2.restore("00028f47")
    check("restore reports success", out.get("ok"), True)
    check("it picked the newest NON-empty version", out.get("bytes"), 5000)
    check("the transfer folder now holds the character", os.path.getsize(player), 5000)
    check("the empty file it replaced was saved first", bool(out.get("previous_saved_as")), True)
    check("restoring an unknown id is refused", b2.restore("nobody").get("error"), "unknown_character")
    check("restoring a version that is not stored is refused",
          b2.restore("00028f47", "20200101-000000.000000__1b").get("error"), "no_version")
    before_count = len(b2.versions(rel))
    b2.poll_once(); b2.poll_once()
    check("the watcher does not re-version what it just restored",
          len(b2.versions(rel)), before_count)

    print("\nOnly the machine holding the folder watches it:")
    os.environ["COMPUTERNAME"] = "HOME-SERVER-1"
    check("UNC on this host", cb.runs_on_this_machine(r"\\HOME-SERVER-1\ClusterData"), True)
    check("case-insensitive (hostname is 'Home-Server-1')",
          cb.runs_on_this_machine(r"\\home-server-1\ClusterData"), True)
    os.environ["COMPUTERNAME"] = "HOME-SERVER-2"
    check("UNC on the other PC (names differ by one digit)",
          cb.runs_on_this_machine(r"\\HOME-SERVER-1\ClusterData"), False)

    print("\nAn unreachable folder is reported, not crashed on:")
    gone = cb.ClusterBackup(os.path.join(tmp, "missing"), store, logger=quiet)
    check("poll returns nothing", gone.poll_once(), [])
    check("error recorded for the status page", bool(gone.last_error), True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed - the real ClusterData was never touched")
