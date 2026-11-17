"""
Find and read the ARK server's active log.

The rule here comes from a real incident on PC2: log auto-detection latched
onto a stale numbered file (ShooterGame_2.log, left over from an old crash
loop) instead of the live ShooterGame.log, silently pointing log tailing at a
dead file. The only symptom was frozen data.

So: the active log is whichever candidate was MOST RECENTLY WRITTEN, never
whichever has the highest number or the tidiest name. Stale numbered logs are
reported rather than ignored, because their presence is what makes the mistake
possible in the first place.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

# ShooterGame.log, ShooterGame_2.log, ... but not the -backup- archives.
_ACTIVE_PATTERN = re.compile(r"^ShooterGame(_\d+)?\.log$", re.I)

# The line ARK prints once a server is genuinely joinable. DSM's zombie
# detection keys off the same string, and so does the post-update check.
READY_MARKER = "has completed startup and is now advertising for join"

STALE_AFTER_SECONDS = 3600


@dataclass
class LogInfo:
    path: str
    size: int
    modified: float
    is_active: bool

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.modified)


def scan_logs(log_dir: str) -> list[LogInfo]:
    """Candidate log files, newest first. Backups are excluded."""
    if not os.path.isdir(log_dir):
        return []
    found: list[LogInfo] = []
    try:
        names = os.listdir(log_dir)
    except OSError:
        return []
    for name in names:
        if not _ACTIVE_PATTERN.match(name):
            continue
        path = os.path.join(log_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        found.append(LogInfo(path, st.st_size, st.st_mtime, False))
    found.sort(key=lambda f: f.modified, reverse=True)
    if found:
        found[0].is_active = True
    return found


def active_log(log_dir: str) -> str | None:
    logs = scan_logs(log_dir)
    return logs[0].path if logs else None


def stale_logs(log_dir: str) -> list[LogInfo]:
    """Numbered logs that are not the active one.

    Surfaced in the UI so the operator can clear them, rather than left to
    quietly poison detection later.
    """
    logs = scan_logs(log_dir)
    return [
        f for f in logs[1:]
        if os.path.basename(f.path).lower() != "shootergame.log"
        or f.age_seconds > STALE_AFTER_SECONDS
    ]


def tail(path: str, lines: int = 200, max_bytes: int = 512_000) -> list[str]:
    """Last `lines` lines, reading only the tail of the file.

    ARK logs grow large and the panel polls often, so this never reads the
    whole file into memory.
    """
    if not path or not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # discard the partial first line
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]


def is_ready(path: str, within_lines: int = 4000) -> bool:
    """Whether the log shows the server reached 'advertising for join'."""
    return any(READY_MARKER in ln for ln in tail(path, lines=within_lines))


def snapshot(log_dir: str) -> dict:
    """Record which log is active and how long it is, before starting a server.

    Needed because ARK ROTATES the log on startup. Checking for 'advertising
    for join' without this reads the PREVIOUS run's log and reports the server
    ready within seconds -- which is exactly what happened on the first
    ClusterForge-driven start of The Island.
    """
    path = active_log(log_dir)
    size = 0
    if path:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
    return {"path": path, "size": size, "at": time.time()}


def read_since(log_dir: str, snap: dict, max_bytes: int = 4_000_000) -> str:
    """Log text written since `snap` was taken.

    Handles three cases: the same file grown (read from the old offset), the
    file rotated to a new name, or the same name truncated and reused (size
    smaller than recorded) -- both of the latter mean read from the start.
    """
    path = active_log(log_dir)
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""

    same_file = path == snap.get("path")
    offset = snap.get("size", 0) if (same_file and size >= snap.get("size", 0)) else 0

    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(max_bytes)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def ready_since(log_dir: str, snap: dict) -> bool:
    """True once the server reports it is joinable, in output NEW since snap."""
    return READY_MARKER in read_since(log_dir, snap)


def summarise(log_dir: str) -> dict:
    logs = scan_logs(log_dir)
    stale = stale_logs(log_dir)
    active = logs[0] if logs else None
    return {
        "active": active.path if active else None,
        "active_name": os.path.basename(active.path) if active else None,
        "active_age_seconds": round(active.age_seconds) if active else None,
        "active_size": active.size if active else 0,
        "candidates": len(logs),
        "stale": [
            {"name": os.path.basename(f.path),
             "age_hours": round(f.age_seconds / 3600, 1),
             "size": f.size}
            for f in stale
        ],
    }
