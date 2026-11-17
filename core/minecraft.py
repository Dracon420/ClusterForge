"""Minecraft Bedrock dedicated server: config, control, and status.

The fourth game, and the one that breaks the last assumption the other three
shared: it has no remote console at all. ARK and Palworld speak Source RCON,
7 Days speaks telnet - Bedrock speaks nothing. Its console is the process's
own stdin, which is useless to a panel that launches servers detached (and
launching them attached is how Palworld's world got corrupted twice).

So two things are done differently here, and both are the point of having an
adapter layer at all:

  * STATUS comes from a RakNet unconnected ping on the game port - the same
    thing the in-game server browser does. That gives MOTD, player count and
    max players without any console, which is most of what a panel displays.

  * STOPPING sends CTRL_BREAK to the process group. Bedrock treats it as a
    clean shutdown and saves the world; terminating it outright does not,
    and an unclean stop on a Minecraft world is how you lose a day's
    building. The process is launched into its own group precisely so this
    signal can be aimed at it.

Config is server.properties - plain key=value, one per line, # for comments.
After ini, a 6000-character tuple and XML, that is a pleasant change.
"""

from __future__ import annotations

import os
import random
import shutil
import signal
import socket
import struct
import subprocess
import time

EXE = "bedrock_server.exe"
PROPERTIES = "server.properties"

# RakNet's "offline message data ID" - a fixed 16-byte magic every Bedrock
# server echoes back. Without it the server ignores the packet.
RAKNET_MAGIC = bytes([0x00, 0xFF, 0xFF, 0x00, 0xFE, 0xFE, 0xFE, 0xFE,
                      0xFD, 0xFD, 0xFD, 0xFD, 0x12, 0x34, 0x56, 0x78])


def properties_path(install_dir: str) -> str:
    return os.path.join(install_dir, PROPERTIES)


def read_settings(install_dir: str) -> dict:
    """server.properties as a dict. Comments and blank lines are ignored."""
    path = properties_path(install_dir)
    if not os.path.isfile(path):
        return {}
    out = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def write_settings(install_dir: str, changes: dict) -> dict:
    """Change values in place, keeping comments, order and spacing.

    Rewriting the file from a dict would throw away Mojang's own explanatory
    comments, which are the only documentation most people ever read for
    these settings.
    """
    path = properties_path(install_dir)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{path} does not exist - has the server run once?")

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    applied = {}
    remaining = dict(changes)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key not in remaining:
            continue
        before = stripped.split("=", 1)[1].strip()
        after = str(remaining.pop(key))
        if before != after:
            lines[index] = f"{key}={after}\n"
            applied[key] = (before, after)

    # Anything server.properties did not already have is appended rather than
    # dropped - Mojang add settings between versions.
    for key, value in remaining.items():
        lines.append(f"{key}={value}\n")
        applied[key] = (None, str(value))

    if applied:
        backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.writelines(lines)
    return applied


# -------------------------------------------------------------- RakNet ping

def ping(host: str, port: int, timeout: float = 2.0) -> dict | None:
    """Ask a Bedrock server who it is, the way the server browser does.

    Returns {motd, players, max_players, version, level_name, gamemode} or
    None if it did not answer. This is the whole reason Bedrock can be shown
    properly without a console.
    """
    packet = (b"\x01"
              + struct.pack(">Q", int(time.time() * 1000))
              + RAKNET_MAGIC
              + struct.pack(">Q", random.getrandbits(64)))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (host, port))
            data, _addr = sock.recvfrom(4096)
    except OSError:
        return None

    # 0x1C, timestamp(8), server GUID(8), magic(16), length(2), then the
    # semicolon-separated identity string.
    if not data or data[0] != 0x1C or len(data) < 35:
        return None
    try:
        text = data[35:].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None

    parts = text.split(";")
    if len(parts) < 6:
        return None

    def at(index, default=""):
        return parts[index] if len(parts) > index else default

    try:
        players = int(at(4, "0") or 0)
        max_players = int(at(5, "0") or 0)
    except ValueError:
        players, max_players = 0, 0
    return {"motd": at(1), "version": at(3), "players": players,
            "max_players": max_players, "level_name": at(7),
            "gamemode": at(8)}


# ----------------------------------------------------------------- process

