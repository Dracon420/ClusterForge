"""Watch Bedrock's transport deprecation so it cannot ambush us.

Mojang deprecated RakNet in 1.26 and NetherNet is now the only transport they
call supported. We are deliberately still on RakNet, because on 2026-10-20 we
measured what NetherNet actually costs here:

    * The Xbox found the world under Worlds -> LAN either way. NetherNet added
      nothing, and it did NOT appear in the Friends tab - a dedicated server
      has no Xbox Live identity to be a friend of.
    * Joining by address died. Nothing answered on 7300; the server opened a
      random port (7551) known only to Xbox Live signalling.
    * BedrockConnect broke. Its menu still loaded, but every entry points at
      an address, and there was no longer an address to point at. That is the
      only route a friend outside the LAN has.
    * The panel went blind - no player count.

So RakNet stays. The risk is that it is removed in some future update and all
four of those break at once, on an evening when somebody wants to play. This
module exists so we find out first.

WHAT THIS CAN AND CANNOT DO, stated plainly because the gap matters:

    It CAN detect that RakNet stopped answering, that the server version
    changed, and that NetherNet has become pingable (which would mean Mojang
    gave dedicated servers a reachable address again - the thing that would
    make switching worthwhile).

    It CANNOT tell you whether the world shows up in anyone's Friends tab.
    That needs a human holding a controller. So on every version change it
    asks for that test by name rather than pretending to have run it.

The version is read from the server's own log, not from a ping, precisely
because the failure we care about is the one where pinging no longer works.
"""

from __future__ import annotations

import os
import re
from datetime import datetime

from . import minecraft

# How many consecutive silent pings before we call RakNet dead. A restart or a
# momentary hiccup should not page anybody; three misses across three passes
# is not a hiccup.
FAIL_STREAK_FOR_ALERT = 3

# Don't judge a server that only just came up - Bedrock opens its port a few
# seconds after "Server started".
MIN_UPTIME_SECONDS = 90

_VERSION_RE = re.compile(r"Version:\s*([0-9][0-9.]*)")
_TRANSPORT_WARNING = "TRANSPORT TYPE ERROR"
_STARTED = "Server started."

# The manual half of the test. Written out here so the Discord message carries
# the steps - nobody should have to come back and ask what to check.
RETEST_STEPS = (
    "To re-test NetherNet (about 5 minutes):\n"
    "1. Panel -> Minecraft -> Settings, set transport to nethernet, restart.\n"
    "2. On the Xbox, look in the FRIENDS tab - not Worlds, not Servers. "
    "Worlds -> LAN works on both transports and proves nothing.\n"
    "3. Check the panel. If it shows a player count, NetherNet became "
    "pingable and is worth keeping.\n"
    "If it is not in Friends, set transport back to raknet and restart. "
    "Nothing in the world is affected either way - transport is a network "
    "setting, not a world setting."
)


def log_path(install_dir: str) -> str:
    return os.path.join(install_dir, "logs", "minecraft.log")


