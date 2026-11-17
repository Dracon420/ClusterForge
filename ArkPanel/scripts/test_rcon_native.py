"""
ArkPanel's own RCON, tested against a fake server that speaks the protocol.

This replaced PatchRaptor's RCONManager, which was the last piece of outside
software the panel depended on. No real ARK server is contacted here: a socket
server in this process plays the part, so the failure modes that matter can be
forced on demand.

Run:  python scripts\\test_rcon_native.py
"""

import os
import socket
import struct
import sys
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import rcon                                                # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r} (expected {want!r})")
    if not ok:
        FAILED.append(label)


def pack(packet_id, packet_type, body):
    payload = struct.pack("<ii", packet_id, packet_type) + body.encode() + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def read_packet(conn):
    raw = conn.recv(4)
    if not raw:
        return None
    (length,) = struct.unpack("<i", raw)
    payload = b""
    while len(payload) < length:
        payload += conn.recv(length - len(payload))
    pid, ptype = struct.unpack("<ii", payload[:8])
    return pid, ptype, payload[8:-2].decode()


class FakeArk(threading.Thread):
    """A one-connection RCON server. `behaviour` decides how it answers."""

    def __init__(self, behaviour="ok", password="s3cret"):
        super().__init__(daemon=True)
        self.behaviour = behaviour
        self.password = password
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received = []

    def run(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            auth = read_packet(conn)
            if auth is None:
                return
            _pid, _ptype, supplied = auth
            if supplied != self.password:
                conn.sendall(pack(-1, rcon.AUTH_RESPONSE, ""))
                return
            conn.sendall(pack(1, rcon.AUTH_RESPONSE, ""))

            while True:
                packet = read_packet(conn)
                if packet is None:
                    return
                pid, _ptype, command = packet
                self.received.append(command)

                if self.behaviour == "split":
                    # A long ListPlayers arrives as several packets.
                    conn.sendall(pack(pid, rcon.RESPONSE_VALUE, "0. Alpha, 0001\n"))
                    conn.sendall(pack(pid, rcon.RESPONSE_VALUE, "1. Bravo, 0002\n"))
                    conn.sendall(pack(pid, rcon.RESPONSE_VALUE, "2. Charlie, 0003"))
                elif self.behaviour == "empty":
                    conn.sendall(pack(pid, rcon.RESPONSE_VALUE, ""))
                elif self.behaviour == "hangup":
                    return
                else:
                    conn.sendall(pack(pid, rcon.RESPONSE_VALUE, f"ran: {command}"))


print("A normal command:")
server = FakeArk()
server.start()
out = rcon.execute("127.0.0.1", server.port, "s3cret", "SaveWorld")
check("returns the server's reply", out, "ran: SaveWorld")
check("sent the command verbatim", server.received, ["SaveWorld"])

print("\nCharacters the old .bat wrapper refused - no shell now, so they pass:")
server = FakeArk()
server.start()
message = "Server restarting in 5 minutes (save & log out)"
out = rcon.execute("127.0.0.1", server.port, "s3cret", f"ServerChat {message}")
check("brackets and ampersand survive", server.received[0], f"ServerChat {message}")

print("\nA reply split across packets (a full server's ListPlayers):")
server = FakeArk(behaviour="split")
server.start()
out = rcon.execute("127.0.0.1", server.port, "s3cret", "ListPlayers")
check("all three players present", out.count("."), 3)
check("nothing truncated", "Charlie" in out, True)

print("\nWrong password:")
server = FakeArk()
server.start()
try:
    rcon.execute("127.0.0.1", server.port, "wrong", "ListPlayers")
    check("raises", False, True)
except rcon.RconAuthError as exc:
    check("raises RconAuthError, not an empty result", "refused" in str(exc), True)
except rcon.RconError as exc:
    check("raises RconAuthError specifically", f"wrong type: {exc}", "RconAuthError")

print("\nNothing listening on the port:")
spare = socket.socket()
spare.bind(("127.0.0.1", 0))
dead_port = spare.getsockname()[1]
spare.close()
try:
    rcon.execute("127.0.0.1", dead_port, "s3cret", "ListPlayers", timeout=2)
    check("raises", False, True)
except rcon.RconError as exc:
    check("raises RconError naming the address", "127.0.0.1" in str(exc), True)

print("\nServer hangs up without answering:")
server = FakeArk(behaviour="hangup")
server.start()
try:
    rcon.execute("127.0.0.1", server.port, "s3cret", "ListPlayers", timeout=3)
    check("raises rather than returning nothing", False, True)
except rcon.RconError:
    check("raises RconError", True, True)

print("\nAn empty but valid reply (nobody online):")
server = FakeArk(behaviour="empty")
server.start()
out = rcon.execute("127.0.0.1", server.port, "s3cret", "ListPlayers")
check("returns an empty string, does NOT raise", out, "")

print("\nAn empty command is refused before connecting:")
try:
    rcon.execute("127.0.0.1", 1, "s3cret", "   ")
    check("raises", False, True)
except rcon.RconError as exc:
    check("raises RconError", "empty command" in str(exc), True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed - no ARK server and no PatchRaptor was involved")