def find_process(install_dir: str):
    import psutil
    target = os.path.normcase(os.path.abspath(install_dir))
    for proc in psutil.process_iter(["name", "exe", "create_time"]):
        try:
            if (proc.info["name"] or "").lower() != EXE.lower():
                continue
            exe = proc.info["exe"] or ""
            if os.path.normcase(os.path.abspath(exe)).startswith(target):
                return proc
        except Exception:  # noqa: BLE001
            continue
    return None


def status(config: dict) -> dict:
    install_dir = config.get("install_dir", "")
    settings = read_settings(install_dir)
    port = int(settings.get("server-port") or config.get("port", 19132))
    proc = find_process(install_dir)
    out = {"installed": os.path.isfile(os.path.join(install_dir, EXE)),
           "running": proc is not None, "pid": None, "uptime_seconds": 0,
           "private_gb": 0, "players": None, "port": port,
           "server_name": settings.get("server-name", ""),
           "level_name": settings.get("level-name", "")}
    if proc is None:
        return out
    try:
        out["pid"] = proc.pid
        out["uptime_seconds"] = round(time.time() - proc.create_time())
        out["private_gb"] = round(proc.memory_info().private / (1024 ** 3), 2)
    except Exception:  # noqa: BLE001
        pass

    # NetherNet servers cannot be pinged. They do not listen on a predictable
    # port at all - they open a random one and register with Xbox Live
    # signalling, so clients find them through a friends list rather than an
    # address. Mojang deprecated RakNet in 1.26, which makes this the
    # supported arrangement and the panel's status blind by design.
    #
    # Saying so beats showing an empty player count that looks like a fault.
    transport = (settings.get("transport") or "raknet").strip().lower()
    out["transport"] = transport
    if transport == "nethernet":
        out["detail"] = ("NetherNet: players join through the Xbox friends "
                         "list, not an address. No player count is available "
                         "- the server does not answer status queries.")
        return out

    answered = ping("127.0.0.1", port)
    if answered:
        out["player_count"] = answered["players"]
        out["max_players"] = answered["max_players"]
        out["version"] = answered["version"]
        out["motd"] = answered["motd"]
        # Bedrock will not tell us WHO is online without a console, so this
        # stays None rather than pretending an empty list means nobody.
        out["players"] = None
    return out


