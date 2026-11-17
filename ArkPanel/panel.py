"""
ArkPanel -- the web UI, talking to one agent per PC.

The panel holds no server-control logic of its own. It aggregates the agents
and renders the interface, so opening it on either machine (or a phone) gives
the same view of the whole cluster.

When an agent is unreachable its server shows as 'unknown' with controls
disabled, and the rest of the cluster stays fully usable. A cluster-wide action
that cannot reach every agent it needs refuses rather than half-applying --
splitting the cluster is worse than doing nothing.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets as secrets_mod
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import requests
from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, session, url_for)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import agentupdate, bedrockdns, codestamp, games, maps, minecraft_transport, minecraft_update, palworld, palworld_schema, serverinfo
from core import rcon as rcon_mod  # noqa: E402
# Imported by name, not as `control`: this module already has a route function
# called control(), which shadowed the module and broke player lookups at runtime.
from core.control import parse_player_ids  # noqa: E402
from core.bouncer import BouncerBot  # noqa: E402
from core import discord_bot  # noqa: E402
from core.discord_bot import GuardianBot  # noqa: E402
from core.updater import Updater  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
SECRETS_PATH = os.path.join(BASE, "secrets.json")

# Short on connect so an offline PC fails fast and never stalls the page; more
# generous on read, since a live agent scanning processes can take a moment.
AGENT_TIMEOUT = (1.5, 8)
# Anything not on this machine. Generous on CONNECT because a lost SYN costs a
# whole second on the retransmit, and generous on READ because /api/status?live=1
# samples CPU for a moment per server before it answers.
REMOTE_AGENT_TIMEOUT = (6, 25)

app = Flask(__name__, static_folder="static", template_folder="templates")

# Windows' MIME registry often has no entry for these, so Flask served them as
# application/octet-stream and the browser would not paint them as a CSS
# background. Registering them here rather than relying on the machine.
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")
# Without this the manifest is served as octet-stream and the browser ignores
# it, so the app is not installable and no icon or name is picked up.
mimetypes.add_type("application/manifest+json", ".webmanifest")


def load_config() -> dict:
    # utf-8-sig: PowerShell's Set-Content -Encoding utf8 writes a BOM.
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def load_secrets() -> dict:
    """Load the shared agent token.

    Unreadable-but-present is fatal rather than ignored: without the token the
    panel would silently send unauthenticated requests, which only shows up as
    confusing 401s later -- or worse, works fine because the agent also failed
    open for the same reason.
    """
    if not os.path.exists(SECRETS_PATH):
        print("WARNING: secrets.json not found - requests to agents will be unauthenticated")
        return {}
    try:
        with open(SECRETS_PATH, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(
            f"FATAL: {SECRETS_PATH} exists but could not be read ({exc}).\n"
            "Refusing to start rather than talking to agents unauthenticated."
        )


CONFIG = load_config()
SECRETS = load_secrets()


def _session_secret() -> str:
    """Key that signs the login cookie.

    Persisted in secrets.json: a key generated fresh on each start would log
    everyone out on every panel restart, and this panel restarts on every
    deploy. Generated once if absent, and never printed.
    """
    existing = (SECRETS.get("panel_secret") or "").strip()
    if existing:
        return existing

    key = secrets_mod.token_hex(32)
    SECRETS["panel_secret"] = key
    try:
        tmp = SECRETS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(SECRETS, fh, indent=2)
        os.replace(tmp, SECRETS_PATH)
        print("[panel] generated a session signing key into secrets.json")
    except OSError as exc:
        print(f"[panel] WARNING: could not persist panel_secret ({exc}) - "
              "logins will not survive a restart")
    return key


app.secret_key = _session_secret()
app.permanent_session_lifetime = timedelta(days=30)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # NOT Secure-only: the panel is still reached over plain http on the LAN
    # (http://192.168.1.10:8090) as well as over the Tailscale HTTPS name, and a
    # Secure cookie would simply never be sent on the LAN, breaking login there.
    SESSION_COOKIE_SECURE=False,
)


def _headers() -> dict:
    token = SECRETS.get("agent_token")
    return {"X-ArkPanel-Token": token} if token else {}


def find(name: str) -> dict | None:
    for s in CONFIG["servers"]:
        if s["name"] == name:
            return s
    return None


def _timeout_for(url: str) -> tuple[float, float]:
    """Loopback gets the tight timeout; anything off this machine gets room.

    (1.5s connect, 8s read) was chosen when every agent was on the LAN. An
    agent across the internet is not slow but it is LOSSY: a lost SYN is
    retransmitted after about a second, so a healthy remote agent regularly
    answers in ~1.05s instead of ~0.04s. Measured on PC3, 12% of connections
    did exactly that, and any extra jitter pushed them past 1.5s and the panel
    called a working machine unreachable.
    """
    if "//127.0.0.1" in url or "//localhost" in url or "//[::1]" in url:
        return AGENT_TIMEOUT
    return REMOTE_AGENT_TIMEOUT


PLANNED_PATH = os.path.join(BASE, "data", "planned_downtime.json")


def planned_downtime() -> dict:
    """Machines that are off ON PURPOSE: {host: {reason, since, until}}.

    Per MACHINE, not per server, and held by the PANEL rather than the agent -
    because the whole point is that the agent cannot be asked. The per-server
    maintenance flag lives on its agent and is useless when the PC is
    unplugged, which is exactly when you most want to say "this is planned".

    HUBMASTER was switched off for building work at that house. Nothing could
    record that, so the panel spent 21 seconds timing out on each of its three
    maps on every page load, and reported them as failures.
    """
    try:
        with open(PLANNED_PATH, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_PLANNED_PROBE: dict[str, float] = {}
PLANNED_PROBE_SECONDS = 60


def _planned_for(server: dict):
    """The planned-downtime entry covering this server, or None.

    Matches on host, and honours `until`: an expired window stops applying by
    itself, so a forgotten flag cannot hide a real outage for ever.

    It also PROBES, at most once a minute, and clears itself the moment the
    machine answers. Without that the flag was a trap: HUBMASTER came back,
    the panel refused to talk to it because a human had said it was off, and
    the servers could not be started from the panel at all. A flag that says
    "do not wait on this machine" must never become "ignore this machine
    forever" - the evidence that it is back is cheap to collect and beats
    anything a person remembered to type.
    """
    host = (server.get("host") or "").strip().lower()
    if not host:
        return None
    entry = planned_downtime().get(host)
    if not entry:
        return None

    until = (entry.get("until") or "").strip()
    if until:
        try:
            if datetime.fromisoformat(until) < datetime.now():
                plog(f"[planned] {host}: window ended - back in service")
                set_planned_downtime(host, on=False)
                return None
        except ValueError:
            pass

    # Cheap liveness probe, rate limited. Short timeout and no retry: this is
    # a machine we believe is off, so the fast answer is the useful one.
    now = time.time()
    if now - _PLANNED_PROBE.get(host, 0.0) >= PLANNED_PROBE_SECONDS:
        _PLANNED_PROBE[host] = now
        try:
            resp = requests.get(server["agent_url"].rstrip("/") + "/api/health",
                                headers=_headers(), timeout=(1.5, 3))
            if resp.status_code < 500:
                plog(f"[planned] {host} is answering again - clearing its "
                     "planned downtime automatically")
                set_planned_downtime(host, on=False)
                return None
        except requests.RequestException:
            pass        # still off, as expected
    return entry


def set_planned_downtime(host: str, reason: str = "", until: str = "", on: bool = True) -> dict:
    state = planned_downtime()
    key = (host or "").strip().lower()
    if on:
        state[key] = {"reason": reason or "planned downtime",
                      "since": datetime.now().isoformat(timespec="seconds"),
                      "until": (until or "").strip()}
    else:
        state.pop(key, None)
    try:
        os.makedirs(os.path.dirname(PLANNED_PATH), exist_ok=True)
        tmp = PLANNED_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, PLANNED_PATH)
    except OSError as exc:
        plog(f"[planned] could not save: {exc}")
    return state


def agent_get(server: dict, path: str, **params) -> tuple[dict | None, str | None]:
    """Call one agent. Returns (payload, error). Never raises.

    Retries ONCE on a timeout. Reads are safe to repeat, and a single retry
    covers the dropped-packet case that produces nearly all of these. Writes go
    through agent_post, which deliberately does not retry: asking twice to
    start a server is not the same as asking twice what its status is.
    """
    # Do not queue behind a machine that is known to be off. Six seconds to
    # connect, retried, times three maps on PC3 made every cluster page take
    # twenty seconds to tell us something we already knew.
    planned = _planned_for(server)
    if planned:
        return None, f"planned: {planned.get('reason') or 'planned downtime'}"

    url = server["agent_url"].rstrip("/") + path
    timeout = _timeout_for(url)
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=timeout)
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=timeout)
        except requests.exceptions.ConnectTimeout:
            return None, "agent did not respond in time"
        except requests.exceptions.ConnectionError:
            return None, f"cannot reach agent at {server['agent_url']}"
        except requests.RequestException as exc:
            return None, f"agent error: {exc}"
    except requests.exceptions.ConnectionError:
        return None, f"cannot reach agent at {server['agent_url']}"
    except requests.RequestException as exc:
        return None, f"agent error: {exc}"
    if resp.status_code == 401:
        return None, "agent rejected the token"
    if resp.status_code >= 400:
        return None, f"agent returned {resp.status_code}"
    try:
        return resp.json(), None
    except ValueError:
        return None, "agent returned malformed JSON"


def _auth_configured() -> tuple[str, str] | None:
    user = (SECRETS.get("panel_user") or "").strip()
    password = (SECRETS.get("panel_password") or "").strip()
    return (user, password) if user and password else None


# Reachable without logging in. None of these carry cluster state: the app's
# name, its icons, and the caching rules.
#
# They are public because Chrome fetches the manifest, icons and service worker
# as part of deciding whether a site is INSTALLABLE, and those fetches do not
# reliably carry credentials. Behind auth they failed silently and Android
# offered only "Add to Home screen" instead of a real install.
PUBLIC_PATHS = {"/login", "/sw.js", "/favicon.ico",
                "/static/manifest.webmanifest", "/static/arkpanel.ico",
                # Does its own, stricter check: loopback only, plus a key from
                # secrets.json. It has to be reachable before a session exists,
                # because handing one out is its entire job.
                "/desktop/open",
                # Authenticated with the AGENT token instead of the panel
                # login, because agents fetch their own code and hold that
                # token already. Both refuse without it.
                "/api/agent-code/manifest", "/api/agent-code/file"}
PUBLIC_PREFIXES = ("/static/icons/",)


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _logged_in() -> bool:
    creds = _auth_configured()
    if creds is None:
        return False
    # A cookie session, or Basic auth for scripts and curl. Basic is kept
    # because every helper script in scripts\ uses it.
    if session.get("user") == creds[0]:
        return True
    auth = request.authorization
    return bool(auth and auth.username == creds[0] and auth.password == creds[1])


@app.before_request
def require_panel_auth():
    """Session-cookie login for the whole panel, Basic auth as a fallback.

    The agents authenticate with a shared token, but the PANEL had nothing --
    so exposing it to the LAN (or a phone) would let anything on the network
    start, stop and reconfigure the servers.

    Why a cookie rather than the browser's Basic auth dialog: an INSTALLED PWA
    running in standalone mode often cannot show that dialog at all, which
    would leave an installed app that can never log in. Cookies behave
    identically installed or not.

    If no credentials are configured the panel stays open, but only to
    loopback: an unauthenticated control surface should never be reachable from
    the network just because someone forgot to configure it.
    """
    if _is_public(request.path):
        return None

    creds = _auth_configured()
    remote = request.remote_addr or ""
    is_local = remote.startswith("127.") or remote in ("::1", "localhost")

    # Behind a reverse proxy (Tailscale serve, or anything else terminating
    # TLS) EVERY request arrives from 127.0.0.1, so "it came from loopback" no
    # longer means "someone is sitting at this PC" -- it would hand the whole
    # tailnet an unauthenticated control surface. Setting behind_proxy drops
    # the loopback exemption entirely.
    if CONFIG.get("panel", {}).get("behind_proxy"):
        is_local = False

    if creds is None:
        if is_local:
            return None
        return Response(
            "ArkPanel is not configured with a username and password, so it "
            "only accepts connections from the machine it runs on.\n\n"
            "Set panel_user and panel_password in secrets.json to use it from "
            "another device.",
            status=403, mimetype="text/plain")

    if _logged_in():
        return None

    # An API caller wants a status code it can act on, not a login page.
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorised",
                        "message": "Session expired. Reload the panel and log in."}), 401

    return redirect(url_for("login", next=request.full_path))


# ---------------------------------------------------------------- palworld
#
# PC1 has room for ONE game server at a time: the Island commits 11 GB of 15.9,
# and Palworld wants 8 or more. So the two take turns rather than coexist, and
# the switch is what makes that a button instead of a chore.

SWITCH_STATE = {"running": False, "target": None, "steps": [], "started": 0.0,
                "finished": None, "error": None}


def _palworld_cfg() -> dict:
    """Config plus the RCON password.

    The cluster's existing rcon_password, by the owner's choice - one password
    for everything rather than another to keep track of. Safe because RCON is
    never reachable from outside: only UDP 8211 is forwarded, never TCP 25575.
    """
    cfg = dict(CONFIG.get("palworld") or {})
    cfg["rcon_password"] = (SECRETS.get("rcon_password") or "").strip()
    # Name and join password come from the game's own ini, not config.json.
    # Reading them rather than duplicating them is what stops the Discord post
    # advertising a password the server stopped using - the exact drift the
    # generated ARK info posts exist to prevent.
    settings = {}
    remote = str((CONFIG.get("palworld") or {}).get("agent_url") or "").rstrip("/")
    if remote:
        # install_dir points at a path on ANOTHER machine, so reading it here
        # yields nothing and the Discord post advertises a blank name and no
        # password. Ask that machine's agent instead. Cached briefly because
        # this is called on every status poll and a 15s HTTP round trip per
        # poll would make the page crawl.
        settings = _palworld_remote_settings(remote)
    else:
        try:
            settings = palworld.read_settings(cfg.get("install_dir", ""))
        except Exception:  # noqa: BLE001
            settings = {}
    cfg["server_name"] = settings.get("ServerName", "").strip('"')
    cfg["password"] = settings.get("ServerPassword", "").strip('"')
    return cfg


_PALWORLD_SETTINGS_CACHE = {"at": 0.0, "url": "", "settings": {}}
_PALWORLD_SETTINGS_TTL = 60.0


def _palworld_remote_settings(url: str) -> dict:
    """PalWorldSettings.ini from the agent that hosts Palworld, cached 60s.

    Returns the last good answer when the agent is briefly unreachable: a
    momentary blip should not blank the server name on the dashboard.
    """
    now = time.time()
    hit = _PALWORLD_SETTINGS_CACHE
    if hit["url"] == url and (now - hit["at"]) < _PALWORLD_SETTINGS_TTL:
        return hit["settings"]
    try:
        resp = requests.get(url + "/api/palworld/settings",
                            headers=_headers(), timeout=(3, 10))
        if resp.status_code >= 400:
            return hit["settings"] if hit["url"] == url else {}
        settings = (resp.json() or {}).get("settings") or {}
    except (requests.RequestException, ValueError):
        return hit["settings"] if hit["url"] == url else {}
    _PALWORLD_SETTINGS_CACHE.update({"at": now, "url": url, "settings": settings})
    return settings


def _palworld_remote() -> str:
    """The agent URL when Palworld lives on another machine, else "".

    Palworld moved to PC2 on 2026-11-09. Everything in core/palworld.py is
    local-only -- RCON to 127.0.0.1, the save read straight off disk -- so when
    the server is elsewhere the panel must go through that machine's agent
    instead, exactly as it already does for the ARK maps. Without this the
    panel happily reports on PC1's stopped copy and calls it healthy.
    """
    return str((CONFIG.get("palworld") or {}).get("agent_url") or "").rstrip("/")


def _palworld_agent(method: str, path: str, payload: dict | None = None,
                    timeout: tuple = (3, 30)):
    """One call to the Palworld host's agent. Returns (body, error)."""
    url = _palworld_remote() + path
    if not url:
        return None, "palworld is not remote"
    try:
        if method == "GET":
            resp = requests.get(url, headers=_headers(), timeout=timeout)
        else:
            resp = requests.post(url, headers=_headers(), json=payload or {},
                                 timeout=timeout)
    except requests.exceptions.ConnectionError:
        return None, f"cannot reach the Palworld agent at {_palworld_remote()}"
    except requests.RequestException as exc:
        return None, f"agent error: {exc}"
    if resp.status_code == 401:
        return None, "agent rejected the token"
    if resp.status_code >= 400:
        try:
            return None, (resp.json() or {}).get("error") or f"agent returned {resp.status_code}"
        except ValueError:
            return None, f"agent returned {resp.status_code}"
    try:
        return resp.json(), None
    except ValueError:
        return None, "agent returned malformed JSON"


def _palworld_status() -> dict:
    cfg = _palworld_cfg()
    if not cfg.get("enabled"):
        return {"enabled": False}
    if _palworld_remote():
        body, error = _palworld_agent("GET", "/api/palworld/status", timeout=(3, 15))
        if error:
            return {"enabled": True, "error": error,
                    "host": cfg.get("host", ""), "remote": True}
        out = dict(body or {})
        out["remote"] = True
    else:
        try:
            out = palworld.status(cfg)
        except Exception as exc:  # noqa: BLE001
            return {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}
    out["enabled"] = True
    out["display_name"] = cfg.get("display_name", "Palworld")
    out["swap_with"] = cfg.get("swap_with", "island")
    # Straight from PalWorldSettings.ini, so the page and the Discord post
    # cannot advertise a name or password the server no longer uses.
    out["server_name"] = cfg.get("server_name") or ""
    out["search_name"] = palworld.search_name(out["server_name"])
    out["password"] = cfg.get("password") or ""
    out["discord_channel"] = str(
        CONFIG.get("discord", {}).get("palworld_channel") or "")
    # Settings saved but not yet loaded. Without this the panel shows a
    # climbing uptime and no sign that anything changed, which reads as
    # "the save did not work".
    try:
        stale, age = palworld.settings_changed_since_start(cfg)
        out["settings_stale"] = stale
        out["settings_changed_seconds_ago"] = age
    except Exception:  # noqa: BLE001
        out["settings_stale"] = None
    # Whether the map Palworld swaps with is up. The web page fetches this
    # from /api/cluster/status itself, but Discord has only this dict, and
    # without it a both-down cluster would be offered a "switch" with no
    # other game to switch to. Left as None on failure rather than False -
    # "unknown" and "stopped" must not look the same to the caller.
    out["ark_running"] = None
    try:
        ark = find(cfg.get("swap_with", "island"))
        if ark:
            body, error = agent_get(ark, f"/api/server/{ark['name']}/status")
            if not error:
                out["ark_running"] = bool((body or {}).get("running"))
    except Exception:  # noqa: BLE001 - never let this break the status call
        pass
    return out


