"""Source RCON, spoken directly.

ArkPanel used to reach its servers through PatchRaptor's RCONManager, which is
somebody else's software under a licence that forbids passing it on. Everything
above this layer -- the bots, the job runner, the countdown ladder, the player
parsing -- was always ArkPanel's own; only the wire protocol was borrowed. This
module replaces it, so the panel owes nothing to anything outside itself.

The protocol (Valve's, which ARK implements) is small:

    packet = int32 length | int32 id | int32 type | body bytes | 0x00 0x00
    length counts everything after itself.

    type 3  auth request          -> server answers type 2 with the SAME id,
                                     or id = -1 meaning the password is wrong
    type 2  command request       -> server answers type 0 with the output
    type 0  response value

Two things are easy to get wrong and are handled here:

  * A wrong password answers with id -1 rather than closing the socket. That
    must be an explicit error, never an empty string, or a bad password looks
    exactly like "nobody is online".

  * A long reply (ListPlayers on a busy server) arrives as SEVERAL type 0
    packets. Reading one packet and stopping silently truncates the list, so
    this drains until the server goes quiet.

Two improvements fall out of owning this. Commands are no longer passed through
a .bat file, so the shell-metacharacter allowlist that mangled countdown
messages is no longer needed -- there is no shell. And nothing is async any
more, which removes the event-loop juggling that made player counts read
"unknown" when a Discord bot asked from inside its own loop.
"""

from __future__ import annotations

import socket
import struct

AUTH = 3
AUTH_RESPONSE = 2
EXEC_COMMAND = 2
RESPONSE_VALUE = 0

DEFAULT_TIMEOUT = 10.0
# How long to keep listening for more of a multi-packet reply once the first
# has arrived. Long enough for a slow server to continue, short enough that
# every command does not pay it.
DRAIN_SECONDS = 0.4
MAX_PACKET = 4 * 1024 * 1024


class RconError(Exception):
    """Any failure to get an answer: connection, auth, or malformed reply."""


class RconAuthError(RconError):
    """The password was refused. Deliberately its own type - this must never be
    mistaken for an empty result."""


def _encode(packet_id: int, packet_type: int, body: str) -> bytes:
    payload = struct.pack("<ii", packet_id, packet_type) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def _read_exactly(sock: socket.socket, count: int) -> bytes:
    chunks, got = [], 0
    while got < count:
        chunk = sock.recv(count - got)
        if not chunk:
            raise RconError("server closed the connection mid-packet")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _read_packet(sock: socket.socket) -> tuple[int, int, str]:
    raw_length = _read_exactly(sock, 4)
    (length,) = struct.unpack("<i", raw_length)
    if length < 10 or length > MAX_PACKET:
        raise RconError(f"implausible packet length {length}")
    payload = _read_exactly(sock, length)
    packet_id, packet_type = struct.unpack("<ii", payload[:8])
    body = payload[8:-2].decode("utf-8", errors="replace")
    return packet_id, packet_type, body


def execute(host: str, port: int, password: str, command: str,
            timeout: float = DEFAULT_TIMEOUT) -> str:
    """Authenticate, run one command, return its output.

    One connection per command, closed afterwards. ARK's RCON is not a
    long-lived session and a held-open socket is one more thing to go stale
    between a restart and the next poll.
    """
    if not command or not command.strip():
        raise RconError("empty command")

    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except OSError as exc:
        raise RconError(f"cannot reach {host}:{port} - {exc}") from exc

    try:
        sock.settimeout(timeout)

        sock.sendall(_encode(1, AUTH, password or ""))
        while True:
            packet_id, packet_type, _body = _read_packet(sock)
            # Some servers send an empty RESPONSE_VALUE before the auth answer.
            if packet_type == AUTH_RESPONSE:
                if packet_id == -1:
                    raise RconAuthError("RCON password refused")
                break
            if packet_type != RESPONSE_VALUE:
                raise RconError(f"unexpected packet type {packet_type} during auth")

        sock.sendall(_encode(2, EXEC_COMMAND, command))
        first_id, first_type, body = _read_packet(sock)
        if first_type != RESPONSE_VALUE:
            raise RconError(f"unexpected packet type {first_type} in reply")
        parts = [body]

        # Drain any continuation packets.
        sock.settimeout(DRAIN_SECONDS)
        while True:
            try:
                _id, packet_type, more = _read_packet(sock)
            except (socket.timeout, TimeoutError):
                break
            except OSError:
                break
            if packet_type == RESPONSE_VALUE and more:
                parts.append(more)
            else:
                break

        return "".join(parts).strip()
    finally:
        try:
            sock.close()
        except OSError:
            pass


def execute_for(server: dict, command: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """The same, addressed by an ArkPanel server record."""
    return execute(server.get("rcon_ip") or "127.0.0.1",
                   int(server["rcon_port"]),
                   server.get("rcon_password") or "",
                   command, timeout=timeout)


# selfupdate proof v3: the agents fetched this themselves.
