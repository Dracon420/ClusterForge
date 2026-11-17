"""
ArkPanel Agent -- one per PC, controls the servers on that machine.

Exists because a server cannot be STARTED over SMB: process launch has to
happen locally. The panel talks to an agent on each PC rather than reaching
across the network share.

Phase 1 is read-only. While config.json has "read_only": true, every mutating
route returns 403 before doing anything. That flag is lifted only after the ini
round-trip gate and launch model are proven.

Run elevated -- an unelevated process cannot read the command line of, or
control, a server that DSM started elevated.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import psutil
import subprocess
import sys
import threading
import traceback
import time
from datetime import datetime
from functools import wraps

from flask import Flask, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import agentupdate, backup, buildstamp, clusterbackup, codestamp, control, filebackup, logtail, maps, notify, palworld, perf, procwatch, steam  # noqa: E402
from core.discord_bot import WatcherBot  # noqa: E402
from core.monitor import Monitor  # noqa: E402
from core.scheduler import Scheduler  # noqa: E402
from core.importer import import_server, to_record  # noqa: E402
from core.inifile import ChangeError, IniFile, apply_changes  # noqa: E402
from core.launch import LaunchConfig, build_start_bat  # noqa: E402
from core.mods import build_mod_table  # noqa: E402
from core.schema import describe_ini, load_schema  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
SECRETS_PATH = os.path.join(BASE, "secrets.json")

app = Flask(__name__)


def load_config() -> dict:
    # utf-8-sig, not utf-8: PowerShell's Set-Content -Encoding utf8 writes a
    # BOM, and plain utf-8 raises on it. utf-8-sig reads both.
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def load_secrets() -> dict:
    """Load the shared agent token.

    A missing file means auth is deliberately off. A file that EXISTS but
    cannot be parsed is a different thing entirely, and must never be treated
    as "no secrets": swallowing that error silently disables authentication on
    a service listening on the LAN, with no visible symptom. That is exactly
    what a BOM in this file used to cause. Fail loudly instead.
    """
    if not os.path.exists(SECRETS_PATH):
        print("WARNING: secrets.json not found - agent authentication is DISABLED")
        return {}
    try:
        with open(SECRETS_PATH, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(
            f"FATAL: {SECRETS_PATH} exists but could not be read ({exc}).\n"
            "Refusing to start, because continuing would run the agent with "
            "authentication silently disabled. Fix or delete the file."
        )
    if not data.get("agent_token"):
        print("WARNING: secrets.json has no agent_token - authentication is DISABLED")
    return data


CONFIG = load_config()
SECRETS = load_secrets()
SCHEMA = load_schema()


def servers() -> list[dict]:
    """Servers this agent is responsible for -- the ones on this machine."""
    return [s for s in CONFIG["servers"] if s.get("local")]


def find(name: str) -> dict | None:
    for s in CONFIG["servers"]:
        if s["name"] == name:
            return s
    return None


def require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = SECRETS.get("agent_token")
        if expected:
            supplied = request.headers.get("X-ArkPanel-Token")
            if supplied != expected:
                return jsonify({"error": "unauthorised"}), 401
        return fn(*args, **kwargs)
    return wrapper


def require_writable(fn):
    """Hard gate for anything that mutates state."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if CONFIG.get("read_only", True):
            return jsonify({
                "error": "read_only",
                "message": (
                    "ArkPanel is in read-only mode. Mutating actions are "
                    "disabled until the ini round-trip gate and launch model "
                    "are proven, then read_only is set false in config.json."
                ),
            }), 403
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------- read routes

@app.get("/api/health")
def health():
    """Liveness check.

    Deliberately reachable without a token so the panel can tell "agent down"
    apart from "agent rejected me" -- but it only returns detail to an
    authenticated caller. Hostname, elevation state and which servers a machine
    runs are not things to hand out to anything that can open the port.
    """
    authorised = True
    expected = SECRETS.get("agent_token")
    if expected:
        authorised = request.headers.get("X-ArkPanel-Token") == expected

    if not authorised:
        return jsonify({"ok": True, "role": "agent"})

    return jsonify({
        "ok": True,
        "role": "agent",
        "host": os.environ.get("COMPUTERNAME", "unknown"),
        "read_only": CONFIG.get("read_only", True),
        "elevated": _is_elevated(),
        "servers": [s["name"] for s in servers()],
        # Lets the panel see when an agent is running older code, which was
        # otherwise invisible until some field turned up missing.
        "code": CODE_FINGERPRINT,
        # Reported so the import of buildstamp is load-bearing. It is what
        # puts the stamp inside the code fingerprint, and an unused import is
        # the kind of thing someone tidies away - taking the smoke test's
        # only safe lever with it.
        "stamp": buildstamp.STAMP,
        "config": CONFIG_FINGERPRINT,
        "config_stale": _config_fingerprint() != CONFIG_FINGERPRINT,
        "restartable": _restart_task_present(),
        # Why this agent is stuck on old code, when it is. Reported here
        # rather than only in /api/selfupdate so the panel's agent list can
        # say WHY a row is behind - "OLD CODE" with no reason is the state
        # that had us restarting the wrong things for an hour.
        "restart_failed": SELFUPDATE_STATE.get("restart_failed"),
        "started": AGENT_STARTED,
    })


# Which task restarts THIS agent. Configurable because a machine can run more
# than one instance - the smoke-test agent lives beside the real one and must
# restart itself, not its neighbour. A customer who names things differently
# gets the same escape hatch.
RESTART_TASK = ((CONFIG.get("agent") or {}).get("restart_task")
                or "ArkPanel Agent Restart")
CODE_FINGERPRINT = codestamp.fingerprint(BASE)


