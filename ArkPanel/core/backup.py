"""
Save backups and the install mirror.

Two tiers doing two different jobs:

  Tier A  versioned save snapshots, every 4 hours. Local on each machine, then
          copied to the external drive. These are what protect against
          corruption, a bad mod update or a wipe -- so they are VERSIONED and
          pruned by count, never overwritten.

  Tier B  a single full install mirror on the external, refreshed in place
          during the scheduled restart. This is for rebuilding fast after a
          drive failure. It is deliberately NOT a restore point: corruption
          propagates into it on the next sync. That is precisely why Tier A
          exists and why the two are not interchangeable.

Underneath both sit the game's own 3-minute autosave and .arkrbf rolling
backups, which do not depend on any manager.

Consistency: a .ark copied while the server is mid-write can be torn, so a
SaveWorld is issued and allowed to settle before copying. That is not as good
as copying with the server stopped, but it is far better than a blind copy and
it needs no downtime.

Only the save data, configs, Profile and start script are copied. The server
binaries and mods are ~26 GB and fully reproducible -- steamcmd redownloads the
server and the ARK server reinstalls mods itself from -mods= on boot. The saves
are the only part that cannot be recreated.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime

STAMP_FORMAT = "%Y-%m-%d_%H%M%S"
STAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")

# robocopy returns 0-7 for success (files copied, extras present, etc).
# 8 and above are genuine failures.
ROBOCOPY_OK = 8

SAVE_SETTLE_SECONDS = 8


def robocopy_args(src: str, dst: str, mirror: bool = False,
                  exclude_dirs: list[str] | None = None,
                  throttle: bool = False,
                  exclude_junctions: bool = False) -> list[str]:
    """The command line for one copy, tuned for where it is going.

    A local disk copy wants many threads and no patience: it either works or the
    disk is broken. A copy ACROSS THE NETWORK is the opposite - PC3 sends its
    backups to PC1's drive over Tailscale, and with the local settings (16
    threads, one retry, one second apart) a single blip failed the whole run
    with RETRY LIMIT EXCEEDED, leaving a half-written snapshot offsite.

    So a UNC destination gets fewer threads, real retries, and /Z, which resumes
    a big file where it left off instead of starting again.
    """
    remote = dst.startswith("\\\\") or src.startswith("\\\\")
    if remote:
        # Tuned by measurement, not guesswork: PC3 -> PC1 over Tailscale managed
        # only ~0.4 MB/s with /Z, whose per-block bookkeeping costs far more than
        # the resume ability is worth for files this size. /J (unbuffered) suits
        # the big .ark files, more threads hide the round-trip latency, and
        # /COMPRESS lets SMB compress on the wire - save files compress well.
        tuning = ["/R:5", "/W:10", "/MT:8", "/J", "/COMPRESS"]
        # An offsite copy leaves the house through the SAME upstream link the
        # server uses to talk to players. Saturating it desynced a player
        # mid-session on 2026-09-16: they could run around while nothing they
        # did reached the server. /IPG puts a gap between packets, trading copy
        # speed for a link that still has room for the game.
        if throttle:
            tuning.append("/IPG:20")
    else:
        tuning = ["/R:1", "/W:1", "/MT:16"]
    args = ["robocopy", src, dst, "/E", *tuning,
            "/NFL", "/NDL", "/NJH", "/NJS", "/NP"]
    if exclude_junctions:
        # /XJ, and it is not optional when copying a user profile. Windows keeps
        # legacy junctions in every profile -- "Local Settings" points at
        # AppData\Local, and AppData\Local\Application Data points at ITSELF.
        # Without /XJ robocopy walks straight into them: the first run of the
        # backup of his profile had copied 19.9 GB of a 16 GB profile and was
        # eleven "Application Data" levels deep and still going.
        args.append("/XJ")
    if mirror:
        args.append("/MIR")
    for name in (exclude_dirs or []):
        args += ["/XD", name]
    return args


def _robocopy(src: str, dst: str, mirror: bool = False,
              exclude_dirs: list[str] | None = None,
              throttle: bool = False) -> tuple[bool, str]:
    """Copy a tree. Returns (ok, detail)."""
    if not os.path.isdir(src):
        return False, f"source missing: {src}"
    os.makedirs(dst, exist_ok=True)

    # /MIR (in robocopy_args) also deletes files removed at the source, which is
    # what makes a mirror a mirror rather than an accumulating pile.
    args = robocopy_args(src, dst, mirror, exclude_dirs, throttle)

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=7200)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"robocopy failed: {exc}"

    # /COMPRESS and /J are recent additions; an older Windows rejects the whole
    # command rather than ignoring them. Drop them and copy anyway.
    combined = (result.stdout or "") + (result.stderr or "")
    if result.returncode >= ROBOCOPY_OK and "Invalid Parameter" in combined:
        plain = [a for a in args if a not in ("/COMPRESS", "/J")]
        try:
            result = subprocess.run(plain, capture_output=True, text=True, timeout=7200)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"robocopy failed: {exc}"

    if result.returncode >= ROBOCOPY_OK:
        tail = (result.stdout or result.stderr or "").strip().splitlines()[-3:]
        return False, f"robocopy exit {result.returncode}: {' | '.join(tail)}"
    return True, f"exit {result.returncode}"


def _tree_stats(path: str) -> tuple[int, int]:
    files = 0
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                continue
    return files, total


def _parts_for(server: dict) -> list[tuple[str, str]]:
    """(label, source path) for everything worth snapshotting."""
    install = server["install_dir"]
    return [
        ("SavedArks", server.get("save_dir") or ""),
        ("Config", server.get("config_dir") or ""),
        ("Profile", os.path.join(install, "Profile")),
    ]


def list_backups(root: str, server_name: str) -> list[dict]:
    """Existing snapshots for one server, newest first."""
    base = os.path.join(root, server_name)
    if not os.path.isdir(base):
        return []
    out = []
    for name in os.listdir(base):
        if not STAMP_PATTERN.match(name):
            continue
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        files, size = _tree_stats(path)
        out.append({
            "stamp": name, "path": path, "files": files,
            "bytes": size, "gb": round(size / (1024 ** 3), 2),
            "created": os.path.getmtime(path),
        })
    out.sort(key=lambda b: b["stamp"], reverse=True)
    return out


def _clear_readonly_and_retry(func, path, _exc):
    """rmtree error hook: drop the read-only bit and try the operation again.

    Every snapshot contains Game.ini and GameUserSettings.ini, which are
    read-only ON PURPOSE -- that flag is what stops ARK overwriting them on
    shutdown. robocopy preserves it into the snapshot, and on Windows rmtree
    cannot delete a read-only file. So every snapshot was undeletable, and
    retention never pruned a single one: 58 snapshots and 103 GB piled up on D:
    against a configured keep of 18, until the drive was 97% full.

    The third argument differs between onexc (an exception) and onerror (an
    exc_info tuple); it is unused, so one hook serves both.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: str) -> None:
    # onerror is deprecated from 3.12 in favour of onexc. Supporting both keeps
    # this working whatever Python each PC's agent happens to run.
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_readonly_and_retry)
    else:
        shutil.rmtree(path, onerror=_clear_readonly_and_retry)