def _switch_to(target: str) -> None:
    """Stop one side, start the other. Runs on its own thread.

    Order matters and is not negotiable: the outgoing server is fully stopped
    before the incoming one starts. Both briefly resident would mean ~19 GB on
    a 15.9 GB machine, which is the paging-and-corruption territory both games
    warn about.
    """
    cfg = _palworld_cfg()
    ark = cfg.get("swap_with", "island")

    def step(message):
        SWITCH_STATE["steps"].append(
            {"at": datetime.now().strftime("%H:%M:%S"), "message": message})
        plog(f"[switch] {message}")

    def maintenance(server, name, on, reason=""):
        """Tell the agent this server is down on purpose.

        The monitor lives in the AGENT and only knows about stops it ran
        itself; the switch runs here in the panel. Without this flag every
        switch to Palworld fired "island is NOT running, and ArkPanel did not
        stop it" at @here, once a minute, for as long as the kid played.
        """
        if not server:
            return
        _body, error, _status = agent_post(
            server, f"/api/server/{name}/maintenance", {"on": on, "reason": reason})
        if error:
            step(f"note: could not {'set' if on else 'clear'} the "
                 f"down-on-purpose flag for {name} ({error})")

    SWITCH_STATE.update({"running": True, "target": target, "steps": [],
                         "started": time.time(), "finished": None, "error": None})
    try:
        if target == "palworld":
            step(f"Stopping {ark}...")
            server = find(ark)
            # Before the stop, not after: a monitor tick between the two would
            # otherwise be the alert this exists to prevent.
            maintenance(server, ark, True, "switched to Palworld")
            body, error, _status = agent_post(
                server, f"/api/server/{ark}/stop",
                {"warn_minutes": [], "save": True}) if server else (None, "unknown server", 0)
            if error:
                raise RuntimeError(f"could not stop {ark}: {error}")
            job_id = ((body or {}).get("job") or {}).get("id")
            deadline = time.time() + 600
            while job_id and time.time() < deadline:
                time.sleep(5)
                info, _err = agent_get(server, f"/api/server/{ark}/jobs/{job_id}")
                if (info or {}).get("state") != "running":
                    break
            step(f"{ark} stopped")

            step("Starting Palworld...")
            _palworld_wanted(True)
            result = palworld.start(cfg, log=lambda m: step(m))
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "Palworld did not start")
            step("Palworld is up - it takes a minute to appear in the server list")
        else:
            step("Stopping Palworld...")
            _palworld_wanted(False)     # switching to ARK: do not resurrect it
            result = palworld.stop(cfg, log=lambda m: step(m))
            if not result.get("ok"):
                raise RuntimeError("Palworld would not stop")
            step("Palworld stopped")

            step(f"Starting {ark}...")
            server = find(ark)
            body, error, _status = agent_post(server, f"/api/server/{ark}/start",
                                              {"wait_ready": True})
            if error:
                raise RuntimeError(f"could not start {ark}: {error}")
            # Cleared only once the start has been accepted. Clearing first
            # would re-arm the alert against a server that is not up yet.
            maintenance(server, ark, False)
            step(f"{ark} starting - it advertises for join in a couple of minutes")
    except Exception as exc:  # noqa: BLE001
        SWITCH_STATE["error"] = f"{type(exc).__name__}: {exc}"
        plog(f"[switch] FAILED: {SWITCH_STATE['error']}")
        # A half-done switch can leave the machine running NOTHING. That is
        # precisely when the monitor should be shouting, so give it back the
        # ability to: whatever went wrong, this was not a planned outage.
        try:
            maintenance(find(ark), ark, False)
        except Exception:  # noqa: BLE001
            pass
    finally:
        SWITCH_STATE["running"] = False
        SWITCH_STATE["finished"] = time.time()
        # Redraw the Palworld post now rather than leaving it stale until
        # somebody presses Refresh. A switch is the one moment that status is
        # guaranteed to have changed, and it is also the moment people are
        # looking at the channel.
        try:
            channel = CONFIG.get("discord", {}).get("palworld_channel")
            if channel and GUARDIAN is not None and GUARDIAN.ready:
                GUARDIAN.published = _load_published()
                GUARDIAN.publish_palworld(int(channel), _palworld_info_chunks())
                _save_published(GUARDIAN.published)
        except Exception as exc:  # noqa: BLE001
            plog(f"[switch] could not refresh the Discord post: {exc}")


# ------------------------------------------------------------------- games
#
# The game-agnostic API. Everything below knows about "a game" and asks it
# questions; nothing here imports a specific game module. A new game is an
# adapter in core/games/ plus a line in its REGISTRY - if adding one ever
# needs an edit in this file, the Game interface is missing something.
#
# The older /api/palworld/* routes still work. They are what the current UI
# calls, and breaking them to make a point would take a running server down.

def _game(game_id: str):
    """Build one game adapter, wired to this panel's agent callers."""
    return games.build(game_id, CONFIG, SECRETS, log=plog,
                       agent_get=agent_get, agent_post=agent_post)


def _game_server(game_id: str, name: str):
    """The server dict a game adapter needs, for `name`.

    A single-instance game (Palworld) has no entry in CONFIG["servers"] - its
    whole config IS the game block - so an empty dict is correct there rather
    than a 404.
    """
    if game_id == games.DEFAULT_GAME or name:
        found = find(name) if name else None
        if found:
            return found
    return {}


@app.get("/api/games")
def games_list():
    """Every game this panel can manage, and which servers run them."""
    out = []
    for described in games.available(CONFIG, SECRETS, log=plog,
                                     agent_get=agent_get, agent_post=agent_post):
        gid = described["id"]
        if gid == games.DEFAULT_GAME:
            servers = [{"name": s["name"], "display_name": s.get("display_name"),
                        "host": s.get("host")}
                       for s in CONFIG["servers"]
                       if games.game_id_for(s) == gid]
        elif (CONFIG.get(gid) or {}).get("enabled"):
            block = CONFIG.get(gid) or {}
            servers = [{"name": gid,
                        "display_name": block.get("display_name", described["short"]),
                        "host": CONFIG.get("panel", {}).get("host_name", "this PC")}]
        else:
            servers = []
        described["servers"] = servers
        described["configured"] = bool(servers)
        out.append(described)
    return jsonify({"games": out, "default": games.DEFAULT_GAME})


@app.get("/api/games/<game_id>/servers/<name>/status")
def game_status(game_id, name):
    try:
        game = _game(game_id)
    except KeyError:
        return jsonify({"error": "unknown_game"}), 404
    st = game.status(_game_server(game_id, name))
    return jsonify({
        "game": game_id, "server": name,
        "running": st.running, "pid": st.pid,
        "uptime_seconds": st.uptime_seconds, "memory_gb": st.memory_gb,
        "players": st.players, "port": st.port, "installed": st.installed,
        "detail": st.detail, **st.extra,
    })


@app.post("/api/games/<game_id>/servers/<name>/<action>")
def game_control(game_id, name, action):
    """start / stop / restart, for any game."""
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "bad_action"}), 404
    if CONFIG.get("read_only", True):
        return jsonify({"error": "read_only"}), 403
    try:
        game = _game(game_id)
    except KeyError:
        return jsonify({"error": "unknown_game"}), 404

    server = _game_server(game_id, name)
    payload = request.get_json(silent=True) or {}

    def work():
        try:
            getattr(game, action)(server, **payload)
        except Exception as exc:  # noqa: BLE001
            plog(f"[{game_id}] {action} {name} failed: {exc}")

    threading.Thread(target=work, daemon=True,
                     name=f"arkpanel-{game_id}-{action}").start()
    plog(f"[{game_id}] {action} requested for {name}")
    return jsonify({"ok": True, "game": game_id, "action": action}), 202


