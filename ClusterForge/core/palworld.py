"""Palworld dedicated server: config, control, and status.

Added because PC1 has room for exactly one game server at a time. The Island
commits 11 GB and had players 0.5% of last week; a Palworld server for two
people is a better use of the same memory. So the two SWAP rather than coexist,
and this module is the Palworld half.

Three things are worth knowing about Palworld specifically:

  * Every setting lives on ONE line. PalWorldSettings.ini holds a single
    OptionSettings=(A=1,B="two",C=3.0) tuple, so changing one value means
    parsing and rewriting the whole line. Quoted strings may contain commas,
    which is why this parses by scanning rather than splitting on ",".

  * It speaks Source RCON, the same protocol ARK uses - so core/rcon.py drives
    it unchanged. Only the command names differ: Info, ShowPlayers, Save,
    Shutdown, Broadcast, DoExit.

  * Shutdown must be graceful. Pocketpair warn that killing the process can
    corrupt the save, so stopping means RCON Save, then Shutdown, and only
    terminating if it refuses to go.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

from . import rcon as rcon_mod

# Keys this panel manages. Everything else in the file is left exactly as the
# game wrote it - Palworld adds settings between versions and a rewrite that
# dropped unknown keys would silently reset them.
# Strings get quotes; booleans and numbers must NOT. RCONEnabled was in here
# by mistake and produced RCONEnabled="True", which Palworld ignores - leaving
# RCON off and the panel unable to control the server at all, with nothing
# saying why.
QUOTED = {"ServerName", "ServerPassword", "AdminPassword", "ServerDescription",
          "PublicIP", "Region", "BanListURL"}

DEFAULT_ARGS = ["-publiclobby",                 # community server: Xbox can see it
                "-useperfthreads", "-NoAsyncLoadingThread", "-UseMultithreadForDS"]

# The REAL server binary. PalServer.exe in the install root is a wrapper that,
# launched without a console, starts, sits at 9 MB, spawns nothing, writes no
# log and never listens - which looked exactly like a server that had started.
# Running the shipping binary directly works first time, and has the better
# property for a managed service: the panel owns the PID it actually needs to
# stop, rather than a wrapper that forks and walks away.
SHIPPING = os.path.join("Pal", "Binaries", "Win64", "PalServer-Win64-Shipping.exe")
PROJECT = "Pal"          # the wrapper's first argument; the engine needs it

# Launch flags that stop this server dying with whoever started it.
#
# This cost a world. The server was launched as a child of a terminal session;
# Windows tears down a session's whole process tree, so Palworld was KILLED,
# not shut down - twice - and Pocketpair are explicit that an unclean stop can
# corrupt the save. It did: Level.sav holds bases, structures and containers,
# so those vanished while the player .sav files (levels, stats) survived
# intact. "Structures and inventory gone, levels fine" is the signature.
#
# DETACHED_PROCESS gives it no console to be signalled through, and
# CREATE_BREAKAWAY_FROM_JOB gets it out of the launcher's job object - which is
# the thing that actually kills children. Breakaway is refused if the job does
# not permit it, so the caller falls back; a scheduled task (see
# scripts/install-palworld-task.ps1) is the belt-and-braces version, because a
# task belongs to Windows rather than to whatever asked for it.
DETACHED = (getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def settings_path(install_dir: str) -> str:
    return os.path.join(install_dir, "Pal", "Saved", "Config", "WindowsServer",
                        "PalWorldSettings.ini")


def default_settings_path(install_dir: str) -> str:
    return os.path.join(install_dir, "DefaultPalWorldSettings.ini")


def _split_options(blob: str) -> list[str]:
    """Split the OptionSettings tuple on commas that are NOT inside quotes.

    ServerName="Dracon, the server" is legal and would be torn in half by a
    plain split(",").
    """
    parts, depth, quoted, current = [], 0, False, []
    for char in blob:
        if char == '"':
            quoted = not quoted
        if char == "(" and not quoted:
            depth += 1
        elif char == ")" and not quoted:
            depth -= 1
        if char == "," and not quoted and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    return [p for p in (part.strip() for part in parts) if p]


def read_settings(install_dir: str) -> dict:
    """Current OptionSettings as a dict. Empty if the file is not there yet."""
    path = settings_path(install_dir)
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    match = re.search(r"OptionSettings=\((.*)\)", text, re.S)
    if not match:
        return {}
    out = {}
    for part in _split_options(match.group(1)):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        out[key.strip()] = value.strip()
    return out


def write_settings(install_dir: str, changes: dict) -> dict:
    """Apply changes to OptionSettings, keeping every other key untouched.

    Returns {key: (before, after)} for what actually changed. Creates the file
    from DefaultPalWorldSettings.ini if the server has not made one yet, which
    is the documented first step and the one people miss - the config directory
    only appears after the first run.
    """
    path = settings_path(install_dir)
    if not os.path.isfile(path):
        source = default_settings_path(install_dir)
        if not os.path.isfile(source):
            raise FileNotFoundError(
                f"neither {path} nor {source} exists - has the server been "
                "installed and started once?")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(source, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()

    match = re.search(r"OptionSettings=\((.*)\)", text, re.S)
    if not match:
        raise ValueError("no OptionSettings=( ... ) found in the settings file")

    current = {}
    order = []
    for part in _split_options(match.group(1)):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        current[key] = value.strip()
        order.append(key)

    applied = {}
    for key, value in changes.items():
        formatted = f'"{value}"' if key in QUOTED and not str(value).startswith('"') \
            else str(value)
        before = current.get(key)
        if before == formatted:
            continue
        current[key] = formatted
        if key not in order:
            order.append(key)
        applied[key] = (before, formatted)

    rebuilt = "OptionSettings=(" + ",".join(f"{k}={current[k]}" for k in order) + ")"
    text = text[:match.start()] + rebuilt + text[match.end():]

    if os.path.isfile(path):
        backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        try:
            with open(backup, "w", encoding="utf-8") as fh:
                with open(path, "r", encoding="utf-8", errors="replace") as original:
                    fh.write(original.read())
        except OSError:
            pass
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return applied


# ------------------------------------------------------------------ process

def find_process(install_dir: str):
    """The running PalServer for THIS install, or None.

    Matched on the executable path, not the name: a second install elsewhere on
    the machine must not look like this one.
    """
    import psutil
    target = os.path.normcase(os.path.abspath(install_dir))
    for proc in psutil.process_iter(["name", "exe", "create_time"]):
        try:
            name = (proc.info["name"] or "").lower()
            # ONLY the shipping binary. PalServer.exe is the wrapper, which we
            # no longer launch - and matching it meant reporting a stranded
            # 9 MB stub as "the server", with 0 GB of memory and nobody
            # listening, while the real 890 MB process went unnoticed.
            if name != "palserver-win64-shipping.exe":
                continue
            exe = proc.info["exe"] or ""
            if os.path.normcase(os.path.abspath(exe)).startswith(target):
                return proc
        except Exception:  # noqa: BLE001 - psutil raises plenty on dead processes
            continue
    return None


def status(config: dict) -> dict:
    """Is it up, for how long, how much memory, and who is on."""
    import psutil
    install_dir = config.get("install_dir", "")
    proc = find_process(install_dir)
    out = {"installed": os.path.isfile(os.path.join(install_dir, SHIPPING)),
           "running": proc is not None, "pid": None, "uptime_seconds": 0,
           "private_gb": 0, "players": None, "port": config.get("port", 8211)}
    if proc is None:
        return out
    try:
        out["pid"] = proc.pid
        out["uptime_seconds"] = round(time.time() - proc.create_time())
        out["private_gb"] = round(proc.memory_info().private / (1024 ** 3), 2)
    except Exception:  # noqa: BLE001
        pass
    try:
        out["players"] = players(config)
    except Exception:  # noqa: BLE001
        out["players"] = None
    return out


def players(config: dict) -> list[str]:
    """Names from RCON ShowPlayers, which returns CSV with a header row."""
    reply = rcon_mod.execute("127.0.0.1", int(config.get("rcon_port", 25575)),
                             config.get("rcon_password") or "", "ShowPlayers")
    names = []
    for line in (reply or "").splitlines()[1:]:
        if not line.strip():
            continue
        names.append(line.split(",")[0].strip())
    return names


def search_name(name: str) -> str:
    """What to actually type into the in-game server search.

    Palworld's search does not deal well with bracketed clan tags, and
    "[ToDs]Dracon" is a lot of characters to get exactly right on an Xbox
    controller, so the distinctive part of the name is what people are told to
    search for. Lives here, and is served to the panel page, so the web UI and
    the Discord post cannot disagree about it.
    """
    name = (name or "").strip().strip('"')
    return re.sub(r"^\s*[\[\(][^\]\)]*[\]\)]\s*", "", name).strip() or name


def info_text(config: dict, ark_label: str = "The Island") -> str:
    """The #palworld post: what the server is, and how to actually get on it.

    Written for a six-year-old's parent rather than an admin, because that is
    who reads it. The join instructions matter more than the settings: Xbox
    cannot join a Palworld server by IP at all, only from the in-game
    Community Servers list, and somebody who does not know that will try the
    IP, fail, and conclude the server is down.
    """
    name = (config.get("server_name") or "").strip('"') or "Palworld"
    password = (config.get("password") or "").strip('"')
    port = config.get("port", 8211)
    search = search_name(name)

    # Deliberately no live status here. The embed underneath carries that and
    # refreshes on its own; repeating it in the prose would mean editing this
    # post every minute, and a post Discord marks as "edited" constantly is
    # one people stop reading.
    return "\n\n".join([
        "# :paw_prints: Palworld",
        "**Server**\n"
        f"- Name: `{name}`\n"
        f"- Password: `{password}`\n"
        f"- Port: UDP {port}",

        "**How to join on Xbox or PlayStation**\n"
        "1. Main menu -> **Join Multiplayer Game**\n"
        "2. Pick the **Community Servers** tab\n"
        f"3. Search for `{search}` - if it is not there yet, wait a minute and "
        "search again, it takes a moment to list after starting\n"
        f"4. Enter the password `{password}`\n\n"
        ":warning: Consoles **cannot** join by IP address. The Community "
        "Servers list is the only way in, so there is nothing wrong if typing "
        "an address does not work.",

        "**How to join on PC**\n"
        "Same Community Servers list, or **Join by IP** using the address in "
        "the panel if you are on the home network.",
        # The "only one game at a time" section was removed 2026-11-12:
        # Palworld moved to PC2 on 2026-11-09 and no longer competes with
        # the ARK maps for memory, so the post was telling people to swap
        # games for no reason.
    ])


def settings_changed_since_start(config: dict):
    """Has PalWorldSettings.ini been edited since the server read it?

    Palworld loads that file once, at startup. So a saved change is real on
    disk and invisible in game until a restart - and the only evidence
    offered was an easy-to-miss status line, while uptime carried on
    climbing. That reads exactly like "it did not work".

    Returns (stale, seconds_since_edit) - stale is None when it cannot tell.
    """
    proc = find_process(config.get("install_dir", ""))
    if proc is None:
        return False, None
    path = settings_path(config.get("install_dir", ""))
    if not os.path.isfile(path):
        return None, None
    try:
        edited = os.path.getmtime(path)
        started = proc.create_time()
    except Exception:  # noqa: BLE001
        return None, None
    return edited > started, round(time.time() - edited)


def save_root(install_dir: str) -> str:
    return os.path.join(install_dir, "Pal", "Saved", "SaveGames")


def snapshot(config: dict, reason: str = "manual", log=print, keep: int = 30):
    """Copy the whole SaveGames tree somewhere versioned. Returns the path.

    Palworld had NO backups at all. The ARK servers get versioned snapshots
    and a mirror; this got nothing but the game's own rotating backup folder,
    which held barely two days - so when the world was damaged, the oldest
    recoverable copy was already past the damage and a base was simply gone.

    Cheap enough to do on every start: the whole tree is under a megabyte.
    """
    install_dir = config.get("install_dir", "")
    src = save_root(install_dir)
    if not os.path.isdir(src):
        return None

    root = config.get("backup_root")
    if not root:
        # Beside the install rather than nowhere. A backup on the same disk is
        # weak, but it is the difference between "lost an afternoon" and
        # "lost the world", and it needs no configuration to work.
        root = os.path.join(install_dir, "_SaveBackups")

    stamp = time.strftime("%Y-%m-%d_%H%M%S") + (f"-{reason}" if reason else "")
    dest = os.path.join(root, stamp)
    os.makedirs(dest, exist_ok=True)

    # Skip the game's own in-flight write files. Palworld writes
    # "<name>.sav.new_tmp" and "~RF....TMP" during a save and renames them
    # away moments later, so copytree would see them, then fail to open them,
    # and abort the WHOLE snapshot over files that are by definition garbage.
    # That lost a real snapshot on 2026-10-12.
    def _skip_transient(_dir, names):
        return {n for n in names
                if n.endswith((".new_tmp", ".TMP", ".tmp"))
                or n.startswith("~")}

    try:
        shutil.copytree(src, os.path.join(dest, "SaveGames"),
                        dirs_exist_ok=True, ignore=_skip_transient)
    except shutil.Error as exc:
        # Still a usable snapshot: copytree reports per-file failures and
        # copies the rest. A save that vanished mid-copy is one the game was
        # rewriting anyway.
        log(f"[palworld] snapshot copied with {len(exc.args[0])} skipped file(s)")

    # Prune oldest first, keeping the most recent `keep`.
    try:
        existing = sorted(d for d in os.listdir(root)
                          if os.path.isdir(os.path.join(root, d)))
        for stale in existing[:-keep] if len(existing) > keep else []:
            shutil.rmtree(os.path.join(root, stale), ignore_errors=True)
    except OSError:
        pass
    return dest


def save_and_snapshot(config: dict, log=print):
    """Ask the server to save, let it finish, then copy the result.

    The game already autosaves every 30 seconds (AutoSaveSpan), so raising
    that would not help - the world was being written constantly when it was
    lost. The danger is a copy (or a kill) landing DURING a write, which is
    how you get a half-written Level.sav that loads with the structures
    missing.

    So this drives the save itself and waits for it to settle, rather than
    grabbing the file at a random moment and hoping.
    """
    try:
        rcon_mod.execute("127.0.0.1", int(config.get("rcon_port", 25575)),
                         config.get("rcon_password") or "", "Save")
        time.sleep(3)      # let the write land before copying it
    except Exception as exc:  # noqa: BLE001
        # Still worth a copy: a snapshot of a slightly older state beats none.
        log(f"[palworld] could not trigger a save over RCON ({exc}) - "
            "snapshotting what is on disk anyway")
    return snapshot(config, reason="auto", log=log)


def newest_snapshot(config: dict):
    """The most recent snapshot directory, or None."""
    root = config.get("backup_root") or os.path.join(
        config.get("install_dir", ""), "_SaveBackups")
    if not os.path.isdir(root):
        return None
    dirs = sorted(d for d in os.listdir(root)
                  if os.path.isdir(os.path.join(root, d)))
    return os.path.join(root, dirs[-1]) if dirs else None


def world_looks_damaged(config: dict, tolerance: float = 0.80):
    """Is the live world much smaller than the last known-good snapshot?

    Returns (damaged, message). A blunt check on purpose - the save is
    compressed and not practical to parse - but it catches the failure that
    actually happened: Level.sav losing a chunk of itself and the server then
    starting anyway and saving over the evidence within 30 seconds.

    Deliberately only flags SHRINKAGE. A world legitimately grows.
    """
    install_dir = config.get("install_dir", "")
    live = None
    for root, _dirs, names in os.walk(save_root(install_dir)):
        if "Level.sav" in names and "backup" not in root.replace(install_dir, ""):
            live = os.path.join(root, "Level.sav")
            break
    if not live or not os.path.isfile(live):
        return False, "no live world to check"

    snap = newest_snapshot(config)
    if not snap:
        return False, "no snapshot to compare against yet"

    previous = None
    for root, _dirs, names in os.walk(snap):
        if "Level.sav" in names:
            previous = os.path.join(root, "Level.sav")
            break
    if not previous:
        return False, "last snapshot has no world file"

    now, was = os.path.getsize(live), os.path.getsize(previous)
    if was and now < was * tolerance:
        return True, (f"the world is {now/1024:.1f} KB but the last snapshot "
                      f"({os.path.basename(snap)}) was {was/1024:.1f} KB - it has "
                      f"lost {100 - now/was*100:.0f}% of its size, which is what "
                      "losing every structure looks like")
    return False, f"world {now/1024:.1f} KB vs {was/1024:.1f} KB in the last snapshot"


def start(config: dict, log=print) -> dict:
    """Launch the server. Returns once the process exists, not once it is ready."""
    install_dir = config["install_dir"]
    exe = os.path.join(install_dir, SHIPPING)
    if not os.path.isfile(exe):
        return {"ok": False, "error": f"not installed: {exe}"}
    if find_process(install_dir):
        return {"ok": True, "already": True, "message": "already running"}

    # Snapshot BEFORE starting. If the previous stop damaged the world, this
    # is the last moment that damage is still recoverable - once the server
    # runs it saves over Level.sav within seconds.
    # Check BEFORE the snapshot, so a damaged world is not what gets kept.
    try:
        damaged, detail = world_looks_damaged(config)
        if damaged:
            log(f"[palworld] WARNING: {detail}")
            log("[palworld] the last good snapshot is "
                f"{newest_snapshot(config)} - restore it rather than starting, "
                "or this server will save over the damage within 30 seconds")
            if not config.get("start_damaged", False):
                return {"ok": False, "damaged": True, "error": detail,
                        "snapshot": newest_snapshot(config)}
        else:
            log(f"[palworld] world looks intact - {detail}")
    except Exception as exc:  # noqa: BLE001 - a check must not block a start
        log(f"[palworld] could not check the world ({exc}) - continuing")

    try:
        kept = snapshot(config, reason="pre-start", log=log)
        if kept:
            log(f"[palworld] save snapshotted to {kept}")
    except Exception as exc:  # noqa: BLE001 - a backup must not block a start
        log(f"[palworld] could not snapshot the save ({exc}) - starting anyway")

    task = (config.get("task_name") or "").strip()
    if task:
        # Preferred: let Task Scheduler own it. Then no parent process exists
        # to take it down - not this panel, not a terminal session.
        log(f"[palworld] starting via scheduled task '{task}'")
        result = subprocess.run(["schtasks", "/Run", "/TN", task],
                                capture_output=True, text=True, timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            log(f"[palworld] task '{task}' would not run "
                f"({(result.stdout or '') + (result.stderr or '')})[:160] - "
                "falling back to launching it directly")
            task = ""

    if not task:
        args = [exe, PROJECT] + list(config.get("args") or DEFAULT_ARGS)
        log(f"[palworld] launching {' '.join(args[1:])}")
        try:
            subprocess.Popen(args, cwd=install_dir, creationflags=DETACHED,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            # CREATE_BREAKAWAY_FROM_JOB is refused when the job forbids it.
            # Better a server that starts and is merely vulnerable than no
            # server at all - but say so, because it IS vulnerable.
            log(f"[palworld] could not detach ({exc}) - starting it as a child "
                "of this process, which means it dies if this process is "
                "killed. Install the scheduled task to fix that properly.")
            subprocess.Popen(args, cwd=install_dir,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + 60
    while time.time() < deadline:
        if find_process(install_dir):
            log("[palworld] process started")
            return {"ok": True, "already": False}
        time.sleep(1)
    return {"ok": False, "error": "process did not appear within 60s"}


def stop(config: dict, warn_seconds: int = 30, log=print) -> dict:
    """Save, warn, shut down gracefully, and only then insist.

    Pocketpair warn that an unclean stop can corrupt the save, so the hard kill
    is the last resort rather than the method.
    """
    install_dir = config["install_dir"]
    proc = find_process(install_dir)
    if proc is None:
        return {"ok": True, "already": True, "message": "not running"}

    port = int(config.get("rcon_port", 25575))
    password = config.get("rcon_password") or ""
    try:
        rcon_mod.execute("127.0.0.1", port, password, "Save")
        log("[palworld] save requested")
    except Exception as exc:  # noqa: BLE001
        log(f"[palworld] could not save over RCON ({exc}) - continuing")

    try:
        rcon_mod.execute("127.0.0.1", port, password,
                         f"Shutdown {warn_seconds} Server_is_restarting")
        log(f"[palworld] shutdown in {warn_seconds}s requested")
    except Exception as exc:  # noqa: BLE001
        log(f"[palworld] could not ask it to shut down ({exc})")

    deadline = time.time() + warn_seconds + 60
    while time.time() < deadline:
        if find_process(install_dir) is None:
            log("[palworld] stopped cleanly")
            try:
                kept = snapshot(config, reason="clean-stop", log=log)
                if kept:
                    log(f"[palworld] save snapshotted to {kept}")
            except Exception as exc:  # noqa: BLE001
                log(f"[palworld] could not snapshot after stopping ({exc})")
            return {"ok": True, "clean": True}
        time.sleep(2)

    # Last resort only. Killing Palworld is how the world got corrupted
    # twice, so this snapshots FIRST - whatever is on disk now is the best
    # copy that will exist, and it is about to be written by a dying process.
    log("[palworld] it would not shut down cleanly - snapshotting before the kill")
    try:
        kept = snapshot(config, reason="BEFORE-FORCED-KILL", log=log)
        if kept:
            log(f"[palworld] save snapshotted to {kept}")
    except Exception as exc:  # noqa: BLE001
        log(f"[palworld] could not snapshot before the kill ({exc})")

    log("[palworld] terminating")
    try:
        proc.terminate()
        proc.wait(timeout=30)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": find_process(install_dir) is None, "clean": False}