def prune(root: str, server_name: str, keep: int) -> tuple[list[str], list[str]]:
    """Delete oldest snapshots beyond `keep`. Returns (removed, failed).

    Failures are RETURNED, not swallowed. The old version caught OSError and
    carried on silently, which is precisely how a retention policy that deleted
    nothing at all went unnoticed until the disk nearly filled.
    """
    if keep <= 0:
        return [], []
    backups = list_backups(root, server_name)
    removed, failed = [], []
    for old in backups[keep:]:
        try:
            _rmtree(old["path"])
            removed.append(old["stamp"])
        except OSError as exc:
            failed.append(f"{old['stamp']}: {exc}")
    return removed, failed


def run_backup(job, server: dict, cfg: dict, rcon_fn=None) -> dict:
    """Take one versioned snapshot, locally and then to the external.

    A failure to reach the external must NOT fail the whole backup: losing the
    offsite copy is bad, losing the local one too because a drive was
    unplugged would be worse.
    """
    name = server["name"]
    backup_cfg = cfg.get("backup", {})
    local_root = backup_cfg.get("local_root") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")
    external_root = backup_cfg.get("external_root")
    stamp = datetime.now().strftime(STAMP_FORMAT)

    # Quiesce the save file before copying it.
    busy = False
    if rcon_fn:
        try:
            rcon_fn(server, "SaveWorld")
            job.step(f"SaveWorld issued; settling {SAVE_SETTLE_SECONDS}s")
            time.sleep(SAVE_SETTLE_SECONDS)
        except Exception as exc:
            job.step(f"SaveWorld failed ({exc}) - copying anyway, saves may be "
                     "slightly behind", level="warn")
        # Is anyone actually playing? An offsite copy shares the upstream link
        # with the game, so it slows itself down rather than desyncing them.
        try:
            listing = (rcon_fn(server, "ListPlayers") or "").strip()
            busy = bool(listing) and "no players" not in listing.lower()
        except Exception:
            busy = False        # unknown: do not slow the copy on a guess

    target = os.path.join(local_root, name, stamp)
    os.makedirs(target, exist_ok=True)

    copied = []
    for label, src in _parts_for(server):
        if not src or not os.path.isdir(src):
            job.step(f"{label}: not present, skipped", level="warn")
            continue
        ok, detail = _robocopy(src, os.path.join(target, label))
        if not ok:
            raise RuntimeError(f"{label} copy failed - {detail}")
        files, size = _tree_stats(os.path.join(target, label))
        copied.append(label)
        job.step(f"{label}: {files} files, {round(size / (1024**3), 2)} GB")

    bat = server.get("start_bat")
    if bat and os.path.exists(bat):
        shutil.copy2(bat, os.path.join(target, "StartServer.bat"))
        job.step("StartServer.bat copied (keeps saves and launch args consistent)")

    files, size = _tree_stats(target)
    job.step(f"Local snapshot: {files} files, {round(size / (1024**3), 2)} GB -> {target}")

    local_keep = int(backup_cfg.get("local_keep", 18))
    removed, failed = prune(local_root, name, local_keep)
    if removed:
        job.step(f"Pruned {len(removed)} old local snapshot(s), keeping {local_keep}")
    if failed:
        job.step(f"Could NOT prune {len(failed)} local snapshot(s) - the disk will keep "
                 f"filling until this is fixed: {failed[0]}", level="warn")

    external = {"attempted": False, "ok": False, "detail": "not configured"}
    if external_root:
        external["attempted"] = True
        parent = os.path.dirname(external_root.rstrip("\\/")) or external_root
        if not os.path.isdir(parent) and not os.path.isdir(external_root):
            external["detail"] = f"external not reachable: {external_root}"
            job.step(f"EXTERNAL COPY SKIPPED - {external['detail']}. "
                     "Local snapshot is fine.", level="warn")
        else:
            ext_target = os.path.join(external_root, name, stamp)
            if busy:
                job.step("Players are on - sending the offsite copy slowly so the "
                         "game keeps its share of the upload")
            ok, detail = _robocopy(target, ext_target, throttle=busy)
            external["ok"] = ok
            external["detail"] = detail
            external["path"] = ext_target
            if ok:
                job.step(f"External copy -> {ext_target}")
                ext_keep = int(backup_cfg.get("external_keep", 84))
                gone, stuck = prune(external_root, name, ext_keep)
                if gone:
                    job.step(f"Pruned {len(gone)} old external snapshot(s), keeping {ext_keep}")
                if stuck:
                    job.step(f"Could NOT prune {len(stuck)} external snapshot(s): "
                             f"{stuck[0]}", level="warn")
            else:
                job.step(f"External copy FAILED ({detail}). Local snapshot is fine.",
                         level="warn")

    return {"stamp": stamp, "path": target, "files": files,
            "gb": round(size / (1024 ** 3), 2), "parts": copied,
            "external": external}


