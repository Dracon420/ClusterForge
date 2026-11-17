"""
Version every file in the cluster transfer folder the moment it changes.

The cluster folder (ClusterData) is where a survivor, tame or item lives while it
is between maps. During a transfer it exists nowhere else: uploading removes it
from the source map, and if the destination then crashes -- which happened on
2026-09-14 -- that one small file is the character.

The regular server backups never covered it, and simply adding it to them would
not have helped: they run every four hours, while an upload happens seconds
before the crash it needs protecting from. So this watches instead, and keeps a
version of each file every time it settles on new content.

Three rules shape it:

  Copy only once a file has stopped changing. A copy taken mid-write would be a
  truncated character that looks like a backup.

  Never prune away the newest NON-EMPTY version. ARK empties a player's file to
  0 bytes once they have downloaded their uploads, so a crash partway through a
  download can leave it empty. A count-based prune on its own could let a run of
  empty writes delete the last good copy -- exactly the one you would need.

  Report failures rather than swallowing them. The server backups silently
  pruned nothing for a week while the disk filled; a silent transfer backup
  would fail the same way, at the worst possible moment.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timedelta

POLL_SECONDS = 10
KEEP_VERSIONS = 40
KEEP_DAYS = 30

# 20260914-172701.123456__9414b
VERSION_RE = re.compile(r"^(\d{8}-\d{6}\.\d{6})__(\d+)b$")


def version_name(mtime: float, size: int) -> str:
    return datetime.fromtimestamp(mtime).strftime("%Y%m%d-%H%M%S.%f") + f"__{size}b"


def parse_version(name: str):
    """(mtime-as-datetime, size) from a version file name, or None."""
    m = VERSION_RE.match(name)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S.%f"), int(m.group(2))


def runs_on_this_machine(cluster_dir: str) -> bool:
    """Only the PC that physically holds the cluster folder should watch it.

    Both agents carry the same cluster_dir. Watching it from the other PC too
    would copy every upload twice, once over the network.
    """
    path = cluster_dir or ""
    if not path.startswith("\\\\"):
        return os.path.isdir(path)
    host = path.lstrip("\\").split("\\", 1)[0]
    return host.lower() == os.environ.get("COMPUTERNAME", "").lower()


class ClusterBackup:
    def __init__(self, cluster_dir: str, store_root: str, mirror_root: str | None = None,
                 logger=print, poll_seconds: float = POLL_SECONDS,
                 keep_versions: int = KEEP_VERSIONS, keep_days: int = KEEP_DAYS):
        self.cluster_dir = cluster_dir
        self.store_root = store_root
        self.mirror_root = mirror_root
        self.log = logger
        self.poll_seconds = poll_seconds
        self.keep_versions = keep_versions
        self.keep_days = keep_days

        self._stop = threading.Event()
        self._thread = None
        self._seen: dict[str, tuple] = {}     # rel -> last signature observed
        self._saved: dict[str, tuple] = {}    # rel -> signature already stored
        self.last_snapshot = None             # {"at", "file", "size"}
        self.last_error = None
        self.snapshots_taken = 0
        self._unreachable_logged = False

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="arkpanel-clusterbackup")
        self._thread.start()
        self.log(f"[cluster-backup] watching {self.cluster_dir} every {self.poll_seconds}s "
                 f"-> {self.store_root}")

    def stop(self) -> None:
        self._stop.set()

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        last_prune = 0.0
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - the watcher must never die
                self.last_error = f"{datetime.now():%H:%M:%S} poll failed: {exc}"
                self.log(f"[cluster-backup] {self.last_error}")
            if time.time() - last_prune > 3600:
                self.prune_all()
                last_prune = time.time()
            self._stop.wait(self.poll_seconds)

    # ---------- watching ----------

    # ARK writes its transfer file under a temporary name first. Versioning those
    # filled the restore list with a phantom "character" whose name ended in
    # .tmpprofile, which is exactly the sort of thing someone would restore by
    # mistake while panicking about a lost survivor.
    IGNORE_SUFFIXES = (".tmp", ".tmpprofile", ".bak")

    def _scan(self) -> dict[str, tuple]:
        found = {}
        for dirpath, _dirs, files in os.walk(self.cluster_dir):
            for name in files:
                if name.lower().endswith(self.IGNORE_SUFFIXES):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, self.cluster_dir)
                found[rel] = (st.st_mtime, st.st_size)
        return found

    def _stored_signature(self, rel: str):
        """Signature of the newest version already on disk, so a restart does not
        re-copy everything it already has."""
        folder = os.path.join(self.store_root, rel)
        best = None
        for name in (os.listdir(folder) if os.path.isdir(folder) else []):
            parsed = parse_version(name)
            if parsed and (best is None or parsed[0] > best[0]):
                best = parsed
        if best is None:
            return None
        return (best[0].strftime("%Y%m%d-%H%M%S.%f"), best[1])

    def poll_once(self) -> list[str]:
        """One pass. Returns the files snapshotted."""
        if not os.path.isdir(self.cluster_dir):
            if not self._unreachable_logged:
                self.last_error = f"{datetime.now():%H:%M:%S} cluster folder not reachable: {self.cluster_dir}"
                self.log(f"[cluster-backup] {self.last_error}")
                self._unreachable_logged = True
            return []
        self._unreachable_logged = False

        taken = []
        current = self._scan()
        for rel, sig in current.items():
            key = (datetime.fromtimestamp(sig[0]).strftime("%Y%m%d-%H%M%S.%f"), sig[1])

            if rel not in self._saved:
                self._saved[rel] = self._stored_signature(rel)

            if self._seen.get(rel) != sig:
                # Changed since the last look: wait for it to settle before copying.
                self._seen[rel] = sig
                continue

            if self._saved.get(rel) != key:
                if self._snapshot(rel, sig):
                    self._saved[rel] = key
                    taken.append(rel)
        return taken

    def _snapshot(self, rel: str, sig: tuple) -> bool:
        src = os.path.join(self.cluster_dir, rel)
        try:
            with open(src, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            self.last_error = f"{datetime.now():%H:%M:%S} cannot read {rel}: {exc}"
            self.log(f"[cluster-backup] {self.last_error}")
            return False

        # If it changed while being read, the next poll will see a new signature
        # and try again once it settles.
        try:
            if os.stat(src).st_size != len(data):
                return False
        except OSError:
            return False

        name = version_name(sig[0], len(data))
        ok = self._write(self.store_root, rel, name, data, sig[0])
        if ok:
            self.snapshots_taken += 1
            self.last_snapshot = {"at": time.time(), "file": rel, "size": len(data)}
            self.log(f"[cluster-backup] saved {rel} ({len(data)} bytes)")
        if self.mirror_root:
            if not self._write(self.mirror_root, rel, name, data, sig[0]):
                self.log(f"[cluster-backup] mirror copy of {rel} FAILED - local copy kept")
        return ok

    def _write(self, root: str, rel: str, name: str, data: bytes, mtime: float) -> bool:
        folder = os.path.join(root, rel)
        dest = os.path.join(folder, name)
        try:
            os.makedirs(folder, exist_ok=True)
            if os.path.exists(dest):
                return True
            tmp = dest + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.utime(tmp, (mtime, mtime))
            os.replace(tmp, dest)
            return True
        except OSError as exc:
            self.last_error = f"{datetime.now():%H:%M:%S} cannot write {dest}: {exc}"
            self.log(f"[cluster-backup] {self.last_error}")
            return False

    # ---------- retention ----------

    def versions(self, rel: str, root: str | None = None) -> list[dict]:
        folder = os.path.join(root or self.store_root, rel)
        out = []
        for name in (os.listdir(folder) if os.path.isdir(folder) else []):
            parsed = parse_version(name)
            if parsed:
                out.append({"name": name, "when": parsed[0], "size": parsed[1],
                            "path": os.path.join(folder, name)})
        out.sort(key=lambda v: v["when"], reverse=True)
        return out

    def prune(self, rel: str, root: str | None = None) -> tuple[list[str], list[str]]:
        """Drop old versions of one file. Returns (removed, failed)."""
        vers = self.versions(rel, root)
        if not vers:
            return [], []
        protect = next((v["name"] for v in vers if v["size"] > 0), None)
        cutoff = datetime.now() - timedelta(days=self.keep_days)
        removed, failed = [], []
        for index, v in enumerate(vers):
            if v["name"] == protect:
                continue
            if index < self.keep_versions and v["when"] >= cutoff:
                continue
            try:
                os.remove(v["path"])
                removed.append(v["name"])
            except OSError as exc:
                failed.append(f"{v['name']}: {exc}")
        return removed, failed

    def _tracked_files(self, root: str) -> list[str]:
        rels = set()
        for dirpath, _dirs, files in os.walk(root):
            if any(parse_version(f) for f in files):
                rels.add(os.path.relpath(dirpath, root))
        return sorted(rels)

    def prune_all(self) -> None:
        for root in filter(None, (self.store_root, self.mirror_root)):
            if not os.path.isdir(root):
                continue
            for rel in self._tracked_files(root):
                _removed, failed = self.prune(rel, root)
                for item in failed:
                    self.last_error = f"{datetime.now():%H:%M:%S} prune failed {rel}: {item}"
                    self.log(f"[cluster-backup] {self.last_error}")

    # ---------- putting one back ----------

    def characters(self) -> list[dict]:
        """One entry per backed-up account: its versions, newest first.

        The id is the player's account id, which is also the file name ARK uses
        in the cluster folder.
        """
        out = []
        for rel in (self._tracked_files(self.store_root) if os.path.isdir(self.store_root) else []):
            if rel.lower().endswith(self.IGNORE_SUFFIXES):
                continue        # temp files kept from before they were ignored
            vers = self.versions(rel)
            live = os.path.join(self.cluster_dir, rel)
            try:
                live_size = os.path.getsize(live)
            except OSError:
                live_size = None
            out.append({
                "id": os.path.basename(rel),
                "file": rel.replace("\\", "/"),
                "live_size": live_size,
                "versions": [{"name": v["name"], "when": v["when"].isoformat(), "size": v["size"]}
                             for v in vers],
                "latest_non_empty": next((v["name"] for v in vers if v["size"] > 0), None),
            })
        return sorted(out, key=lambda c: c["id"])

    def restore(self, account_id: str, version: str | None = None) -> dict:
        """Put a stored copy of one character back into the cluster transfer folder.

        The player can then download it on any map. Two rules make this safe to
        offer as a button:

          The file that is there now is snapshotted first, under its own real
          mtime, so restoring the wrong version is itself undoable.

          Default to the newest NON-EMPTY version. The newest version is often
          0 bytes - that is what ARK leaves after a successful download - and
          restoring that would hand the player an empty transfer slot.
        """
        matches = [rel for rel in self._tracked_files(self.store_root)
                   if os.path.basename(rel).lower() == (account_id or "").lower()]
        if not matches:
            return {"ok": False, "error": "unknown_character",
                    "message": f"no backups stored for {account_id}"}
        rel = matches[0]
        vers = self.versions(rel)
        if version:
            chosen = next((v for v in vers if v["name"] == version), None)
        else:
            chosen = next((v for v in vers if v["size"] > 0), None)
        if chosen is None:
            return {"ok": False, "error": "no_version",
                    "message": "that version is not stored" if version
                               else "this character has no non-empty backup to restore"}

        live = os.path.join(self.cluster_dir, rel)
        try:
            data = open(chosen["path"], "rb").read()
        except OSError as exc:
            return {"ok": False, "error": "read_failed", "message": str(exc)}

        # Snapshot whatever is there now, so this is reversible.
        saved_current = None
        if os.path.exists(live):
            try:
                st = os.stat(live)
                current = open(live, "rb").read()
                name = version_name(st.st_mtime, len(current))
                if self._write(self.store_root, rel, name, current, st.st_mtime):
                    saved_current = name
            except OSError as exc:
                return {"ok": False, "error": "backup_failed",
                        "message": f"could not save the current file first: {exc}"}

        stamp = chosen["when"].timestamp()
        try:
            os.makedirs(os.path.dirname(live), exist_ok=True)
            tmp = live + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            # Carry the original upload's timestamp across, so the restored file
            # IS that upload as far as everything here is concerned - and the
            # watcher recognises it instead of storing a duplicate.
            os.utime(tmp, (stamp, stamp))
            os.replace(tmp, live)
        except OSError as exc:
            return {"ok": False, "error": "write_failed", "message": str(exc)}

        self._saved[rel] = (chosen["when"].strftime("%Y%m%d-%H%M%S.%f"), chosen["size"])
        self._seen[rel] = (stamp, chosen["size"])
        self.log(f"[cluster-backup] RESTORED {rel} from {chosen['name']} ({len(data)} bytes)")
        return {"ok": True, "id": account_id, "restored": chosen["name"],
                "bytes": len(data), "taken_at": chosen["when"].isoformat(),
                "previous_saved_as": saved_current}

    # ---------- reporting ----------

    def status(self) -> dict:
        files = []
        root = self.store_root
        for rel in (self._tracked_files(root) if os.path.isdir(root) else []):
            vers = self.versions(rel)
            newest = vers[0] if vers else None
            good = next((v for v in vers if v["size"] > 0), None)
            files.append({
                "file": rel.replace("\\", "/"),
                "versions": len(vers),
                "newest": newest["when"].isoformat() if newest else None,
                "newest_size": newest["size"] if newest else None,
                "latest_non_empty": good["when"].isoformat() if good else None,
                "latest_non_empty_size": good["size"] if good else None,
            })
        return {
            "running": self.running(),
            "cluster_dir": self.cluster_dir,
            "store_root": self.store_root,
            "mirror_root": self.mirror_root,
            "poll_seconds": self.poll_seconds,
            "keep_versions": self.keep_versions,
            "keep_days": self.keep_days,
            "snapshots_taken": self.snapshots_taken,
            "last_snapshot": self.last_snapshot,
            "last_error": self.last_error,
            "files": files,
        }