def start(config: dict, log=print) -> dict:
    install_dir = config["install_dir"]
    exe = os.path.join(install_dir, EXE)
    if not os.path.isfile(exe):
        return {"ok": False, "error": f"not installed: {exe}"}
    if find_process(install_dir):
        return {"ok": True, "already": True, "message": "already running"}

    # CREATE_NEW_PROCESS_GROUP is not optional here: it is what makes a clean
    # CTRL_BREAK shutdown possible later. DETACHED_PROCESS keeps the server
    # alive when whatever started it goes away.
    flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
             | getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))

    task = (config.get("task_name") or "").strip()
    if task:
        log(f"[minecraft] starting via scheduled task '{task}'")
        result = subprocess.run(["schtasks", "/Run", "/TN", task],
                                capture_output=True, text=True, timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            log("[minecraft] task would not run - launching directly")
            task = ""
    if not task:
        # Bedrock needs somewhere to write and something to read.
        #
        # Sending its output to DEVNULL makes it stall: it printed "NO LOG
        # FILE! - setting up server logging..." and then sat at 0.22 GB
        # forever, never opening the world, never answering a ping. The same
        # binary run with a console reached "Server started" in four seconds.
        #
        # So stdout goes to a real file - which also gives us the server log,
        # the only record of what Bedrock is doing - and stdin comes from NUL
        # so its console reader gets a clean EOF instead of blocking.
        os.makedirs(os.path.join(install_dir, "logs"), exist_ok=True)
        log_path = os.path.join(install_dir, "logs", "bedrock_server.log")
        log(f"[minecraft] launching {EXE}, logging to {log_path}")
        handle = open(log_path, "ab", buffering=0)
        devnull = open(os.devnull, "rb")
        try:
            subprocess.Popen([exe], cwd=install_dir, creationflags=flags,
                             stdin=devnull, stdout=handle, stderr=handle)
        except OSError as exc:
            log(f"[minecraft] could not detach ({exc}) - starting as a child, "
                "which means it dies with this process")
            subprocess.Popen([exe], cwd=install_dir,
                             creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                             stdin=devnull, stdout=handle, stderr=handle)

    deadline = time.time() + 90
    while time.time() < deadline:
        if find_process(install_dir):
            log("[minecraft] process started")
            return {"ok": True, "already": False}
        time.sleep(1)
    return {"ok": False, "error": "process did not appear within 90s"}


CONSOLE_FILE = "console-in.txt"


def console_ready(install_dir: str) -> bool:
    """True when the task's wrapper (scripts/run-server-logged.ps1, console
    mode) holds Bedrock's stdin and is watching CONSOLE_FILE."""
    import psutil
    try:
        with open(os.path.join(install_dir, CONSOLE_FILE + ".ready"), encoding="utf-8") as fh:
            pid = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return False
    return pid > 0 and psutil.pid_exists(pid)


def send_console(install_dir: str, commands: list) -> bool:
    """Type lines into the running server's console through the wrapper.
    Written to a temp file and renamed, so the wrapper never reads half a file."""
    if not console_ready(install_dir):
        return False
    path = os.path.join(install_dir, CONSOLE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(str(c) for c in commands) + "\n")
    os.replace(tmp, path)
    return True


def stop(config: dict, warn_seconds: int = 0, log=print) -> dict:
    """Ask Bedrock to shut down cleanly, and only kill it as a last resort.

    CTRL_BREAK is the closest thing Bedrock has to a shutdown command when
    you do not own its stdin: it saves the world and exits. Terminating the
    process does neither, and an unclean stop is how Minecraft worlds come
    back missing a day's work.
    """
    install_dir = config["install_dir"]
    proc = find_process(install_dir)
    if proc is None:
        return {"ok": True, "already": True, "message": "not running"}

    # The real clean shutdown (2026-11-12): type "stop" into its console. The
    # task's wrapper owns Bedrock's stdin and passes on what we write. Before
    # this, every stop of the task-owned server ended in schtasks /End.
    try:
        typed = send_console(install_dir, ["stop"])
    except OSError as exc:
        log(f"[minecraft] could not reach the console ({exc})")
        typed = False
    if typed:
        log("[minecraft] clean shutdown requested (typed 'stop' into its console)")
        deadline = time.time() + 90
        while time.time() < deadline:
            if find_process(install_dir) is None:
                log("[minecraft] stopped cleanly - world saved")
                return {"ok": True, "clean": True}
            time.sleep(1)
        log("[minecraft] still running 90s after 'stop' - falling back")

    # CTRL_BREAK only reaches a process group this process owns. A server
    # started by an earlier run of the panel is not in one, and the call
    # fails with "the parameter is incorrect" rather than doing anything.
    # Worth trying - it is the only clean shutdown Bedrock has - but the
    # fallback below is what usually runs.
    try:
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        log("[minecraft] clean shutdown requested (CTRL_BREAK)")
    except Exception as exc:  # noqa: BLE001
        log(f"[minecraft] CTRL_BREAK did not apply ({exc}) - it will be "
            "terminated instead. Bedrock writes its world to LevelDB as it "
            "goes rather than in one save at the end, so this is far less "
            "dangerous than it would be for Palworld, but it is not free.")

    deadline = time.time() + 60
    while time.time() < deadline:
        if find_process(install_dir) is None:
            log("[minecraft] stopped cleanly")
            return {"ok": True, "clean": True}
        time.sleep(2)

    # Ending the TASK is the next thing to try, and usually the thing that
    # works: once Task Scheduler owns the server it runs as SYSTEM, and an
    # unelevated caller can neither signal it nor terminate it - both come
    # back "Access is denied". Task Scheduler can, because it owns it.
    task = (config.get("task_name") or "").strip()
    if task:
        log(f"[minecraft] asking Task Scheduler to end '{task}'")
        try:
            subprocess.run(["schtasks", "/End", "/TN", task],
                           capture_output=True, text=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:  # noqa: BLE001
            log(f"[minecraft] could not end the task ({exc})")
        deadline = time.time() + 45
        while time.time() < deadline:
            if find_process(install_dir) is None:
                log("[minecraft] stopped via the task")
                return {"ok": True, "clean": False}
            time.sleep(2)

    log("[minecraft] terminating as a last resort (the world may lose "
        "whatever happened since its last autosave)")
    try:
        proc.terminate()
        proc.wait(timeout=30)
    except Exception as exc:  # noqa: BLE001
        log(f"[minecraft] terminate failed ({exc}) - this usually means the "
            "server runs as SYSTEM and the caller is not elevated. Stop it "
            "from the panel, which is.")
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": find_process(install_dir) is None, "clean": False}


def worlds_root(install_dir: str) -> str:
    return os.path.join(install_dir, "worlds")


def run_console_commands(config: dict, commands: list, log=print,
                         ready_timeout: int = 180) -> dict:
    """Run commands in Bedrock's console, without anyone joining the game.

    Bedrock has no remote console, which normally means gamerules can only be
    set by a player typing them in chat. But the server DOES read its own
    stdin - the problem is only that a detached server has none.

    So this runs a deliberately short-lived attached instance: start it with
    a pipe, wait for "Server started", send the commands, send "stop", and
    let it shut down cleanly. Gamerules are saved into the world, so they
    persist once the normal detached server comes back.

    The caller must stop the running server first. Two Bedrock servers on one
    world would both write to the same LevelDB.
    """
    install_dir = config["install_dir"]
    exe = os.path.join(install_dir, EXE)
    if not os.path.isfile(exe):
        return {"ok": False, "error": f"not installed: {exe}"}
    if find_process(install_dir):
        return {"ok": False, "error": "the server is still running - stop it first"}

    log(f"[minecraft] starting a temporary console session for "
        f"{len(commands)} command(s)")
    proc = subprocess.Popen(
        [exe], cwd=install_dir,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    transcript, applied = [], []
    try:
        # Wait for it to be ready. Sending commands before the world is open
        # gets them ignored with no error.
        deadline = time.time() + ready_timeout
        ready = False
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            transcript.append(line.rstrip())
            if "Server started" in line:
                ready = True
                break
        if not ready:
            proc.kill()
            return {"ok": False, "error": "the server never reported 'Server started'",
                    "transcript": transcript[-15:]}

        for command in commands:
            proc.stdin.write(command + "\n")
            proc.stdin.flush()
            time.sleep(0.6)
            applied.append(command)
            log(f"[minecraft] > {command}")

        time.sleep(1.5)
        proc.stdin.write("stop\n")
        proc.stdin.flush()

        # Drain what it says on the way out - that is where a rejected
        # command reports itself.
        end = time.time() + 90
        while time.time() < end and proc.poll() is None:
            line = proc.stdout.readline()
            if not line:
                break
            transcript.append(line.rstrip())
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    log(f"[minecraft] console session finished, {len(applied)} command(s) sent")
    return {"ok": True, "applied": applied, "transcript": transcript[-25:]}


# ----------------------------------------------------------------- backups
#
# Bedrock had NO backups at all. The adapter advertised backups=True and
# offered save_paths(), config.json carried a backup_root and a
# snapshot_minutes - and nothing in the panel ever called any of it. The
# backup root did not exist on disk. That is worse than an obvious gap,
# because everything about it reads as configured.
#
# It came to light the day a world a child had spent weeks building was
# imported from an Xbox and became the live world, with the only other copies
# sitting on a Realm trial and the console it came from.
#
# Snapshots are written as .mcworld files on purpose. That is just a zip with
# level.dat, db/ and levelname.txt at the root - which means every snapshot is
# directly loadable by the game: rename it, open it on a phone, a PC or the
# Xbox, and the world is there. A backup you can hand to a six-year-old
# without a restore procedure is worth more than a faster one.

SNAPSHOT_SUFFIX = ".mcworld"


def active_world_dir(install_dir: str) -> str:
    """The folder of the world the server is actually configured to load."""
    level = (read_settings(install_dir).get("level-name") or "").strip()
    if not level:
        return ""
    return os.path.join(worlds_root(install_dir), level)


def snapshot_root(config: dict) -> str:
    root = (config.get("backup_root") or "").strip()
    if root:
        return root
    # Beside the install rather than nowhere. A backup on the same disk is
    # weak, but it is the difference between losing an afternoon and losing
    # the world, and it needs no configuration to work.
    return os.path.join(config.get("install_dir", ""), "_WorldBackups")


def _mount_anchor(path: str) -> str:
    """The drive or network share a path lives on, which is the thing whose
    reachability actually matters.

    "I:\\ASA-Backups\\MinecraftBedrock" -> "I:\\"
    "\\\\host\\share\\backups"          -> "\\\\host\\share\\"

    Returns "" for a path with no anchor (a relative one), which the caller
    treats as "nothing to probe".
    """
    drive, _rest = os.path.splitdrive(os.path.abspath(path))
    return drive + os.sep if drive else ""


def list_snapshots(config: dict) -> list[str]:
    """Snapshot files, oldest first. The timestamp sorts lexically."""
    root = snapshot_root(config)
    try:
        names = [n for n in os.listdir(root) if n.endswith(SNAPSHOT_SUFFIX)]
    except OSError:
        return []
    return [os.path.join(root, n) for n in sorted(names)]


def newest_snapshot(config: dict):
    found = list_snapshots(config)
    return found[-1] if found else None


def prune_snapshots(config: dict, keep: int, log=print) -> list[str]:
    found = list_snapshots(config)
    removed = []
    for path in found[:max(0, len(found) - max(1, keep))]:
        try:
            os.remove(path)
            removed.append(path)
        except OSError as exc:
            log(f"[minecraft] could not remove old snapshot {path}: {exc}")
    return removed


def snapshot(config: dict, reason: str = "manual", log=print,
             keep: int = 30, reachable_fn=None):
    """Copy the live world to a timestamped .mcworld. Returns the path.

    Taken while the server runs, which is a deliberate trade. Bedrock writes
    its world to LevelDB continuously rather than in one save at the end, and
    it has no console to ask for a flush, so there is no way to get a
    guaranteed-quiet moment without stopping the server. A copy taken mid-write
    can in principle hold a torn set of table files; a world with no copies at
    all is certainly unrecoverable. The first risk is much smaller than the
    second.

    The archive is built under a temporary name and renamed into place, so a
    snapshot interrupted half-way never looks like a complete one.
    """
    install_dir = config.get("install_dir", "")
    src = active_world_dir(install_dir)
    if not src or not os.path.isdir(src):
        log(f"[minecraft] no world to snapshot at {src!r}")
        return None

    root = snapshot_root(config)

    # Never touch an unreachable network path without asking first. A dead
    # SMB share does not fail - it blocks in kernel I/O that cannot be killed
    # or timed out, and it has wedged a backup here for thirteen hours.
    #
    # The probe asks about the DRIVE, not about the backup folder. The folder
    # legitimately does not exist before the first snapshot, and a
    # reachability check that is really os.path.isdir() cannot tell "the share
    # is dead" from "nobody has created this yet" - so probing the leaf meant
    # the first snapshot could never be taken, forever.
    if reachable_fn is not None:
        anchor = _mount_anchor(root)
        if anchor and not reachable_fn(anchor):
            log(f"[minecraft] {anchor} is not reachable - skipping this "
                "snapshot rather than blocking on it")
            return None

    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        log(f"[minecraft] cannot create {root}: {exc}")
        return None

    world = os.path.basename(src.rstrip("\\/"))
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    final = os.path.join(root, f"{world}-{stamp}-{reason}{SNAPSHOT_SUFFIX}")
    partial = final + ".partial"

    import zipfile
    count = 0
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as archive:
            for folder, _dirs, files in os.walk(src):
                for name in files:
                    full = os.path.join(folder, name)
                    # Relative to the WORLD folder, so level.dat sits at the
                    # archive root. That is what makes it a valid .mcworld
                    # rather than a zip of a folder.
                    arc = os.path.relpath(full, src)
                    try:
                        archive.write(full, arc)
                        count += 1
                    except (OSError, ValueError) as exc:
                        # A file being rewritten underneath us is expected;
                        # losing the whole snapshot over one is not.
                        log(f"[minecraft] skipped {arc} in snapshot: {exc}")
        os.replace(partial, final)
    except Exception as exc:  # noqa: BLE001
        log(f"[minecraft] snapshot failed: {exc}")
        try:
            os.remove(partial)
        except OSError:
            pass
        return None

    size_mb = os.path.getsize(final) / (1024 ** 2)
    log(f"[minecraft] snapshot {os.path.basename(final)} "
        f"({count} files, {size_mb:.1f} MB)")
    prune_snapshots(config, keep, log=log)
    return final