@app.post("/api/games/<game_id>/servers/<name>/console")
def game_console(game_id, name):
    """Run console commands against a game.

    Exists because some games have no live console: Bedrock gamerules can
    normally only be set by a player typing them in chat. The adapter decides
    how - live where it can, by briefly cycling the server where it cannot.

    Runs here rather than from a script because the panel is elevated, and a
    server owned by Task Scheduler cannot be stopped by anything that is not.
    """
    if CONFIG.get("read_only", True):
        return jsonify({"error": "read_only"}), 403
    try:
        game = _game(game_id)
    except KeyError:
        return jsonify({"error": "unknown_game"}), 404

    payload = request.get_json(silent=True) or {}
    commands = payload.get("commands") or []
    if isinstance(commands, str):
        commands = [commands]
    commands = [str(c).strip() for c in commands if str(c).strip()]
    if not commands:
        return jsonify({"error": "no_commands"}), 400

    server = _game_server(game_id, name)
    try:
        result = game.run_console(server, commands)
    except NotImplementedError as exc:
        return jsonify({"error": "unsupported", "message": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": "failed", "message": str(exc)}), 500
    return jsonify(result), (200 if result.get("ok") else 409)


@app.get("/api/games/<game_id>/servers/<name>/settings")
def game_settings_get(game_id, name):
    try:
        game = _game(game_id)
    except KeyError:
        return jsonify({"error": "unknown_game"}), 404
    if not game.capabilities.settings:
        return jsonify({"error": "unsupported",
                        "message": f"{game.name} settings are not editable here"}), 400
    server = _game_server(game_id, name)
    return jsonify({
        "game": game_id,
        "groups": game.describe_settings(server),
        "presets": game.presets(),
        "needs_stop": game.capabilities.settings_need_stop,
        "running": game.status(server).running,
    })


@app.post("/api/games/<game_id>/servers/<name>/settings")
def game_settings_set(game_id, name):
    """Write settings, stopping first when the game requires it.

    `settings_need_stop` is the generalisation of a Palworld-specific trap:
    it rewrites its config file from memory on shutdown, so a setting saved
    while running is silently undone. Any game that does the same declares it
    and gets the correct stop-write-start order for free.
    """
    if CONFIG.get("read_only", True):
        return jsonify({"error": "read_only"}), 403
    try:
        game = _game(game_id)
    except KeyError:
        return jsonify({"error": "unknown_game"}), 404
    if not game.capabilities.settings:
        return jsonify({"error": "unsupported"}), 400

    payload = request.get_json(silent=True) or {}
    changes = payload.get("changes") or {}
    preset = (payload.get("preset") or "").strip()
    if preset:
        known = game.presets().get(preset)
        if not known:
            return jsonify({"error": "unknown_preset"}), 400
        changes = dict(known["values"])
    if not changes:
        return jsonify({"error": "no_changes"}), 400

    locked = getattr(game, "locked_settings", lambda: {})()
    blocked = sorted(k for k in changes if k in locked)
    if blocked:
        return jsonify({"error": "locked",
                        "message": "; ".join(f"{k} - {locked[k]}" for k in blocked)}), 400

    server = _game_server(game_id, name)

    # Refuse settings the game does not actually have.
    #
    # Writing an unknown key does not fail - it quietly adds a property the
    # server ignores, so the change appears to save and does nothing. That is
    # how a 7 Days preset came to target thirty settings that had been moved
    # out of serverconfig.xml versions earlier, every one of them written
    # successfully and none of them doing anything.
    #
    # Checked against the LIVE config rather than a schema, because the
    # schema is exactly the thing that goes stale.
    try:
        known = set(game.read_settings(server))
    except Exception:  # noqa: BLE001 - a game that cannot be read gets no guard
        known = set()
    if known:
        unknown = sorted(k for k in changes if k not in known)
        if unknown:
            return jsonify({
                "error": "unknown_settings",
                "unknown": unknown,
                "message": (f"{game.name} has no setting called "
                            + ", ".join(unknown[:4])
                            + (f" (and {len(unknown) - 4} more)" if len(unknown) > 4 else "")
                            + ". Writing it would change nothing - this "
                              "version of the game may keep that value "
                              "somewhere else."),
            }), 400

    running = game.status(server).running

    # Two different reasons a running server cannot simply be written to, and
    # one remedy. settings_need_stop: the game would undo the write on
    # shutdown (Palworld). settings_need_restart: the write sticks but the
    # server never re-reads it, so the save appears to do nothing at all
    # (Minecraft Bedrock). Either way the server has to go down and come back,
    # and the panel must not report success until it has.
    need_stop = game.capabilities.settings_need_stop
    need_restart = getattr(game.capabilities, "settings_need_restart", False)

    if not (running and (need_stop or need_restart)):
        try:
            applied = game.write_settings(server, changes)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": "write_failed", "message": str(exc)}), 500
        return jsonify({"ok": True, "cycled": False, "count": len(applied),
                        "applied": {k: {"before": b, "after": a}
                                    for k, (b, a) in applied.items()}})

    why = ("rewrites its config on shutdown" if need_stop
           else "only reads its settings when it starts")

    def cycle():
        try:
            plog(f"[{game_id}] applying {len(changes)} setting(s): stopping "
                 f"first, because {game.name} {why}")
            game.stop(server)
            time.sleep(4)
            applied = game.write_settings(server, changes)
            for key, (before, after) in applied.items():
                plog(f"[{game_id}] setting {key}: {before} -> {after}")
            if not game.status(server).running:
                game.start(server)
            plog(f"[{game_id}] settings applied and restarted ({len(applied)})")
        except Exception as exc:  # noqa: BLE001
            plog(f"[{game_id}] applying settings failed: {exc}")
            # Never leave it down because applying settings failed. A server
            # that is off because a save went wrong is a worse outcome than
            # the old value staying put.
            try:
                if not game.status(server).running:
                    game.start(server)
                    plog(f"[{game_id}] started it again after the failure")
            except Exception as restart_exc:  # noqa: BLE001
                plog(f"[{game_id}] AND could not start it again: {restart_exc}")

    threading.Thread(target=cycle, daemon=True,
                     name=f"arkpanel-{game_id}-settings").start()
    return jsonify({
        "ok": True, "cycled": True, "count": len(changes),
        "message": (f"Applying. {game.name} {why}, so it will stop, take the "
                    "changes and come back."),
    }), 202


@app.get("/api/palworld")
def palworld_status():
    return jsonify({**_palworld_status(), "switch": SWITCH_STATE})


PALWORLD_WANTED = os.path.join(BASE, "data", "palworld_wanted.json")


def _palworld_wanted(value=None) -> bool:
    """Should Palworld be running? Read, or set when `value` is given.

    Needed because "not running" alone cannot tell a crash from a deliberate
    stop, and the two need opposite responses. On 2026-10-12 the server died
    of an access violation at 03:19 and simply stayed dead: the daily restart
    at 05:00 looked, saw nothing running, and correctly did nothing - because
    it had no way to know that was wrong.

    On disk, so a panel restart does not forget.
    """
    if value is not None:
        try:
            os.makedirs(os.path.dirname(PALWORLD_WANTED), exist_ok=True)
            with open(PALWORLD_WANTED, "w", encoding="utf-8") as fh:
                json.dump({"wanted": bool(value),
                           "at": datetime.now().isoformat(timespec="seconds")}, fh)
        except OSError as exc:
            plog(f"[palworld] could not record wanted state: {exc}")
        return bool(value)
    try:
        with open(PALWORLD_WANTED, "r", encoding="utf-8-sig") as fh:
            return bool(json.load(fh).get("wanted"))
    except (OSError, ValueError):
        # Unknown means "do not resurrect". Starting a server nobody asked for
        # is worse than leaving one down that somebody wanted.
        return False


def _start_palworld_watchdog() -> None:
    """Restart Palworld if it dies while it is supposed to be up.

    Only when it is WANTED - a deliberate stop, or a switch to ARK, must not
    be undone. Palworld crashes on its own (EXCEPTION_ACCESS_VIOLATION after
    22 hours, 2026-10-12); the ARK maps have had a watchdog for this for
    months and Palworld never did.
    """
    cfg = CONFIG.get("palworld") or {}
    if not cfg.get("enabled"):
        return
    seconds = int(cfg.get("watchdog_seconds", 120) or 0)
    if seconds <= 0:
        print("[palworld] watchdog disabled")
        return

    def loop():
        time.sleep(90)
        misses = 0
        while True:
            time.sleep(seconds)
            try:
                if SWITCH_STATE.get("running") or not _palworld_wanted():
                    misses = 0
                    continue
                live = _palworld_cfg()
                if palworld.find_process(live.get("install_dir", "")):
                    misses = 0
                    continue
                # Two consecutive misses before acting: one miss can be a
                # process mid-restart, and racing it would start a second.
                misses += 1
                if misses < 2:
                    continue
                misses = 0
                plog("[palworld] watchdog: it is DOWN but should be up - restarting")
                _notify_palworld("Palworld stopped unexpectedly",
                                 "It was not stopped by ArkPanel. Restarting it now.",
                                 "warn")
                result = palworld.start(live, log=plog)
                if result.get("ok"):
                    plog("[palworld] watchdog: restarted")
                    _notify_palworld("Palworld is back up",
                                     "The watchdog restarted it.", "ok")
                else:
                    plog(f"[palworld] watchdog: restart FAILED - {result}")
                    _notify_palworld(
                        "Palworld is down and will not restart",
                        f"{result.get('error') or result}. It needs a look.",
                        "critical")
            except Exception as exc:  # noqa: BLE001 - this thread must not die
                plog(f"[palworld] watchdog error: {exc}")

    threading.Thread(target=loop, daemon=True,
                     name="arkpanel-palworld-watchdog").start()
    print(f"[palworld] watchdog checking every {seconds}s")


def _notify_palworld(title: str, message: str, severity: str = "info") -> None:
    """Say something about Palworld in Discord, if Guardian is connected."""
    try:
        if GUARDIAN is not None and GUARDIAN.ready:
            GUARDIAN.announce(f"🐾 {title}", message, severity)
    except Exception as exc:  # noqa: BLE001
        plog(f"[palworld] could not announce: {exc}")


def _start_palworld_snapshots() -> None:
    """Keep a rolling set of known-good Palworld saves while it is running.

    The game autosaves every 30 seconds already, so this is not about saving
    more often - it is about having COPIES. When the world was lost, the only
    backups were the game's own rotating folder, barely two days deep and
    already past the damage by the time anyone looked.

    Each pass drives an RCON Save and waits for it to land before copying, so
    a snapshot is never a half-written file.
    """
    cfg = CONFIG.get("palworld") or {}
    if not cfg.get("enabled"):
        return
    minutes = float(cfg.get("snapshot_minutes", 20) or 20)
    if minutes <= 0:
        print("[palworld] periodic snapshots disabled")
        return

    def loop():
        time.sleep(120)          # let a restart settle before touching saves
        while True:
            try:
                live = _palworld_cfg()
                if palworld.find_process(live.get("install_dir", "")):
                    kept = palworld.save_and_snapshot(live, log=plog)
                    if kept:
                        plog(f"[palworld] snapshot {os.path.basename(kept)}")
            except Exception as exc:  # noqa: BLE001 - this thread must not die
                plog(f"[palworld] snapshot failed: {exc}")
            time.sleep(minutes * 60)

    threading.Thread(target=loop, daemon=True,
                     name="arkpanel-palworld-snapshots").start()
    print(f"[palworld] snapshotting the save every {minutes:g} min while it runs")


def _start_palworld_restarts() -> None:
    """Restart Palworld once a day, like the ARK maps already are.

    Nothing was restarting it. It ran 396 hours straight and its committed
    memory grew 0.85 GB -> 2.04 GB, while its RESIDENT memory fell to 0.52 GB
    - the leak pushed it into the page file on a machine that was already
    oversubscribed. A server running from disk produces exactly the symptoms
    reported: parachutes drifting upward, wild pals teleporting. Those are
    desync, not a corrupt world.

    Early morning by default, because this is a two-person family server and
    05:00 is the hour nobody is on it.
    """
    cfg = CONFIG.get("palworld") or {}
    if not cfg.get("enabled"):
        return
    target = str(cfg.get("restart_time", "05:00") or "").strip()
    if not target:
        print("[palworld] daily restart disabled")
        return
    try:
        hour, minute = (int(x) for x in target.split(":", 1))
    except ValueError:
        print(f"[palworld] restart_time {target!r} is not HH:MM - not scheduling")
        return

    warn_seconds = int(cfg.get("restart_warn_seconds", 120))
    done_on = {"date": None}

    def loop():
        while True:
            time.sleep(30)
            try:
                now = datetime.now()
                if (now.hour, now.minute) != (hour, minute):
                    continue
                today = now.strftime("%Y-%m-%d")
                if done_on["date"] == today:
                    continue
                done_on["date"] = today

                live = _palworld_cfg()
                if not palworld.find_process(live.get("install_dir", "")):
                    plog("[palworld] daily restart: not running - nothing to do")
                    continue
                if SWITCH_STATE.get("running"):
                    plog("[palworld] daily restart: a switch is in progress - skipping")
                    continue

                # Tell anyone on before pulling it out from under them. The
                # ARK maps do this; there is no reason Palworld should not.
                try:
                    online = palworld.players(live) or []
                except Exception:  # noqa: BLE001
                    online = []
                if online:
                    plog(f"[palworld] daily restart: warning {len(online)} player(s)")
                    for remaining in (warn_seconds, warn_seconds // 2):
                        try:
                            rcon_mod.execute(
                                "127.0.0.1", int(live.get("rcon_port", 25575)),
                                live.get("rcon_password") or "",
                                f"Broadcast Server_restarting_in_{remaining}_seconds")
                        except Exception:  # noqa: BLE001
                            pass
                        time.sleep(max(1, warn_seconds // 2))

                plog("[palworld] daily restart: stopping")
                palworld.stop(live, warn_seconds=10, log=plog)
                time.sleep(5)
                plog("[palworld] daily restart: starting")
                result = palworld.start(live, log=plog)
                plog(f"[palworld] daily restart: {'done' if result.get('ok') else result}")
            except Exception as exc:  # noqa: BLE001 - this thread must not die
                plog(f"[palworld] daily restart failed: {exc}")

    threading.Thread(target=loop, daemon=True,
                     name="arkpanel-palworld-restart").start()
    print(f"[palworld] daily restart scheduled for {hour:02d}:{minute:02d}")


def _start_bedrockconnect() -> None:
    """Make sure the console server-list menu is running.

    BedrockConnect is half the console story - the menu an Xbox sees when it
    connects to what it thinks is a featured server. _start_bedrock_dns is
    the other half, which sends it there. Neither is useful alone, so the
    panel owns both rather than leaving one to be started by hand.

    Started through its scheduled task so Task Scheduler owns the process:
    a Java server launched as a child of the panel would die with it.
    """
    cfg = CONFIG.get("bedrockconnect") or {}
    if not cfg.get("enabled"):
        return

    def already_running() -> bool:
        jar = os.path.basename(cfg.get("jar", "BedrockConnect.jar")).lower()
        try:
            for proc in psutil.process_iter(["name", "cmdline"]):
                if (proc.info["name"] or "").lower() != "java.exe":
                    continue
                if any(jar in str(part).lower()
                       for part in (proc.info["cmdline"] or [])):
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def work():
        time.sleep(8)          # let the DNS claim port 53 first
        if already_running():
            plog("[bedrockconnect] already running")
            return
        task = (cfg.get("task_name") or "").strip()
        if not task:
            plog("[bedrockconnect] no task_name - run "
                 "scripts/install-game-task.ps1 -Game bedrockconnect")
            return
        try:
            result = subprocess.run(["schtasks", "/Run", "/TN", task],
                                    capture_output=True, text=True, timeout=60,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0:
                plog(f"[bedrockconnect] started via '{task}'")
            else:
                plog(f"[bedrockconnect] task '{task}' would not run: "
                     f"{(result.stdout or '') + (result.stderr or '')}"[:160])
        except Exception as exc:  # noqa: BLE001
            plog(f"[bedrockconnect] could not start: {exc}")

    threading.Thread(target=work, daemon=True,
                     name="arkpanel-bedrockconnect").start()


def _start_bedrock_dns() -> None:
    """Point a console's Minecraft lookups at our own BedrockConnect.

    Xbox will only connect to Mojang's featured servers - there is no "add
    server" on console. BedrockConnect answers as one of those servers and
    shows a menu instead; this is the DNS that gets the console to it.

    Everything that is not a Minecraft hostname is forwarded upstream and
    relayed back, so the rest of the console - store, updates, sign-in - is
    untouched. A DNS server that only knew Minecraft names would brick the
    console for everything else.

    LAN-only on purpose. See core/bedrockdns for why.
    """
    cfg = CONFIG.get("bedrockconnect") or {}
    if not cfg.get("enabled"):
        return
    redirect = (cfg.get("redirect_to") or "").strip()
    if not redirect:
        print("[bedrockdns] no redirect_to configured - not starting")
        return
    try:
        bedrockdns.start(redirect, bind_host=cfg.get("dns_bind", "0.0.0.0"),
                         bind_port=int(cfg.get("dns_port", 53)), log=plog)
    except OSError as exc:
        # Something else owns port 53 - a local DNS service, Pi-hole, ICS.
        # Worth saying plainly rather than failing silently, because the
        # symptom otherwise is "the Xbox still cannot see the server".
        plog(f"[bedrockdns] could not start: {exc}. Something else is using "
             f"port {cfg.get('dns_port', 53)} on this machine.")


def _start_minecraft_snapshots() -> None:
    """Keep versioned copies of the Minecraft world. There were none.

    The adapter declared backups=True, offered save_paths(), and config.json
    carried a backup_root and a snapshot_minutes - and nothing called any of
    it. The backup root did not exist on disk. Everything about it read as
    configured, which is why nobody questioned it for weeks.

    It surfaced the day a world a child had spent a long time building was
    imported off an Xbox and became the live world, leaving its only other
    copies on a Realm trial and the console it came from.

    Snapshots are .mcworld files, which is to say directly loadable by the
    game. Restoring does not need the panel or a procedure: open it on a
    phone, a PC or the Xbox and the world is there.

    The first snapshot is taken straight away rather than one interval in.
    An hour of history beginning now beats a schedule that starts later.
    """
    cfg = CONFIG.get("minecraft") or {}
    if not cfg.get("enabled"):
        return
    minutes = float(cfg.get("snapshot_minutes", 20) or 20)
    if minutes <= 0:
        print("[minecraft] world snapshots disabled")
        return
    keep = int(cfg.get("snapshot_keep", 30) or 30)

    def loop():
        time.sleep(90)           # let a panel restart settle first
        first = True
        while True:
            try:
                # Through the adapter, not the game module: the reachability
                # check, the keep count and the .mcworld format are the
                # game's business, and panel.py importing core.minecraft is
                # how the adapter layer rots.
                game = _game("minecraft")
                server = _game_server("minecraft", "")
                if game.status(server).running:
                    if game.snapshot(server,
                                     reason="startup" if first else "auto"):
                        first = False
            except Exception as exc:  # noqa: BLE001 - this thread must not die
                plog(f"[minecraft] snapshot pass failed: {exc}")
            time.sleep(minutes * 60)

    threading.Thread(target=loop, daemon=True,
                     name="arkpanel-minecraft-snapshots").start()
    plog(f"[minecraft] snapshotting the world every {minutes:g} min, "
         f"keeping {keep}")


def _start_minecraft_autoupdate() -> None:
    """Keep Bedrock on the version its clients already auto-updated to.

    Minecraft clients update themselves; the server does not. So a release
    locks everyone out of a server that looks perfectly healthy - green in
    the panel, answering pings, admitting nobody. On 2026-10-29 the first
    warning was a child unable to join her own world, five weeks after the
    release shipped. The RakNet watcher could not catch it: the server's own
    version had not changed, which is exactly the point.

    Guards, in the order they matter:

      * NEVER while players are online. An update is a stop, and a stop with
        someone building is the rudest thing this panel can do.
      * A settle delay before applying, same reasoning as the ARK updater -
        a build that appeared four minutes ago has been tested by nobody.
      * The snapshot must SUCCEED first. No backup, no update.
      * The server is started again on every exit path, including failure.
    """
    cfg = (CONFIG.get("minecraft") or {})
    if not cfg.get("enabled"):
        return
    au = cfg.get("autoupdate") or {}
    if not au.get("enabled", True):
        print("[minecraft] autoupdate disabled")
        return
    poll = float(au.get("poll_minutes", 60) or 60)
    settle = float(au.get("settle_minutes", 60) or 0)
    auto_apply = bool(au.get("auto_apply", True))
    first_seen: dict = {}

    def loop():
        time.sleep(240)          # let a panel restart settle
        while True:
            try:
                live = dict(CONFIG.get("minecraft") or {})
                install = live.get("install_dir", "")
                state = minecraft_update.check(install)

                if state["error"]:
                    plog(f"[minecraft] update check failed: {state['error']}")
                elif state["needs_update"]:
                    latest = state["latest"]
                    first = first_seen.setdefault(latest, time.time())
                    waited = (time.time() - first) / 60.0

                    game = _game("minecraft")
                    server = _game_server("minecraft", "")
                    status = game.status(server)
                    online = status.extra.get("player_count")

                    if not auto_apply:
                        if first_seen.get("told") != latest:
                            first_seen["told"] = latest
                            _updater_notify(
                                "minecraft_update_available", "critical",
                                f"Minecraft Bedrock **{state['installed']}** is "
                                f"behind Mojang's **{latest}**. Clients update "
                                "themselves, so players cannot join until the "
                                "server is updated. Auto-apply is off - run "
                                "scripts/update-minecraft-server.ps1.")
                    elif online:
                        plog(f"[minecraft] {latest} available but "
                             f"{online} player(s) online - waiting")
                    elif waited < settle:
                        plog(f"[minecraft] {latest} available, settling "
                             f"({waited:.0f}/{settle:.0f} min)")
                    else:
                        _apply_minecraft_update(game, server, live, state)
                else:
                    first_seen.clear()
            except Exception as exc:  # noqa: BLE001 - must not kill the thread
                plog(f"[minecraft] autoupdate pass failed: {exc}")
            time.sleep(poll * 60)

    threading.Thread(target=loop, daemon=True,
                     name="arkpanel-minecraft-update").start()
    plog(f"[minecraft] checking for server updates every {poll:g} min "
         f"(auto_apply={auto_apply}, settle={settle:g} min)")


def _apply_minecraft_update(game, server: dict, live: dict, state: dict) -> None:
    """One update, with a backup in front of it and an alert behind it."""
    latest, installed = state["latest"], state["installed"]
    plog(f"[minecraft] updating {installed} -> {latest}")

    snapshot = None
    try:
        snapshot = game.snapshot(server, reason="pre-update")
    except Exception as exc:  # noqa: BLE001
        plog(f"[minecraft] pre-update snapshot failed: {exc}")
    if not snapshot:
        _updater_notify(
            "minecraft_update_failed", "critical",
            f"Refusing to update Minecraft to **{latest}**: the pre-update "
            "snapshot failed, so there would be no way back. The server is "
            "still running on " + installed + ".")
        return

    result = minecraft_update.apply_update(
        live, state["url"], latest, log=plog,
        stop_fn=lambda: game.stop(server),
        start_fn=lambda: game.start(server))

    if not result.get("ok"):
        _updater_notify(
            "minecraft_update_failed", "critical",
            f"Updating Minecraft to **{latest}** FAILED: "
            f"{result.get('error')}\nThe server was started again on "
            f"{installed}. A snapshot was taken first: "
            f"`{os.path.basename(snapshot)}`.")
        return

    ok = minecraft_update.wait_for_version(live.get("install_dir", ""),
                                           latest, log=plog)
    _updater_notify(
        "minecraft_updated", "info" if ok else "critical",
        f"Minecraft Bedrock updated **{installed}** -> **{latest}**.\n"
        + ("The server is running the new version and players can join again."
           if ok else
           "The files were replaced but the server has NOT yet reported the "
           "new version - worth checking logs/minecraft.log.")
        + f"\nConfig preserved: {', '.join(result.get('preserved') or []) or 'none'}."
        + f"\nSnapshot before the update: `{os.path.basename(snapshot)}`.")


MC_TRANSPORT_PATH = os.path.join(BASE, "data", "minecraft_transport.json")


def _mc_transport_state() -> dict:
    try:
        with open(MC_TRANSPORT_PATH, encoding="utf-8-sig") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def _save_mc_transport_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(MC_TRANSPORT_PATH), exist_ok=True)
        tmp = MC_TRANSPORT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, MC_TRANSPORT_PATH)
    except OSError as exc:
        plog(f"[minecraft] could not save transport state: {exc}")


def _start_minecraft_transport_watch() -> None:
    """Notice if Bedrock's deprecated RakNet transport ever stops working.

    We stay on RakNet on purpose: NetherNet was tested on 2026-10-20 and lost
    PC joining by address, BedrockConnect, and the panel's player count while
    adding nothing - the Xbox found the world under Worlds -> LAN either way
    and never appeared in the Friends tab.

    The danger is that Mojang removes RakNet in some update and all of that
    breaks at once. The LAN entry would very likely keep working, so the first
    person to notice would be a friend who cannot get in - probably on an
    evening when a six-year-old wants to play. Checking costs one UDP packet.

    What this cannot do is check the Friends tab, which needs a human with a
    controller. So on a version change it asks for that test by name instead
    of pretending to have run it - see core/minecraft_transport.py.
    """
    cfg = CONFIG.get("minecraft") or {}
    if not cfg.get("enabled"):
        return
    minutes = float(cfg.get("transport_check_minutes", 30) or 30)
    if minutes <= 0:
        print("[minecraft] transport watch disabled")
        return

    def loop():
        time.sleep(180)          # let a panel restart settle first
        while True:
            try:
                state, findings = minecraft_transport.check(
                    dict(CONFIG.get("minecraft") or {}),
                    _mc_transport_state())
                _save_mc_transport_state(state)
                for kind, severity, message in findings:
                    _updater_notify(kind, severity, message)
            except Exception as exc:  # noqa: BLE001 - must not kill the thread
                plog(f"[minecraft] transport check failed: {exc}")
            time.sleep(minutes * 60)

    threading.Thread(target=loop, daemon=True,
                     name="arkpanel-mc-transport").start()
    # plog, not print: the panel runs as a scheduled task, so stdout goes
    # nowhere. A watcher that never started looks exactly like one with
    # nothing to report, and the only way to tell them apart afterwards is a
    # line in a file somebody can read.
    plog(f"[minecraft] watching the RakNet transport every {minutes:g} min")


def _palworld_info_chunks() -> list:
    """The #palworld how-to-join post, split to Discord's message limit."""
    cfg = _palworld_cfg()
    ark = find(cfg.get("swap_with", "island")) or {}
    text = palworld.info_text(cfg, ark.get("display_name") or "The Island")
    return serverinfo.split_for_discord(text)


@app.get("/api/discord/channels")
def discord_channels():
    """Channels Guardian can post in, for the channel pickers."""
    if GUARDIAN is None:
        return jsonify({"ok": False, "error": "guardian not started"}), 503
    out = GUARDIAN.list_channels()
    return jsonify(out), (200 if out.get("ok") else 409)


@app.post("/api/discord/palworld/channel")
def set_palworld_channel():
    """Remember which channel the Palworld post lives in."""
    if CONFIG.get("read_only", True):
        return jsonify({"error": "read_only",
                        "message": "The panel is read-only."}), 403
    payload = request.get_json(silent=True) or {}
    channel = str(payload.get("channel") or "").strip()
    if channel and not channel.isdigit():
        return jsonify({"error": "bad_channel",
                        "message": "A channel id is all digits."}), 400
    CONFIG.setdefault("discord", {})["palworld_channel"] = channel
    _save_config()
    return jsonify({"ok": True, "channel": channel})


@app.post("/api/discord/palworld/publish")
def publish_palworld():
    """Fill the Palworld channel with the info post and the switch buttons."""
    if GUARDIAN is None:
        return jsonify({"error": "guardian not started"}), 503
    if not (CONFIG.get("palworld") or {}).get("enabled"):
        return jsonify({"error": "not_configured"}), 400
    payload = request.get_json(silent=True) or {}
    channel = (payload.get("channel")
               or CONFIG.get("discord", {}).get("palworld_channel"))
    if not channel:
        return jsonify({"error": "no_channel",
                        "message": ("Set discord.palworld_channel in "
                                    "config.json, or pass a channel id.")}), 400

    chunks = _palworld_info_chunks()
    if payload.get("dry_run"):
        return jsonify({"ok": True, "dry_run": True, "channel": str(channel),
                        "messages": len(chunks),
                        "chars": sum(len(c) for c in chunks),
                        "preview": chunks})

    GUARDIAN.published = _load_published()
    out = GUARDIAN.publish_palworld(int(channel), chunks)
    _save_published(GUARDIAN.published)
    return jsonify(out), (200 if out.get("ok") else 409)


@app.get("/api/palworld/settings")
def palworld_settings_get():
    """Every setting in PalWorldSettings.ini, grouped and explained."""
    if not (CONFIG.get("palworld") or {}).get("enabled"):
        return jsonify({"error": "not_configured"}), 400
    cfg = _palworld_cfg()
    if _palworld_remote():
        body, error = _palworld_agent("GET", "/api/palworld/settings")
        if error:
            return jsonify({"error": "unreadable", "message": error}), 502
        settings = (body or {}).get("settings") or {}
        running = bool((body or {}).get("running"))
    else:
        try:
            settings = palworld.read_settings(cfg.get("install_dir", ""))
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": "unreadable", "message": str(exc)}), 500
        running = bool(palworld.find_process(cfg.get("install_dir", "")))
    return jsonify({
        "groups": palworld_schema.describe(settings),
        "presets": {k: {"label": v["label"], "why": v["why"],
                        "values": v["values"]}
                    for k, v in palworld_schema.PRESETS.items()},
        "count": len(settings),
        "running": running,
    })


@app.post("/api/palworld/settings")
def palworld_settings_set():
    """Apply setting changes. Returns exactly what changed.

    write_settings rewrites the single OptionSettings line, keeps every key it
    was not asked to touch, quotes the ones that need quoting, and backs the
    file up first. Hand-editing that line is how people lose 121 settings
    while fixing one.
    """
    if CONFIG.get("read_only", True):
        return jsonify({"error": "read_only"}), 403
    if not (CONFIG.get("palworld") or {}).get("enabled"):
        return jsonify({"error": "not_configured"}), 400

    payload = request.get_json(silent=True) or {}
    changes = payload.get("changes") or {}
    preset = (payload.get("preset") or "").strip()
    if preset:
        known = palworld_schema.PRESETS.get(preset)
        if not known:
            return jsonify({"error": "unknown_preset"}), 400
        changes = dict(known["values"])
    if not isinstance(changes, dict) or not changes:
        return jsonify({"error": "no_changes"}), 400

    blocked = sorted(k for k in changes if k in palworld_schema.LOCKED)
    if blocked:
        return jsonify({
            "error": "locked",
            "message": "These are not editable here: "
                       + "; ".join(f"{k} - {palworld_schema.LOCKED[k]}" for k in blocked),
        }), 400

    # When the server is on another machine, its agent does the whole
    # stop -> write -> start dance locally. Writing from here would edit this
    # machine's stale copy of the ini and change nothing that players see.
    if _palworld_remote():
        body, error = _palworld_agent("POST", "/api/palworld/settings",
                                      {"changes": changes}, timeout=(3, 240))
        if error:
            return jsonify({"error": "write_failed", "message": error}), 502
        return jsonify(body or {"ok": True})

    cfg = _palworld_cfg()
    running = bool(palworld.find_process(cfg.get("install_dir", "")))

    # Palworld REWRITES PalWorldSettings.ini from memory when it shuts down.
    # So a setting written while the server is up survives exactly until the
    # next stop, which then clobbers it - and the server starts again on its
    # own old values. Every change made this way silently undid itself, and
    # from the outside it looked like the panel had not saved anything.
    #
    # The only reliable order is stop -> write -> start, so that is what this
    # does. Proved 2026-10-12: written while running, 0/18 survived; written
    # while stopped, 18/18.
    if not running:
        try:
            applied = palworld.write_settings(cfg.get("install_dir", ""), changes)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": "write_failed", "message": str(exc)}), 500
        for key, (before, after) in applied.items():
            plog(f"[palworld] setting {key}: {before} -> {after}")
        return jsonify({
            "ok": True, "cycled": False,
            "applied": {k: {"before": b, "after": a} for k, (b, a) in applied.items()},
            "count": len(applied), "restart_required": False,
            "message": "Saved. The server is stopped, so these load when it starts.",
        })

    if SWITCH_STATE.get("running"):
        return jsonify({"error": "busy",
                        "message": "a switch is in progress - try again shortly"}), 409

    def cycle():
        try:
            plog(f"[palworld] applying {len(changes)} setting(s): stopping first, "
                 "because Palworld overwrites its settings file on shutdown")
            _palworld_wanted(False)
            palworld.stop(cfg, warn_seconds=15, log=plog)
            time.sleep(4)
            applied = palworld.write_settings(cfg.get("install_dir", ""), changes)
            for key, (before, after) in applied.items():
                plog(f"[palworld] setting {key}: {before} -> {after}")
            result = palworld.start(cfg, log=plog)
            _palworld_wanted(bool(result.get("ok")))
            plog(f"[palworld] settings applied and server restarted "
                 f"({len(applied)} changed)")
        except Exception as exc:  # noqa: BLE001
            plog(f"[palworld] applying settings failed: {exc}")
            _palworld_wanted(True)

    threading.Thread(target=cycle, daemon=True,
                     name="arkpanel-palworld-settings").start()
    return jsonify({
        "ok": True, "cycled": True, "count": len(changes),
        "message": ("Applying. The server has to be stopped to save settings - "
                    "Palworld overwrites its own settings file on shutdown - so "
                    "it will save, stop, take the changes and come back. "
                    "About a minute."),
    }), 202


@app.post("/api/palworld/control/<action>")
def palworld_control(action):
    """Start, stop or restart Palworld on its own.

    The switch was the only control there was, and it always moved BOTH games
    - so there was no way to bring Palworld back after it crashed without
    also stopping the Island, and no way to stop it without starting one.
    Two games sharing a machine still need their own switches.
    """
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "bad_action",
                        "message": "use start, stop or restart"}), 404
    if CONFIG.get("read_only", True):
        return jsonify({"error": "read_only"}), 403
    if not (CONFIG.get("palworld") or {}).get("enabled"):
        return jsonify({"error": "not_configured"}), 400
    if SWITCH_STATE.get("running"):
        return jsonify({"error": "busy",
                        "message": "a switch is already in progress"}), 409

    cfg = _palworld_cfg()
    ark = cfg.get("swap_with", "island")

    # Starting Palworld while the ARK map is up would put both on a machine
    # with memory for one. Refuse and say which button they want instead.
    # The memory guard below only makes sense when Palworld and the ARK map
    # share a machine. Since the 2026-11-09 move they do not, and leaving it in
    # would refuse to start Palworld whenever the Island happens to be up.
    if action in ("start", "restart") and not _palworld_remote():
        server = find(ark)
        if server:
            body, error = agent_get(server, f"/api/server/{ark}/status")
            if not error and (body or {}).get("running"):
                return jsonify({
                    "error": "ark_running",
                    "message": (f"{(body or {}).get('display_name', ark)} is running. "
                                "They cannot both be up - use Switch to Palworld, "
                                "which stops it first."),
                }), 409

    def work():
        try:
            if _palworld_remote():
                # The agent owns the wanted-flag on its own machine, so we do
                # not touch ours -- two flags would disagree.
                if action in ("stop", "restart"):
                    _, error = _palworld_agent("POST", "/api/palworld/stop",
                                               {"warn_seconds": 10}, timeout=(3, 180))
                    if error:
                        plog(f"[palworld] remote stop failed: {error}")
                        return
                if action in ("start", "restart"):
                    _, error = _palworld_agent("POST", "/api/palworld/start",
                                               timeout=(3, 180))
                    if error:
                        plog(f"[palworld] remote start failed: {error}")
                return
            if action in ("stop", "restart"):
                _palworld_wanted(False)        # so the watchdog does not fight us
                palworld.stop(cfg, warn_seconds=10, log=plog)
            if action in ("start", "restart"):
                result = palworld.start(cfg, log=plog)
                _palworld_wanted(bool(result.get("ok")))
                if not result.get("ok"):
                    plog(f"[palworld] {action} failed: {result}")
        except Exception as exc:  # noqa: BLE001
            plog(f"[palworld] {action} failed: {exc}")

    threading.Thread(target=work, daemon=True,
                     name=f"arkpanel-palworld-{action}").start()
    plog(f"[palworld] {action} requested from the panel")
    return jsonify({"ok": True, "action": action}), 202


@app.post("/api/palworld/switch")
def palworld_switch():
    """Swap which game owns this machine's memory."""
    payload = request.get_json(silent=True) or {}
    target = (payload.get("to") or "").lower()
    if target not in ("palworld", "ark"):
        return jsonify({"error": "bad_target",
                        "message": "to must be 'palworld' or 'ark'"}), 400
    if SWITCH_STATE["running"]:
        return jsonify({"error": "busy",
                        "message": f"already switching to {SWITCH_STATE['target']}"}), 409
    if not (CONFIG.get("palworld") or {}).get("enabled"):
        return jsonify({"error": "not_configured"}), 400

    threading.Thread(target=_switch_to, args=(target,), daemon=True,
                     name="arkpanel-switch").start()
    return jsonify({"ok": True, "switching_to": target}), 202


def _agent_token_ok() -> bool:
    expected = (SECRETS.get("agent_token") or "").strip()
    supplied = (request.headers.get("X-ArkPanel-Token") or "").strip()
    return bool(expected) and secrets_mod.compare_digest(expected, supplied)


@app.get("/api/agent-code/manifest")
def agent_code_manifest():
    """What an agent's code should look like, so it can fetch its own updates.

    Authenticated with the AGENT token rather than the panel login: agents
    already hold it, and it is the credential that says "I am part of this
    cluster". Adding the panel password to every agent would be a second secret
    on every machine for no gain.
    """
    if not _agent_token_ok():
        return jsonify({"error": "unauthorised"}), 401
    return jsonify(agentupdate.manifest(BASE))


@app.get("/api/agent-code/file")
def agent_code_file():
    """One file from the manifest. Refuses anything outside it.

    The path arrives over HTTP and decides what gets read, so it is resolved
    through safe_path and then checked against the manifest itself - being
    inside the folder is not enough, it has to be a file agents are meant to
    have.
    """
    if not _agent_token_ok():
        return jsonify({"error": "unauthorised"}), 401

    rel = request.args.get("path") or ""
    absolute = agentupdate.safe_path(BASE, rel)
    wanted = agentupdate.manifest(BASE)["files"]
    if absolute is None or rel.replace("\\", "/") not in wanted:
        plog(f"[agent-code] refused request for {rel!r}")
        return jsonify({"error": "not_in_manifest"}), 404
    if not os.path.isfile(absolute):
        return jsonify({"error": "missing"}), 404
    with open(absolute, "rb") as fh:
        return Response(fh.read(), mimetype="application/octet-stream")


@app.get("/desktop/open")
def desktop_open():
    """Sign in the desktop app without showing it a login form.

    The desktop window runs ON this machine, so it can read secrets.json --
    anything able to do that already has the password and could log in anyway.
    What this avoids is making someone type it into a window that has no
    address bar and no password manager.

    Two gates, both required:
      * the request must come from loopback. The panel also listens on the LAN
        for the phone, and a key that worked from another machine would be a
        password that never expires.
      * it must carry the desktop key from secrets.json.
    """
    creds = _auth_configured()
    if creds is None:
        return redirect(url_for("index"))

    if request.remote_addr not in ("127.0.0.1", "::1"):
        plog(f"[desktop] refused /desktop/open from {request.remote_addr}")
        return redirect(url_for("login"))

    expected = (SECRETS.get("desktop_key") or "").strip()
    supplied = (request.args.get("key") or "").strip()
    if not expected or not secrets_mod.compare_digest(expected, supplied):
        plog("[desktop] refused /desktop/open - wrong or missing key")
        return redirect(url_for("login"))

    session["user"] = creds[0]
    session.permanent = True
    return redirect("/")


@app.route("/login", methods=["GET", "POST"])
def login():
    creds = _auth_configured()
    if creds is None:
        return redirect(url_for("index"))

    target = request.args.get("next") or "/"
    # Only ever bounce back to somewhere on this site.
    if not target.startswith("/") or target.startswith("//"):
        target = "/"

    if request.method == "POST":
        user = (request.form.get("user") or "").strip()
        password = request.form.get("password") or ""
        if user == creds[0] and password == creds[1]:
            session["user"] = user
            # "Stay signed in" matters on a phone -- otherwise the session dies
            # with the browser and you retype a password every time.
            session.permanent = bool(request.form.get("remember"))
            return redirect(request.form.get("next") or "/")
        return render_template("login.html", error="Wrong username or password.",
                               next=target, cluster=CONFIG["cluster"]), 401

    if _logged_in():
        return redirect(target)
    return render_template("login.html", error=None, next=target,
                           cluster=CONFIG["cluster"])


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    return render_template("index.html", cluster=CONFIG["cluster"])


@app.get("/sw.js")
def service_worker():
    """Serve the service worker from the ROOT, not /static/.

    A worker's default scope is the directory it is served from, so one at
    /static/sw.js could only control /static/* -- it would never see a
    navigation to "/", and Chrome would keep offering a plain shortcut instead
    of a real install. Serving it here gives it scope over the whole site.

    no-store because a stale worker is genuinely nasty: it would keep serving
    an old cached shell after a deploy, with no obvious way to clear it.

    The cache VERSION is stamped in HERE rather than hardcoded in the file.
    sw.js caches /static/* cache-first and purges only caches whose key does
    not start with VERSION -- so with a constant version it never purged
    anything, and a UI fix could sit on the server while phones and browsers
    kept running the JavaScript they cached weeks ago. Deriving it from the
    static files' own fingerprint means a deploy changes the worker, which
    makes the browser install it, which drops the previous cache.
    """
    path = os.path.join(BASE, "static", "sw.js")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        source = source.replace("'arkpanel-v1'", f"'arkpanel-{_static_fingerprint()}'")
        response = Response(source, mimetype="application/javascript")
    except OSError:
        response = send_file(path, mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def _static_fingerprint() -> str:
    """Short hash over the cached static assets - content, not timestamps.

    Timestamps differ on every copy of the tree, which would churn the cache
    version for no reason and re-download the UI after every deploy even when
    nothing about it changed.
    """
    import hashlib
    digest = hashlib.sha256()
    static_dir = os.path.join(BASE, "static")
    for name in sorted(os.listdir(static_dir)) if os.path.isdir(static_dir) else []:
        if not name.endswith((".js", ".css", ".webmanifest")):
            continue
        try:
            with open(os.path.join(static_dir, name), "rb") as fh:
                digest.update(name.encode("utf-8"))
                digest.update(fh.read())
        except OSError:
            continue
    return digest.hexdigest()[:10]


@app.get("/api/config")
def config():
    """Everything the UI needs that isn't live state."""
    return jsonify({
        "cluster": CONFIG["cluster"],
        "read_only": CONFIG.get("read_only", True),
        "schedule": CONFIG.get("schedule", {}),
        "backup": CONFIG.get("backup", {}),
        "updates": CONFIG.get("updates", {}),
        "servers": [
            {
                "name": s["name"],
                "display_name": s["display_name"],
                "host": s.get("host"),
                "map_name": s.get("map_name"),
                "local": bool(s.get("local")),
                "install_dir": s.get("install_dir"),
                "expected_game_port": s.get("expected_game_port"),
                "rcon_port": s.get("rcon_port"),
                "port_range": [s.get("port_range_min"), s.get("port_range_max")],
            }
            for s in CONFIG["servers"]
        ],
    })


@app.get("/api/cluster/status")
def cluster_status():
    """One row per server, with unreachable agents degraded rather than fatal."""
    out = []
    system = None
    for server in CONFIG["servers"]:
        payload, error = agent_get(server, "/api/status")
        if error:
            planned = _planned_for(server)
            out.append({
                "name": server["name"],
                "display_name": server["display_name"],
                "host": server.get("host"),
                "map_name": server.get("map_name"),
                # Planned is not unknown. "Unknown" means we cannot tell, and
                # that is a question; this is an answer.
                "state": "planned" if planned else "unknown",
                "planned": planned,
                "agent_error": None if planned else error,
                "running": None,
            })
            continue
        if server.get("local") and system is None:
            system = payload.get("system")
        match = next(
            (s for s in payload.get("servers", []) if s["name"] == server["name"]),
            None,
        )
        if match is None:
            out.append({
                "name": server["name"],
                "display_name": server["display_name"],
                "host": server.get("host"),
                "state": "unknown",
                "agent_error": "agent does not manage this server",
                "running": None,
            })
        else:
            # "offline" and "down on purpose" are different facts and must not
            # share a colour. A server stopped so Palworld can have the memory
            # is working as intended; one that is merely offline is a question.
            if match.get("running"):
                match["state"] = "online"
            elif match.get("maintenance"):
                match["state"] = "maintenance"
            else:
                match["state"] = "offline"
            out.append(match)
    return jsonify({
        "servers": out,
        "system": system,
        "read_only": CONFIG.get("read_only", True),
        "reachable": sum(1 for s in out if s.get("state") != "unknown"),
        "total": len(out),
    })


def _proxy(name: str, path: str, **params):
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    payload, error = agent_get(server, path, **params)
    if error:
        return jsonify({"error": "agent_unreachable", "message": error}), 502
    return jsonify(payload)


@app.get("/api/server/<name>/mods")
def mods(name):
    return _proxy(name, f"/api/server/{name}/mods")


@app.get("/api/cluster/transfer-backups")
def cluster_transfer_backups():
    """The cluster transfer-folder backup, from whichever agent holds the folder.

    Only one PC watches it, so ask each agent and report the one that does. If
    none does, say so plainly: a missing transfer backup must not look like an
    empty one.
    """
    reasons = []
    for server in CONFIG["servers"]:
        data, error = agent_get(server, "/api/cluster/transfer-backups")
        if error:
            reasons.append(f"{server['name']}: {error}")
            continue
        if data.get("here"):
            data["host"] = server.get("host")
            return jsonify(data)
    return jsonify({"running": False, "here": False,
                    "reason": "no agent reports watching the cluster folder",
                    "errors": reasons})


ROSTER_PATH = os.path.join(BASE, "data", "roster.json")


def _roster() -> dict:
    """account id -> {name, last_seen}. Built from RCON player lists, because the
    cluster folder only ever names people by account id."""
    try:
        with open(ROSTER_PATH, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _remember_players(server_name: str, players: list) -> None:
    roster = _roster()
    changed = False
    for player in players or []:
        entry = roster.get(player["id"]) or {}
        if entry.get("name") != player["name"] or entry.get("last_seen_on") != server_name:
            changed = True
        roster[player["id"]] = {"name": player["name"], "last_seen_on": server_name,
                                "last_seen": datetime.now().isoformat(timespec="seconds")}
    if not changed:
        return
    try:
        os.makedirs(os.path.dirname(ROSTER_PATH), exist_ok=True)
        tmp = ROSTER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(roster, fh, indent=2)
        os.replace(tmp, ROSTER_PATH)
    except OSError:
        pass          # a missing roster only costs nicer names


def _cluster_backup_agent():
    """(server dict, status) for the PC that actually holds the cluster folder."""
    for server in CONFIG["servers"]:
        data, error = agent_get(server, "/api/cluster/characters")
        if not error and (data or {}).get("here"):
            return server, data
    return None, None


@app.get("/api/cluster/characters")
def cluster_characters():
    """Characters that can be put back into the transfer folder, with who they are."""
    server, data = _cluster_backup_agent()
    if not server:
        return jsonify({"ok": False, "characters": [],
                        "message": "no agent reports holding the cluster folder"}), 200
    roster = _roster()
    online = {}
    for srv in CONFIG["servers"]:
        for player in (_guardian_players(srv["name"]) or []):
            online[player["id"]] = srv.get("display_name", srv["name"])
    for character in data.get("characters", []):
        known = roster.get(character["id"], {})
        character["name"] = known.get("name")
        character["last_seen_on"] = known.get("last_seen_on")
        character["online_on"] = online.get(character["id"])
    return jsonify({"ok": True, "host": server.get("host"), **data})


@app.post("/api/cluster/characters/<account_id>/restore")
def cluster_character_restore(account_id):
    """Put one character's saved transfer file back so they can download it elsewhere.

    Refused while that player is connected anywhere: ARK would then hold a live
    copy on a map AND a copy in the transfer folder, which is how duplicates and
    lost characters happen. Resend with force to override.
    """
    payload = request.get_json(silent=True) or {}
    server, _data = _cluster_backup_agent()
    if not server:
        return jsonify({"error": "no_cluster_agent",
                        "message": "no agent reports holding the cluster folder"}), 409

    if not payload.get("force"):
        for srv in CONFIG["servers"]:
            for player in (_guardian_players(srv["name"]) or []):
                if player["id"].lower() == account_id.lower():
                    return jsonify({
                        "error": "player_online",
                        "message": (f"{player['name']} is connected to "
                                    f"{srv.get('display_name', srv['name'])} right now. "
                                    "Have them fully quit the game first, or resend with force."),
                    }), 409

    body, error, status = agent_post(
        server, f"/api/cluster/characters/{account_id}/restore",
        {"version": payload.get("version")})
    if error:
        return jsonify(body or {"error": "agent_unreachable", "message": error}), status
    if body.get("ok"):
        name = _roster().get(account_id, {}).get("name", account_id[:8])
        plog(f"[cluster] restored character {name} from {body.get('restored')}")
    return jsonify(body), (200 if body.get("ok") else 400)


@app.get("/api/server/<name>/perf")
def server_perf(name):
    """Recorded CPU/RAM for one server, idle and active reported separately."""
    params = {k: v for k, v in request.args.items() if k in ("hours", "samples")}
    return _proxy(name, f"/api/server/{name}/perf", **params)


@app.get("/api/server/<name>/launch")
def launch(name):
    return _proxy(name, f"/api/server/{name}/launch")


@app.post("/api/server/<name>/mods")
def write_mods(name):
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    body, error, status = agent_post(
        server, f"/api/server/{name}/mods", request.get_json(silent=True) or {})
    if error:
        return jsonify(body or {"error": "agent_unreachable", "message": error}), status

    # Only on a real change. A reorder rewrites the launch line but does not
    # alter what #mods-we-use says, so it is not worth a Discord edit.
    added = (body or {}).get("added") or []
    removed = (body or {}).get("removed") or []
    if added or removed:
        request_info_refresh(f"{name}: +{len(added)} -{len(removed)} mods")
    return jsonify(body)


@app.post("/api/server/<name>/launch")
def adopt_launch(name):
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    body, error, status = agent_post(
        server, f"/api/server/{name}/launch", request.get_json(silent=True) or {})
    if error:
        return jsonify(body or {"error": "agent_unreachable", "message": error}), status
    return jsonify(body)


CONTROL_ACTIONS = {"start", "stop", "restart", "kill", "save", "rcon",
                   "backup", "mirror", "restore"}


@app.get("/api/server/<name>/backups")
def server_backups(name):
    return _proxy(name, f"/api/server/{name}/backups")


@app.get("/api/server/<name>/version")
def version_info(name):
    return _proxy(name, f"/api/server/{name}/version")


@app.get("/api/cluster/updates")
def cluster_updates():
    if UPDATER is None:
        return jsonify({"running": False, "error": "updater not started"})
    return jsonify(UPDATER.status())


@app.post("/api/cluster/updates/check")
def cluster_updates_check():
    if UPDATER is None:
        return jsonify({"error": "updater not started"}), 503
    return jsonify(UPDATER.check())


@app.post("/api/cluster/update")
def cluster_update():
    """Update every server that needs it, together.

    dry_run returns the plan without touching anything; force updates even
    servers already reporting the current build.
    """
    if UPDATER is None:
        return jsonify({"error": "updater not started"}), 503
    payload = request.get_json(silent=True) or {}
    result = UPDATER.apply(force=bool(payload.get("force")),
                           dry_run=bool(payload.get("dry_run")))
    return jsonify(result), (200 if result.get("ok") else 409)


# "Cannot reach the agent" alerts are made up by the panel on every request, so
# acknowledging one is remembered here: until that agent answers again. The next
# outage then shows up fresh (user wants every alert dismissable, 2026-11-11).
UNREACHABLE_ACKED: set[str] = set()


@app.get("/api/cluster/alerts")
def cluster_alerts():
    """Alerts from every agent, newest first, with unreachable agents surfaced
    as an alert in their own right rather than silently omitted."""
    merged = []
    per_agent = {}
    for server in CONFIG["servers"]:
        data, error = agent_get(server, "/api/alerts", limit=50)
        if error:
            per_agent[server["name"]] = {"error": error}
            acked = server["name"] in UNREACHABLE_ACKED
            merged.append({
                "server": server["name"], "kind": "agent_unreachable",
                "severity": "critical", "active": not acked, "acknowledged": acked,
                "condition_active": True, "at": None,
                "message": (f"Cannot reach the agent on {server.get('host')}: {error}. "
                            "This server is unmonitored until it is back."),
            })
            continue
        UNREACHABLE_ACKED.discard(server["name"])
        per_agent[server["name"]] = {
            "active_alerts": data.get("active_alerts"),
            "worst_severity": data.get("worst_severity"),
            "running": data.get("running"),
        }
        merged.extend(data.get("recent", []))

    merged.sort(key=lambda a: a.get("at") or 0, reverse=True)
    active = [a for a in merged if a.get("active")]
    worst = None
    for level in ("critical", "warn", "info"):
        if any(a.get("severity") == level for a in active):
            worst = level
            break
    return jsonify({"agents": per_agent, "alerts": merged[:100],
                    "active": len(active), "worst_severity": worst})


@app.get("/api/server/<name>/health-check")
def health_check(name):
    return _proxy(name, "/api/health-check")


@app.post("/api/server/<name>/alerts/<action>")
def alerts_action(name, action):
    if action not in ("acknowledge", "test"):
        return jsonify({"error": "unknown action"}), 404
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    req = request.get_json(silent=True) or {}
    if action == "acknowledge" and req.get("kind") == "agent_unreachable":
        UNREACHABLE_ACKED.add(name)                      # the panel's own alert
        return jsonify({"ok": True, "acknowledged": 1})
    body, error, status = agent_post(server, f"/api/alerts/{action}", req)
    if error:
        if action == "acknowledge" and not req.get("at"):
            UNREACHABLE_ACKED.add(name)                  # "acknowledge all" covers this one too
            return jsonify({"ok": True, "acknowledged": 1, "note": "agent unreachable"})
        return jsonify(body or {"error": "agent_unreachable", "message": error}), status
    return jsonify(body)


@app.get("/api/cluster/schedule")
def cluster_schedule():
    out = {}
    for server in CONFIG["servers"]:
        data, error = agent_get(server, "/api/schedule")
        out[server["name"]] = data if not error else {"error": error}
    return jsonify({"agents": out, "panel_config": {
        "backup": CONFIG.get("backup", {}), "schedule": CONFIG.get("schedule", {})}})


@app.post("/api/cluster/schedule")
def cluster_schedule_write():
    """Apply one restart schedule to every agent.

    Cluster-wide on purpose: two maps restarting at different times means
    transfers land on a map that is down, and someone has to remember to change
    both. Each agent writes its own config.json, so a partial failure is
    reported per agent rather than pretended away.
    """
    payload = request.get_json(silent=True) or {}

    def fan_out(body_to_send):
        out, bad = {}, []
        for server in CONFIG["servers"]:
            body, error, status = agent_post(server, "/api/schedule", body_to_send)
            if error or (body or {}).get("error"):
                bad.append(server["name"])
                detail = error or (body or {}).get("error")
                if error and "malformed JSON" in str(error):
                    detail = ("this agent does not have the schedule endpoint - it is "
                              "running older code and needs restarting to pick it up")
                out[server["name"]] = {"error": detail,
                                       "messages": (body or {}).get("messages"),
                                       "message": (body or {}).get("message")}
            else:
                out[server["name"]] = body
        return out, bad

    # Two passes. Everyone validates first, and nobody writes unless everyone
    # can -- a restart time that applied to one map and not the other is the
    # split this whole design exists to avoid.
    checked, rejected = fan_out({**payload, "validate_only": True})
    if rejected:
        return jsonify({"ok": False, "phase": "validate", "failed": rejected,
                        "results": checked}), 409

    results, failed = fan_out({k: v for k, v in payload.items() if k != "validate_only"})
    return jsonify({"ok": not failed, "phase": "apply",
                    "failed": failed, "results": results}), \
        (200 if not failed else 409)


@app.get("/api/maps")
def map_catalog():
    """Known ASA maps, which server runs which, and whether we have an image.

    The catalog is data, not code, so adding a map is editing one JSON file.
    """
    path = os.path.join(BASE, "data", "maps", "catalog.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            catalog = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify({"error": "catalog_unreadable", "message": str(exc),
                        "maps": []}), 500

    image_dir = os.path.join(BASE, "data", "maps", "images")
    running = {}
    for server in CONFIG["servers"]:
        running.setdefault(server.get("map_name", ""), []).append({
            "name": server["name"], "display_name": server.get("display_name"),
        })

    for entry in catalog.get("maps", []):
        entry["servers"] = running.get(entry.get("map_name", ""), [])
        # What this map WOULD be called and where it would save, so the UI can
        # show the consequence of a switch before anyone commits to it.
        entry["session_name"] = maps.session_name(entry)
        entry["save_folder"] = maps.save_folder(entry)
        # Normalised here so the UI can test one field rather than repeat the
        # "missing means yes" rule.
        entry["available"] = maps.is_available(entry)
        if not entry["available"]:
            entry["unavailable_reason"] = maps.unavailable_reason(entry)
        entry["image"] = None
        for ext in ("jpg", "jpeg", "png", "webp"):
            candidate = os.path.join(image_dir, f"{entry['key']}.{ext}")
            if os.path.exists(candidate):
                entry["image"] = f"/api/maps/{entry['key']}/image"
                break

    return jsonify({"maps": catalog.get("maps", []),
                    "image_dir": image_dir})


@app.post("/api/server/<name>/switch-map")
def switch_map(name):
    """Point one server at a different map from the catalogue.

    The Island slot rotates through the story maps and comes back, so this is a
    routine action rather than a rebuild. It is cheap because the cluster keeps
    ONE install per PC: the rates, the mods, the ports and the password are the
    same on every map, and ARK already files each map's world in its own
    SavedArks folder. So nothing is copied, nothing is deleted, and the map you
    just left is waiting exactly as you left it.

    The server must already be stopped -- the agent refuses otherwise, since
    the start script is only read at launch. The UI stops, switches and starts
    as one button; this endpoint is the middle step so the long-running halves
    stay on the existing job plumbing instead of blocking an HTTP request for
    the several minutes a first boot on a new map can take.
    """
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404

    payload = request.get_json(silent=True) or {}
    key = (payload.get("map") or "").strip()

    # Two servers on the same map in one cluster would advertise the same
    # session name and share a save folder over the cluster share. Refused
    # here as well as in the UI, because the UI is not the only caller.
    entry = maps.find(BASE, key)
    if entry:
        clash = [s for s in CONFIG["servers"]
                 if s["name"] != name
                 and (s.get("map_name") or "").lower() == entry["map_name"].lower()]
        if clash:
            other = clash[0].get("display_name") or clash[0]["name"]
            return jsonify({
                "error": "map_in_use",
                "message": (f"{other} is already running {entry['name']}. Two servers on "
                            "one map would share a save folder and advertise the same "
                            "name."),
            }), 409

    # Refuse while anyone is still in the world. They would be dropped into a
    # different map's server on reconnect and find their base gone -- it is
    # still there, on the other map, but that is not the first thought anyone has.
    if not payload.get("force"):
        who, error = agent_get(server, f"/api/server/{name}/players")
        count = (who or {}).get("count")
        if not error and count:
            names = ", ".join((who or {}).get("names") or []) or f"{count} player(s)"
            return jsonify({
                "error": "players_online",
                "message": (f"{names} still online. Switching maps now would drop them "
                            "onto a different world. Warn and stop the server first, "
                            "or resend with force."),
                "count": count,
            }), 409

    body, error, status = agent_post(server, f"/api/server/{name}/map",
                                     {"map": key, "force": bool(payload.get("force"))})
    if error:
        return jsonify(body or {"error": "agent_unreachable", "message": error}), status
    if not (body or {}).get("ok"):
        return jsonify(body), status

    # The agent persisted the change in ITS config.json. For the local agent
    # that is the same file the panel reads, so only this process's memory is
    # behind; for a remote agent the panel keeps a separate copy and must write
    # its own. Mutating in place rather than reloading, because the scheduler
    # and monitor threads hold references to these same dicts.
    after = body.get("after") or {}
    server["map_name"] = after.get("map_name", server.get("map_name"))
    server["display_name"] = after.get("display_name", server.get("display_name"))
    server["save_dir"] = after.get("save_dir", server.get("save_dir"))
    server["map_key"] = (body.get("map") or {}).get("key", "")
    if not server.get("local"):
        try:
            _save_config()
        except OSError as exc:
            body.setdefault("warnings", []).append(f"panel config not saved: {exc}")

    # Repoint Discord at the new map. Best effort: the switch itself already
    # succeeded, and a Guardian that happens to be reconnecting must not turn a
    # good map change into a failure.
    if payload.get("publish", True):
        try:
            body["discord"] = _publish_all()
        except Exception as exc:                       # noqa: BLE001
            body["discord"] = {"ok": False, "error": str(exc)}

    return jsonify(body), 200


def _save_config() -> None:
    """Write the panel's own config.json, keeping the previous copy."""
    backup_dir = os.path.join(BASE, "backups", "config")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        shutil.copy2(CONFIG_PATH, os.path.join(backup_dir, f"config_panel_{stamp}.json"))
    except OSError:
        pass
    tmp = CONFIG_PATH + ".panel.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(CONFIG, fh, indent=2)
    os.replace(tmp, CONFIG_PATH)


@app.get("/api/cluster/players")
def cluster_players():
    """Who is online, per map.

    Separate from /api/cluster/status because it costs an RCON round trip per
    map: the status poll runs every 15s and should stay cheap, while this is
    called on a slower cadence.
    """
    out, total, unknown = {}, 0, False
    for server in CONFIG["servers"]:
        data, error = agent_get(server, f"/api/server/{server['name']}/players")
        if error:
            out[server["name"]] = {"error": error}
            unknown = True
            continue
        out[server["name"]] = data
        if data.get("count") is None:
            unknown = True
        else:
            total += data["count"]
    return jsonify({"servers": out, "total": total, "total_known": not unknown})


@app.post("/api/cluster/deploy-agents")
def deploy_agents():
    """Stage this panel's agent code to every remote PC, then restart them.

    Exists because every agent-side change needed a manual trip to PC2 --
    a deploy script here, then a scheduled-task restart at that machine. That
    was the last routine chore in the workflow, and forgetting half of it left
    an agent silently running older code.

    Local agents are skipped: restarting the agent on the panel's own machine
    is a click away and does not need this.
    """
    want = codestamp.fingerprint(BASE)
    steps, failed = [], []

    def step(text, level="info"):
        steps.append({"level": level, "message": text})

    step(f"Panel agent code is {want}")

    # 1. Ask each agent to PULL, rather than pushing files at it.
    #
    #    This used to run deploy_agent.ps1, which copied over an SMB share.
    #    That is the mechanism self-update was written to replace: it needs a
    #    route into every machine, and when one has no share it reports
    #    success having delivered nothing - PC3 was "deployed to" three times
    #    in an evening without receiving a file. The agents now fetch their
    #    own code over the same HTTP they already use, hash-verified, and
    #    restart themselves. This button just says "now" instead of waiting
    #    for the next poll.
    #
    #    Deduplicated by agent URL: PC3 serves three maps, and asking it three
    #    times would mean three restarts to accomplish one.
    nudged: dict[str, dict] = {}
    for server in CONFIG["servers"]:
        url = server.get("agent_url", "")
        if url and url not in nudged and not server.get("local"):
            nudged[url] = server

    for url, server in nudged.items():
        host = server.get("host") or url
        before, _err = agent_get(server, "/api/health")
        body, err, _status = agent_post(server, "/api/selfupdate/check", {})
        if err:
            step(f"{host}: could not ask it to update ({err})", "error")
            continue
        result = (body or {}).get("result") or (body or {}).get("error") or "no reply"
        if not (body or {}).get("ok"):
            # Not fatal on its own - the restart pass below still runs, and an
            # agent whose files are already correct only needs restarting.
            step(f"{host}: update reported a problem - {result}", "warn")
            continue
        step(f"{host}: {result}")
        # An agent that updated restarts ITSELF, three seconds later. Wait for
        # it here, or the restart pass below would catch it mid-restart and
        # report a perfectly healthy machine as unreachable.
        if "restarting" in result:
            _await_agent(server, want, step, host,
                         before_started=(before or {}).get("started"))

    # 2. Restart each remote agent and wait for it to come back on the new code.
    for server in CONFIG["servers"]:
        name = server["name"]

        # The LOCAL agent is updated too. It was skipped originally on the
        # reasoning that restarting it on the panel's own machine is a click
        # away -- but that left the panel reporting "OLD CODE" for a server it
        # then refused to update, which is a worse experience than the manual
        # step it was avoiding. Its files are already in place (this IS that
        # machine), so it only needs the restart.
        before, error = agent_get(server, "/api/health")
        if error:
            step(f"{name}: agent unreachable ({error}) - not restarted", "error")
            failed.append(name)
            continue
        # Restart for new code OR for config the agent has not loaded. Checking
        # code alone missed config-only changes entirely -- adding a server is
        # nothing but config -- and left the agent silently behind.
        config_stale = bool(before.get("config_stale"))
        if before.get("code") == want and not config_stale:
            step(f"{name}: already on {want} - no restart needed")
            continue
        if before.get("code") == want and config_stale:
            step(f"{name}: code is current but its config.json changed since it "
                 "started - restarting so it loads the new config")
        if not before.get("restartable"):
            where = "this machine" if server.get("local") else server.get("host")
            step(f"{name}: staged, but {where} has no restart task yet. "
                 "Run scripts/install-restart-task.ps1 once, elevated, there "
                 "- then this button works unattended.", "warn")
            failed.append(name)
            continue

        body, err, _status = agent_post(server, "/api/admin/restart", {})
        if err or (body or {}).get("error"):
            step(f"{name}: {(body or {}).get('message') or err}", "error")
            failed.append(name)
            continue
        step(f"{name}: restart triggered"
             + (" (local agent)" if server.get("local") else "")
             + ", waiting for it to come back...")

        if _await_agent(server, want, step, name, before_started=before.get("started")):
            continue
        failed.append(name)

    return jsonify({"ok": not failed, "failed": failed,
                    "code": want, "steps": steps}), (200 if not failed else 409)


def _await_agent(server: dict, want: str, step, name: str,
                 timeout: int = 120, before_started: float | None = None) -> bool:
    """Poll one agent until it answers again, and check WHICH code it is on.

    Coming back is not the same as coming back updated -- a restart that
    silently relaunched the old files is exactly the failure this is meant to
    make impossible.
    """
    deadline = time.time() + timeout
    seen_down = False
    while time.time() < deadline:
        time.sleep(3)
        health, error = agent_get(server, "/api/health")
        if error:
            seen_down = True
            continue
        code = health.get("code")
        # A config-only restart leaves the code fingerprint unchanged, so
        # "code == want" was already true before the restart even happened. Only
        # count it as done once the agent's start time has actually moved.
        restarted = (before_started is None
                     or (health.get("started") or 0) > before_started)
        if code == want and restarted:
            step(f"{name}: back up on {code}"
                 + ("" if seen_down else " (it answered without ever dropping)"))
            return True
        if seen_down and code != want:
            step(f"{name}: came back on {code}, but the panel is on {want}. "
                 "The restart did not pick up the new files.", "error")
            return False
        # Otherwise the code already matches and the restart just hasn't landed
        # yet -- keep waiting rather than reporting a mismatch that isn't one.
    step(f"{name}: did not come back within {timeout}s. Check it at "
         f"{server.get('host')}.", "error")
    return False


@app.get("/api/background")
def page_background():
    """Serve whichever background image is present in static/bg.

    One URL for any format, so the stylesheet does not have to probe three
    filenames and log two 404s on every page load. Drop in a file named
    background.<ext> and reload; no code or config changes.
    """
    bg_dir = os.path.join(BASE, "static", "bg")
    for ext in ("jpg", "jpeg", "png", "webp", "avif"):
        candidate = os.path.join(bg_dir, f"background.{ext}")
        if os.path.exists(candidate):
            resp = send_file(candidate)
            # It changes rarely and is large; let the browser keep it.
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp
    # No image is a normal state, not an error: the theme's procedural
    # backdrop stands on its own.
    return Response(b"", status=204)


@app.get("/api/maps/<key>/image")
def map_image(key):
    """Serve a map picture the user dropped into data/maps/images.

    Deliberately local files rather than a CDN: the panel has to work when the
    internet does not, and these are Wildcard's artwork, not ours to rehost.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key or ""):
        return jsonify({"error": "bad key"}), 400
    image_dir = os.path.join(BASE, "data", "maps", "images")
    for ext in ("jpg", "jpeg", "png", "webp"):
        candidate = os.path.join(image_dir, f"{key}.{ext}")
        if os.path.exists(candidate):
            return send_file(candidate)
    return jsonify({"error": "no image for this map"}), 404


@app.post("/api/server/<name>/<action>")
def control(name, action):
    """Forward a control action to the agent that owns this server."""
    if action not in CONTROL_ACTIONS:
        return jsonify({"error": f"unknown action '{action}'"}), 404
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    body, error, status = agent_post(
        server, f"/api/server/{name}/{action}", request.get_json(silent=True) or {})
    if error:
        return jsonify(body or {"error": "agent_unreachable", "message": error}), status
    return jsonify(body), status


@app.get("/api/server/<name>/jobs")
def server_jobs(name):
    return _proxy(name, "/api/jobs")


@app.get("/api/server/<name>/jobs/<job_id>")
def job_detail(name, job_id):
    return _proxy(name, f"/api/jobs/{job_id}")


@app.post("/api/server/<name>/jobs/<job_id>/cancel")
def job_cancel(name, job_id):
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    body, error, status = agent_post(server, f"/api/jobs/{job_id}/cancel", {})
    if error:
        return jsonify(body or {"error": "agent_unreachable", "message": error}), status
    return jsonify(body)


@app.post("/api/cluster/mods")
def cluster_mods_apply():
    """Add and/or remove mods across BOTH maps in one action.

    This exists because doing it by hand on two PCs is exactly what let the
    start scripts drift out of sync in the first place.

    If any agent is unreachable the whole thing is refused rather than applied
    to one map only -- a half-applied mod change is how a cluster ends up with
    two maps that disagree about what is loaded.
    """
    payload = request.get_json(silent=True) or {}
    add = [int(m) for m in payload.get("add", [])]
    remove = [int(m) for m in payload.get("remove", [])]
    if not add and not remove:
        return jsonify({"error": "nothing to add or remove"}), 400

    # Pre-flight: every agent must answer before anything is written.
    current: dict[str, list[int]] = {}
    for server in CONFIG["servers"]:
        data, error = agent_get(server, f"/api/server/{server['name']}/mods")
        if error:
            return jsonify({
                "error": "agent_unreachable",
                "message": (f"{server['display_name']}: {error}. No changes were made to "
                            "either server Ã¢â‚¬â€ a cluster-wide mod change is applied to both "
                            "maps or neither."),
            }), 502
        current[server["name"]] = [
            m["mod_id"] for m in data.get("mods", []) if m.get("in_load_order")
        ]

    plan = {}
    for server in CONFIG["servers"]:
        name = server["name"]
        mods = [m for m in current[name] if m not in remove]
        for mod_id in add:
            if mod_id not in mods:
                mods.append(mod_id)
        plan[name] = mods

    if payload.get("dry_run"):
        return jsonify({"dry_run": True, "before": current, "after": plan})

    results, failures = {}, []
    for server in CONFIG["servers"]:
        name = server["name"]
        if plan[name] == current[name]:
            results[name] = {"skipped": "no change for this map"}
            continue
        body, error, status = agent_post(server, f"/api/server/{name}/mods", {
            "mods": plan[name],
            "confirm_removals": payload.get("confirm_removals", False),
        })
        if error:
            failures.append(f"{server['display_name']}: {error}")
            results[name] = body or {"error": error}
        else:
            results[name] = body

    # One publish for the whole cluster change, not one per map -- the debounce
    # would collapse them anyway, but asking once is clearer.
    if any(not r.get("skipped") and not r.get("error") for r in results.values()):
        request_info_refresh(f"cluster mods: +{len(add)} -{len(remove)}")

    return jsonify({
        "ok": not failures,
        "results": results,
        "failures": failures,
        "applies": "on next server restart",
    }), (207 if failures else 200)


def agent_post(server: dict, path: str, payload: dict) -> tuple[dict | None, str | None, int]:
    """POST to one agent. Returns (payload, error, status)."""
    url = server["agent_url"].rstrip("/") + path
    try:
        resp = requests.post(url, headers=_headers(), json=payload, timeout=(1.5, 30))
    except requests.exceptions.ConnectionError:
        return None, f"cannot reach agent at {server['agent_url']}", 502
    except requests.RequestException as exc:
        return None, f"agent error: {exc}", 502
    try:
        body = resp.json()
    except ValueError:
        return None, "agent returned malformed JSON", 502
    if resp.status_code >= 400:
        return body, body.get("message") or body.get("error") or f"HTTP {resp.status_code}", resp.status_code
    return body, None, resp.status_code


@app.get("/api/server/<name>/settings/<path:filename>")
def settings(name, filename):
    return _proxy(name, f"/api/server/{name}/settings/{filename}")


@app.post("/api/server/<name>/settings/<path:filename>")
def write_settings(name, filename):
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    payload = request.get_json(silent=True) or {}
    body, error, status = agent_post(
        server, f"/api/server/{name}/settings/{filename}", payload)
    if error:
        return jsonify(body or {"error": "agent_unreachable", "message": error}), status
    return jsonify(body)


@app.post("/api/server/<name>/open-notepad")
def open_notepad(name):
    """Open a config file in Notepad on the machine running the panel.

    Deliberately handled here rather than on the agent: opening Notepad on
    ChaoticControl would be useless when you are sitting at this PC. Both
    servers' config folders are reachable from here -- Svartalfheim's via the
    Z: share -- so the panel opens the path it can see.
    """
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    filename = (request.get_json(silent=True) or {}).get("file", "")
    if filename not in ("Game.ini", "GameUserSettings.ini", "Engine.ini"):
        return jsonify({"error": "unsupported file"}), 400

    path = os.path.join(server["config_dir"], filename)
    if not os.path.exists(path):
        return jsonify({"error": f"{filename} not reachable at {path}"}), 404
    try:
        subprocess.Popen(["notepad.exe", path])
    except OSError as exc:
        return jsonify({"error": "launch_failed", "message": str(exc)}), 500
    return jsonify({"ok": True, "path": path})


@app.get("/api/server/<name>/settings/<path:filename>/raw")
def settings_raw(name, filename):
    return _proxy(name, f"/api/server/{name}/settings/{filename}/raw")


def _key_mod_ids():
    """Curated shortlist for the info posts. Missing file = no shortlist, which
    shows up as an empty {{key_mods}} rather than silently listing everything."""
    path = os.path.join(BASE, "data", "serverinfo", "key_mods.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
            return data.get("key_mod_ids", []), data.get("display_names", {})
    except (OSError, json.JSONDecodeError):
        return [], {}


def _info_template_path(name: str) -> str:
    return os.path.join(BASE, "data", "serverinfo", f"{name}.md")


def _build_server_info(name: str) -> tuple[dict | None, str | None]:
    """Render one map's Discord info post from live config. (payload, error)."""
    server = find(name)
    if not server:
        return None, "unknown server"

    template_path = _info_template_path(name)
    try:
        with open(template_path, "r", encoding="utf-8-sig") as fh:
            template = fh.read()
    except OSError as exc:
        return None, f"no template at {template_path} ({exc})"

    # Read the inis through the AGENT rather than off the share: the panel runs
    # as a scheduled task with no mapped drives, so Z: does not exist for it.
    texts = {}
    for filename in ("GameUserSettings.ini", "Game.ini"):
        data, error = agent_get(server, f"/api/server/{name}/settings/{filename}/raw")
        if error:
            return None, f"could not read {filename}: {error}"
        texts[filename] = (data or {}).get("text", "")

    mods, error = agent_get(server, f"/api/server/{name}/mods")
    if error:
        return None, f"could not read mods: {error}"

    version, _ = agent_get(server, f"/api/server/{name}/version")

    cfg = dict(server)
    cfg["cluster_id"] = CONFIG.get("cluster", {}).get("id", "")
    key_ids, display_names = _key_mod_ids()

    def build(ids):
        vals = serverinfo.facts(
            cfg, texts["GameUserSettings.ini"], texts["Game.ini"],
            (mods or {}).get("mods", []), CONFIG.get("schedule", {}),
            build=(version or {}).get("installed"),
            key_ids=ids, display_names=display_names)
        rendered, missing = serverinfo.render(template, vals)
        return vals, rendered, missing

    values, text, unknown = build(key_ids)

    # Make it FIT rather than making someone re-tune it by hand.
    #
    # A one-message post is the goal, but mod names change on CurseForge and a
    # single renamed mod can push it over. So if it overflows, drop key mods
    # from the END of key_mods.json until it fits -- that file's order is the
    # priority order, which is the knob to turn if the wrong one goes.
    dropped = 0
    trimmed = list(key_ids)
    while len(text) > serverinfo.DISCORD_LIMIT and len(trimmed) > 3:
        trimmed = trimmed[:-1]
        dropped += 1
        values, text, unknown = build(trimmed)

    chunks = serverinfo.split_for_discord(text)
    return {
        "server": name,
        "text": text,
        "length": len(text),
        # Discord hard-limits a message to 2000 characters, and a real mod list
        # pushes the post past it, so the bot owns a sequence of messages.
        "chunks": chunks,
        "chunk_lengths": [len(c) for c in chunks],
        "key_mods_dropped_to_fit": dropped,
        "unknown_placeholders": sorted(set(unknown)),
        "conflicts": serverinfo.conflicts(texts["GameUserSettings.ini"],
                                          texts["Game.ini"]),
        "values": values,
        "template": template_path,
    }, None


PUBLISHED_PATH = os.path.join(BASE, "data", "serverinfo", "published.json")


def _load_published() -> dict:
    """Which messages the bot owns in each channel.

    Persisted so a panel restart keeps EDITING the same posts instead of
    posting a fresh set every time -- which is exactly how an info channel
    turns into a wall of duplicates.
    """
    try:
        with open(PUBLISHED_PATH, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_published(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(PUBLISHED_PATH), exist_ok=True)
        tmp = PUBLISHED_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, PUBLISHED_PATH)
    except OSError as exc:
        print(f"[serverinfo] could not save published ids: {exc}")


def _publish_all(dry_run: bool = False) -> dict:
    """Push all three channels: both info posts and the mod catalogue."""
    channels = CONFIG.get("discord", {}).get("info_channels", {})
    results, errors = {}, []

    # Several maps may share ONE channel (#server-info, 2026-11-12). Guardian
    # tracks its posts per channel, so each channel is synced once with every
    # map's chunks in config order -- syncing them one by one would make each
    # map overwrite the last. If any map in a shared channel fails to build,
    # that channel is skipped entirely: syncing without it would delete its
    # post and shuffle the rest, and the next refresh retries anyway.
    grouped, broken = {}, set()
    for server in CONFIG["servers"]:
        name = server["name"]
        channel = channels.get(name)
        if not channel:
            continue
        payload, error = _build_server_info(name)
        if error:
            errors.append(f"{name}: {error}")
            broken.add(str(channel))
            continue
        names, chunks = grouped.setdefault(str(channel), ([], []))
        names.append(name)
        chunks.extend(payload["chunks"])
    targets = [("+".join(names), channel, chunks)
               for channel, (names, chunks) in grouped.items()
               if channel not in broken]

    catalogue_channel = channels.get("mods")
    if catalogue_channel:
        with app.test_request_context():
            data = mods_catalogue().get_json()
        if data.get("chunks"):
            targets.append(("mods", catalogue_channel, data["chunks"]))
        else:
            errors.append("mods: catalogue produced nothing")

    if dry_run:
        return {"ok": not errors, "dry_run": True, "errors": errors,
                "would_publish": {name: {"channel": channel,
                                         "messages": len(chunks),
                                         "chars": sum(len(c) for c in chunks)}
                                  for name, channel, chunks in targets}}

    if GUARDIAN is None or not GUARDIAN.ready:
        return {"ok": False, "error": "guardian_offline",
                "message": "Guardian is not connected, so nothing was posted.",
                "errors": errors}

    GUARDIAN.published = _load_published()
    for name, channel, chunks in targets:
        outcome = GUARDIAN.sync_channel(int(channel), chunks)
        results[name] = outcome
        if not outcome.get("ok"):
            errors.append(f"{name}: {outcome.get('error')}")
    _save_published(GUARDIAN.published)

    return {"ok": not errors, "results": results, "errors": errors}


@app.post("/api/cluster/info/publish")
def publish_info():
    payload = request.get_json(silent=True) or {}
    out = _publish_all(dry_run=bool(payload.get("dry_run")))
    return jsonify(out), (200 if out.get("ok") else 409)


@app.get("/api/cluster/mods/catalogue")
def mods_catalogue():
    """Every mod in the cluster, grouped and linked, for #mods-we-use.

    Split into several messages: 26 mods with links and summaries is well past
    Discord's per-message limit, and the bot keeps editing the same sequence
    rather than reposting the whole thing each time.
    """
    mods_by_server, labels, errors = {}, {}, []
    for server in CONFIG["servers"]:
        name = server["name"]
        labels[name] = server.get("display_name", name)
        data, error = agent_get(server, f"/api/server/{name}/mods")
        if error:
            errors.append(f"{name}: {error}")
            continue
        mods_by_server[name] = (data or {}).get("mods", [])

    if errors and not mods_by_server:
        return jsonify({"error": "unreachable", "messages": errors}), 502

    groups = serverinfo.catalogue(mods_by_server)
    body = serverinfo.render_catalogue(groups, labels)
    total = sum(len(m) for _, m in groups)

    header = (f"🧩 **MODS WE USE** — all {total} across the cluster\n"
              "Every mod name links straight to its CurseForge page. "
              "Kept up to date automatically by ArkPanel.\n")
    chunks = serverinfo.split_for_discord(header + body)

    return jsonify({
        "groups": [{"category": c, "count": len(m)} for c, m in groups],
        "total": total,
        "text": header + body,
        "chunks": chunks,
        "chunk_lengths": [len(c) for c in chunks],
        "errors": errors,
    })


@app.get("/api/server/<name>/info/preview")
def server_info_preview(name):
    """Render the info post WITHOUT publishing it.

    Publishing to a channel players read is not something to do sight-unseen,
    so rendering and posting are deliberately separate endpoints.
    """
    payload, error = _build_server_info(name)
    if error:
        return jsonify({"error": "preview_failed", "message": error}), 400
    return jsonify(payload)


@app.get("/api/server/<name>/logs")
def logs(name):
    return _proxy(name, f"/api/server/{name}/logs", lines=request.args.get("lines", 200))


@app.get("/api/cluster/mods")
def cluster_mods():
    """Both servers' mod tables plus the difference between them.

    The two maps intentionally run different sets, so this reports the
    difference without implying either side is wrong.
    """
    tables: dict[str, list] = {}
    errors: dict[str, str] = {}
    for server in CONFIG["servers"]:
        payload, error = agent_get(server, f"/api/server/{server['name']}/mods")
        if error:
            errors[server["name"]] = error
        else:
            tables[server["name"]] = payload.get("mods", [])

    names = list(tables)
    diff = None
    if len(names) == 2:
        a, b = names
        ids_a = {m["mod_id"] for m in tables[a] if m["in_load_order"]}
        ids_b = {m["mod_id"] for m in tables[b] if m["in_load_order"]}
        by_id = {m["mod_id"]: m["name"] for t in tables.values() for m in t}
        diff = {
            "a": a, "b": b,
            "shared": sorted(ids_a & ids_b),
            "only_a": [{"mod_id": i, "name": by_id.get(i, str(i))} for i in sorted(ids_a - ids_b)],
            "only_b": [{"mod_id": i, "name": by_id.get(i, str(i))} for i in sorted(ids_b - ids_a)],
            "shared_count": len(ids_a & ids_b),
        }
    return jsonify({"tables": tables, "errors": errors, "diff": diff})


@app.post("/api/server/<name>/verify-code")
def verify_agent_code(name):
    """Ask an agent to re-exec and pick up newly deployed code."""
    server = find(name)
    if not server:
        return jsonify({"error": "unknown server"}), 404
    body, error, status = agent_post(server, "/api/admin/verify-code", {})
    if error:
        return jsonify(body or {"error": "agent_unreachable", "message": error}), status
    return jsonify(body)


@app.post("/api/cluster/sync-external")
def sync_external():
    """Pull remote agents' local snapshots onto the external drive.

    PC2 has no route to I: -- it is a local disk on PC1 -- and sharing it would
    mean a new SMB share and permission set. PC1 already reaches PC2 through
    the existing Z: mapping, so the copy is done from this side instead. That
    keeps the offsite copy without adding any new network exposure.
    """
    import shutil
    import subprocess

    backup_cfg = CONFIG.get("backup", {})
    external_root = backup_cfg.get("external_root")
    if not external_root:
        return jsonify({"error": "no external_root configured"}), 400
    if not os.path.isdir(os.path.dirname(external_root.rstrip("\\/")) or external_root):
        return jsonify({"error": "external_unreachable",
                        "message": f"{external_root} is not reachable"}), 503

    keep = int(backup_cfg.get("external_keep", 84))
    results = {}

    for server in CONFIG["servers"]:
        name = server["name"]
        if server.get("local"):
            results[name] = {"skipped": "local agent writes to the external directly"}
            continue

        # The remote agent's backup folder, as this machine sees it.
        remote_backups = server.get("remote_backups_path")
        if not remote_backups or not os.path.isdir(remote_backups):
            results[name] = {"error": f"remote backups not reachable: {remote_backups}"}
            continue

        src = os.path.join(remote_backups, name)
        dst = os.path.join(external_root, name)
        if not os.path.isdir(src):
            results[name] = {"error": f"no snapshots at {src}"}
            continue

        args = ["robocopy", src, dst, "/E", "/R:1", "/W:1", "/MT:8",
                "/NFL", "/NDL", "/NJH", "/NJS", "/NP"]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=7200)
            ok = proc.returncode < 8
        except (OSError, subprocess.TimeoutExpired) as exc:
            results[name] = {"error": f"copy failed: {exc}"}
            continue

        copied = sorted(os.listdir(dst)) if os.path.isdir(dst) else []
        # Prune the external copy to the retention count.
        pruned = []
        for stale in sorted(copied, reverse=True)[keep:]:
            try:
                shutil.rmtree(os.path.join(dst, stale))
                pruned.append(stale)
            except OSError:
                pass

        results[name] = {"ok": ok, "robocopy_exit": proc.returncode,
                         "snapshots_on_external": len(copied) - len(pruned),
                         "pruned": pruned, "path": dst}

    return jsonify({"ok": all(r.get("ok", True) for r in results.values()),
                    "results": results})


@app.get("/api/downtime")
def downtime_get():
    """Machines currently marked as off on purpose."""
    hosts = sorted({(s.get("host") or "") for s in CONFIG["servers"] if s.get("host")})
    return jsonify({"planned": planned_downtime(), "hosts": hosts})


@app.post("/api/downtime")
def downtime_set():
    """Mark a machine as off on purpose, or bring it back into service.

    Per machine, because that is the unit that gets switched off: HUBMASTER
    going down for building work takes three maps with it, and saying so once
    beats three separate flags that cannot be set anyway once the PC is off.
    """
    if CONFIG.get("read_only", True):
        return jsonify({"error": "read_only"}), 403
    payload = request.get_json(silent=True) or {}
    host = str(payload.get("host") or "").strip()
    if not host:
        return jsonify({"error": "no_host", "message": "which machine?"}), 400
    known = {(s.get("host") or "").lower() for s in CONFIG["servers"]}
    if host.lower() not in known:
        return jsonify({"error": "unknown_host",
                        "message": f"no server in this cluster runs on {host}"}), 400

    on = payload.get("on", True)
    state = set_planned_downtime(host, str(payload.get("reason") or ""),
                                 str(payload.get("until") or ""), bool(on))
    plog(f"[planned] {host}: " + ("ON - " + str(payload.get('reason') or '')
                                  if on else "back in service"))
    return jsonify({"ok": True, "planned": state})


@app.get("/api/agents")
def agents():
    want = codestamp.fingerprint(BASE)
    out = []
    for server in CONFIG["servers"]:
        payload, error = agent_get(server, "/api/health")
        code = (payload or {}).get("code")
        out.append({
            "server": server["name"],
            "host": server.get("host"),
            "url": server["agent_url"],
            "reachable": error is None,
            "error": error,
            "elevated": (payload or {}).get("elevated"),
            "read_only": (payload or {}).get("read_only"),
            "local": bool(server.get("local")),
            "code": code,
            "started": (payload or {}).get("started"),
            # An agent that predates the fingerprint reports nothing at all,
            # which is itself proof it is behind.
            "planned": _planned_for(server),
            "stale": error is None and code != want,
            "restartable": (payload or {}).get("restartable", False),
            # Present only when a self-update applied its files and then
            # failed to restart onto them. That is the one case where "stale"
            # will not clear on its own, so the row needs to say so.
            "restart_failed": (payload or {}).get("restart_failed"),
        })
    return jsonify({"agents": out, "panel_code": want,
                    "stale": [a["server"] for a in out if a["stale"]]})


UPDATER: Updater | None = None


def _version_of(server: dict):
    data, error = agent_get(server, f"/api/server/{server['name']}/version")
    return None if error else data


def _players_of(server: dict):
    """Player count, or None when genuinely unknown.

    None is not 0. An update must not skip its warning countdown because a
    query failed.
    """
    data, error = agent_get(server, f"/api/server/{server['name']}/players")
    if error or not data:
        return None
    if data.get("running") is False:
        return 0
    return data.get("count") if data.get("known", True) else None


def _agent_reachable(server: dict) -> bool:
    _, error = agent_get(server, "/api/health")
    return error is None


def _start_update(server: dict, warn_minutes, target_build=None):
    # target_build lets the agent verify the patch actually landed. Without it,
    # steamcmd exiting 0 on a validate-only pass reads as a successful update
    # and the server starts on the old build with nobody the wiser.
    body, error, _status = agent_post(
        server, f"/api/server/{server['name']}/update",
        {"warn_minutes": warn_minutes, "target_build": target_build})
    if error:
        raise RuntimeError(error)
    return body


TITLES = {
    "update_available": "Update available",
    "update_starting": "Cluster update starting",
    "update_held": "Update held",
    "update_finished": "Cluster update finished",
    "build_mismatch": "Build mismatch",
    "minecraft_updated": "Minecraft updated - NetherNet re-test due",
    "minecraft_raknet_dead": "Minecraft: RakNet has stopped answering",
    "minecraft_raknet_ok_again": "Minecraft: RakNet warning gone",
    "minecraft_nethernet_pingable": "Minecraft: NetherNet now answers pings",
    "minecraft_update_available": "Minecraft server is behind",
    "minecraft_updated": "Minecraft server updated",
    "minecraft_update_failed": "Minecraft update FAILED",
}


def _updater_notify(kind: str, severity: str, message: str) -> None:
    """Announce updater events.

    Goes to Guardian's channel first. Auto-apply is on, so an update can happen
    with nobody watching -- it must say so somewhere visible, not just in a log
    file on one machine. The webhook is used too when configured.
    """
    title = TITLES.get(kind, kind.replace("_", " ").title())

    if GUARDIAN is not None and GUARDIAN.announce(title, message, severity):
        pass  # delivered to the channel
    else:
        print(f"[updater] {severity}: {message}")

    url = (SECRETS.get("discord_webhook") or "").strip()
    if url:
        from core import notify as notify_mod
        from core.monitor import Alert
        notify_mod.send_discord(
            url, Alert(server="cluster", kind=kind, severity=severity,
                       message=message),
            CONFIG.get("cluster", {}).get("id", ""))


GUARDIAN: GuardianBot | None = None

# Guardian may only do these. Deliberately no mod or settings editing: a
# mistyped value from a phone should not be able to rewrite a live config.
GUARDIAN_ACTIONS = {"start", "stop", "restart", "save", "backup"}


def _guardian_action(action: str, server_name: str, opts: dict) -> dict:
    if action not in GUARDIAN_ACTIONS:
        return {"error": "not_allowed", "message": f"'{action}' is not permitted"}
    server = find(server_name)
    if not server:
        return {"error": "unknown_server", "message": f"no server called {server_name}"}
    body, error, _status = agent_post(
        server, f"/api/server/{server_name}/{action}", opts or {})
    if error:
        return {"error": "failed", "message": error}
    return body or {}


def _guardian_players(server_name: str) -> list | None:
    """[{name, id}] logged in to one server over RCON, or None if it could not ask.

    Guardian gets this and _guardian_kick instead of raw RCON: a button that can
    type arbitrary commands into a live server is far more than clearing a stuck
    login needs.
    """
    server = find(server_name)
    if not server:
        return None
    body, error, _status = agent_post(server, f"/api/server/{server_name}/rcon",
                                      {"command": "ListPlayers"})
    if error or not (body or {}).get("ok"):
        return None
    players = parse_player_ids((body or {}).get("response", ""))
    # The cluster folder names people only by account id, so remember who is who
    # whenever a server tells us. Without this the restore list is a wall of hex.
    _remember_players(server_name, players)
    return players


def _guardian_recent(server_name: str, hours: float = 24.0) -> list:
    """Who this server has seen lately, from the roster.

    Needed because the worst stuck login is the one RCON does not report. On
    2026-09-18 a player's Xbox held a session on Genesis while ListPlayers,
    the cluster status and Discord all said nobody was on - so the Clear login
    button had an empty list and there was nothing to do but wait it out.
    """
    cutoff = datetime.now() - timedelta(hours=hours)
    out = []
    for account_id, entry in (_roster() or {}).items():
        if entry.get("last_seen_on") != server_name:
            continue
        try:
            seen = datetime.fromisoformat(entry.get("last_seen", ""))
        except ValueError:
            continue
        if seen >= cutoff:
            out.append({"id": account_id, "name": entry.get("name") or account_id[:8],
                        "last_seen": entry.get("last_seen")})
    return sorted(out, key=lambda p: p["last_seen"], reverse=True)


def _guardian_kick(server_name: str, account_id: str, force: bool = False) -> dict:
    """Kick one account from a server.

    Normally the id must be in the live player list, so a stale button press
    cannot kick someone by an id that was never on the map.

    force=True skips that check, and exists because the check was wrong for the
    case the button is FOR: a crashed console can leave a session the server
    honours on join but does not report to ListPlayers. Refusing to kick an id
    RCON does not list meant refusing exactly when it mattered. KickPlayer on
    someone genuinely absent is harmless - the server ignores it.
    """
    account_id = (account_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{16,40}", account_id):
        return {"error": "bad_id", "message": "not a valid account id"}
    players = _guardian_players(server_name)
    if players is None and not force:
        return {"error": "rcon_failed", "message": "could not ask the server who is logged in"}
    match = next((p for p in (players or []) if p["id"] == account_id), None)
    if match is None and not force:
        return {"error": "not_logged_in",
                "message": "that player is no longer logged in - nothing to clear"}
    if match is None:
        known = (_roster() or {}).get(account_id) or {}
        match = {"name": known.get("name") or account_id[:8], "id": account_id}
    server = find(server_name)
    body, error, _status = agent_post(server, f"/api/server/{server_name}/rcon",
                                      {"command": f"KickPlayer {account_id}"})
    if error or not (body or {}).get("ok"):
        return {"error": "kick_failed",
                "message": error or (body or {}).get("message") or "kick failed"}
    plog(f"[discord] cleared login of {match['name']} on {server_name}")
    return {"ok": True, "name": match["name"], "response": (body or {}).get("response", "")}


def _guardian_characters() -> list:
    """Characters with a saved transfer copy, for Guardian's rescue button."""
    with app.test_request_context():
        data = cluster_characters().get_json()
    return data.get("characters", []) if isinstance(data, dict) else []


def _guardian_rescue(account_id: str, version: str | None = None) -> dict:
    """Restore one character through the same route (and guards) the panel uses."""
    with app.test_request_context(json={"version": version}):
        response = cluster_character_restore(account_id)
    body, status = response if isinstance(response, tuple) else (response, 200)
    out = body.get_json() if hasattr(body, "get_json") else body
    if status != 200 and isinstance(out, dict):
        out.setdefault("ok", False)
    return out or {"ok": False, "error": "no response"}


def _guardian_job(server_name: str, job_id: str) -> dict:
    server = find(server_name)
    if not server:
        return {}
    data, _error = agent_get(server, f"/api/jobs/{job_id}")
    return data or {}


def _guardian_update(dry_run: bool = True) -> dict:
    if UPDATER is None:
        return {"error": "updater not started"}
    return UPDATER.check() if dry_run else UPDATER.apply()


BOUNCER = None


def _bouncer_config() -> dict:
    path = os.path.join(BASE, "data", "bouncer", "roles.json")
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def _start_bouncer() -> None:
    """Onboarding bot: rules, the agree button, and the role pickers."""
    global BOUNCER
    token = (SECRETS.get("discord_bouncer_token") or "").strip()
    if not token:
        print("[bouncer] no discord_bouncer_token - not started")
        return
    try:
        config = _bouncer_config()
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[bouncer] cannot read roles.json ({exc}) - not started")
        return

    BOUNCER = BouncerBot(
        name="Dracon Cluster Bouncer", token=token,
        channel_id=config.get("channel_id"), config=config,
        registry_path=os.path.join(BASE, "data", "bouncer", "survivors.json"))
    BOUNCER.start()


@app.get("/api/bouncer/inventory")
def bouncer_inventory():
    """Roles and channels as the bot sees them, including whether each role is
    actually assignable. A bot cannot grant a role above its own, and that is
    the usual reason an onboarding button appears to do nothing."""
    if BOUNCER is None:
        return jsonify({"error": "bouncer not started"}), 503
    return jsonify(BOUNCER.inventory())


@app.post("/api/bouncer/backfill")
def bouncer_backfill():
    """Give the survivors already here their numbers, and post their welcomes."""
    if BOUNCER is None or not BOUNCER.ready:
        return jsonify({"error": "bouncer_offline"}), 409
    BOUNCER.config = _bouncer_config()
    out = BOUNCER.backfill()
    return jsonify(out), (200 if out.get("ok") else 409)


@app.post("/api/bouncer/publish")
def bouncer_publish():
    if BOUNCER is None or not BOUNCER.ready:
        return jsonify({"error": "bouncer_offline",
                        "message": "The bouncer bot is not connected."}), 409
    try:
        config = _bouncer_config()
        BOUNCER.config = config
        rules = open(os.path.join(BASE, "data", "bouncer", "rules.md"),
                     encoding="utf-8-sig").read()
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify({"error": "config", "message": str(exc)}), 400

    picker = ("**🎭 PICK YOUR ROLES**\n"
              "Choose from the menus below - you can change them any time. "
              "Selecting nothing in a menu removes that role.")

    BOUNCER.published = _load_published()
    out = BOUNCER.publish(int(config["channel_id"]), rules, picker)
    if out.get("ok"):
        _save_published(BOUNCER.published)
    return jsonify(out), (200 if out.get("ok") else 409)


_REFRESH_LOCK = threading.Lock()
_REFRESH_TIMER: threading.Timer | None = None
# Long enough to collect a burst of edits into one publish, short enough that
# nobody wonders whether it worked. Adding three mods one after another should
# produce one Discord update, not three.
REFRESH_DEBOUNCE_SECONDS = 25


def request_info_refresh(reason: str, delay: float = REFRESH_DEBOUNCE_SECONDS) -> None:
    """Push the Discord info channels soon because something actually changed.

    The nightly timer stays as a backstop, but waiting up to 24 hours for
    #mods-we-use to notice a mod that was added minutes ago is the kind of
    staleness that made these channels wrong in the first place.

    Debounced and off-thread on purpose: a mod save must not block on Discord,
    and a cluster-wide change fires this once per map.
    """
    global _REFRESH_TIMER

    def run():
        try:
            out = _publish_all()
            changed = sum((r.get("edited", 0) + r.get("posted", 0))
                          for r in (out.get("results") or {}).values())
            if out.get("ok"):
                plog(f"[serverinfo] refresh after {reason} - "
                     + (f"{changed} message(s) changed" if changed else "nothing changed"))
            else:
                plog(f"[serverinfo] refresh after {reason} failed: "
                     f"{out.get('errors') or out.get('message') or out.get('error')}")
        except Exception as exc:  # noqa: BLE001 - never take the request down
            plog(f"[serverinfo] refresh after {reason} crashed: {exc}")

    with _REFRESH_LOCK:
        if _REFRESH_TIMER is not None:
            _REFRESH_TIMER.cancel()
        _REFRESH_TIMER = threading.Timer(delay, run)
        _REFRESH_TIMER.daemon = True
        _REFRESH_TIMER.start()
    plog(f"[serverinfo] Discord refresh queued in {int(delay)}s ({reason})")


def _start_info_refresher() -> None:
    """Re-publish the Discord info channels on a slow timer.

    Daily, because these change when the user changes something -- not on their
    own -- and a channel that reposts hourly is noise. The publish is a no-op
    when nothing changed: sync_channel compares content and skips the edit, so
    a quiet day leaves no "(edited)" marker anywhere.
    """
    hours = float(CONFIG.get("discord", {}).get("info_refresh_hours", 24) or 0)
    if hours <= 0:
        print("[serverinfo] refresh disabled (info_refresh_hours = 0)")
        return
    if not CONFIG.get("discord", {}).get("info_channels"):
        print("[serverinfo] no info_channels configured - refresher not started")
        return

    def loop():
        # Let Guardian finish connecting before the first pass, or it would
        # simply report "not connected" and skip a day.
        time.sleep(120)
        while True:
            try:
                out = _publish_all()
                if out.get("ok"):
                    changed = sum((r.get("edited", 0) + r.get("posted", 0))
                                  for r in (out.get("results") or {}).values())
                    print(f"[serverinfo] refresh done - {changed} message(s) changed"
                          if changed else "[serverinfo] refresh done - nothing changed")
                else:
                    print(f"[serverinfo] refresh problem: {out.get('errors') or out.get('message')}")
            except Exception as exc:  # noqa: BLE001 - a poster must never die
                print(f"[serverinfo] refresh failed: {exc}")
            time.sleep(hours * 3600)

    threading.Thread(target=loop, daemon=True, name="arkpanel-serverinfo").start()
    print(f"[serverinfo] refreshing Discord info channels every {hours}h")


def _remote_status_rows() -> dict:
    """Live status of the servers on the PC that has no watcher of its own.

    One /api/status per machine, not per server: his three maps share one agent,
    and three identical calls over Tailscale every refresh would be three times
    the traffic for the same answer. Player counts cost an RCON round trip each,
    so they are only asked of maps that are actually running.
    """
    wanted = [s for s in CONFIG["servers"]
              if s["name"] in set(_live_status_config().get("servers", []))]
    hosts: dict = {}
    rows, system, host_name = [], None, ""

    for server in wanted:
        key = server.get("host") or server["name"]
        if key not in hosts:
            # live=1: a CPU sample and the disk reading, so this embed says what
            # a watcher's does. Once a minute, once per machine -- cheap.
            payload, error = agent_get(server, "/api/status", live=1)
            hosts[key] = (payload or {}, error)
        payload, error = hosts[key]
        label = server.get("display_name", server["name"])

        if error:
            rows.append((label, {"agent_error": error}, None))
            continue
        system = system or payload.get("system")
        status = next((s for s in payload.get("servers", [])
                       if s["name"] == server["name"]), None) or {}
        host_name = host_name or status.get("host") or server.get("host", "")

        players = 0
        if status.get("running"):
            who, who_error = agent_get(server, f"/api/server/{server['name']}/players")
            # None, not 0: an unanswered RCON is not an empty server, and the
            # embed prints an honest "3+" rather than a number that lost one.
            players = None if who_error else (who or {}).get("count")
        rows.append((label, status, players))

    return {"rows": rows, "system": system, "host": host_name}


def _live_status_config() -> dict:
    return CONFIG.get("discord", {}).get("live_status") or {}


def _remote_status():
    """Guardian's live-status embed for a watcher-less machine, or None."""
    config = _live_status_config()
    if not (config.get("channel") and config.get("servers")):
        return None
    return discord_bot.RemoteStatus(
        channel_id=config["channel"],
        rows_fn=_remote_status_rows,
        # Slower than a watcher's 30s on purpose: every refresh crosses
        # Tailscale, and one edit a minute still reads as live.
        refresh_seconds=int(config.get("refresh_seconds", 60)),
        state_path=os.path.join(BASE, "data", "remote_status.json"),
        label=config.get("label", "remote maps"),
    )


def _guardian_switch(target: str) -> dict:
    """Guardian's handle on the game swap.

    Starts it and returns immediately: the switch takes minutes - a server has
    to save and shut down before the other starts - and a Discord interaction
    cannot be held open that long.
    """
    if SWITCH_STATE["running"]:
        return {"error": "busy",
                "message": f"already switching to {SWITCH_STATE['target']}"}
    if not (CONFIG.get("palworld") or {}).get("enabled"):
        return {"error": "not_configured", "message": "Palworld is not set up"}
    threading.Thread(target=_switch_to, args=(target,), daemon=True,
                     name="arkpanel-switch").start()
    return {"ok": True, "switching_to": target}


def _start_palworld_post_refresh() -> None:
    """Redraw the Palworld Discord post once Guardian is connected.

    The post's buttons are built by PalworldView, so a panel update that
    changes those buttons leaves the LIVE message showing the old ones until
    somebody happens to press Refresh. That bit on 2026-11-09: the controls
    were replaced but a retired "Switch to Palworld" button sat in the channel
    looking usable. Republishing on start keeps the message honest about what
    the running code can actually do.

    publish_palworld edits the existing message rather than posting a new one,
    so this is quiet - a restart does not spam the channel.
    """
    channel = CONFIG.get("discord", {}).get("palworld_channel")
    if not channel:
        print("[palworld] no discord.palworld_channel - post not refreshed")
        return
    if not (CONFIG.get("palworld") or {}).get("enabled"):
        return

    def loop():
        # Guardian connects on its own thread; give it a few minutes before
        # giving up rather than racing it and silently doing nothing.
        for _ in range(100):
            time.sleep(3)
            if GUARDIAN is not None and getattr(GUARDIAN, "ready", False):
                break
        else:
            plog("[palworld] Guardian never became ready - post not refreshed")
            return
        try:
            GUARDIAN.published = _load_published()
            GUARDIAN.publish_palworld(int(channel), _palworld_info_chunks())
            _save_published(GUARDIAN.published)
            plog("[palworld] Discord post refreshed on startup")
        except Exception as exc:  # noqa: BLE001
            plog(f"[palworld] could not refresh the Discord post: {exc}")

    threading.Thread(target=loop, daemon=True,
                     name="arkpanel-palworld-post").start()


# ------------------------------------------------------ Discord game channels
#
# THE STANDARD for any non-ARK game with its own Discord channel (2026-11-12):
# add it to discord.game_channels in config.json -
#     "minecraft": {"channel": "<id>", "emoji": "⛏️"}
# - and the game's adapter supplies discord_info() (how to join) plus the
# status/start/stop/restart it already has. Guardian then keeps the channel
# holding the join post and ONE live status embed with buttons, refreshed every
# minute, republished on every panel start. No per-game code here.

def _game_posts() -> dict:
    """{game_id: {"channel", "emoji", "display_name"}} for enabled games."""
    out = {}
    for gid, entry in (CONFIG.get("discord", {}).get("game_channels") or {}).items():
        if gid.startswith("_") or not isinstance(entry, dict) or not entry.get("channel"):
            continue
        if not (CONFIG.get(gid) or {}).get("enabled"):
            continue
        out[gid] = {"channel": str(entry["channel"]), "emoji": entry.get("emoji", "🎮"),
                    "display_name": (CONFIG.get(gid) or {}).get("display_name", gid.title())}
    return out


def _guardian_game_status(game_id: str) -> dict:
    st = _game(game_id).status(_game_server(game_id, ""))
    return {"running": st.running, "uptime_seconds": st.uptime_seconds,
            "memory_gb": st.memory_gb, "players": st.players, "port": st.port,
            "detail": st.detail, **st.extra}


def _guardian_game_control(game_id: str, verb: str) -> dict:
    """start/stop/restart for a Discord button; the work runs on a thread,
    because a stop saves and a restart then cold-starts, far longer than an
    interaction should wait."""
    if verb not in ("start", "stop", "restart"):
        return {"error": "bad_action"}
    if game_id not in _game_posts():
        return {"error": "not_configured", "message": f"{game_id} is not set up."}
    game, server = _game(game_id), _game_server(game_id, "")

    def work():
        try:
            getattr(game, verb)(server)
        except Exception as exc:  # noqa: BLE001
            plog(f"[{game_id}] discord {verb} failed: {exc}")

    threading.Thread(target=work, daemon=True, name=f"arkpanel-{game_id}-discord-{verb}").start()
    return {}


def _publish_game_post(game_id: str) -> dict:
    posts = _game_posts()
    if game_id not in posts:
        return {"ok": False, "error": "not_configured"}
    text = _game(game_id).discord_info(_game_server(game_id, ""))
    GUARDIAN.published = _load_published()
    out = GUARDIAN.publish_game(game_id, int(posts[game_id]["channel"]),
                                serverinfo.split_for_discord(text))
    _save_published(GUARDIAN.published)
    return out


@app.post("/api/discord/games/<game_id>/publish")
def publish_game_post(game_id):
    if GUARDIAN is None or not GUARDIAN.ready:
        return jsonify({"error": "guardian_offline"}), 503
    out = _publish_game_post(game_id)
    return jsonify(out), (200 if out.get("ok") else 409)


def _start_game_posts() -> None:
    """Republish every game channel once Guardian connects, so the join post
    and buttons always match the running code (same reason as Palworld's)."""
    if not _game_posts():
        return

    def loop():
        for _ in range(100):
            time.sleep(3)
            if GUARDIAN is not None and getattr(GUARDIAN, "ready", False):
                break
        else:
            plog("[games] Guardian never became ready - game posts not refreshed")
            return
        for gid in _game_posts():
            try:
                out = _publish_game_post(gid)
                plog(f"[games] {gid} Discord post "
                     + ("refreshed" if out.get("ok") else f"failed: {out.get('error')}"))
            except Exception as exc:  # noqa: BLE001
                plog(f"[games] {gid} Discord post failed: {exc}")

    threading.Thread(target=loop, daemon=True, name="arkpanel-game-posts").start()


def _guardian_palworld_control(action: str) -> dict:
    """Drive Palworld for a Discord button. {} on success, {"error": ...} not.

    start/stop/restart hand off to a thread and return immediately: a stop
    warns players and counts down, and a restart does that and then a cold
    start, which together run well past the few seconds a Discord interaction
    should take. The button text says "give it a minute, then Refresh" to
    match. A save is quick, so that one is awaited and reported honestly.
    """
    if not (CONFIG.get("palworld") or {}).get("enabled"):
        return {"error": "not_configured", "message": "Palworld is not configured."}

    remote = _palworld_remote()

    if action == "save":
        if remote:
            _, error = _palworld_agent("POST", "/api/palworld/save", timeout=(3, 120))
            return {"error": "save_failed", "message": error} if error else {}
        try:
            palworld.save_and_snapshot(_palworld_cfg(), log=plog)
            return {}
        except Exception as exc:  # noqa: BLE001
            return {"error": "save_failed", "message": str(exc)}

    if action not in ("start", "stop", "restart"):
        return {"error": "bad_action", "message": f"unknown action {action}"}

    def work():
        try:
            if remote:
                if action in ("stop", "restart"):
                    _, error = _palworld_agent("POST", "/api/palworld/stop",
                                               {"warn_seconds": 30}, timeout=(3, 300))
                    if error:
                        plog(f"[palworld] discord {action}: remote stop failed: {error}")
                        return
                if action in ("start", "restart"):
                    _, error = _palworld_agent("POST", "/api/palworld/start",
                                               timeout=(3, 300))
                    if error:
                        plog(f"[palworld] discord {action}: remote start failed: {error}")
                return
            cfg = _palworld_cfg()
            if action in ("stop", "restart"):
                _palworld_wanted(False)        # so the watchdog does not fight us
                palworld.stop(cfg, warn_seconds=30, log=plog)
            if action in ("start", "restart"):
                result = palworld.start(cfg, log=plog)
                _palworld_wanted(bool(result.get("ok")))
        except Exception as exc:  # noqa: BLE001
            plog(f"[palworld] discord {action} failed: {exc}")

    threading.Thread(target=work, daemon=True,
                     name=f"guardian-palworld-{action}").start()
    plog(f"[palworld] {action} requested from Discord")
    return {}


def _start_guardian() -> None:
    global GUARDIAN
    token = (SECRETS.get("discord_guardian_token") or "").strip()
    channel = SECRETS.get("discord_guardian_channel")
    owner = SECRETS.get("discord_owner_id")
    if not token or not channel:
        print("[discord] no discord_guardian_token - Guardian not started")
        return
    if not owner:
        print("[discord] WARNING: no discord_owner_id set - Guardian will refuse "
              "every control command. Set it in secrets.json.")

    GUARDIAN = GuardianBot(
        name="Guardian", token=token, channel_id=channel, owner_id=owner,
        servers_fn=lambda: [{"name": s["name"],
                             "display_name": s.get("display_name", s["name"])}
                            for s in CONFIG["servers"]],
        action_fn=_guardian_action,
        players_fn=_guardian_players,
        kick_fn=_guardian_kick,
        recent_fn=_guardian_recent,
        switch_fn=_guardian_switch,
        palworld_fn=_palworld_status,
        characters_fn=_guardian_characters,
        rescue_fn=_guardian_rescue,
        job_fn=_guardian_job,
        update_fn=_guardian_update,
        # Live map states for the control panel embed.
        status_fn=_guardian_status,
        # The live embed for the machine with no watcher of its own.
        remote_status=_remote_status(),
        # Empty by default: owner only. Add Discord role ids here to let the
        # admin team press the buttons without giving anyone the panel login.
        control_role_ids=CONFIG.get("discord", {}).get("control_role_ids", []),
        # Anyone with Discord Administrator may press. The owner asked for
        # this on 2026-11-09 so the admin team is not funnelled through one
        # person; set discord.control_allow_admins false to go back to
        # owner-plus-named-roles only.
        control_allow_admins=CONFIG.get("discord", {}).get("control_allow_admins", True),
        palworld_control_fn=_guardian_palworld_control,
        # Game channels: see _game_posts. Fixed at start, like the views.
        game_posts={gid: {"display_name": p["display_name"], "emoji": p["emoji"]}
                    for gid, p in _game_posts().items()},
        game_status_fn=_guardian_game_status,
        game_control_fn=_guardian_game_control,
        control_state_path=os.path.join(BASE, "data", "control_panel.json"),
        logger=plog,
    )
    GUARDIAN.start()


def plog(message: str) -> None:
    """Append a line to logs/panel.log as well as stdout.

    The panel runs under pythonw.exe, which discards stdout, so everything it
    printed went nowhere. That was tolerable while Guardian only reported; now
    that it carries buttons which can stop a server, "who pressed what and did
    it work" needs somewhere to live. Discord shows the outcome publicly; this
    is the panel's own side of it.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(message)
    try:
        log_dir = os.path.join(BASE, "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "panel.log"), "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except OSError:
        pass   # logging must never take the panel down


def _guardian_status() -> dict:
    """Cluster status for Guardian's control embed.

    cluster_status is a Flask view, so it needs a request context -- Guardian
    calls this from its own thread, where there is none.
    """
    with app.test_request_context():
        return cluster_status().get_json()


@app.post("/api/discord/controls/publish")
def publish_controls():
    """Post (or refresh) the button control panel in its Discord channel."""
    if GUARDIAN is None:
        return jsonify({"error": "guardian not started"}), 503
    payload = request.get_json(silent=True) or {}
    channel = (payload.get("channel")
               or CONFIG.get("discord", {}).get("control_channel"))
    if not channel:
        return jsonify({"error": "no_channel",
                        "message": ("Set discord.control_channel in config.json, "
                                    "or pass a channel id.")}), 400
    out = GUARDIAN.publish_controls(int(channel))
    return jsonify(out), (200 if out.get("ok") else 409)


@app.post("/api/discord/test")
def discord_test():
    """Prove Guardian can post into its channel.

    Worth having permanently: with auto-apply on, this announcement is the only
    thing that will tell you an unattended update ran. A delivery path you have
    never exercised is not a notification system.
    """
    if GUARDIAN is None:
        return jsonify({"ok": False, "error": "Guardian is not configured"}), 503
    payload = request.get_json(silent=True) or {}
    severity = payload.get("severity", "info")
    sent = GUARDIAN.announce(
        "Test announcement",
        "If you can see this, ArkPanel can reach this channel. Update and "
        "restart notifications will arrive the same way.",
        severity)
    return jsonify({
        "ok": sent,
        "ready": GUARDIAN.ready,
        "channel_id": GUARDIAN.channel_id,
        "detail": ("posted" if sent else
                   "Guardian is not connected yet, so nothing was sent"),
    })


@app.get("/api/discord")
def discord_status():
    """Whether the bots are connected, for the panel to show."""
    out = {"guardian": GUARDIAN.status() if GUARDIAN else
           {"configured": False, "running": False}}
    for server in CONFIG["servers"]:
        data, error = agent_get(server, "/api/discord")
        out[server["name"]] = data if not error else {"error": error}
    return jsonify(out)


def _ensure_desktop_key() -> None:
    """Mint the desktop app's key once, and keep it.

    Generated here rather than by the desktop app so the running panel always
    has it in memory: a key written to secrets.json by another process would
    not exist for this one until it restarted.
    """
    if (SECRETS.get("desktop_key") or "").strip():
        return
    SECRETS["desktop_key"] = secrets_mod.token_urlsafe(32)
    path = os.path.join(BASE, "secrets.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            on_disk = json.load(fh)
        on_disk["desktop_key"] = SECRETS["desktop_key"]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(on_disk, fh, indent=4)
        os.replace(tmp, path)
        plog("[desktop] generated a desktop key in secrets.json")
    except (OSError, ValueError) as exc:
        plog(f"[desktop] could not save the desktop key: {exc}")


def _start_embedded_agent() -> None:
    """Run an agent inside the panel process, for this machine's own servers.

    The product shape this serves: someone with ONE PC installs ArkPanel and it
    manages their server straight away, with no second program to install, no
    second service to keep alive and nothing to pair with itself. Adding a
    second machine means running the agent installer there and joining it to
    this panel -- so an extra PC costs an install, and the common case costs
    none.

    It is the same agent module a remote machine runs, serving on the same
    port, so there is no "local mode" behaving subtly differently from the code
    that gets tested on every other machine. The panel still talks to it over
    HTTP at 127.0.0.1 exactly as it talks to any other agent: one code path for
    local and remote, which is why there is a port here at all rather than
    direct function calls.
    """
    cfg = CONFIG.get("panel", {})
    if not cfg.get("embedded_agent"):
        return

    port = int(cfg.get("embedded_agent_port", 8767))
    try:
        import agent as agent_mod
    except Exception as exc:  # noqa: BLE001
        plog(f"[embedded-agent] could not import the agent: {exc}")
        return

    try:
        agent_mod.start_services()
    except Exception as exc:  # noqa: BLE001
        plog(f"[embedded-agent] background services failed to start: {exc}")
        return

    def serve_agent():
        from waitress import serve as serve_agent_app
        try:
            serve_agent_app(agent_mod.app, host="127.0.0.1", port=port, threads=8)
        except Exception as exc:  # noqa: BLE001
            plog(f"[embedded-agent] stopped serving on {port}: {exc}")

    threading.Thread(target=serve_agent, daemon=True,
                     name="arkpanel-embedded-agent").start()
    plog(f"[embedded-agent] serving this machine's servers on 127.0.0.1:{port}")


def main() -> None:
    from waitress import serve

    _ensure_desktop_key()
    _start_embedded_agent()

    global UPDATER
    UPDATER = Updater(
        config=CONFIG,
        servers_fn=lambda: CONFIG["servers"],
        version_fn=_version_of,
        players_fn=_players_of,
        update_fn=_start_update,
        reachable_fn=_agent_reachable,
        notify_fn=_updater_notify,
        job_status_fn=_guardian_job,
    )
    UPDATER.start()
    _start_guardian()
    _start_bouncer()
    _start_info_refresher()
    # When Palworld lives on another machine its own agent owns these -- see
    # _palworld_remote(). Running them here would snapshot PC1's stale copy
    # and the watchdog would resurrect a server that moved away.
    if _palworld_remote():
        print(f"[palworld] hosted on {(CONFIG.get('palworld') or {}).get('host') or 'another machine'}"
              " - its agent owns snapshots, restarts and the watchdog")
    else:
        _start_palworld_snapshots()
        _start_palworld_restarts()
        _start_palworld_watchdog()
    _start_palworld_post_refresh()
    _start_game_posts()
    _start_bedrock_dns()
    _start_bedrockconnect()
    _start_minecraft_snapshots()
    _start_minecraft_autoupdate()
    _start_minecraft_transport_watch()

    host = CONFIG["panel"].get("host", "0.0.0.0")
    port = int(CONFIG["panel"].get("port", 8090))
    mode = "READ-ONLY" if CONFIG.get("read_only", True) else "READ-WRITE"
    print(f"ArkPanel  {mode}  on http://{host}:{port}")
    for s in CONFIG["servers"]:
        print(f"  {s['display_name']:16} -> {s['agent_url']}  ({s.get('host')})")
    serve(app, host=host, port=port, threads=12)


if __name__ == "__main__":
    main()