# Excluded from the install mirror. "Saved" holds the save data and logs: it is
# already covered by the versioned snapshots, it is the part the running server
# keeps locked, and including it made robocopy fail with RETRY LIMIT EXCEEDED.
# Leaving it out both fixes that and sharpens the split -- the mirror is the
# INSTALL (binaries and mods, slow to rebuild), the snapshots are the DATA.
MIRROR_EXCLUDE_DIRS = ["Saved", ".sentry-native", "Logs", "Cache", "dumps"]


def reachable(path: str, timeout: float = 5.0) -> bool:
    """Is this path there, answering within `timeout` seconds?

    Needed because os.path.isdir() on a dead UNC path does not fail - it
    BLOCKS, ~30s per call, in kernel SMB code. So the check runs on a throwaway
    thread and the caller stops waiting; the thread is left to unblock in its
    own time rather than being joined.

    Worth the trouble because a robocopy that reaches a dead share cannot be
    recovered at all: the process wedges in uninterruptible I/O, TerminateProcess
    does not take, and subprocess.run's own timeout then blocks forever waiting
    to read pipes that will never close. Its 2-hour timeout sat there for 13
    hours on 2026-09-23 while three of his maps stayed down. The only reliable
    fix is not to start.
    """
    result = {}

    def probe():
        try:
            result["ok"] = os.path.isdir(path)
        except OSError:
            result["ok"] = False

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    thread.join(timeout)
    return bool(result.get("ok"))