def _tail(path: str, max_bytes: int = 262144) -> str:
    """Last chunk of a log file, as text. Empty string if unreadable."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            raw = fh.read()
    except OSError:
        return ""
    return raw.decode("utf-8", "replace")


def log_version(install_dir: str) -> str:
    """The version the server last reported, from its log.

    Deliberately not from a ping: if RakNet is removed the ping goes silent,
    and that is the exact moment we most need to know which version did it.
    """
    found = _VERSION_RE.findall(_tail(log_path(install_dir)))
    return found[-1] if found else ""


def deprecation_warning_present(install_dir: str) -> bool:
    """Is Mojang still printing the transport warning on the latest start?

    Only the most recent start counts - an older block further up the log is
    history, not the current stance.
    """
    text = _tail(log_path(install_dir))
    if not text:
        return False
    cut = text.rfind(_STARTED)
    return _TRANSPORT_WARNING in (text[cut:] if cut >= 0 else text)


def check(config: dict,
          state: dict | None = None
          ) -> tuple[dict, list[tuple[str, str, str]]]:
    """One pass. Returns (new_state, findings).

    A finding is (kind, severity, message) - the shape the panel's notifier
    already takes, so this needs no notifier of its own.
    """
    state = dict(state or {})
    findings: list[tuple[str, str, str]] = []
    alerted = dict(state.get("alerted") or {})

    install_dir = config.get("install_dir", "")
    raw = minecraft.status(config)
    transport = (raw.get("transport") or "raknet").strip().lower()
    version = log_version(install_dir) or state.get("version", "")

    state["transport"] = transport
    state["version"] = version
    state["last_checked"] = datetime.now().isoformat(timespec="seconds")

    if not raw.get("running"):
        state["fail_streak"] = 0
        state["alerted"] = alerted
        return state, findings

    # status() only pings when the transport is raknet, so this doubles as
    # "RakNet answered".
    answered = bool(raw.get("version"))

    # ---- did the version change? Then the manual test is due again. -------
    previous = alerted.get("version_seen", "")
    if version and previous and version != previous:
        state_of_raknet = (
            "RakNet is still answering, so joining by address and "
            "BedrockConnect are fine."
            if answered or transport != "raknet" else
            "RakNet is NOT answering - see the separate alert.")
        findings.append((
            "minecraft_updated", "info",
            f"Minecraft Bedrock updated: {previous} -> {version}.\n"
            f"{state_of_raknet}\n\n{RETEST_STEPS}"))
    if version:
        alerted["version_seen"] = version

    uptime = raw.get("uptime_seconds") or 0

    if transport == "raknet":
        # ---- the canary: is RakNet still serving? -------------------------
        if answered:
            state["fail_streak"] = 0
            state["raknet_ok"] = True
        elif uptime >= MIN_UPTIME_SECONDS:
            streak = int(state.get("fail_streak") or 0) + 1
            state["fail_streak"] = streak
            state["raknet_ok"] = False
            if streak >= FAIL_STREAK_FOR_ALERT \
                    and alerted.get("raknet_dead") != version:
                alerted["raknet_dead"] = version
                findings.append((
                    "minecraft_raknet_dead", "critical",
                    "Minecraft is running on transport=raknet but has not "
                    f"answered a status ping in {streak} checks "
                    f"(version {version}).\n\n"
                    "If Mojang has removed RakNet, four things are down at "
                    "once: joining by address from a PC, BedrockConnect, the "
                    "panel's player count, and the only route a friend "
                    "outside the LAN has. Xbox LAN discovery may still work "
                    "on the same network, which can make this look fine from "
                    "the sofa.\n\n"
                    "There is no settings flip that recovers those - "
                    "NetherNet reaches clients through Xbox Live signalling a "
                    "dedicated server has no identity in. Switching to "
                    "nethernet keeps the world playable on the LAN but leaves "
                    "remote joining unsolved."))

        # ---- has Mojang quietly stopped complaining? ----------------------
        if version and not deprecation_warning_present(install_dir) \
                and alerted.get("warning_gone") != version:
            alerted["warning_gone"] = version
            findings.append((
                "minecraft_raknet_ok_again", "info",
                f"Bedrock {version} no longer prints the RakNet deprecation "
                "warning on startup. Possibly un-deprecated, possibly just "
                "reworded - worth a look at the release notes."))

    elif transport == "nethernet":
        # ---- the good-news case we would otherwise never notice -----------
        port = int(raw.get("port") or config.get("port") or 19132)
        if minecraft.ping("127.0.0.1", port) \
                and alerted.get("nethernet_pingable") != version:
            alerted["nethernet_pingable"] = version
            findings.append((
                "minecraft_nethernet_pingable", "info",
                f"On {version}, a NetherNet server ANSWERED a status ping on "
                f"port {port}. That is new - it was blind by design before. "
                "If NetherNet now has a reachable address, BedrockConnect and "
                "PC joining could work on the supported transport. Worth "
                "testing properly."))

    state["alerted"] = alerted
    return state, findings


# RakNet's unconnected ping. The one question you can ask any Bedrock-protocol
# listener without logging in, and the only way to learn what it ADVERTISES as
# opposed to what it is.
_PING_MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")
_MOTD_FIELDS = ("edition", "name", "protocol", "version", "players", "max",
                "server_id", "level", "mode")


def ping(host: str = "127.0.0.1", port: int = 19132,
         timeout: float = 4.0) -> dict:
    """Ask a listener what it claims to be. Returns {} if nothing answers.

    This exists because of a failure that looked nothing like its cause. On
    2026-10-29 a child's Xbox stopped seeing the server in its LAN list. The
    server log's loudest line was a TRANSPORT TYPE ERROR, which had been
    printing for six days while she played - pure noise. The real fault was
    BedrockConnect, the separate menu an Xbox needs because consoles have no
    "Add Server" field: it still advertised protocol 2193 (1.26.50) after her
    client updated to 2225 (1.26.61), and a Bedrock client silently HIDES a
    LAN server whose protocol it does not support. Nothing was down. Nothing
    logged an error. The entry simply vanished.

    One ping each at the server and at BedrockConnect would have found it in
    seconds, which is why this is a function and not a snippet in a script.
    A version mismatch between the two is the thing to look for.
    """
    import socket
    import struct

    packet = (b"\x01" + struct.pack(">q", 0) + _PING_MAGIC
              + struct.pack(">q", 2))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (host, port))
        data, _addr = sock.recvfrom(4096)
    except OSError:
        return {}
    finally:
        sock.close()

    # id(1) + time(8) + server guid(8) + magic(16) + length(2), then the MOTD.
    if len(data) < 35:
        return {}
    parts = data[35:].decode("utf-8", "replace").split(";")
    out = dict(zip(_MOTD_FIELDS, parts))
    out["raw"] = ";".join(parts)
    return out