def _config_fingerprint() -> str:
    import hashlib
    try:
        with open(CONFIG_PATH, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:
        return "unreadable"


# The config this process LOADED. Code has had a fingerprint for a long time,
# but config was deliberately left out of it -- two agents legitimately hold
# different config -- and that left a hole: adding Nyrandil changed only config,
# the deploy saw matching code and restarted nothing, and PC2's agent carried on
# not knowing its second server existed. Comparing this against the file on
# disk says whether THIS agent is behind its own config, which is the question.
CONFIG_FINGERPRINT = _config_fingerprint()

# When this agent process started. Distinct from the SERVER's uptime: an agent
# that restarted five minutes ago while its map has been up for hours is a
# normal deploy, and telling those two apart at a glance saves a lot of
# guessing about what actually happened.
AGENT_STARTED = time.time()


def _restart_task_present() -> bool:
    """Is the on-demand restart task registered on this machine?

    The panel asks before offering a remote restart, so a machine without it
    gets clear instructions instead of a request that silently does nothing.
    """
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", RESTART_TASK],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@app.post("/api/admin/restart")
@require_token
@require_writable
def admin_restart():
    """Restart this agent by running a SEPARATE scheduled task.

    Never restarts in-process. Four earlier attempts at that all failed, and
    one of them took an agent down completely -- on a machine that cannot be
    reached remotely, a stranded agent means physically walking to it. So if
    the restarter task is missing this REFUSES and says what to run, rather
    than trying something clever.
    """
    if not _restart_task_present():
        return jsonify({
            "error": "no_restart_task",
            "message": (
                f"The '{RESTART_TASK}' task is not registered on "
                f"{os.environ.get('COMPUTERNAME', 'this machine')}. "
                "Run this once, elevated, at that machine:  "
                f"powershell -ExecutionPolicy Bypass -File "
                f"{os.path.join(BASE, 'scripts', 'install-restart-task.ps1')}"
                "  -- after that the panel can restart this agent on its own."),
        }), 409

    busy = [s["name"] for s in servers() if control.JOBS.active_for(s["name"])]
    if busy and not (request.get_json(silent=True) or {}).get("force"):
        return jsonify({
            "error": "busy",
            "message": (f"A control job is running for {', '.join(busy)}. Restarting "
                        "the agent now would abandon it. Wait for it to finish, or "
                        "resend with force."),
        }), 409

    alog(f"[admin] restart requested via {RESTART_TASK}")
    try:
        subprocess.Popen(
            ["schtasks", "/Run", "/TN", RESTART_TASK],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": "trigger_failed", "message": str(exc)}), 500

    # Answer BEFORE the restarter stops us, or the caller sees a dropped
    # connection and cannot tell success from failure.
    return jsonify({"ok": True, "restarting": True, "code": CODE_FINGERPRINT,
                    "message": "Restart task triggered. The agent goes away for "
                               "a few seconds."})


def _is_elevated() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _status_for(server: dict, cpu_interval: float | None = None) -> dict:
    proc = procwatch.find_server_process(server, cpu_interval=cpu_interval)
    duplicates = procwatch.find_duplicates(server)
    log_dir = server.get("log_dir", "")
    logs = logtail.summarise(log_dir) if log_dir else {}

    return {
        "name": server["name"],
        "display_name": server["display_name"],
        "host": server.get("host"),
        "map_name": server.get("map_name"),
        "install_dir": server.get("install_dir"),
        "running": proc is not None,
        # Why it is down, when it is down deliberately. Without this the panel
        # shows a server stopped for Palworld exactly like one that crashed -
        # same red, same "not running" - and the honest question "is the
        # Island broken?" has no answer on screen.
        "maintenance": _maintenance_reason(server["name"]) if proc is None else None,
        "pid": proc.pid if proc else None,
        "uptime": proc.uptime_human if proc else None,
        "uptime_seconds": round(proc.uptime_seconds) if proc else 0,
        "memory_gb": proc.memory_gb if proc else 0,        # working set - trimmed when idle
        "private_gb": proc.private_gb if proc else 0,      # committed - the real footprint
        "cpu_percent": proc.cpu_percent if proc else 0,
        "started_at": proc.started_at if proc else None,
        "cmdline_readable": bool(proc and proc.cmdline),
        "udp_ports": proc.all_udp_ports if proc else [],
        "port_health": procwatch.port_health(server, proc),
        "expected_game_port": server.get("expected_game_port"),
        "rcon_port": server.get("rcon_port"),
        # More than one PID means two processes share the same save files.
        "duplicate_pids": duplicates if len(duplicates) > 1 else [],
        "logs": logs,
        "disk": procwatch.disk_free(server.get("install_dir", "")),
    }


@app.get("/api/status")
@require_token
def status_all():
    """Status of every server on this machine.

    ?live=1 pays for a short CPU sample and the disk reading, which is what a
    watcher does locally. Without it CPU reads 0.0% -- psutil needs two samples
    -- and the panel's Discord embed for a watcher-less PC showed 0% for maps
    that were working hard. The panel's own 15s status poll leaves it off: it
    would add 0.3s per server to a call made four times a minute.
    """
    live = request.args.get("live") in ("1", "true", "yes")
    system = procwatch.system_stats(interval=0.4) if live else procwatch.system_stats()
    local = servers()
    if live and local:
        system["disk"] = procwatch.disk_free(local[0].get("install_dir", "")) or {}
    return jsonify({
        "system": system,
        "read_only": CONFIG.get("read_only", True),
        "servers": [_status_for(s, cpu_interval=0.3 if live else None)
                    for s in local],
    })


@app.get("/api/server/<name>/status")
@require_token
def status_one(name):
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    return jsonify(_status_for(server))


@app.get("/api/server/<name>/launch")
@require_token
def launch_info(name):
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    result = import_server(
        name=name,
        install_dir=server["install_dir"],
        profile_name=server.get("dsm_profile", ""),
        bat_path=server.get("start_bat"),
    )
    record = to_record(result)
    # Ownership is about which file ArkPanel maintains, which the importer has
    # no view of -- it only ranks sources by trustworthiness.
    record["owned"] = _owned(server)
    record["bat_path"] = _bat_path(server)
    return jsonify(record)


@app.get("/api/server/<name>/mods")
@require_token
def mods(name):
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    cfg = _current_launch(server)
    order = cfg.mods if cfg else []
    table = build_mod_table(server["install_dir"], order)
    return jsonify({
        "server": name,
        "count": len(table),
        "in_launch_args": sum(1 for m in table if m.in_load_order),
        "installed": sum(1 for m in table if m.installed),
        "drifted": sum(1 for m in table if m.drift),
        "mods": [m.to_dict() for m in table],
    })


def _current_launch(server: dict) -> LaunchConfig | None:
    """Best available view of this server's launch args.

    Delegates to _launch_basis so the READ path and the WRITE path can never
    disagree about which file is authoritative. They used to: writes went
    through _launch_basis, which prefers StartServer.bat once ArkPanel owns it,
    while this read DSM's management log first. DSM is retired but its log file
    is still on disk, frozen at whatever it last launched -- so saving a mod
    rewrote the bat correctly and then the UI re-read the fossil and showed the
    old list, making a successful save look like the mod had been deleted.
    """
    cfg, _source = _launch_basis(server)
    return cfg


@app.get("/api/server/<name>/settings/<path:filename>")
@require_token
def settings(name, filename):
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    if filename not in ("Game.ini", "GameUserSettings.ini", "Engine.ini"):
        return jsonify({"error": "unsupported file"}), 400

    path = os.path.join(server["config_dir"], filename)
    if not os.path.exists(path):
        return jsonify({"error": f"{filename} not found"}), 404

    # The mod table lets undocumented sections be labelled with the real mod
    # name and logo instead of a raw [SectionName].
    cfg = _current_launch(server)
    mod_table = build_mod_table(server["install_dir"], cfg.mods if cfg else [])

    ini = IniFile.load(path)
    view = describe_ini(ini, filename, SCHEMA, [m.to_dict() for m in mod_table])
    view["path"] = path
    view["encoding"] = ini.encoding
    view["line_count"] = len(ini.lines)
    # Returned to the UI and handed back on save, so a file edited in Notepad
    # meanwhile is detected instead of silently overwritten.
    view["etag"] = _file_sha(path)
    return jsonify(view)


def _file_sha(path: str) -> str:
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


@app.get("/api/server/<name>/settings/<path:filename>/raw")
@require_token
def settings_raw(name, filename):
    """Raw text, for the Custom Override pane and 'open in Notepad' parity."""
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    if filename not in ("Game.ini", "GameUserSettings.ini", "Engine.ini"):
        return jsonify({"error": "unsupported file"}), 400
    path = os.path.join(server["config_dir"], filename)
    if not os.path.exists(path):
        return jsonify({"error": f"{filename} not found"}), 404
    ini = IniFile.load(path)
    return jsonify({"path": path, "encoding": ini.encoding, "text": ini.render()})


@app.get("/api/server/<name>/logs")
@require_token
def logs(name):
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    log_dir = server.get("log_dir", "")
    count = min(int(request.args.get("lines", 200)), 2000)
    path = logtail.active_log(log_dir)
    return jsonify({
        "summary": logtail.summarise(log_dir),
        "lines": logtail.tail(path, count) if path else [],
    })


# ------------------------------------------------------- write routes (gated)

def _bat_path(server: dict) -> str:
    return server.get("start_bat") or os.path.join(server["install_dir"], "StartServer.bat")


def _owned(server: dict) -> bool:
    """True once ArkPanel has taken over this server's start script.

    Marked by the presence of the .pre-arkpanel archive, written on first
    generation. Before takeover the authoritative view is DSM's launch log;
    after it, the bat itself is authoritative, otherwise ArkPanel's own edits
    would be discarded on the next write.
    """
    return os.path.exists(_bat_path(server) + ".pre-arkpanel")


def _launch_basis(server: dict) -> tuple[LaunchConfig | None, str]:
    from core.importer import latest_launch_line, read_start_bat

    if _owned(server):
        bat = read_start_bat(server["install_dir"], _bat_path(server))
        if bat:
            return LaunchConfig.parse(bat[1]), "StartServer.bat (ArkPanel-owned)"

    entry = latest_launch_line(server.get("dsm_profile", ""))
    if entry:
        return LaunchConfig.parse(entry[1]), "DSM management log"
    bat = read_start_bat(server["install_dir"], _bat_path(server))
    if bat:
        return LaunchConfig.parse(bat[1]), "StartServer.bat"
    return None, "none"


def _server_exe(server: dict) -> str:
    from core.importer import read_start_bat

    bat = read_start_bat(server["install_dir"], _bat_path(server))
    if bat and bat[0]:
        return bat[0]
    return os.path.join(server["install_dir"], "ShooterGame", "Binaries",
                        "Win64", "ArkAscendedServer.exe")


def _write_bat(server: dict, cfg: LaunchConfig, note: str) -> dict:
    """Generate StartServer.bat from the launch model.

    Archives the pre-ArkPanel original once, then keeps timestamped backups of
    every later write. Writing this file is safe at any time -- it is read only
    when a server launches, so a running server is unaffected until it restarts.
    """
    path = _bat_path(server)
    archive = path + ".pre-arkpanel"
    archived = False
    if os.path.exists(path) and not os.path.exists(archive):
        shutil.copy2(path, archive)
        archived = True

    backup = None
    if os.path.exists(path):
        backup_dir = os.path.join(BASE, "backups", "launch", server["name"])
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(backup_dir, f"StartServer.bat.{stamp}.bak")
        shutil.copy2(path, backup)

    header = (
        f"Generated by ArkPanel {datetime.now():%Y-%m-%d %H:%M}. {note}\n"
        "Edit mods and launch options in ArkPanel rather than here -- this file\n"
        "is regenerated from the launch model, so hand edits will be overwritten.\n"
        f"Pre-ArkPanel original kept as {os.path.basename(archive)}"
    )
    text = build_start_bat(_server_exe(server), cfg.render(), header=header)

    was_readonly = os.path.exists(path) and not os.access(path, os.W_OK)
    if was_readonly:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    try:
        tmp = path + ".arkpanel.tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if was_readonly:
            try:
                os.chmod(path, stat.S_IREAD)
            except OSError:
                pass

    return {"path": path, "backup": backup, "archived_original": archived,
            "archive": archive if os.path.exists(archive) else None}


@app.post("/api/server/<name>/mods")
@require_token
@require_writable
def write_mods(name):
    """Set this server's mod list and load order.

    The list is written into the -mods= launch argument. Nothing is downloaded:
    the ARK server fetches and installs CurseForge mods itself on boot from
    that list. Changes take effect on the next restart.
    """
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404

    payload = request.get_json(silent=True) or {}
    raw = payload.get("mods")
    if not isinstance(raw, list) or not raw:
        return jsonify({"error": "mods must be a non-empty list of IDs"}), 400

    try:
        mods = [int(m) for m in raw]
    except (TypeError, ValueError):
        return jsonify({"error": "mod IDs must be numeric"}), 400

    if len(set(mods)) != len(mods):
        dupes = sorted({m for m in mods if mods.count(m) > 1})
        return jsonify({"error": f"duplicate mod IDs: {dupes}"}), 400

    cfg, source = _launch_basis(server)
    if cfg is None:
        return jsonify({"error": "no launch configuration found for this server"}), 500

    before = cfg.mods
    removed = [m for m in before if m not in mods]
    added = [m for m in mods if m not in before]
    reordered = (not removed and not added and before != mods)

    # Removing a mod destroys anything players stored in it, so it needs an
    # explicit acknowledgement rather than being a side effect of a save.
    if removed and not payload.get("confirm_removals"):
        table = build_mod_table(server["install_dir"], before)
        names = {m.mod_id: m.name for m in table}
        return jsonify({
            "error": "removal_not_confirmed",
            "message": ("Removing a mod permanently destroys anything players have "
                        "stored in it, and the server must restart for it to apply. "
                        "Resend with confirm_removals to proceed."),
            "removing": [{"mod_id": m, "name": names.get(m, f"Mod{m}")} for m in removed],
        }), 409

    cfg.set_mods(mods)
    try:
        written = _write_bat(server, cfg, note=f"mod list updated (was based on {source})")
    except OSError as exc:
        return jsonify({"error": "write_failed", "message": str(exc)}), 500

    return jsonify({
        "ok": True,
        "basis": source,
        "before": before,
        "after": mods,
        "added": added,
        "removed": removed,
        "reordered": reordered,
        "written": written,
        "applies": "on next server restart",
    })


@app.post("/api/server/<name>/launch")
@require_token
@require_writable
def write_launch(name):
    """Regenerate StartServer.bat from the current authoritative launch args.

    Used by the reconciliation screen to take ownership of the start script
    without changing anything about how the server launches.
    """
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    cfg, source = _launch_basis(server)
    if cfg is None:
        return jsonify({"error": "no launch configuration found"}), 500
    try:
        written = _write_bat(server, cfg, note=f"adopted from {source}")
    except OSError as exc:
        return jsonify({"error": "write_failed", "message": str(exc)}), 500
    return jsonify({"ok": True, "basis": source, "cmdline": cfg.render(), "written": written})


@app.post("/api/server/<name>/map")
@require_token
@require_writable
def write_map(name):
    """Point this server at a different map.

    The whole cluster shares one install, one pair of ini files, one set of
    ports and one password, so a map change is deliberately small: the map
    name, the advertised server name, and any mod that exists only to BE a
    map. Rates, QOL mods and ports are untouched by design -- the maps are
    meant to feel the same.

    Nothing is copied or deleted. ARK keeps every map's world in its own
    SavedArks folder, so the outgoing map's save simply stops being written to
    and is waiting, intact, whenever the rotation comes back to it.

    Refuses while the server is running: the bat is only read at launch, so
    writing it under a live server would leave the panel describing a map that
    is not the one players are on.
    """
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404

    payload = request.get_json(silent=True) or {}
    key = (payload.get("map") or "").strip()
    entry = maps.find(BASE, key)
    if not entry:
        known = ", ".join(sorted(maps.by_key(BASE))) or "none"
        return jsonify({"error": "unknown map",
                        "message": f"no map '{key}' in the catalog. Known: {known}"}), 400

    if not maps.is_available(entry):
        return jsonify({
            "error": "map_unavailable",
            "message": (f"{entry.get('name', key)} cannot be run: "
                        f"{maps.unavailable_reason(entry)}"),
        }), 409

    cfg, source = _launch_basis(server)
    if cfg is None:
        return jsonify({"error": "no launch configuration found for this server"}), 500

    before = {
        "map_name": cfg.map_name,
        "session_name": cfg.resolve("SessionName") or "",
        "mods": list(cfg.mods),
        "save_dir": server.get("save_dir", ""),
        "display_name": server.get("display_name", ""),
    }

    # Asked for the map it is already on. Answered before the running check,
    # because being told to shut the server down to change nothing is nonsense.
    if before["map_name"] == entry["map_name"]:
        return jsonify({"ok": True, "unchanged": True,
                        "message": f"already running {entry['name']}",
                        "before": before, "after": before})

    if not payload.get("force") and procwatch.find_server_process(server) is not None:
        return jsonify({
            "error": "server_running",
            "message": ("Stop the server before changing its map. The start script is "
                        "only read at launch, so changing it now would make ArkPanel "
                        "report a map the players are not actually on."),
        }), 409

    session = maps.session_name(entry)
    mods_after = maps.apply_map_mods(BASE, cfg.mods, entry)

    try:
        cfg.set_map(entry["map_name"])
        cfg.set_session_name(session)
        cfg.set_mods(mods_after)
    except ValueError as exc:
        return jsonify({"error": "launch_line_unparsed", "message": str(exc)}), 500

    try:
        written = _write_bat(
            server, cfg,
            note=f"map changed {before['map_name']} -> {entry['map_name']} "
                 f"(was based on {source})")
    except OSError as exc:
        return jsonify({"error": "write_failed", "message": str(exc)}), 500

    # The launch line wins in game, but the Discord info post reads SessionName
    # out of GameUserSettings.ini. Leaving that stale is how the post ends up
    # advertising a server name nobody can find in the browser.
    ini_result = _set_session_name_ini(server, session)

    # Backups snapshot save_dir, so it has to follow the map or the next
    # snapshot would faithfully archive an empty folder.
    server["map_name"] = entry["map_name"]
    server["display_name"] = entry["name"]
    server["save_dir"] = maps.save_dir(server["install_dir"], entry)
    server["map_key"] = entry["key"]
    save_config()

    after = {
        "map_name": entry["map_name"],
        "session_name": session,
        "mods": mods_after,
        "save_dir": server["save_dir"],
        "display_name": entry["name"],
    }
    alog(f"map switched: {name} {before['map_name']} -> {entry['map_name']}")
    return jsonify({
        "ok": True,
        "basis": source,
        "map": maps.profile(BASE, entry, server["install_dir"]),
        "before": before,
        "after": after,
        "mods_added": [m for m in mods_after if m not in before["mods"]],
        "mods_removed": [m for m in before["mods"] if m not in mods_after],
        "written": written,
        "session_name_ini": ini_result,
        "save_exists": os.path.isdir(after["save_dir"]),
        # What SavedArks actually contains. A map's save folder is not always
        # its map name -- Svartalfheim drops the _WP -- so when a catalog guess
        # is wrong this is what makes it visible instead of silently backing up
        # an empty folder.
        "save_folders_present": _saved_arks(server),
        "applies": "on next server start",
    })


def _saved_arks(server: dict) -> list[str]:
    root = os.path.join(server.get("install_dir", ""), "ShooterGame", "Saved", "SavedArks")
    try:
        return sorted(d for d in os.listdir(root)
                      if os.path.isdir(os.path.join(root, d)))
    except OSError:
        return []


def _set_session_name_ini(server: dict, session: str) -> dict:
    """Keep GameUserSettings.ini's SessionName in step with the launch line.

    Best effort on purpose: the map has already been switched by the time this
    runs, and a read-only ini or a parser refusal should show up as a warning
    on a successful switch, not fail the switch and leave the bat and the ini
    disagreeing.
    """
    path = os.path.join(server.get("config_dir", ""), "GameUserSettings.ini")
    if not os.path.exists(path):
        return {"ok": False, "message": "GameUserSettings.ini not found"}
    try:
        with open(path, "rb") as fh:
            original = fh.read()
        ini = IniFile.load(path)
        if ini.to_bytes() != original:
            return {"ok": False,
                    "message": "GameUserSettings.ini does not round-trip; left untouched"}

        # Find the existing line rather than naming a section. SessionName sits
        # in [SessionSettings], not the [ServerSettings] most keys live in, and
        # a wrong guess would not fail -- set_single would APPEND a second
        # SessionName in the wrong section and the engine would pick one of them.
        found = [ln for ln in ini.lines if (ln.key or "") == "SessionName"]
        if len(found) != 1:
            return {"ok": False,
                    "message": f"expected 1 SessionName line, found {len(found)}; left untouched"}
        found[0].set_value(session)

        backup, was_readonly = ini.save(
            path, backup_dir=os.path.join(BASE, "backups", "config", server["name"]))
        return {"ok": True, "value": session, "backup": backup,
                "readonly_restored": was_readonly}
    except (OSError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}


def _control_server(name: str) -> dict | None:
    """Server dict with the RCON password merged in from secrets.

    The password lives in secrets.json rather than config.json so the config
    can be read, copied and diffed without carrying a credential around.
    """
    server = find(name)
    if not server:
        return None
    merged = dict(server)
    merged["rcon_password"] = (
        SECRETS.get("rcon_password")
        or SECRETS.get("rcon_passwords", {}).get(name, "")
    )
    return merged


def _rcon_tool() -> None:
    """Kept only so the many call sites can pass something.

    ArkPanel speaks RCON itself now (core/rcon.py); there is no external tool to
    point at. control.rcon_command ignores this argument.
    """
    return None


def _schedule(kind: str, name: str, fn):
    try:
        job = control.JOBS.start(kind, name, fn)
    except RuntimeError as exc:
        return jsonify({"error": "busy", "message": str(exc)}), 409
    return jsonify({"ok": True, "job": job.to_dict()}), 202


@app.post("/api/server/<name>/start")
@require_token
@require_writable
def start(name):
    server = _control_server(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    payload = request.get_json(silent=True) or {}
    wait_ready = payload.get("wait_ready", True)

    # An explicit start OVERRIDES the down-on-purpose flag and clears it.
    # The flag's job is to stop the scheduler, the updater and the boot
    # autostart from reviving a server behind your back - not to argue with
    # somebody who has just asked for it. Clearing it also keeps the flag
    # honest: a running server must never still be marked as stopped, or the
    # monitor would go on ignoring it the next time it really did die.
    held = _maintenance_reason(name)
    if held:
        _set_maintenance(name, False)
        alog(f"[maintenance] {name}: cleared - started explicitly (was: {held})")

    return _schedule("start", name,
                     lambda job: control.do_start(job, server, wait_ready=wait_ready))


@app.post("/api/server/<name>/stop")
@require_token
@require_writable
def stop(name):
    server = _control_server(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    payload = request.get_json(silent=True) or {}
    sched = CONFIG.get("schedule", {})
    warn = payload.get("warn_minutes", sched.get("warn_minutes", []))
    message = payload.get("message", sched.get("warn_message",
                                               "Server restarting in {minutes} minutes"))
    save = payload.get("save", True)
    tool = _rcon_tool()
    return _schedule("stop", name, lambda job: control.do_stop(
        job, server, tool, warn, message, save=save))


@app.post("/api/server/<name>/restart")
@require_token
@require_writable
def restart(name):
    server = _control_server(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    payload = request.get_json(silent=True) or {}
    sched = CONFIG.get("schedule", {})
    warn = payload.get("warn_minutes", sched.get("warn_minutes", []))
    message = payload.get("message", sched.get("warn_message",
                                               "Server restarting in {minutes} minutes"))
    tool = _rcon_tool()

    def sequence(job):
        # Stop fully before starting. do_stop raises if the process has not
        # actually exited, which prevents the duplicate-process failure.
        control.do_stop(job, server, tool, warn, message, save=True)
        job.step("Shutdown confirmed - starting back up")
        control.do_start(job, server, wait_ready=True)

    return _schedule("restart", name, sequence)


@app.post("/api/server/<name>/kill")
@require_token
@require_writable
def kill(name):
    server = _control_server(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    return _schedule("kill", name, lambda job: control.do_kill(job, server))


@app.post("/api/server/<name>/save")
@require_token
@require_writable
def save_world(name):
    server = _control_server(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    try:
        out = control.rcon_command(server, "SaveWorld", _rcon_tool())
    except Exception as exc:
        return jsonify({"error": "rcon_failed", "message": str(exc)}), 502
    return jsonify({"ok": True, "response": (out or "").strip()})


@app.post("/api/server/<name>/rcon")
@require_token
@require_writable
def rcon_passthrough(name):
    """Send one RCON command. The allowlist still applies."""
    server = _control_server(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    cmd = (request.get_json(silent=True) or {}).get("command", "").strip()
    if not cmd:
        return jsonify({"error": "no command"}), 400
    try:
        out = control.rcon_command(server, cmd, _rcon_tool())
    except Exception as exc:
        return jsonify({"error": "rcon_failed", "message": str(exc)}), 400
    return jsonify({"ok": True, "response": (out or "").strip()})


@app.get("/api/server/<name>/players")
@require_token
def players(name):
    """Connected players, straight from the server over RCON.

    Returns null rather than 0 when the query fails. Reporting an unknown count
    as "nobody online" would let an update skip its warning countdown and drop
    players without notice.
    """
    server = _control_server(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    if procwatch.find_server_process(server) is None:
        return jsonify({"server": name, "count": 0, "names": [], "running": False})
    names = control.player_names(server, _rcon_tool())
    return jsonify({"server": name,
                    "count": None if names is None else len(names),
                    "names": names or [],
                    "running": True,
                    "known": names is not None})


@app.get("/api/server/<name>/version")
@require_token
def version_info(name):
    """Installed build vs what Steam is offering on the public branch."""
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    steamcmd = server.get("steamcmd") or ""
    result = steam.compare(server["install_dir"], steamcmd,
                           app_id=str(CONFIG.get("cluster", {}).get("app_id", steam.APP_ID)),
                           branch=CONFIG.get("updates", {}).get("branch", "public"))
    result["server"] = name
    result["steamcmd"] = steamcmd
    return jsonify(result)


@app.post("/api/server/<name>/update")
@require_token
@require_writable
def update_server(name):
    """Warn, save, stop, back up, patch, restart, verify.

    Ordering is deliberate. The process must be confirmed EXITED before
    steamcmd touches the files -- patching underneath a running server is how
    an install gets corrupted. And a failed or partial update must never be
    followed by a start attempt.
    """
    server = _control_server(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404

    payload = request.get_json(silent=True) or {}
    sched = CONFIG.get("schedule", {})
    warn = payload.get("warn_minutes", sched.get("warn_minutes", []))
    message = payload.get("message",
                          "Server updating in {minutes} minutes - please log out safely")
    tool = _rcon_tool()
    steamcmd = server.get("steamcmd") or ""
    app_id = str(CONFIG.get("cluster", {}).get("app_id", steam.APP_ID))
    branch = CONFIG.get("updates", {}).get("branch", "public")

    def sequence(job):
        before = steam.installed_build(server["install_dir"], app_id)
        job.step(f"Installed build: {before['build']}")

        # PRE-FLIGHT, while the server is still UP and players are still on.
        #
        # The expensive lesson of 2026-09-08: the update stopped both maps and
        # only then discovered it could not download, then refused to restart.
        # Anything we can detect before the shutdown costs zero downtime; the
        # same thing detected afterwards costs an outage.
        pre = steam.preflight(server["install_dir"], steamcmd, app_id)
        if not pre["ok"]:
            for problem in pre["problems"]:
                job.step(f"Pre-flight: {problem}", level="warn")
            if "clear_manifest" in pre["fixable"]:
                moved = steam.clear_stuck_manifest(server["install_dir"], app_id)
                job.step(f"Pre-flight: cleared the stuck manifest"
                         + (f" (kept as {os.path.basename(moved)})" if moved else ""))
            else:
                raise RuntimeError(
                    "Pre-flight failed and cannot be fixed automatically: "
                    + "; ".join(pre["problems"])
                    + ". The server has NOT been stopped.")

        was_running = procwatch.find_server_process(server) is not None
        if was_running:
            control.do_stop(job, server, tool, warn, message, save=True)
            job.step("Shutdown confirmed - safe to patch")
        else:
            job.step("Server was not running; patching directly")

        # A backup before an update, not after: if the patch breaks something,
        # this is the last known-good state.
        try:
            snap = backup.run_backup(job, server, CONFIG,
                                     rcon_fn=None)  # server is down; no SaveWorld
            job.step(f"Pre-update backup: {snap['stamp']}")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Pre-update backup failed ({exc}). Refusing to patch without one."
            )

        job.step("Running steamcmd app_update (validate on)...")
        result = steam.update_with_retries(
            server["install_dir"], steamcmd, app_id=app_id, validate=True,
            on_line=lambda ln: job.step(ln[:160]))
        if not result["ok"]:
            # steamcmd's console output is nearly useless on failure -- the
            # actual reason is only in content_log.txt, so surface it here
            # rather than making someone go and find it.
            for line in result.get("content_log", [])[-6:]:
                if line.strip():
                    job.step(f"content_log: {line[:170]}", level="warn")

            # Did the failed attempt actually TOUCH the install?
            #
            # A refusal at the manifest stage writes nothing -- the install is
            # byte-for-byte what it was, so leaving the server down achieves
            # nothing except downtime. Only a failure that got as far as
            # writing files justifies staying stopped.
            wrote = steam.wrote_anything(server["install_dir"], app_id)
            if was_running and not wrote:
                job.step("The failed update never wrote to the install - it is "
                         "untouched, so the server is being started again rather "
                         "than left down.", level="warn")
                try:
                    control.do_start(job, server, wait_ready=True)
                    # Be honest about what this is worth. Restarting on the OLD
                    # build does NOT restore service: ARK clients auto-update,
                    # so nobody can join a server behind Steam's current build.
                    # It limits the damage, it does not fix it.
                    job.step("Server is back up on its PREVIOUS build - but "
                             "updated clients still cannot join it. This is not "
                             "service restored; the patch must still be applied.",
                             level="warn")
                except Exception as exc:  # noqa: BLE001
                    job.step(f"Could not restart: {exc}", level="error")
                raise RuntimeError(
                    f"steamcmd update failed after {result.get('attempt', 1)} "
                    f"attempt(s): {result.get('error')}. The install was NOT "
                    "modified and the server was restarted on its previous build, "
                    "which updated clients cannot join. Still needs patching.")

            raise RuntimeError(
                f"steamcmd update failed after {result.get('attempt', 1)} attempt(s): "
                f"{result.get('error')}. "
                "NOT starting the server - the install may be incomplete. "
                f"Last output: {' | '.join(result.get('tail', [])[-3:])}")
        job.step(f"steamcmd finished in {result['seconds']}s "
                 f"(attempt {result.get('attempt', 1)})")

        after = steam.installed_build(server["install_dir"], app_id)
        job.step(f"Build {before['build']} -> {after['build']}")

        # An unchanged build after an update that was ASKED FOR is a failure,
        # not a note. Svartalfheim downloaded 9.25 GB, ended on the same build,
        # logged this as a warning and started anyway -- so a patch that never
        # happened reported as success and the cluster silently stayed behind.
        target = payload.get("target_build")
        if after["build"] == before["build"] and target and str(target) != str(after["build"]):
            raise RuntimeError(
                f"steamcmd reported success but the build is still {after['build']} "
                f"(expected {target}). The patch did not apply. Not treating this "
                "as an update; the server is left as it was.")

        # A server flagged as down on purpose gets PATCHED but not started.
        # That is the whole point of updating it while it is off: it comes
        # back current whenever it is next switched on, and costs nothing
        # while it waits. Starting it here would put it alongside Palworld on
        # a machine with memory for one of them.
        held = _maintenance_reason(name)
        if held:
            job.step(f"Patched, and staying stopped on purpose - {held}. "
                     "It will come up on this build when it is next started.")
        elif was_running or payload.get("start_after", True):
            control.do_start(job, server, wait_ready=True)
        else:
            job.step("Leaving the server stopped, as it was before the update")

    return _schedule("update", name, sequence)


@app.post("/api/server/<name>/backup")
@require_token
@require_writable
def backup_now(name):
    server = _control_server(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    tool = _rcon_tool()

    def rcon_fn(srv, cmd):
        return control.rcon_command(srv, cmd, tool)

    # run_backup reports its own progress through job.step as it goes.
    return _schedule("backup", name,
                     lambda job: backup.run_backup(job, server, CONFIG, rcon_fn))


@app.post("/api/server/<name>/mirror")
@require_token
@require_writable
def mirror_now(name):
    server = _control_server(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    return _schedule("mirror", name,
                     lambda job: backup.run_mirror(job, server, CONFIG))


@app.get("/api/server/<name>/backups")
@require_token
def backups_list(name):
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    cfg = CONFIG.get("backup", {})
    local_root = cfg.get("local_root") or os.path.join(BASE, "backups")
    external_root = cfg.get("external_root")

    external = []
    external_error = None
    if external_root:
        parent = os.path.dirname(external_root.rstrip("\\/")) or external_root
        if os.path.isdir(external_root) or os.path.isdir(parent):
            external = backup.list_backups(external_root, name)
        else:
            external_error = f"external not reachable from this agent: {external_root}"

    return jsonify({
        "server": name,
        "local": {"root": local_root, "keep": cfg.get("local_keep"),
                  "backups": backup.list_backups(local_root, name)},
        "external": {"root": external_root, "keep": cfg.get("external_keep"),
                     "backups": external, "error": external_error},
    })


@app.post("/api/server/<name>/restore")
@require_token
@require_writable
def restore_backup(name):
    """Restore a snapshot. Refuses while the server is running."""
    server = _control_server(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404

    payload = request.get_json(silent=True) or {}
    stamp = payload.get("stamp")
    if not stamp:
        return jsonify({"error": "no snapshot specified"}), 400
    if payload.get("confirm") != "RESTORE":
        return jsonify({
            "error": "not_confirmed",
            "message": ("Restoring overwrites live save data. Resend with "
                        "confirm=RESTORE once the server is stopped."),
        }), 409

    cfg = CONFIG.get("backup", {})
    source = payload.get("source", "local")
    root = (cfg.get("local_root") or os.path.join(BASE, "backups")
            if source == "local" else cfg.get("external_root"))
    if not root:
        return jsonify({"error": f"no {source} backup root configured"}), 400

    parts = payload.get("parts") or ["SavedArks", "Config"]
    return _schedule("restore", name, lambda job: backup.restore(
        job, server, CONFIG, stamp, root, parts))


@app.get("/api/schedule")
@require_token
def schedule_status():
    if SCHEDULER is None:
        return jsonify({"running": False, "error": "scheduler not started"})
    out = SCHEDULER.status()
    out["schedule"] = CONFIG.get("schedule", {})
    out["autostart"] = CONFIG.get("autostart", {})
    return jsonify(out)


WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]


def save_config() -> None:
    """Write CONFIG back to config.json, keeping a timestamped copy first.

    Only ever called after the incoming values have been validated. The whole
    file is rewritten, so the previous copy is the undo path.
    """
    backup_dir = os.path.join(BASE, "backups", "config")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        shutil.copy2(CONFIG_PATH, os.path.join(backup_dir, f"config_{stamp}.json"))
    except OSError:
        pass

    tmp = CONFIG_PATH + ".arkpanel.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(CONFIG, fh, indent=2)
    os.replace(tmp, CONFIG_PATH)


@app.post("/api/schedule")
@require_token
@require_writable
def schedule_write():
    """Update the restart schedule.

    The Scheduler holds a reference to this same CONFIG dict and re-reads it on
    every tick, so the schedule block is updated IN PLACE -- rebinding CONFIG
    would leave the running scheduler pointed at the old settings while the
    file on disk said something else.
    """
    payload = request.get_json(silent=True) or {}
    sched = CONFIG.setdefault("schedule", {})
    errors = []
    changes = {}

    if "enabled" in payload:
        changes["enabled"] = bool(payload["enabled"])

    if "restart_days" in payload:
        days = payload["restart_days"] or []
        if not isinstance(days, list):
            errors.append("restart_days must be a list")
        else:
            bad = [d for d in days if d not in WEEKDAY_NAMES]
            if bad:
                errors.append(f"not weekday names: {', '.join(str(b) for b in bad)}")
            else:
                # Keep calendar order regardless of click order, so the UI and
                # the log read the way a week does.
                changes["restart_days"] = [d for d in WEEKDAY_NAMES if d in days]

    if "restart_time" in payload:
        text = str(payload["restart_time"]).strip()
        try:
            hh, mm = text.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError
            changes["restart_time"] = f"{int(hh):02d}:{int(mm):02d}"
        except (ValueError, AttributeError):
            errors.append(f"restart_time must be HH:MM, got '{text}'")

    if "skip_if_restarted_within_hours" in payload:
        try:
            hours = float(payload["skip_if_restarted_within_hours"])
            if hours < 0:
                raise ValueError
            changes["skip_if_restarted_within_hours"] = hours
        except (TypeError, ValueError):
            errors.append("skip_if_restarted_within_hours must be a number of hours, 0 or more")

    if "warn_minutes" in payload:
        try:
            rungs = sorted({int(m) for m in (payload["warn_minutes"] or []) if int(m) > 0},
                           reverse=True)
            changes["warn_minutes"] = rungs
        except (TypeError, ValueError):
            errors.append("warn_minutes must be a list of whole minutes")

    if "warn_message" in payload:
        raw = str(payload["warn_message"]).strip()
        # Validate the message as it will actually be SENT. {minutes} is
        # substituted before the text ever reaches RCON, so the braces in the
        # TEMPLATE are not braces RCON will see. Sanitising the raw template
        # ate the placeholder and turned every warning into
        # "Server restarting in minutes minutes".
        rendered = raw.replace("{minutes}", "30")
        clean, altered = control.sanitise_message(rendered)
        if not clean:
            errors.append("warn_message is empty once RCON-forbidden characters are removed")
        elif altered:
            bad = sorted({c for c in rendered if c in control.FORBIDDEN_RCON_CHARS})
            errors.append(
                "warn_message contains characters RCON rejects: "
                + " ".join(bad)
                + ". PatchRaptor's allowlist refuses the whole message, so it would "
                  "never reach players. Remove them. {minutes} itself is fine - it is "
                  "substituted before sending.")
        else:
            changes["warn_message"] = raw

    if "wipe_wild_dinos" in payload:
        changes["wipe_wild_dinos"] = bool(payload["wipe_wild_dinos"])

    if errors:
        return jsonify({"error": "invalid", "messages": errors}), 400

    # A cluster-wide setting must not land on one map and fail on the other, so
    # the panel asks every agent to validate BEFORE any of them writes.
    if payload.get("validate_only"):
        return jsonify({"ok": True, "validated": True, "would_change": changes})

    sched.update(changes)
    try:
        save_config()
    except OSError as exc:
        return jsonify({"error": "write_failed", "message": str(exc)}), 500

    alog(f"[schedule] updated: {json.dumps(changes)}")
    return jsonify({"ok": True, "schedule": sched})


@app.get("/api/discord")
@require_token
def discord_status():
    if WATCHER is None:
        return jsonify({"configured": bool(SECRETS.get("discord_watcher_token")),
                        "running": False, "ready": False})
    return jsonify(WATCHER.status())


@app.get("/api/alerts")
@require_token
def alerts():
    if MONITOR is None:
        return jsonify({"running": False, "alerts": []})
    return jsonify({
        **MONITOR.summary(),
        "recent": MONITOR.recent(limit=int(request.args.get("limit", 50))),
    })


@app.post("/api/alerts/acknowledge")
@require_token
@require_writable
def alerts_ack():
    if MONITOR is None:
        return jsonify({"error": "monitor not running"}), 503
    body = request.get_json(silent=True) or {}
    try:
        at = float(body["at"]) if body.get("at") is not None else None
    except (TypeError, ValueError):
        return jsonify({"error": "bad 'at'"}), 400
    return jsonify({"ok": True, "acknowledged": MONITOR.acknowledge(
        at=at, kind=body.get("kind") or None, server=body.get("server") or None)})


@app.get("/api/health-check")
@require_token
def health_check():
    """Run the monitor's checks right now and return what it sees.

    Useful for confirming the alerting logic agrees with reality without
    waiting for the next scheduled tick.
    """
    if MONITOR is None:
        return jsonify({"error": "monitor not running"}), 503
    return jsonify({"results": MONITOR.tick(), "summary": MONITOR.summary()})


@app.post("/api/alerts/test")
@require_token
@require_writable
def alerts_test():
    """Send a test alert through the configured sink, to prove delivery works
    before relying on it for something that matters."""
    if MONITOR is None:
        return jsonify({"error": "monitor not running"}), 503
    severity = (request.get_json(silent=True) or {}).get("severity", "info")
    server_name = servers()[0]["name"] if servers() else "cluster"

    # Route through the monitor's own _raise so the test behaves EXACTLY like a
    # real alert: recorded in the panel list and pushed to any sink. An earlier
    # version built the alert separately and only handed it to the sink, so
    # with no webhook configured it claimed "appears in the panel" and then
    # appeared nowhere at all.
    before = len(MONITOR.alerts)
    MONITOR._raise(
        server_name, "test", severity,
        "Test alert from ArkPanel. If you can see this, alert delivery works.")
    recorded = len(MONITOR.alerts) > before

    # Close it straight away. A test represents no ongoing condition, so
    # leaving it "active" would sit in the alert band forever with nothing
    # able to clear it -- which is exactly how a panel trains you to ignore
    # its own alerts.
    if recorded:
        for alert in reversed(MONITOR.alerts):
            if alert.kind == "test" and alert.resolved_at is None:
                alert.resolved_at = time.time()
                break
        MONITOR._save()

    return jsonify({
        "ok": True,
        "recorded_in_panel": recorded,
        "sink_configured": MONITOR.notify_fn is not None,
        "detail": ("sent to the configured sink and recorded in the panel"
                   if MONITOR.notify_fn else
                   "recorded in the panel; no webhook configured, so nothing was pushed"),
        "note": (None if recorded else
                 "Not recorded - a test alert was already raised recently and "
                 "the repeat-suppression window has not elapsed."),
        "severity": severity,
    })


@app.post("/api/admin/verify-code")
@require_token
def admin_verify_code():
    """Check that deployed code compiles, and report whether a restart is due.

    This deliberately does NOT restart the agent. Three attempts were made at
    remote self-restart and all three failed on Windows, twice taking the agent
    offline:

      os.execv              no true exec on Windows - spawns a new process
                            while the old one lingers holding the port,
                            producing duplicate agents.
      exit + RestartCount   a task whose action exits counts as "completed",
                            not "failed", so restart-on-failure never fires.
      detached relauncher   the helper did not reliably bring the task back.

    The feature only ever saved a couple of clicks, while repeatedly costing
    agent downtime, so the restart half is gone. Restart with
    restart-agent.bat on the machine, or Stop-ScheduledTask then
    Start-ScheduledTask (in that order - Start alone on a Running task is a
    silent no-op).

    Verifying the code compiles is still worth doing remotely: it catches a
    broken deploy before anyone walks over to restart anything.
    """
    import py_compile
    import sys
    import threading

    targets = [os.path.join(BASE, "agent.py")]
    core_dir = os.path.join(BASE, "core")
    if os.path.isdir(core_dir):
        targets += [os.path.join(core_dir, f)
                    for f in os.listdir(core_dir) if f.endswith(".py")]

    errors = []
    for path in targets:
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"{os.path.basename(path)}: {exc.msg.strip()}")
    if errors:
        return jsonify({
            "ok": False,
            "error": "compile_failed",
            "message": "Deployed code does NOT compile. Do not restart this "
                       "agent until it is fixed - it would fail to start.",
            "errors": errors,
        }), 400

    # Is the running agent older than the deployed files? If so a restart is
    # needed for the deploy to take effect.
    try:
        started = psutil.Process(os.getpid()).create_time()
    except Exception:  # noqa: BLE001
        started = 0
    newest = max((os.path.getmtime(p) for p in targets), default=0)
    stale = newest > started

    return jsonify({
        "ok": True,
        "compiles": True,
        "checked": len(targets),
        "restart_required": stale,
        "agent_started": started,
        "newest_file": newest,
        "message": ("Deployed code compiles. A restart is required to load it."
                    if stale else
                    "Deployed code compiles and this agent is already running it."),
        "how_to_restart": ("Run restart-agent.bat on that machine, or "
                           "Stop-ScheduledTask then Start-ScheduledTask - in "
                           "that order, because Start alone on a Running task "
                           "does nothing."),
    })


@app.get("/api/jobs")
@require_token
def jobs():
    return jsonify({"jobs": control.JOBS.recent()})


@app.get("/api/jobs/<job_id>")
@require_token
def job_detail(job_id):
    job = control.JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job.to_dict())


@app.post("/api/jobs/<job_id>/cancel")
@require_token
@require_writable
def job_cancel(job_id):
    job = control.JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    job.cancel.set()
    return jsonify({"ok": True, "message": "Cancellation requested", "job": job.to_dict()})


@app.post("/api/server/<name>/settings/<path:filename>")
@require_token
@require_writable
def write_settings(name, filename):
    """Apply edits to an ini file.

    Four guards, in order, before anything is written:

      1. The file must still match the etag the UI was given, so an edit made
         in Notepad meanwhile is never silently clobbered.
      2. The file must round-trip byte-identically as loaded. If this parser
         cannot reproduce the file it is holding, it has no business writing
         it -- refuse rather than risk mangling a live config.
      3. Every change must resolve to a real line before any mutation happens,
         so a stale request fails whole rather than half-applying.
      4. A timestamped backup is taken.
    """
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    if filename not in ("Game.ini", "GameUserSettings.ini", "Engine.ini"):
        return jsonify({"error": "unsupported file"}), 400

    payload = request.get_json(silent=True) or {}
    changes = payload.get("changes") or []
    if not changes:
        return jsonify({"error": "no changes supplied"}), 400

    path = os.path.join(server["config_dir"], filename)
    if not os.path.exists(path):
        return jsonify({"error": f"{filename} not found"}), 404

    with open(path, "rb") as fh:
        original = fh.read()

    supplied_etag = payload.get("etag")
    import hashlib
    current_etag = hashlib.sha256(original).hexdigest()
    if supplied_etag and supplied_etag != current_etag:
        return jsonify({
            "error": "stale",
            "message": (
                f"{filename} changed on disk since it was loaded. Reload and "
                "reapply your edits so nothing is overwritten."),
        }), 409

    ini = IniFile.load(path)
    if ini.to_bytes() != original:
        return jsonify({
            "error": "roundtrip_failed",
            "message": (
                f"Refusing to write: {filename} does not reproduce byte-identically "
                "when parsed, so an edit could corrupt it. This is a parser bug — "
                "please report the file."),
        }), 500

    try:
        applied = apply_changes(ini, changes)
    except (ChangeError, ValueError) as exc:
        return jsonify({"error": "change_rejected", "message": str(exc)}), 400

    backup_dir = os.path.join(BASE, "backups", "config", name)
    try:
        backup, was_readonly = ini.save(path, backup_dir=backup_dir)
    except OSError as exc:
        return jsonify({"error": "write_failed", "message": str(exc)}), 500

    return jsonify({
        "ok": True,
        "applied": applied,
        "count": len(applied),
        "backup": backup,
        # Surfaced so the operator can see the protection was put back. These
        # files are read-only on purpose -- it is what stops ARK overwriting
        # them on shutdown.
        "readonly_restored": was_readonly,
        "etag": _file_sha(path),
    })


SCHEDULER: Scheduler | None = None
MONITOR: Monitor | None = None
PERF: "perf.PerfRecorder | None" = None
CLUSTER_BACKUP: "clusterbackup.ClusterBackup | None" = None


def _start_cluster_backup() -> None:
    """Version the cluster transfer folder, on the one PC that holds it.

    Both agents carry the same cluster_dir; only the machine it physically lives
    on watches it, so an upload is never copied twice or over the network.
    """
    global CLUSTER_BACKUP
    cluster_dir = CONFIG.get("cluster", {}).get("cluster_dir", "")
    if not clusterbackup.runs_on_this_machine(cluster_dir):
        alog(f"[cluster-backup] not on this machine ({cluster_dir}) - not watching")
        return
    bcfg = CONFIG.get("backup", {})
    local_root = bcfg.get("local_root") or os.path.join(BASE, "backups")
    external = bcfg.get("external_root")
    CLUSTER_BACKUP = clusterbackup.ClusterBackup(
        cluster_dir=cluster_dir,
        store_root=os.path.join(local_root, "_cluster"),
        mirror_root=os.path.join(external, "_cluster") if external else None,
        logger=alog,
    )
    CLUSTER_BACKUP.start()


@app.get("/api/cluster/transfer-backups")
@require_token
def cluster_transfer_backups():
    """Status of the cluster transfer-folder backup on this machine."""
    if CLUSTER_BACKUP is None:
        return jsonify({"running": False, "here": False,
                        "reason": "the cluster folder is not on this machine"})
    out = CLUSTER_BACKUP.status()
    out["here"] = True
    return jsonify(out)


@app.get("/api/cluster/characters")
@require_token
def cluster_characters():
    """Characters with stored copies of their transfer file, newest version first."""
    if CLUSTER_BACKUP is None:
        return jsonify({"here": False, "characters": [],
                        "reason": "the cluster folder is not on this machine"})
    return jsonify({"here": True, "cluster_dir": CLUSTER_BACKUP.cluster_dir,
                    "characters": CLUSTER_BACKUP.characters()})


@app.post("/api/cluster/characters/<account_id>/restore")
@require_token
@require_writable
def cluster_character_restore(account_id):
    """Put a stored copy of one character back into the transfer folder.

    The caller (the panel) is responsible for checking the player is offline
    everywhere first: this agent can only see its own machine's servers.
    """
    if CLUSTER_BACKUP is None:
        return jsonify({"error": "not_here",
                        "message": "the cluster folder is not on this machine"}), 409
    payload = request.get_json(silent=True) or {}
    out = CLUSTER_BACKUP.restore(account_id, payload.get("version"))
    if not out.get("ok"):
        return jsonify(out), 400
    alog(f"cluster character restored: {account_id} from {out.get('restored')}")
    return jsonify(out)


@app.get("/api/server/<name>/perf")
@require_token
def server_perf(name):
    """What this server has actually cost, idle and with players on.

    ?hours= widens the window (default 24). ?samples=1 includes the raw rows.
    """
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    try:
        hours = max(0.1, min(float(request.args.get("hours", 24)), 24 * perf.KEEP_DAYS))
    except ValueError:
        return jsonify({"error": "hours must be a number"}), 400
    rows = perf.load(BASE, name, hours)
    out = {
        "server": name,
        "hours": hours,
        "recording": bool(PERF and PERF._thread and PERF._thread.is_alive()),
        "sample_seconds": perf.SAMPLE_SECONDS,
        "cpu_cores_on_machine": psutil.cpu_count(),
        "summary": perf.summarise(rows),
    }
    if request.args.get("samples"):
        out["samples"] = rows
    return jsonify(out)


MAINTENANCE_PATH = os.path.join(BASE, "data", "maintenance.json")


def _maintenance() -> dict:
    """Servers that are down ON PURPOSE, and why.

    A job only covers the minutes a stop is running. Something deliberately
    left off - the Island while Palworld has PC1's memory - is down for hours,
    and the monitor would otherwise raise a critical "NOT running, and ArkPanel
    did not stop it" and @here the Discord every time somebody switched games.

    On disk, because an agent restart must not resurrect the alert.
    """
    try:
        with open(MAINTENANCE_PATH, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _set_maintenance(name: str, on: bool, reason: str = "") -> dict:
    state = _maintenance()
    if on:
        state[name] = {"reason": reason or "stopped on purpose",
                       "since": datetime.now().isoformat(timespec="seconds")}
    else:
        state.pop(name, None)
    try:
        os.makedirs(os.path.dirname(MAINTENANCE_PATH), exist_ok=True)
        tmp = MAINTENANCE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, MAINTENANCE_PATH)
    except OSError as exc:
        alog(f"[maintenance] could not save: {exc}")
    return state


@app.get("/api/maintenance")
@require_token
def maintenance_status():
    return jsonify(_maintenance())


@app.post("/api/server/<name>/maintenance")
@require_token
@require_writable
def maintenance_set(name):
    """Mark a server as intentionally down, or back in service."""
    payload = request.get_json(silent=True) or {}
    on = bool(payload.get("on"))
    state = _set_maintenance(name, on, str(payload.get("reason") or ""))
    alog(f"[maintenance] {name}: {'ON - ' + str(payload.get('reason') or '') if on else 'cleared'}")
    return jsonify({"ok": True, "maintenance": state})


def _maintenance_reason(name: str):
    """Why this server is deliberately down, or None if it is not.

    The flag used to feed only the monitor, which muted the alert and nothing
    else. Everything that can START a server has to consult it too, or the
    machine quietly ends up running both games: the Monday scheduled restart
    did exactly that to the Island while Palworld held PC1's memory.
    """
    entry = _maintenance().get(name)
    return (entry or {}).get("reason") or "stopped on purpose" if entry else None


def _server_busy(name: str) -> bool:
    """True while this server's state is not news.

    Either a control job is running, or it has been deliberately left down.
    Both mean "we know, stop telling us".
    """
    if control.JOBS.active_for(name) is not None:
        return True
    return name in _maintenance()


def _scheduled_backup(server: dict):
    """Fire a backup from the scheduler. Returns None if the server is busy."""
    merged = _control_server(server["name"])
    tool = _rcon_tool()

    def rcon_fn(srv, cmd):
        return control.rcon_command(srv, cmd, tool)

    try:
        return control.JOBS.start(
            "backup", server["name"],
            lambda job: backup.run_backup(job, merged, CONFIG, rcon_fn))
    except RuntimeError:
        return None  # a control job is already running; try the next tick


def _file_backup(job=None):
    """Copy this machine's irreplaceable files to the configured destination.

    Throttled while anyone is playing, for the reason /IPG exists at all: this
    copy leaves the house through the same upstream link the servers use to
    talk to players, and saturating it desynced someone mid-session.
    """
    cfg = CONFIG.get("file_backup", {})
    busy = False
    try:
        for server in servers():
            merged = _control_server(server["name"]) or server
            if procwatch.find_server_process(merged) is None:
                continue
            count = control.player_count(merged, _rcon_tool())
            # None means the question could not be asked -- treat that as busy,
            # because guessing "nobody is on" is how a copy gets to trample a
            # session.
            if count is None or count > 0:
                busy = True
                break
    except Exception as exc:  # noqa: BLE001
        alog(f"[filebackup] could not check for players ({exc}) - throttling anyway")
        busy = True

    def step(message):
        alog(f"[filebackup] {message}")
        if job is not None:
            job.step(message)

    step(f"Starting file backup -> {cfg.get('destination')}"
         + (" (throttled, players online)" if busy else ""))
    result = filebackup.run_jobs(cfg, log=step, throttle=busy)
    step(f"Finished: ok={result.get('ok')} in {result.get('seconds')}s")
    return result


def _scheduled_file_backup():
    try:
        return control.JOBS.start("file-backup", "files", _file_backup)
    except RuntimeError:
        return None  # a control job is already running; try the next tick


@app.post("/api/backup/files")
@require_token
@require_writable
def backup_files():
    """Run the personal-file backup now, rather than waiting for the week."""
    cfg = CONFIG.get("file_backup", {})
    if not cfg.get("destination"):
        return jsonify({"error": "not configured",
                        "message": "Set file_backup.destination in config.json"}), 400
    return _schedule("file-backup", "files", _file_backup)


@app.get("/api/backup/files")
@require_token
def backup_files_status():
    cfg = CONFIG.get("file_backup", {})
    destination = cfg.get("destination")
    return jsonify({
        "enabled": bool(cfg.get("enabled")),
        "destination": destination,
        "interval_hours": cfg.get("interval_hours", 168),
        "jobs": [j.get("name") for j in cfg.get("jobs", [])],
        "reachable": bool(destination) and os.path.isdir(destination),
        "last_run": (SCHEDULER.state.get("last_file_backup") if SCHEDULER else None),
    })


def _scheduled_restart(server: dict):
    merged = _control_server(server["name"])
    sched = CONFIG.get("schedule", {})
    tool = _rcon_tool()
    name = server["name"]

    # A nightly restart must never RESURRECT a server that is off on purpose.
    # This is what put the Island back alongside Palworld on Monday: the
    # scheduler stopped an already-stopped server and dutifully started it
    # again, leaving 11 GB and 1 GB of game resident on a 15.9 GB machine
    # until somebody noticed two days later.
    #
    # The maintenance flag is still honoured for the OFFLINE half of the job -
    # see below - because keeping the install mirrored while it waits costs
    # nothing and means it is ready the moment it is switched back on.
    held = _maintenance_reason(name)

    def sequence(job):
        if held:
            job.step(f"{name} is stopped on purpose ({held}), so it is NOT "
                     "being restarted.")
            if CONFIG.get("backup", {}).get("mirror_on_scheduled_restart", True):
                try:
                    backup.run_mirror(job, merged, CONFIG)
                    job.step("Mirrored while it is down - nothing was started.")
                except Exception as exc:  # noqa: BLE001
                    job.step(f"Mirror failed ({exc})", level="warn")
            return

        control.do_stop(job, merged, tool,
                        sched.get("warn_minutes", []),
                        sched.get("warn_message", "Server restarting in {minutes} minutes"),
                        save=True)
        job.step("Shutdown confirmed - starting back up")
        control.do_start(job, merged, wait_ready=True,
                         wipe_wild_dinos=bool(sched.get("wipe_wild_dinos")),
                         rcon_tool=tool)

        # The mirror runs AFTER the server is back up, not between the stop
        # and the start.
        #
        # It used to sit in the gap, on the reasoning that nothing holds files
        # open while the server is down. But it mirrors to a share on another
        # machine, and on 2026-09-23 that share went away: robocopy wedged in
        # unkillable SMB I/O, its own 2-hour timeout never fired, and all
        # three of his maps stayed down for twelve hours waiting on a backup.
        # Nobody was playing at 23:00, so nothing alerted.
        #
        # Availability must not depend on a backup completing. run_mirror
        # already excludes Saved, Logs and Cache - the directories a running
        # server holds - which is why its own docstring says it is safe with
        # the server up. So the worst a hung mirror can now cost is a missed
        # mirror.
        if CONFIG.get("backup", {}).get("mirror_on_scheduled_restart", True):
            try:
                backup.run_mirror(job, merged, CONFIG)
            except Exception as exc:  # noqa: BLE001
                job.step(f"Mirror skipped ({exc}) - the server is up regardless",
                         level="warn")

    try:
        return control.JOBS.start("scheduled-restart", server["name"], sequence)
    except RuntimeError:
        return None


def _last_restart(server: dict):
    proc = procwatch.find_server_process(server)
    return proc.started_at if proc else None


SELFUPDATE_STATE = {"last_check": 0.0, "last_result": "not started yet",
                    "fingerprint": CODE_FINGERPRINT,
                    # Set once a triggered restart demonstrably did not
                    # happen. Sticky on purpose - see _finish_selfupdate.
                    "restart_failed": None}

# How long to wait before concluding a triggered restart is not coming. The
# real ones take 6-18 seconds end to end, so this is generous.
RESTART_GRACE_SECONDS = 45


def _selfupdate_once() -> str:
    """Fetch the panel's agent code if this machine is behind. Returns a note.

    Pull, not push. The panel does not need a route into this machine, a file
    share, or a remote session - which is the whole point, because the deploy
    button had none of those for PC3 and restarted it three times without
    delivering a file.
    """
    cfg = CONFIG.get("selfupdate", {}) or {}
    panel_url = (cfg.get("panel_url") or "").rstrip("/")
    if not panel_url:
        return "no panel_url configured"

    token = (SECRETS.get("agent_token") or "").strip()

    def fetch(path: str, params: dict | None = None, timeout: int = 30) -> bytes:
        """urllib, not requests. The agent's other work happens to have requests
        available on THIS machine, but PC2's Python did not - and an agent that
        cannot update itself because of a missing library is precisely the
        problem this feature exists to remove. The standard library is always
        there."""
        import urllib.parse
        import urllib.request
        url = f"{panel_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url)
        if token:
            request.add_header("X-ArkPanel-Token", token)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    try:
        wanted = json.loads(fetch("/api/agent-code/manifest").decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return f"could not reach the panel: {type(exc).__name__}: {exc}"
    if not isinstance(wanted, dict) or not wanted.get("files"):
        return f"panel gave no manifest ({str(wanted)[:80]})"

    # CODE_FINGERPRINT is what this PROCESS loaded at import. Comparing against
    # a fresh read of the disk instead was wrong in the one state that matters:
    # after files are written but before the restart, disk matches the panel
    # while the running code is still the old one, so the agent reported
    # "already current" and never retried the restart it had failed to trigger.
    if wanted.get("fingerprint") == CODE_FINGERPRINT:
        return f"already current ({CODE_FINGERPRINT})"

    behind = agentupdate.differences(BASE, wanted)
    if not behind:
        # Files are already right; only this process is stale. Nothing to
        # download - it just needs to be restarted onto what is on disk.
        return _finish_selfupdate(wanted, applied=0)

    # Never swap code out from under a running job. A backup or a map switch
    # matters more than being current a few minutes sooner. active_for() is
    # the real check - JobRunner tracks one active job per server, and the
    # file-backup job registers under "files".
    busy = [name for name in [s["name"] for s in servers()] + ["files"]
            if control.JOBS.active_for(name)]
    if busy:
        return f"busy ({', '.join(busy)}) - will retry"

    staging = os.path.join(BASE, "data", "_selfupdate")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    staged = {}
    for rel in behind:
        try:
            content = fetch("/api/agent-code/file", {"path": rel}, timeout=60)
            temp = os.path.join(staging, rel.replace("/", "__"))
            with open(temp, "wb") as fh:
                fh.write(content)
            staged[rel] = temp
        except Exception as exc:  # noqa: BLE001
            return f"download of {rel} failed: {type(exc).__name__}: {exc}"

    result = agentupdate.apply_update(BASE, staged, wanted, log=alog)
    shutil.rmtree(staging, ignore_errors=True)
    if not result["ok"]:
        return f"update refused: {'; '.join(result['rejected'])[:160]}"

    return _finish_selfupdate(wanted, applied=len(result["applied"]))


def _finish_selfupdate(wanted: dict, applied: int) -> str:
    """Restart this agent so the new files on disk become the running code.

    Deliberately DELAYED and on its own thread. The restart stops this very
    process, so firing it inline killed the HTTP request that asked for it and
    the caller saw a 500 from an update that had in fact worked perfectly.

    The return code of schtasks is checked. It was ignored before, which is how
    an update could write every file, log "restarting", and leave the agent
    running the old code with nothing saying otherwise.
    """
    target = wanted.get("fingerprint")

    # Already proved, this process lifetime, that the restart does not work.
    # Without this the agent retried every minute for ever: re-downloading,
    # re-applying, re-triggering, filling the log with identical lines and
    # hiding the one that mattered. Stay on the old code and keep SAYING so;
    # it clears when someone restarts the agent by hand, which is exactly the
    # intervention needed.
    if SELFUPDATE_STATE.get("restart_failed"):
        return (f"files are at {target} but the restart is broken, so this "
                f"agent is STILL running {CODE_FINGERPRINT}. "
                f"{SELFUPDATE_STATE['restart_failed']}")

    if not _restart_task_present():
        return (f"files are at {target} but the '{RESTART_TASK}' task is "
                "missing, so this agent is still running the OLD code. "
                "Restart it to finish.")

    def restart_soon():
        time.sleep(3)          # let the HTTP response go out first
        try:
            result = subprocess.run(
                ["schtasks", "/Run", "/TN", RESTART_TASK],
                capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != 0:
                alog(f"[selfupdate] RESTART FAILED ({result.returncode}): "
                     f"{(result.stdout or '') + (result.stderr or '')}"[:200])
                SELFUPDATE_STATE["restart_failed"] = (
                    f"schtasks refused to run '{RESTART_TASK}' "
                    f"(code {result.returncode})")
                return
            alog(f"[selfupdate] restart triggered for {target}")
        except Exception as exc:  # noqa: BLE001
            alog(f"[selfupdate] could not trigger the restart: {exc}")
            SELFUPDATE_STATE["restart_failed"] = f"could not trigger: {exc}"
            return

        # schtasks /Run returning 0 means "the run was REQUESTED", not "the
        # restart happened". A task whose action points at a missing script
        # launches powershell.exe, which exits immediately - and schtasks has
        # already reported success. That is how this agent came to log
        # "restart triggered" once a minute for an hour while never
        # restarting, each cycle re-applying files it had already applied.
        #
        # The only honest test is whether THIS process is still here.
        time.sleep(RESTART_GRACE_SECONDS)
        alog(f"[selfupdate] STILL RUNNING {RESTART_GRACE_SECONDS}s after "
             f"triggering '{RESTART_TASK}' - the restart did NOT happen. "
             f"Files are at {target}; this process is still on "
             f"{CODE_FINGERPRINT}. Check that the task exists and that its "
             f"action points at a script that is really there.")
        SELFUPDATE_STATE["restart_failed"] = (
            f"'{RESTART_TASK}' was triggered but this process is still alive "
            f"{RESTART_GRACE_SECONDS}s later - the task did nothing. Files "
            f"are at {target}, running {CODE_FINGERPRINT}.")

    threading.Thread(target=restart_soon, daemon=True,
                     name="arkpanel-selfupdate-restart").start()
    what = f"updated {applied} file(s) to {target}" if applied \
        else f"files already at {target}, only this process was stale"
    return f"{what}, restarting in 3s"


def _start_selfupdate() -> None:
    """Poll the panel for new agent code, on a slow timer."""
    cfg = CONFIG.get("selfupdate", {}) or {}
    if not cfg.get("enabled"):
        print("[selfupdate] disabled")
        return
    # Five, not fifteen. The cost is one small manifest fetch over the LAN or
    # Tailscale; the cost of the old default was up to a quarter of an hour of
    # the panel showing "OLD CODE" after every change, which is indisputably
    # the worse trade. The panel's Update agents button is instant and remains
    # the way to skip the wait entirely; this is just the backstop.
    minutes = float(cfg.get("check_minutes", 5) or 5)
    # Configurable so a smoke test does not cost 90 seconds before it can even
    # begin. The default is unchanged; only a test instance lowers it.
    settle = float(cfg.get("settle_seconds", 90))

    def loop():
        # Settle first: an agent that just started should not immediately
        # decide to restart itself.
        time.sleep(settle)
        while True:
            try:
                note = _selfupdate_once()
            except Exception as exc:  # noqa: BLE001 - this thread must not die
                note = f"check failed: {exc}"
            SELFUPDATE_STATE["last_check"] = time.time()
            SELFUPDATE_STATE["last_result"] = note
            if not note.startswith("already current"):
                alog(f"[selfupdate] {note}")
            time.sleep(minutes * 60)

    threading.Thread(target=loop, daemon=True, name="arkpanel-selfupdate").start()
    print(f"[selfupdate] checking the panel every {minutes:g} min")


@app.get("/api/selfupdate")
@require_token
def selfupdate_status():
    """What this agent is running, what is on its disk, and whether they agree.

    Reporting one "fingerprint" was worse than useless. It was a fresh read of
    DISK, so in the one state that matters - files updated, process not yet
    restarted - it showed the new code and read as "up to date" while the
    agent was still running the old one. _selfupdate_once() already learned
    this and compares against CODE_FINGERPRINT; this endpoint, the one a human
    actually looks at, did not.

    So both are named, and `stale` is the answer to the real question.
    """
    on_disk = codestamp.fingerprint(BASE)
    return jsonify({**SELFUPDATE_STATE,
                    "running": CODE_FINGERPRINT,
                    "on_disk": on_disk,
                    "stale": on_disk != CODE_FINGERPRINT,
                    # Kept, and deliberately the RUNNING code: anything already
                    # reading this field wanted to know what the agent is
                    # executing, which is what it now gets.
                    "fingerprint": CODE_FINGERPRINT,
                    "restartable": _restart_task_present(),
                    "enabled": bool((CONFIG.get("selfupdate") or {}).get("enabled")),
                    "panel_url": (CONFIG.get("selfupdate") or {}).get("panel_url")})


@app.post("/api/selfupdate/check")
@require_token
@require_writable
def selfupdate_now():
    """Check immediately rather than waiting for the timer.

    Reports its own failure rather than letting Flask return a bare 500. An
    agent that cannot say why its update failed is one somebody has to go and
    read logs on - which, for a machine in another house, is the whole problem
    this feature exists to avoid.
    """
    try:
        note = _selfupdate_once()
    except Exception as exc:  # noqa: BLE001
        detail = traceback.format_exc(limit=4)
        alog(f"[selfupdate] check FAILED: {detail}")
        SELFUPDATE_STATE["last_check"] = time.time()
        SELFUPDATE_STATE["last_result"] = f"failed: {type(exc).__name__}: {exc}"
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                        "traceback": detail}), 500
    SELFUPDATE_STATE["last_check"] = time.time()
    SELFUPDATE_STATE["last_result"] = note
    return jsonify({"ok": True, "result": note})


def start_services() -> None:
    """Everything the agent runs in the background, without serving HTTP.

    Split out of main() so the PANEL can host an agent inside itself. A person
    with one PC should install one program, not two that have to find each
    other on localhost; a second machine gets the agent installer and joins the
    cluster. Same code either way -- an embedded agent is not a cut-down one.

    main() is now this plus _serve(), which is what the standalone agent on
    another PC still does.
    """
    global SCHEDULER, MONITOR
    SCHEDULER = Scheduler(
        base_dir=BASE, config=CONFIG, servers_fn=servers,
        backup_fn=_scheduled_backup, restart_fn=_scheduled_restart,
        last_restart_fn=_last_restart,
        file_backup_fn=_scheduled_file_backup,
    )
    SCHEDULER.start()

    def _answers_rcon(server: dict) -> bool | None:
        """True if the server replies at all. None if the question could not be asked."""
        try:
            return control.player_names(_control_server(server["name"]) or server,
                                        _rcon_tool()) is not None
        except Exception:  # noqa: BLE001
            return None

    MONITOR = Monitor(
        base_dir=BASE, config=CONFIG, servers_fn=servers,
        busy_fn=_server_busy,
        notify_fn=notify.make_sink(SECRETS, CONFIG),
        alive_fn=_answers_rcon,
    )
    MONITOR.start(interval=int(CONFIG.get("monitor", {}).get("interval_seconds", 60)))

    global PERF
    PERF = perf.PerfRecorder(
        base_dir=BASE, servers_fn=servers,
        players_fn=lambda s: control.player_count(
            _control_server(s["name"]) or s, _rcon_tool()),
        logger=alog,
    )
    PERF.start()

    _start_cluster_backup()

    # Without this the agent never polls the panel and stays on whatever code
    # it booted with, for ever. It was written, endpoint and all, and simply
    # never called - which is why every agent but PC1 sat on OLD CODE while
    # /api/selfupdate cheerfully reported "enabled": true.
    _start_selfupdate()

    _start_watcher_bot()
    _start_palworld_snapshots()
    _start_palworld_watchdog()
    _autostart_after_boot()



# ------------------------------------------------------------------ palworld
#
# WHY THIS LIVES IN THE AGENT.
# Until 2026-11-09 Palworld was panel-only: core/palworld.py talks RCON to
# 127.0.0.1 and reads the save off the local disk, so the panel could only ever
# drive a Palworld on its OWN machine. When the server moved to PC2 the panel
# kept pointing at PC1's stopped copy and the live world silently lost every
# backup it had. Running it here means the agent on whichever box hosts
# Palworld owns it completely -- snapshots and watchdog included -- and the
# panel proxies over HTTP exactly as it already does for the ARK maps.
#
# Deliberately self-contained: if the panel is down, the world still gets
# snapshotted and a crashed server still gets restarted.

PALWORLD_WANTED = os.path.join(BASE, "data", "palworld_wanted.json")


def _palworld_enabled() -> bool:
    block = CONFIG.get("palworld") or {}
    return bool(block.get("enabled")) and bool(block.get("install_dir"))


def _palworld_cfg() -> dict:
    """The palworld block, plus the cluster RCON password and the live ini.

    Mirrors panel.py:_palworld_cfg(). The password lives in secrets.json, never
    config.json -- handing core.palworld a raw config block gives it no
    password and every RCON call fails with "password refused".
    """
    cfg = dict(CONFIG.get("palworld") or {})
    cfg["rcon_password"] = (SECRETS.get("rcon_password") or "").strip()
    try:
        settings = palworld.read_settings(cfg.get("install_dir", ""))
    except Exception:  # noqa: BLE001
        settings = {}
    cfg["server_name"] = settings.get("ServerName", "").strip('"')
    cfg["password"] = settings.get("ServerPassword", "").strip('"')
    return cfg


def _palworld_wanted(value=None) -> bool:
    """Should Palworld be running here? On disk, so a restart does not forget.

    "Not running" alone cannot tell a crash from a deliberate stop, and the two
    need opposite responses.
    """
    if value is not None:
        try:
            os.makedirs(os.path.dirname(PALWORLD_WANTED), exist_ok=True)
            with open(PALWORLD_WANTED, "w", encoding="utf-8") as fh:
                json.dump({"wanted": bool(value),
                           "at": datetime.now().isoformat(timespec="seconds")}, fh)
        except OSError as exc:
            alog(f"[palworld] could not record wanted state: {exc}")
        return bool(value)
    try:
        with open(PALWORLD_WANTED, "r", encoding="utf-8-sig") as fh:
            return bool(json.load(fh).get("wanted"))
    except (OSError, ValueError):
        return True


def _palworld_guard(fn):
    """404 with a clear reason when this machine does not host Palworld."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _palworld_enabled():
            return jsonify({"enabled": False,
                            "error": "this agent does not host Palworld"}), 404
        return fn(*args, **kwargs)
    return wrapper


@app.get("/api/palworld/status")
@require_token
@_palworld_guard
def palworld_status():
    cfg = _palworld_cfg()
    out = dict(palworld.status(cfg))
    out["enabled"] = True
    out["wanted"] = _palworld_wanted()
    out["host"] = os.environ.get("COMPUTERNAME", "")
    out["server_name"] = cfg.get("server_name", "")
    try:
        out["newest_snapshot"] = palworld.newest_snapshot(cfg)
    except Exception as exc:  # noqa: BLE001
        out["newest_snapshot"] = None
        out["snapshot_error"] = str(exc)
    return jsonify(out)


@app.get("/api/palworld/players")
@require_token
@_palworld_guard
def palworld_players():
    names = palworld.players(_palworld_cfg())
    # None is not an empty list: an unknown result reported as "nobody online"
    # lets a restart skip its warning countdown and drop players without
    # notice, which is the whole reason this distinction exists.
    return jsonify({"known": names is not None,
                    "count": len(names) if names is not None else None,
                    "names": names or []})


@app.post("/api/palworld/start")
@require_token
@require_writable
@_palworld_guard
def palworld_start():
    _palworld_wanted(True)
    result = palworld.start(_palworld_cfg(), log=alog)
    return jsonify(result), (200 if result.get("ok") else 500)


@app.post("/api/palworld/stop")
@require_token
@require_writable
@_palworld_guard
def palworld_stop():
    payload = request.get_json(silent=True) or {}
    warn = int(payload.get("warn_seconds", 30))
    # Record the intent FIRST, or the watchdog sees it go down and races us
    # back up -- exactly what happened on PC1 on 2026-11-09.
    _palworld_wanted(False)
    result = palworld.stop(_palworld_cfg(), warn_seconds=warn, log=alog)
    return jsonify(result), (200 if result.get("ok") else 500)


@app.post("/api/palworld/save")
@require_token
@require_writable
@_palworld_guard
def palworld_save():
    out = palworld.save_and_snapshot(_palworld_cfg(), log=alog)
    return jsonify({"ok": True, "result": out})


@app.post("/api/palworld/snapshot")
@require_token
@require_writable
@_palworld_guard
def palworld_snapshot():
    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason", "manual"))[:40]
    kept = palworld.snapshot(_palworld_cfg(), reason=reason, log=alog)
    return jsonify({"ok": True, "snapshot": kept})


@app.get("/api/palworld/settings")
@require_token
@_palworld_guard
def palworld_settings_get():
    """Raw PalWorldSettings.ini keys. The panel groups and explains them."""
    cfg = _palworld_cfg()
    try:
        settings = palworld.read_settings(cfg.get("install_dir", ""))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": "unreadable", "message": str(exc)}), 500
    return jsonify({
        "settings": settings,
        "count": len(settings),
        "running": bool(palworld.find_process(cfg.get("install_dir", ""))),
    })


@app.post("/api/palworld/settings")
@require_token
@require_writable
@_palworld_guard
def palworld_settings_set():
    """Apply changes through write_settings, which rewrites the single
    OptionSettings line and keeps every key it was not asked to touch."""
    payload = request.get_json(silent=True) or {}
    changes = payload.get("changes") or {}
    if not changes:
        return jsonify({"error": "no_changes"}), 400

    cfg = _palworld_cfg()
    install = cfg.get("install_dir", "")
    running = bool(palworld.find_process(install))

    # Palworld REWRITES PalWorldSettings.ini from memory when it shuts down,
    # so a setting written while the server is up survives exactly until the
    # next stop, which clobbers it. The only reliable order is
    # stop -> write -> start. Proved 2026-10-12: written while running, 0/18
    # survived; written while stopped, 18/18. This logic lives here, on the
    # machine that owns the process, rather than in the panel.
    if not running:
        try:
            applied = palworld.write_settings(install, changes)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": "write_failed", "message": str(exc)}), 500
        for key, (before, after) in applied.items():
            alog(f"[palworld] setting {key}: {before} -> {after}")
        return jsonify({
            "ok": True, "cycled": False, "count": len(applied),
            "applied": {k: {"before": b, "after": a} for k, (b, a) in applied.items()},
            "restart_required": False,
            "message": "Saved. The server is stopped, so these load when it starts.",
        })

    def cycle():
        try:
            alog(f"[palworld] applying {len(changes)} setting(s): stopping first, "
                 "because Palworld overwrites its settings file on shutdown")
            _palworld_wanted(False)
            palworld.stop(cfg, warn_seconds=15, log=alog)
            time.sleep(4)
            applied = palworld.write_settings(install, changes)
            for key, (before, after) in applied.items():
                alog(f"[palworld] setting {key}: {before} -> {after}")
            result = palworld.start(_palworld_cfg(), log=alog)
            _palworld_wanted(bool(result.get("ok")))
            alog(f"[palworld] settings applied and server restarted "
                 f"({len(applied)} changed)")
        except Exception as exc:  # noqa: BLE001
            alog(f"[palworld] applying settings failed: {exc}")
            _palworld_wanted(True)

    threading.Thread(target=cycle, daemon=True,
                     name="arkpanel-palworld-settings").start()
    return jsonify({
        "ok": True, "cycled": True, "count": len(changes),
        "restart_required": True,
        "message": ("Stopping, saving and restarting - Palworld overwrites its "
                    "settings file on shutdown, so it has to be done in that order."),
    }), 202


def _start_palworld_snapshots() -> None:
    """Snapshot the world on a timer, as the panel does for a local one."""
    if not _palworld_enabled():
        return
    minutes = int((CONFIG.get("palworld") or {}).get("snapshot_minutes", 20) or 0)
    if minutes <= 0:
        print("[palworld] snapshots disabled")
        return

    def loop():
        time.sleep(120)
        while True:
            try:
                cfg = _palworld_cfg()
                if palworld.find_process(cfg.get("install_dir", "")):
                    palworld.save_and_snapshot(cfg, log=alog)
            except Exception as exc:  # noqa: BLE001
                alog(f"[palworld] snapshot failed: {exc}")
            time.sleep(minutes * 60)

    threading.Thread(target=loop, daemon=True,
                     name="arkpanel-palworld-snapshots").start()
    print(f"[palworld] snapshotting every {minutes} min")


def _start_palworld_watchdog() -> None:
    """Restart it if it dies, but never if it was stopped on purpose."""
    if not _palworld_enabled():
        return
    seconds = int((CONFIG.get("palworld") or {}).get("watchdog_seconds", 120) or 0)
    if seconds <= 0:
        print("[palworld] watchdog disabled")
        return

    def loop():
        time.sleep(150)
        misses = 0
        while True:
            time.sleep(seconds)
            try:
                if not _palworld_wanted():
                    misses = 0
                    continue
                if palworld.find_process(_palworld_cfg().get("install_dir", "")):
                    misses = 0
                    continue
                # Two consecutive misses before acting: one miss can be a
                # process mid-restart, and racing it would start a second.
                misses += 1
                if misses < 2:
                    continue
                misses = 0
                alog("[palworld] watchdog: it is DOWN but should be up - restarting")
                result = palworld.start(_palworld_cfg(), log=alog)
                if result.get("ok"):
                    alog("[palworld] watchdog: restarted")
                else:
                    alog(f"[palworld] watchdog: restart FAILED - {result}")
            except Exception as exc:  # noqa: BLE001
                alog(f"[palworld] watchdog error: {exc}")

    threading.Thread(target=loop, daemon=True,
                     name="arkpanel-palworld-watchdog").start()
    print(f"[palworld] watchdog checking every {seconds}s")


def main() -> None:
    start_services()
    _serve()   # blocks until the process exits


# ------------------------------------------------------------- boot recovery

def alog(message: str) -> None:
    """Append a line to logs/agent.log as well as stdout.

    The boot Scheduled Task runs pythonw.exe, which has no console and throws
    stdout away -- so print() alone makes the one thing you need after an
    unattended reboot, namely WHY the servers did or did not come back,
    completely unobservable. This is the only record of that decision.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(message)
    try:
        log_dir = os.path.join(BASE, "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "agent.log"), "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except OSError:
        pass   # logging must never take the agent down


def _autostart_after_boot() -> None:
    """Bring this PC's servers up again after an unattended reboot.

    The boot Scheduled Task starts the AGENT, and that has always worked -- but
    nothing ever started the ARK servers themselves. After a power-loss reboot
    both agents came back healthy and elevated while the cluster stayed down,
    which is the gap this closes. DSM used to do this; nothing replaced it.

    Gated on the machine's BOOT TIME, not merely on the agent having started.
    The agent is restarted by hand every time code is deployed, and a deploy
    must never resurrect a server that was stopped on purpose -- during a mod
    removal, say, which requires the server to be down.
    """
    cfg = CONFIG.get("autostart", {})
    try:
        since_boot = time.time() - psutil.boot_time()
    except Exception as exc:  # noqa: BLE001
        alog(f"[autostart] cannot read boot time ({exc}) - standing down")
        return

    go, reason = control.should_autostart(cfg, since_boot)
    if not go:
        alog(f"[autostart] not starting anything: {reason}")
        return

    delay = int(cfg.get("delay_seconds", 60))
    alog(f"[autostart] {reason} - starting this PC's servers in {delay}s")
    threading.Thread(target=_autostart_worker, args=(delay,), daemon=True,
                     name="arkpanel-autostart").start()


def _autostart_worker(delay: int) -> None:
    # The delay is on top of the Scheduled Task's own 90s boot delay. It gives
    # the network, the cluster share and any mapped drives time to settle --
    # ARK reads its mod list and cluster directory during startup.
    time.sleep(delay)

    for server in servers():
        name = server["name"]
        if server.get("autostart") is False:
            alog(f"[autostart] {name}: disabled for this server - skipping")
            continue

        # Same rule as the scheduler: a reboot must not undo a deliberate
        # stop. Power comes back, this runs, and the Island would be up
        # against Palworld again with nobody having asked for it.
        held = _maintenance_reason(name)
        if held:
            alog(f"[autostart] {name}: stopped on purpose ({held}) - not starting")
            continue

        merged = _control_server(name)
        if not merged:
            alog(f"[autostart] {name}: no such server - skipping")
            continue

        # The duplicate guard that matters most is exactly here: boot is when a
        # second copy is most likely, and two processes on one save is the
        # worst outcome this panel can produce.
        try:
            existing = procwatch.find_server_process(merged)
        except Exception as exc:  # noqa: BLE001
            alog(f"[autostart] {name}: cannot check for a running process ({exc}) "
                  "- skipping rather than risking a second copy")
            continue
        if existing:
            alog(f"[autostart] {name}: already running as PID {existing.pid} - nothing to do")
            continue

        try:
            job = control.JOBS.start(
                "autostart", name,
                lambda job, s=merged: control.do_start(job, s, wait_ready=True))
        except RuntimeError as exc:
            alog(f"[autostart] {name}: {exc}")
            continue

        alog(f"[autostart] {name}: start job {job.id}")
        threading.Thread(target=_autostart_report, args=(name, job), daemon=True,
                         name=f"arkpanel-autostart-{name}").start()


def _autostart_report(name: str, job) -> None:
    """Say whether the unattended recovery actually worked.

    Silence after a power cut looks identical to success, so both outcomes are
    announced -- the failure loudly.
    """
    while job.state == "running":
        time.sleep(5)

    if MONITOR is None:
        return
    if job.state == "done":
        MONITOR.note(name, "autostart", "info",
                     f"{name} was started automatically after a reboot and reached "
                     f"'advertising for join' in {int(time.time() - job.started)}s.")
    else:
        MONITOR.note(name, "autostart_failed", "critical",
                     f"{name} did NOT come back after the reboot: {job.error}. "
                     "The server is down and needs attention.")


WATCHER: WatcherBot | None = None


def _start_watcher_bot() -> None:
    """Start this machine's watcher bot, if one is configured.

    Each watcher reports only on the server that lives on its own PC -- that is
    what makes 'The Island Watcher' and 'Svartalfhiem Watcher' meaningful names
    rather than two bots saying the same thing.
    """
    global WATCHER
    token = (SECRETS.get("discord_watcher_token") or "").strip()
    channel = SECRETS.get("discord_watcher_channel")
    local = servers()
    if not token or not channel or not local:
        if not token:
            print("[discord] no discord_watcher_token - watcher bot not started")
        return

    # The bot's NAME stays the first server's ("Svartalfhiem Watcher") -- that
    # is the Discord identity players already know -- but it now reports every
    # server on this machine, one field each.
    server = local[0]

    def make_target(srv: dict) -> dict:
        # Bound per server. A lambda closing over a loop variable would make
        # every target report the LAST server, all of them identically.
        merged = _control_server(srv["name"]) or srv
        return {
            "name": srv["name"],
            "display_name": srv.get("display_name", srv["name"]),
            # Read at every refresh: a map switch renames the server in place, and
            # the copy above froze the old map's name until the agent restarted.
            "display_fn": lambda: srv.get("display_name", srv["name"]),
            # A short CPU sample, because this is read by a person every 30s and
            # a fresh Process object reports 0.0% without one.
            "status_fn": lambda: _status_for(srv, cpu_interval=0.3),
            "players_fn": lambda: (control.player_count(merged, _rcon_tool())
                                   if procwatch.find_server_process(srv) else 0),
        }

    def system_health():
        # A short sampling interval, because this reading is shown to a person
        # every 30s and a delta-based one can read 0.0% when callers sample
        # close together.
        stats = procwatch.system_stats(interval=0.4)
        stats["disk"] = procwatch.disk_free(server.get("install_dir", "")) or {}
        return stats

    WATCHER = WatcherBot(
        name=server.get("display_name", server["name"]) + " Watcher",
        token=token, channel_id=channel,
        targets=[make_target(s) for s in local],
        system_fn=system_health,
        # Persisted so an agent restart edits the existing live message rather
        # than leaving an orphan and posting a fresh one.
        state_path=os.path.join(BASE, "data", "watcher_message.json"),
        refresh_seconds=int(CONFIG.get("discord", {}).get("refresh_seconds", 30)),
        # Without this the watcher logs through print(), which pythonw.exe
        # discards -- so when it silently abandoned its status message there was
        # no record of why. Its diagnostics belong in agent.log like everything
        # else the agent does.
        logger=alog,
    )
    WATCHER.start()


def _serve() -> None:
    """Start the HTTP server. This blocks, so it must be the last thing main
    does -- an earlier edit accidentally left this inside the watcher-bot
    helper, which meant the agent set everything up and then never listened."""
    from waitress import serve

    host = CONFIG["agent"].get("host", "0.0.0.0")
    port = int(CONFIG["agent"].get("port", 8767))
    mode = "READ-ONLY" if CONFIG.get("read_only", True) else "READ-WRITE"
    print(f"ArkPanel Agent  {mode}  on {host}:{port}")
    print(f"  elevated : {_is_elevated()}")
    print(f"  servers  : {', '.join(s['name'] for s in servers()) or '(none local)'}")
    if not SECRETS.get("agent_token"):
        print("  WARNING: no agent_token in secrets.json - auth is disabled")
    serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
