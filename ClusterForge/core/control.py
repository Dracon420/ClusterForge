"""
Start, stop and restart ARK servers.

Long operations run as background jobs. A stop with a 30-minute warning ladder
cannot be a synchronous HTTP request, and a restart that dies with its request
would leave a server half-stopped.

Every rule here comes from an incident in the cluster briefing:

  - A shutdown is only "done" once the process has ACTUALLY EXITED. Sending
    DoExit and assuming success is how two ARK processes once ended up writing
    to the same save files for several minutes.
  - Nothing starts while any process for that map is still alive.
  - The real PID is re-read after every start, never trusted from before.
  - Player warnings go out as ServerChat -- there is no Broadcast -- and are
    sanitised first, because the RCON allowlist rejects ( ) $ & | < > \\ and
    would otherwise silently refuse the message.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import logtail, procwatch
from . import rcon as rcon_mod

# Newlines and carriage returns only. The long list that used to live here
# (; & | ` $ ( ) { } < > \) existed because commands were passed through
# PatchRaptor's rcon_wrapper.BAT, where those are shell metacharacters and an
# injection risk. ClusterForge now opens the socket itself, so there is no shell to
# inject into and a countdown message may contain brackets again. Line breaks
# still go, because they would split one command into two on the wire.
FORBIDDEN_RCON_CHARS = set('\n\r')

DEFAULT_STOP_TIMEOUT = 180
DEFAULT_START_TIMEOUT = 600
READY_POLL_SECONDS = 5


def sanitise_message(text: str) -> tuple[str, bool]:
    """Strip characters the RCON allowlist rejects.

    Returns (clean, was_changed). Better to send a slightly altered warning
    than to have the whole message silently refused because someone typed
    a bracket.
    """
    # A newline becomes a SPACE, not nothing: dropping it outright turned
    # "two\nlines" into "twolines" and would run the last word of one line into
    # the first of the next.
    clean = "".join(" " if c in FORBIDDEN_RCON_CHARS else c for c in (text or ""))
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean, clean != re.sub(r"\s+", " ", text or "").strip()


# --------------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    kind: str
    server: str
    state: str = "running"          # running | done | failed
    steps: list[dict] = field(default_factory=list)
    error: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def step(self, message: str, level: str = "info") -> None:
        self.steps.append({"t": round(time.time() - self.started, 1),
                           "level": level, "message": message})

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "server": self.server,
            "state": self.state, "steps": self.steps, "error": self.error,
            "started": self.started, "finished": self.finished,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
        }


class JobRunner:
    """Tracks background control operations, one at a time per server."""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._active: dict[str, str] = {}   # server -> job id
        self._lock = threading.Lock()

    def active_for(self, server: str) -> Job | None:
        with self._lock:
            job_id = self._active.get(server)
            job = self._jobs.get(job_id) if job_id else None
            return job if job and job.state == "running" else None

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[dict]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.started, reverse=True)
        return [j.to_dict() for j in jobs[:limit]]

    def start(self, kind: str, server: str, fn) -> Job:
        """Begin a job, refusing if one is already running for that server."""
        with self._lock:
            existing_id = self._active.get(server)
            existing = self._jobs.get(existing_id) if existing_id else None
            if existing and existing.state == "running":
                raise RuntimeError(
                    f"{existing.kind} is already running for {server}. "
                    "Wait for it to finish rather than starting a second operation."
                )
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, server=server)
            self._jobs[job.id] = job
            self._active[server] = job.id

        def runner():
            try:
                fn(job)
                if job.state == "running":
                    job.state = "done"
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                job.state = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.step(str(exc), level="error")
            finally:
                job.finished = time.time()

        threading.Thread(target=runner, daemon=True).start()
        return job


JOBS = JobRunner()


# ----------------------------------------------------------------- helpers

def rcon_command(server: dict, cmd: str, rcon_tool: str | None = None) -> str:
    """Run one RCON command synchronously, from any thread.

    Speaks the protocol itself through core.rcon. This used to call
    PatchRaptor's async RCONManager, which brought two problems with it: a
    licence that forbids ClusterForge being passed to anyone else, and an event
    loop. asyncio.run() REFUSES to run inside a thread that already has a
    running loop -- exactly the case when a Discord bot builds its status embed
    from inside its own -- and the exception surfaced as "Players: unknown".
    A plain socket has no loop and works identically from any thread.

    rcon_tool is accepted and ignored. Every caller passes it, and they can
    stop at their own pace rather than in one sweeping edit.
    """
    return rcon_mod.execute_for(server, cmd)


def player_count(server: dict, rcon_tool: str) -> int | None:
    names = player_names(server, rcon_tool)
    return None if names is None else len(names)


# ListPlayers prints one line per player: "0. Player3, 00028f470605...".
# The id is deliberately not surfaced -- a player name is enough to know who is
# on, and platform ids do not belong on a page that can be opened from a phone.
_PLAYER_LINE = re.compile(r"^\s*\d+\.\s*(?P<name>.+?)\s*,\s*[0-9a-fx]+\s*$", re.I)


def player_names(server: dict, rcon_tool: str) -> list[str] | None:
    """Who is connected, or None if the query failed.

    None is not an empty list. Reporting an unknown result as "nobody online"
    would let an update skip its warning countdown and drop players without
    notice, which is the whole reason this distinction exists.
    """
    try:
        out = (rcon_command(server, "ListPlayers", rcon_tool) or "").strip()
    except Exception:
        return None
    if not out or "no players" in out.lower():
        return []

    names = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _PLAYER_LINE.match(line)
        # An unparsed line still counts as a player -- better a placeholder
        # than a count that silently drops someone because the format shifted.
        names.append(match.group("name") if match else line)
    return names


_PLAYER_WITH_ID = re.compile(r"^\s*\d+\.\s*(?P<name>.+?)\s*,\s*(?P<id>[0-9a-f]{16,40})\s*$", re.I)


def parse_player_ids(out: str) -> list[dict]:
    """ListPlayers output as [{"name", "id"}], for kicking a stuck login.

    Only lines that parse cleanly are returned: kicking needs the exact account
    id, and a guessed one could hit the wrong player.
    """
    players = []
    for line in (out or "").splitlines():
        match = _PLAYER_WITH_ID.match(line.strip())
        if match:
            players.append({"name": match.group("name"), "id": match.group("id").lower()})
    return players


# ------------------------------------------------------------------ actions

def do_stop(job: Job, server: dict, rcon_tool: str, warn_minutes: list[int],
            message: str, save: bool = True,
            timeout: int = DEFAULT_STOP_TIMEOUT) -> None:
    """Warn, save, exit, and confirm the process is really gone."""
    proc = procwatch.find_server_process(server)
    if proc is None:
        job.step("Server is not running - nothing to stop")
        return
    job.step(f"Server running as PID {proc.pid}, uptime {proc.uptime_human}")

    players = player_count(server, rcon_tool)
    job.step(f"Players connected: {players if players is not None else 'unknown'}")

    if warn_minutes and players:
        # Walk the ladder (30, 15, 10, 5, 4, 3, 2, 1): announce each rung, then
        # wait the GAP to the next one. The last rung is followed by its own
        # duration, so "1 minute" means a real minute before shutdown.
        rungs = sorted({int(m) for m in warn_minutes if int(m) > 0}, reverse=True)
        job.step(f"Warning ladder: {', '.join(str(r) for r in rungs)} minutes")

        for index, minutes in enumerate(rungs):
            next_rung = rungs[index + 1] if index + 1 < len(rungs) else 0
            gap_seconds = (minutes - next_rung) * 60

            text, changed = sanitise_message(message.replace("{minutes}", str(minutes)))
            if changed:
                job.step("Warning text contained characters RCON rejects; sent sanitised",
                         level="warn")
            try:
                rcon_command(server, f"ServerChat {text}", rcon_tool)
                job.step(f"Warned players: {minutes} minute(s) remaining")
            except Exception as exc:
                job.step(f"Warning at {minutes}m failed ({exc}) - continuing", level="warn")

            # Sleep in one-second slices so Cancel stays responsive.
            deadline = time.time() + gap_seconds
            while time.time() < deadline:
                if job.cancel.is_set():
                    job.step("Cancelled during countdown - server left running", level="warn")
                    job.state = "failed"
                    job.error = "cancelled"
                    return
                time.sleep(1)
    elif warn_minutes:
        job.step("No players connected - skipping the warning countdown")

    if save:
        try:
            rcon_command(server, "SaveWorld", rcon_tool)
            job.step("SaveWorld issued")
            time.sleep(5)
        except Exception as exc:
            job.step(f"SaveWorld failed: {exc}", level="warn")

    try:
        rcon_command(server, "DoExit", rcon_tool)
        job.step("DoExit issued")
    except Exception as exc:
        job.step(f"DoExit failed: {exc}", level="warn")

    # The critical part: confirm the process actually exited.
    deadline = time.time() + timeout
    while time.time() < deadline:
        if procwatch.find_server_process(server) is None:
            job.step(f"Process exited after {round(time.time() - job.started)}s")
            return
        time.sleep(2)

    raise RuntimeError(
        f"Server did not exit within {timeout}s. It is still running - "
        "NOT starting anything else. Use Kill if it is genuinely hung."
    )


def do_start(job: Job, server: dict, wait_ready: bool = True,
             timeout: int = DEFAULT_START_TIMEOUT,
             wipe_wild_dinos: bool = False, rcon_tool: str = "") -> None:
    """Launch via StartServer.bat, then verify it actually came up.

    wipe_wild_dinos runs DestroyWildDinos once the server is joinable. It is a
    parameter rather than a config lookup so it fires only where it is asked
    for -- on the scheduled restart. A wild wipe on every autostart would mean
    a power cut silently erased every untamed dino on the map.
    """
    duplicates = procwatch.find_duplicates(server)
    if duplicates:
        raise RuntimeError(
            f"Refusing to start: ArkAscendedServer is already running for this map "
            f"(PID {', '.join(str(p) for p in duplicates)}). Starting a second copy "
            "would have two processes writing to the same save files."
        )

    bat = server.get("start_bat") or os.path.join(server["install_dir"], "StartServer.bat")
    if not os.path.exists(bat):
        raise RuntimeError(f"Start script not found: {bat}")

    # Snapshot the log BEFORE launching. ARK rotates its log on startup, so
    # scanning without a baseline finds the previous run's "advertising for
    # join" and declares the server ready in about two seconds.
    log_dir = server.get("log_dir", "")
    snap = logtail.snapshot(log_dir) if log_dir else None
    if snap and snap.get("path"):
        job.step(f"Log baseline: {os.path.basename(snap['path'])} at {snap['size']} bytes")

    job.step(f"Launching {bat}")

    subprocess.Popen(
        [bat], cwd=server.get("install_dir") or os.path.dirname(bat),
        shell=False, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )

    # Re-read the real PID rather than trusting anything from before the start.
    deadline = time.time() + 120
    proc = None
    while time.time() < deadline:
        proc = procwatch.find_server_process(server)
        if proc is not None:
            break
        time.sleep(2)
    if proc is None:
        raise RuntimeError("Process did not appear within 120s of launching the start script.")
    job.step(f"Process started, PID {proc.pid}")

    if not wait_ready:
        return

    if not snap:
        job.step("No log directory configured - cannot confirm readiness", level="warn")
        return

    job.step("Waiting for 'advertising for join' (new log output only)...")
    deadline = time.time() + timeout
    ready = False
    while time.time() < deadline:
        if job.cancel.is_set():
            job.step("Stopped waiting (cancelled) - server is still starting", level="warn")
            return
        if logtail.ready_since(log_dir, snap):
            job.step(f"Server is advertising for join after {round(time.time() - job.started)}s")
            ready = True
            break
        time.sleep(READY_POLL_SECONDS)

    if not ready:
        raise RuntimeError(
            f"Server did not report 'advertising for join' within {timeout}s. "
            "It may still be loading mods, or it may have failed - check the log."
        )

    # Ports bind around the time the server becomes joinable, not the instant
    # the process appears, so this retries rather than judging on one look.
    deadline = time.time() + 90
    health = None
    while time.time() < deadline:
        proc = procwatch.find_server_process(server)
        health = procwatch.port_health(server, proc)
        if health["state"] == "ok":
            break
        time.sleep(3)

    level = "info" if health and health["state"] == "ok" else "warn"
    job.step(f"Port check: {health['message'] if health else 'no result'}", level=level)

    if wipe_wild_dinos:
        # After 'advertising for join', not before: the command is refused
        # while the world is still loading, and a wipe that silently did
        # nothing is worse than one that failed loudly.
        if not rcon_tool:
            job.step("Wild dino wipe requested but no RCON tool configured - skipped",
                     level="warn")
        else:
            try:
                out = rcon_command(server, "DestroyWildDinos", rcon_tool)
                job.step(f"Wild dinos wiped: {(out or '').strip() or 'no response'}")
            except Exception as exc:  # noqa: BLE001
                # Never fail the start over this. The server is up and joinable;
                # a missed wipe is a nuisance, a failed restart is an outage.
                job.step(f"Wild dino wipe FAILED ({exc}) - the server is up regardless",
                         level="error")


def do_kill(job: Job, server: dict) -> None:
    """Force-terminate. Last resort - the save is whatever was last written."""
    import psutil

    pids = procwatch.find_duplicates(server)
    if not pids:
        job.step("Nothing to kill - no process found")
        return
    for pid in pids:
        try:
            p = psutil.Process(pid)
            p.terminate()
            job.step(f"Sent terminate to PID {pid}")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise RuntimeError(
                f"Access denied terminating PID {pid}. The agent needs to run "
                "elevated to control a process started by another elevated program."
            )
    deadline = time.time() + 30
    while time.time() < deadline:
        if not procwatch.find_duplicates(server):
            job.step("Process gone")
            return
        time.sleep(1)
    for pid in procwatch.find_duplicates(server):
        try:
            psutil.Process(pid).kill()
            job.step(f"Force killed PID {pid}", level="warn")
        except psutil.Error:
            pass


# ------------------------------------------------------- boot autostart policy

def should_autostart(cfg: dict, since_boot_seconds: float) -> tuple[bool, str]:
    """Decide whether an agent starting up should also start its servers.

    Pure so it can be tested without booting a machine. Returns (go, reason);
    the reason is logged either way, because "why didn't the cluster come back"
    is the question this feature exists to answer.

    The gate is the machine's boot time, NOT the agent's own start. The agent is
    restarted by hand on every code deploy, and a deploy must never start a
    server that was deliberately stopped -- during a mod removal, for instance,
    which cannot be done with the server running.
    """
    if not (cfg or {}).get("enabled", False):
        return False, "autostart is disabled in config.json"

    try:
        window = int(cfg.get("boot_window_seconds", 900))
    except (TypeError, ValueError):
        return False, "boot_window_seconds is not a number - standing down"

    if since_boot_seconds is None or since_boot_seconds < 0:
        return False, "boot time is unreadable - standing down rather than guessing"

    if since_boot_seconds > window:
        return False, (f"the agent started {int(since_boot_seconds)}s after boot, outside "
                       f"the {window}s boot window - this is a manual agent restart, "
                       "not a reboot")

    return True, f"the machine booted {int(since_boot_seconds)}s ago"
