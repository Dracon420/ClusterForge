"""7 Days to Die dedicated server: config, control, and status.

The third game, and the one that proves the adapter layer is worth having -
because it does nothing the way the first two do:

  * Control is TELNET, not Source RCON. core/rcon.py cannot drive it. The
    protocol is a plain line-oriented socket: connect, send the password,
    send commands, read until the prompt goes quiet.

  * Config is XML - <property name="X" value="Y"/> - not ARK's ini and not
    Palworld's single 6000-character tuple. Only the value attribute may be
    touched; the wiki is explicit that editing anything else breaks it.

  * The server is told which config file to use with -configfile=. The
    shipped serverconfig.xml should be left alone and a named copy used
    instead, so a game update cannot overwrite your settings.

Ports worth knowing: the game wants 26900 plus the two above it. On a machine
whose router only forwards 7000-7999 (which is this one), set ServerPort into
that range instead of forwarding more - the property exists precisely so you
can.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
import xml.etree.ElementTree as ET

STEAM_APP_ID = "294420"

# The server binary, relative to the install directory.
EXE = "7DaysToDieServer.exe"

# Our own config file rather than the shipped one. A 7DtD update replaces
# serverconfig.xml; it does not replace a file it has never heard of.
CONFIG_NAME = "arkpanel-serverconfig.xml"

DEFAULT_ARGS = ["-logfile", "", "-quit", "-batchmode", "-nographics",
                "-dedicated"]


def config_path(install_dir: str) -> str:
    return os.path.join(install_dir, CONFIG_NAME)


def shipped_config(install_dir: str) -> str:
    return os.path.join(install_dir, "serverconfig.xml")


def ensure_config(install_dir: str) -> str:
    """Our config file, copied from the shipped one the first time."""
    ours = config_path(install_dir)
    if not os.path.isfile(ours):
        source = shipped_config(install_dir)
        if not os.path.isfile(source):
            raise FileNotFoundError(
                f"neither {ours} nor {source} exists - is the server installed?")
        shutil.copy2(source, ours)
    return ours


def read_settings(install_dir: str) -> dict:
    """Every <property name=... value=.../> as a dict. Empty if not installed."""
    path = config_path(install_dir)
    if not os.path.isfile(path):
        path = shipped_config(install_dir)
    if not os.path.isfile(path):
        return {}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {}
    return {el.get("name"): el.get("value", "")
            for el in root.iter("property") if el.get("name")}


def write_settings(install_dir: str, changes: dict) -> dict:
    """Change only the value attributes. Returns {key: (before, after)}.

    Rewrites via ElementTree rather than string replacement so a value
    containing XML-special characters cannot corrupt the document - and backs
    the file up first, because a broken serverconfig.xml is a server that
    will not start.
    """
    path = ensure_config(install_dir)
    tree = ET.parse(path)
    root = tree.getroot()

    existing = {el.get("name"): el for el in root.iter("property") if el.get("name")}
    applied = {}
    for key, value in changes.items():
        value = str(value)
        element = existing.get(key)
        if element is None:
            # A property the shipped file does not have. Adding it is correct:
            # 7DtD accepts properties absent from the default template.
            element = ET.SubElement(root, "property")
            element.set("name", key)
            before = None
        else:
            before = element.get("value", "")
        if before == value:
            continue
        element.set("value", value)
        applied[key] = (before, value)

    if applied:
        backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
        tree.write(path, encoding="utf-8", xml_declaration=True)
    return applied


# ------------------------------------------------------------------ telnet

class TelnetError(RuntimeError):
    pass


def telnet(host: str, port: int, password: str, command: str,
           timeout: float = 8.0) -> str:
    """Run one command over 7DtD's telnet console and return what it said.

    Written out rather than using telnetlib: that module was removed in
    Python 3.13, and this protocol is plain enough not to need it - the
    server sends a password prompt, takes a line, then echoes command output.
    """
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise TelnetError(f"cannot reach the console at {host}:{port}: {exc}") from exc

    sock.settimeout(timeout)
    buffer = []

    def drain(seconds: float = 0.6):
        """Read whatever arrives in the next `seconds`, then stop.

        There is no reliable end-of-response marker, so this reads until the
        server goes quiet - the same approach core/rcon.py uses for ARK's
        multi-packet replies.
        """
        deadline = time.time() + seconds
        sock.settimeout(0.3)
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer.append(chunk.decode("utf-8", "replace"))
            deadline = time.time() + seconds

    try:
        drain(1.0)                       # banner and password prompt
        if password:
            sock.sendall((password + "\r\n").encode())
            drain(1.0)
            seen = "".join(buffer)
            if "password incorrect" in seen.lower():
                raise TelnetError("the console refused that password")
        buffer.clear()
        sock.sendall((command + "\r\n").encode())
        drain(1.2)
        return "".join(buffer).strip()
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ----------------------------------------------------------------- process

def find_process(install_dir: str):
    """The running server for THIS install, or None."""
    import psutil
    target = os.path.normcase(os.path.abspath(install_dir))
    for proc in psutil.process_iter(["name", "exe", "create_time"]):
        try:
            if (proc.info["name"] or "").lower() != EXE.lower():
                continue
            exe = proc.info["exe"] or ""
            if os.path.normcase(os.path.abspath(exe)).startswith(target):
                return proc
        except Exception:  # noqa: BLE001 - psutil raises plenty on dead processes
            continue
    return None


def players(config: dict) -> list[str] | None:
    """Names from the `lp` (list players) console command.

    Output looks like:
        1. id=171, Bob, pos=(...), ...
    so the name is the second comma-separated field.
    """
    try:
        reply = telnet("127.0.0.1", int(config.get("telnet_port", 8081)),
                       config.get("telnet_password") or "", "lp")
    except TelnetError:
        return None
    names = []
    for line in (reply or "").splitlines():
        match = re.match(r"\s*\d+\.\s*id=\d+,\s*([^,]+),", line)
        if match:
            names.append(match.group(1).strip())
    return names


def status(config: dict) -> dict:
    import psutil  # noqa: F401 - imported for the same reason find_process does
    install_dir = config.get("install_dir", "")
    settings = read_settings(install_dir)
    proc = find_process(install_dir)
    out = {"installed": os.path.isfile(os.path.join(install_dir, EXE)),
           "running": proc is not None, "pid": None, "uptime_seconds": 0,
           "private_gb": 0, "players": None,
           "port": int(settings.get("ServerPort") or config.get("port", 26900)),
           "server_name": settings.get("ServerName", ""),
           "password": settings.get("ServerPassword", "")}
    if proc is None:
        return out
    try:
        out["pid"] = proc.pid
        out["uptime_seconds"] = round(time.time() - proc.create_time())
        out["private_gb"] = round(proc.memory_info().private / (1024 ** 3), 2)
    except Exception:  # noqa: BLE001
        pass
    out["players"] = players(config)
    return out


def start(config: dict, log=print) -> dict:
    install_dir = config["install_dir"]
    exe = os.path.join(install_dir, EXE)
    if not os.path.isfile(exe):
        return {"ok": False, "error": f"not installed: {exe}"}
    if find_process(install_dir):
        return {"ok": True, "already": True, "message": "already running"}

    ensure_config(install_dir)
    args = [exe, "-configfile=" + CONFIG_NAME] + list(
        config.get("args") or DEFAULT_ARGS)
    log(f"[7dtd] launching {EXE} with {CONFIG_NAME}")

    # Detached, for the reason Palworld is: a server that dies with whoever
    # started it is a server that dies when a terminal closes.
    detached = (getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    task = (config.get("task_name") or "").strip()
    if task:
        result = subprocess.run(["schtasks", "/Run", "/TN", task],
                                capture_output=True, text=True, timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            log(f"[7dtd] task '{task}' would not run - launching directly")
            task = ""
    if not task:
        try:
            subprocess.Popen(args, cwd=install_dir, creationflags=detached,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log(f"[7dtd] could not detach ({exc}) - starting as a child process")
            subprocess.Popen(args, cwd=install_dir,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + 90
    while time.time() < deadline:
        if find_process(install_dir):
            log("[7dtd] process started")
            return {"ok": True, "already": False}
        time.sleep(1)
    return {"ok": False, "error": "process did not appear within 90s"}


def stop(config: dict, warn_seconds: int = 30, log=print) -> dict:
    """Shut down over telnet. `shutdown` saves and exits cleanly."""
    install_dir = config["install_dir"]
    proc = find_process(install_dir)
    if proc is None:
        return {"ok": True, "already": True, "message": "not running"}

    port = int(config.get("telnet_port", 8081))
    password = config.get("telnet_password") or ""
    if warn_seconds:
        try:
            telnet("127.0.0.1", port, password,
                   f'say "Server restarting in {warn_seconds} seconds"')
            time.sleep(warn_seconds)
        except TelnetError as exc:
            log(f"[7dtd] could not warn players ({exc}) - continuing")
    try:
        telnet("127.0.0.1", port, password, "shutdown")
        log("[7dtd] shutdown requested")
    except TelnetError as exc:
        log(f"[7dtd] could not ask it to shut down ({exc})")

    deadline = time.time() + 90
    while time.time() < deadline:
        if find_process(install_dir) is None:
            log("[7dtd] stopped cleanly")
            return {"ok": True, "clean": True}
        time.sleep(2)

    log("[7dtd] still running - terminating")
    try:
        proc.terminate()
        proc.wait(timeout=30)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": find_process(install_dir) is None, "clean": False}


def save_root(install_dir: str, config: dict | None = None) -> str:
    """Where worlds live. 7DtD saves OUTSIDE the install, under AppData."""
    settings = read_settings(install_dir)
    explicit = (config or {}).get("save_dir") or settings.get("UserDataFolder")
    if explicit:
        return explicit
    return os.path.join(os.environ.get("APPDATA", ""), "7DaysToDie", "Saves")