def run_mirror(job, server: dict, cfg: dict) -> dict:
    """Refresh the install mirror in place on the external drive.

    Deliberately a mirror and not a snapshot: it costs its size once rather
    than per run. It is a fast-rebuild path after a drive or hardware failure,
    NOT a restore point -- corruption reaches it on the next sync.

    Save data is excluded, so this can run safely while the server is up.
    """
    mirror_root = cfg.get("backup", {}).get("mirror_root")
    if not mirror_root:
        raise RuntimeError("No mirror_root configured")

    src = server["install_dir"]
    dst = os.path.join(mirror_root, server["name"])

    # Refuse rather than hang. mirror_root is a share on another machine, and
    # when it is away robocopy does not fail, it wedges - unkillably.
    if not reachable(mirror_root):
        raise RuntimeError(
            f"mirror destination {mirror_root} is not responding - skipping the "
            "mirror rather than risking a robocopy that cannot be killed")

    job.step(f"Mirroring {src} -> {dst}")
    job.step(f"Excluding {', '.join(MIRROR_EXCLUDE_DIRS)} - save data is covered "
             "by the versioned snapshots and is what the live server keeps locked")

    before_files, before_size = _tree_stats(dst) if os.path.isdir(dst) else (0, 0)
    started = time.time()
    ok, detail = _robocopy(src, dst, mirror=True, exclude_dirs=MIRROR_EXCLUDE_DIRS)
    if not ok:
        raise RuntimeError(
            f"Mirror failed - {detail}. Exit 8 or above usually means locked "
            "files; check nothing else is holding the install directory open."
        )

    files, size = _tree_stats(dst)
    job.step(f"Mirror now {files} files, {round(size / (1024**3), 2)} GB "
             f"(was {round(before_size / (1024**3), 2)} GB) in "
             f"{round(time.time() - started)}s")
    return {"path": dst, "files": files, "gb": round(size / (1024 ** 3), 2)}


def restore(job, server: dict, cfg: dict, stamp: str, source_root: str,
            parts: list[str] | None = None) -> dict:
    """Restore a snapshot over the live server directories.

    Destructive and heavily gated. The caller must have confirmed the server is
    STOPPED -- restoring under a running server would be overwritten instantly
    and could corrupt the save. A safety snapshot of current state is taken
    first, so a restore can itself be undone.
    """
    name = server["name"]
    src_base = os.path.join(source_root, name, stamp)
    if not os.path.isdir(src_base):
        raise RuntimeError(f"Snapshot not found: {src_base}")

    from . import procwatch
    if procwatch.find_server_process(server) is not None:
        raise RuntimeError(
            "Refusing to restore while the server is running. Stop it first - "
            "a live server would overwrite the restored files immediately."
        )

    job.step("Taking a safety snapshot of current state before overwriting")
    safety = run_backup(job, server, cfg, rcon_fn=None)
    job.step(f"Safety snapshot: {safety['stamp']}")

    wanted = parts or ["SavedArks", "Config"]
    restored = []
    for label, dest in _parts_for(server):
        if label not in wanted or not dest:
            continue
        src = os.path.join(src_base, label)
        if not os.path.isdir(src):
            job.step(f"{label}: not in this snapshot, skipped", level="warn")
            continue
        ok, detail = _robocopy(src, dest, mirror=True)
        if not ok:
            raise RuntimeError(f"Restore of {label} failed - {detail}")
        restored.append(label)
        job.step(f"{label}: restored from {stamp}")

    return {"stamp": stamp, "restored": restored,
            "safety_snapshot": safety["stamp"]}
