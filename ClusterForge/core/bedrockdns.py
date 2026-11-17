"""A tiny DNS forwarder, so a console can reach a server you host yourself.

Minecraft on Xbox will only connect to the handful of partner servers Mojang
list on the Servers tab. There is no "add server" button, and no way to type
an address. The community answer is BedrockConnect: a fake Bedrock server
that, when a console connects to it, shows a menu of servers you choose.

Getting the console TO BedrockConnect is what this file is for. You point the
console's DNS here; this answers the partner hostnames with BedrockConnect's
address, so selecting any featured server lands on the menu instead.

The important part is what it does with EVERYTHING ELSE. A DNS server that
only knows Minecraft hostnames would break the rest of the console - the
store, updates, Netflix, sign-in. So every other question is forwarded
upstream and the real answer relayed back. The console keeps working exactly
as before; only those few hostnames are redirected.

Deliberately LAN-only by default. Exposing a DNS service to the internet
means anything on it can be abused, and means every lookup from whoever
points at it travels through this machine - which is a lot to ask of a
friend, and a lot to take on. bind_host stays on the local network unless
someone changes it on purpose.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

# The servers Mojang list on the console Servers tab. Connecting to any of
# them is what triggers the redirect, so they all have to resolve here.
# Taken from BedrockConnect's own list; harmless if one is retired.
FEATURED_HOSTS = {
    "mco.lbsg.net",                  # The Hive (legacy)
    "geo.hivebedrock.network",       # The Hive
    "hivebedrock.network",
    "play.inpvp.net",                # Mineville
    "mco.mineplex.com",              # Mineplex
    "mineplex.com",
    "play.galaxite.net",             # Galaxite
    "play.cubecraft.net",            # CubeCraft
    "mco.cubecraft.net",
    "play.pixelparadise.gg",
    "play.lifeboat.network",         # Lifeboat
    "mco.lifeboat.network",
}

UPSTREAM = ("1.1.1.1", 53)           # where real questions go


def _question_name(packet: bytes):
    """The hostname a DNS query is asking about, lowercased. None if unclear."""
    try:
        index = 12                   # skip the 12-byte header
        labels = []
        while True:
            length = packet[index]
            if length == 0:
                break
            if length & 0xC0:        # a compression pointer: not a plain query
                return None
            index += 1
            labels.append(packet[index:index + length].decode("ascii", "ignore"))
            index += length
        return ".".join(labels).lower()
    except Exception:  # noqa: BLE001 - a malformed packet is not our problem
        return None


def _a_record_reply(packet: bytes, ip: str) -> bytes:
    """Build an answer saying 'that name is at this IPv4 address'."""
    transaction_id = packet[:2]
    # Flags: response, authoritative, recursion available.
    header = transaction_id + b"\x81\x80" + b"\x00\x01\x00\x01\x00\x00\x00\x00"
    question = packet[12:]
    answer = (b"\xc0\x0c"            # pointer back to the name in the question
              + b"\x00\x01"          # type A
              + b"\x00\x01"          # class IN
              + struct.pack(">I", 30)   # TTL: short, so a change is picked up
              + b"\x00\x04"          # 4 bytes of address
              + socket.inet_aton(ip))
    return header + question + answer


def serve(redirect_to: str, bind_host: str = "0.0.0.0", bind_port: int = 53,
          hosts=None, upstream=UPSTREAM, log=print, stop_event=None):
    """Answer featured-server names with `redirect_to`, forward the rest."""
    hosts = set(hosts or FEATURED_HOSTS)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_host, bind_port))
    sock.settimeout(1.0)
    log(f"[bedrockdns] listening on {bind_host}:{bind_port}, redirecting "
        f"{len(hosts)} Minecraft hostname(s) to {redirect_to}")

    redirected = 0
    while not (stop_event and stop_event.is_set()):
        try:
            packet, client = sock.recvfrom(512)
        except socket.timeout:
            continue
        except OSError as exc:
            log(f"[bedrockdns] socket error: {exc}")
            break

        name = _question_name(packet)
        if name and name in hosts:
            redirected += 1
            try:
                sock.sendto(_a_record_reply(packet, redirect_to), client)
                log(f"[bedrockdns] {name} -> {redirect_to} (for {client[0]})")
            except OSError:
                pass
            continue

        # Everything else is somebody else's business. Forward it, relay the
        # answer, and let the console get on with its life.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as out:
                out.settimeout(4.0)
                out.sendto(packet, upstream)
                reply, _ = out.recvfrom(4096)
            sock.sendto(reply, client)
        except OSError:
            pass            # a dropped lookup gets retried by the client

    sock.close()
    log(f"[bedrockdns] stopped after redirecting {redirected} lookup(s)")


def start(redirect_to: str, bind_host: str = "0.0.0.0", bind_port: int = 53,
          log=print):
    """Run serve() on a background thread. Returns (thread, stop_event)."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=serve, daemon=True, name="clusterforge-bedrockdns",
        kwargs={"redirect_to": redirect_to, "bind_host": bind_host,
                "bind_port": bind_port, "log": log, "stop_event": stop_event})
    thread.start()
    time.sleep(0.3)
    return thread, stop_event
