"""
ArkPanel desktop launcher.

Opens the panel in a native window using WebView2 -- the same approach DSM
takes (its install carries Microsoft.Web.WebView2.*.dll and a ui.html). So this
looks and launches like a normal Windows program while the web server
underneath keeps working, which is what lets the cluster also be checked from a
phone or the other PC.

Service handling is deliberate:

  - If a service already answers on its port it was started elsewhere -- most
    likely the boot Scheduled Task -- so this leaves it completely alone and
    does not kill it on exit.
  - Only services this launcher started are stopped when the window closes.

Getting that backwards would mean closing the window silently killed the agent
that is supposed to be supervising the servers.
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
ICON = os.path.join(BASE, "static", "arkpanel.ico")

STARTUP_TIMEOUT = 25
_spawned: list[subprocess.Popen] = []


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def responding(url: str, timeout: float = 1.5) -> bool:
    """Is something alive on that address?

    A 401 counts as YES. The agent's /api/health requires a token, so an
    anonymous probe gets 401 -- and urlopen raises HTTPError for that, which is
    a subclass of URLError. Treating the exception as "not running" made this
    launcher start a SECOND agent on a port the first one already held; the
    child died instantly and the launcher reported "A service exited during
    startup", which is what a working cluster looked like from the desktop.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        # It answered. Refusing to answer WITHOUT credentials is still an answer.
        return exc.code < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def spawn(script: str) -> subprocess.Popen:
    """Start a service with no console window."""
    flags = 0
    startupinfo = None
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    # sys.executable is pythonw.exe when launched from the .pyw shortcut, which
    # would suppress the child's own output handling; force the console
    # interpreter for the services so their logging still works.
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        candidate = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(candidate):
            exe = candidate

    proc = subprocess.Popen(
        [exe, os.path.join(BASE, script)],
        cwd=BASE, creationflags=flags, startupinfo=startupinfo,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _spawned.append(proc)
    return proc


def stop_spawned() -> None:
    """Stop only what we started. Anything pre-existing is left running."""
    for proc in _spawned:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass


def ensure_services(config: dict) -> tuple[str, list[str]]:
    """Bring the agent and panel up if they aren't already. Returns (url, notes)."""
    agent_port = int(config["agent"].get("port", 8767))
    panel_port = int(config["panel"].get("port", 8090))
    agent_url = f"http://127.0.0.1:{agent_port}/api/health"
    panel_url = f"http://127.0.0.1:{panel_port}/"
    notes: list[str] = []

    if config.get("panel", {}).get("embedded_agent"):
        # This machine's agent runs INSIDE the panel process now, so there is
        # no separate one to start - and starting one would fight the panel for
        # the same port.
        notes.append("agent is embedded in the panel")
    elif responding(agent_url):
        notes.append("agent already running (left as-is)")
    else:
        spawn("agent.py")
        notes.append("agent started")

    if responding(panel_url):
        notes.append("panel already running (left as-is)")
    else:
        spawn("panel.py")
        notes.append("panel started")

    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if responding(panel_url):
            return panel_url, notes
        for proc in _spawned:
            if proc.poll() is not None:
                raise RuntimeError(
                    "A service exited during startup. Run 'python panel.py' in a "
                    "terminal to see the error."
                )
        time.sleep(0.4)
    raise RuntimeError(f"Panel did not respond on port {panel_port} within {STARTUP_TIMEOUT}s.")


def main() -> int:
    try:
        import webview
    except ImportError:
        print("pywebview is not installed.  pip install pywebview")
        return 1

    config = load_config()
    atexit.register(stop_spawned)

    try:
        url, notes = ensure_services(config)
    except (RuntimeError, OSError) as exc:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, f"ArkPanel could not start:\n\n{exc}", "ArkPanel", 0x10)
        except Exception:
            print(f"ArkPanel could not start: {exc}")
        return 1

    print("ArkPanel desktop  |  " + "; ".join(notes))

    # Sign in without showing a login form. /desktop/open trades a key from
    # secrets.json for a session and only accepts it from this machine, so a
    # window with no address bar and no password manager does not have to ask
    # for a password that is already readable to anything running here.
    try:
        with open(os.path.join(BASE, "secrets.json"), "r", encoding="utf-8-sig") as fh:
            key = (json.load(fh).get("desktop_key") or "").strip()
        if key:
            url = url.rstrip("/") + "/desktop/open?key=" + urllib.parse.quote(key)
    except (OSError, ValueError):
        pass          # no key: the login page still works

    mode = "READ-ONLY" if config.get("read_only", True) else ""
    title = "ArkPanel — DraconCluster" + (f"  [{mode}]" if mode else "")

    window = webview.create_window(
        title, url,
        width=1500, height=950, min_size=(1100, 700),
        background_color="#1b1b1f",
        text_select=True,
    )

    def on_closed():
        stop_spawned()

    window.events.closed += on_closed

    # gui=None lets pywebview pick EdgeChromium (WebView2) on Windows, which is
    # already installed here and is the same engine DSM embeds.
    webview.start(icon=ICON if os.path.exists(ICON) else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
