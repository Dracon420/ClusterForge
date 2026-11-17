"""The RakNet canary has to fire for the right reason, and only once.

Minecraft Bedrock runs on transport=raknet here by choice. NetherNet was
measured on 2026-10-20 and lost PC joining by address, BedrockConnect, and the
panel's player count while adding nothing - the Xbox found the world under
Worlds -> LAN on both transports and never showed in the Friends tab.

That choice has a tail risk: Mojang removes RakNet and all of it breaks at
once. The nasty part is that Xbox LAN discovery would probably keep working,
so from the sofa everything looks fine while nobody outside the house can get
in. The watcher exists to catch that first.

So the things worth pinning are the ones that would make it useless:

  * It must not cry wolf during a restart. A server that has been up eight
    seconds has not opened its port yet.
  * It must not repeat the same alert every half hour. A critical that arrives
    48 times a day is a critical nobody reads.
  * It must read the version from the LOG, not from a ping. If RakNet is gone
    the ping is silent, which is exactly when we need to know which version
    did it - reading the version from a ping would go blind at the worst
    possible moment.
  * It must be wired into start_services(), because a watcher that is never
    started is worse than none: it reports healthy by saying nothing.
    [[test_selfupdate_wiring]] exists because that exact bug shipped twice.
"""
import ast
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import minecraft_transport as mt  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


LOG = """\
[2026-10-19 14:16:58:320 INFO] Starting Server
[2026-10-19 14:16:58:320 INFO] Version: 1.26.50.1
[2026-10-19 14:16:58:400 INFO] Server started.
[2026-10-19 14:16:58:401 ERROR] ================ TRANSPORT TYPE ERROR  ===========
[2026-10-20 08:43:10:857 INFO] Starting Server
[2026-10-20 08:43:10:857 INFO] Version: 1.26.52.3
[2026-10-20 08:43:13:868 INFO] Server started.
[2026-10-20 08:43:13:868 ERROR] ================ TRANSPORT TYPE ERROR  ===========
[2026-10-20 08:43:13:868 ERROR] NetherNet is the only supported transport type.
"""


def write_log(tmp, text):
    d = os.path.join(tmp, "logs")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "minecraft.log"), "w", encoding="utf-8") as fh:
        fh.write(text)


import tempfile  # noqa: E402

tmp = tempfile.mkdtemp(prefix="mctransport-")
write_log(tmp, LOG)

print("reading the version from the log, not from a ping")
check("takes the LAST version in the log", mt.log_version(tmp) == "1.26.52.3",
      mt.log_version(tmp))
check("missing log is empty, not an error", mt.log_version(os.path.join(tmp, "nope")) == "")

print("\nthe deprecation warning is read from the latest start only")
check("sees the warning after the last start", mt.deprecation_warning_present(tmp))
write_log(tmp, LOG + "[2026-10-21 09:00:00:000 INFO] Server started.\n")
check("a start with no warning after it reads as gone",
      not mt.deprecation_warning_present(tmp),
      "an older block further up the log is history, not the current stance")
write_log(tmp, LOG)

# ------------------------------------------------------------------ stubs
class FakeStatus:
    """Stand in for core.minecraft so no server is needed."""

    def __init__(self, running=True, uptime=600, transport="raknet",
                 answered=True, port=7300):
        self.running, self.uptime = running, uptime
        self.transport, self.answered, self.port = transport, answered, port
        self.pinged = []

    def status(self, config):
        out = {"running": self.running, "uptime_seconds": self.uptime,
               "transport": self.transport, "port": self.port}
        if self.answered and self.transport == "raknet":
            out["version"] = "1.26.52.3"
            out["player_count"] = 0
        return out

    def ping(self, host, port, timeout=2.0):
        self.pinged.append((host, port))
        return {"version": "1.26.52.3"} if self.answered else None


def run(fake, state, config=None):
    real_status, real_ping = mt.minecraft.status, mt.minecraft.ping
    mt.minecraft.status, mt.minecraft.ping = fake.status, fake.ping
    try:
        return mt.check(config or {"install_dir": tmp, "port": 7300}, state)
    finally:
        mt.minecraft.status, mt.minecraft.ping = real_status, real_ping


print("\na healthy server is quiet")
state, found = run(FakeStatus(), {"alerted": {"version_seen": "1.26.52.3"}})
check("no findings when RakNet answers", not found, [f[0] for f in found])
check("records the transport", state["transport"] == "raknet")
check("fail streak stays at zero", state.get("fail_streak") == 0)

print("\na restarting server is not accused")
state = {"alerted": {"version_seen": "1.26.52.3"}}
for _ in range(5):
    state, found = run(FakeStatus(uptime=8, answered=False), state)
check("no alert while uptime is under the settle window", not found,
      "eight seconds in, Bedrock has not opened its port yet")
check("and no streak is accumulated", not state.get("fail_streak"))

print("\nRakNet going silent raises ONE critical")
state = {"alerted": {"version_seen": "1.26.52.3"}}
kinds = []
for _ in range(8):
    state, found = run(FakeStatus(answered=False), state)
    kinds += [f[0] for f in found]
dead = [k for k in kinds if k == "minecraft_raknet_dead"]
check("fires after the streak threshold", len(dead) >= 1, f"fired {len(dead)}x")
check("fires exactly once, not every pass", len(dead) == 1,
      "a critical that arrives 48 times a day is one nobody reads")
sev = "critical"
state2, found2 = run(FakeStatus(answered=False),
                     {"alerted": {"version_seen": "1.26.52.3"},
                      "fail_streak": mt.FAIL_STREAK_FOR_ALERT - 1})
check("severity is critical, not info",
      any(f[1] == sev for f in found2 if f[0] == "minecraft_raknet_dead"))
msg = next((f[2] for f in found2 if f[0] == "minecraft_raknet_dead"), "")
check("the message warns that LAN can mask it", "LAN" in msg,
      "otherwise it looks fine from the sofa")
check("the message names BedrockConnect", "BedrockConnect" in msg)

print("\na stopped server is not a fault")
state, found = run(FakeStatus(running=False, answered=False),
                   {"alerted": {"version_seen": "1.26.52.3"}, "fail_streak": 2})
check("no findings", not found)
check("the streak is reset", state.get("fail_streak") == 0)

print("\na version change asks a human to re-test")
state, found = run(FakeStatus(), {"alerted": {"version_seen": "1.26.50.1"}})
upd = [f for f in found if f[0] == "minecraft_updated"]
check("raises minecraft_updated", len(upd) == 1, [f[0] for f in found])
check("names both versions",
      "1.26.50.1" in upd[0][2] and "1.26.52.3" in upd[0][2])
check("says to check the FRIENDS tab", "FRIENDS" in upd[0][2].upper())
check("warns that the LAN entry proves nothing", "proves nothing" in upd[0][2],
      "LAN discovery works on both transports")
check("it is info, not critical", upd[0][1] == "info")
state, found = run(FakeStatus(), state)
check("and not repeated on the next pass",
      not [f for f in found if f[0] == "minecraft_updated"])

print("\nfirst ever run does not claim an update")
state, found = run(FakeStatus(), {})
check("no spurious update alert with no previous version",
      not [f for f in found if f[0] == "minecraft_updated"])
check("but the version is remembered",
      state["alerted"]["version_seen"] == "1.26.52.3")

print("\nNetherNet becoming pingable is reported as good news")
fake = FakeStatus(transport="nethernet", answered=True)
state, found = run(fake, {"alerted": {"version_seen": "1.26.52.3"}})
check("raises minecraft_nethernet_pingable",
      [f for f in found if f[0] == "minecraft_nethernet_pingable"],
      [f[0] for f in found])
fake = FakeStatus(transport="nethernet", answered=False)
state, found = run(fake, {"alerted": {"version_seen": "1.26.52.3"}})
check("silent when NetherNet is blind, as expected", not found)
check("no raknet_dead alert while on nethernet",
      not [f for f in found if f[0] == "minecraft_raknet_dead"],
      "a nethernet server not answering is correct behaviour")

print("\nwiring: a watcher nobody starts reports healthy by saying nothing")
tree = ast.parse(open(os.path.join(BASE, "panel.py"), encoding="utf-8").read())
functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def calls_within(name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return {c.func.id for c in ast.walk(node)
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    return set()


check("_start_minecraft_transport_watch() exists",
      "_start_minecraft_transport_watch" in functions)
# The panel starts its workers in main(); agent.py is the one with
# start_services(). Checking the wrong name here would have passed forever
# while the watcher sat unstarted.
check("main() calls it",
      "_start_minecraft_transport_watch" in calls_within("main"),
      "otherwise the loop never runs")
check("it sits with the other background workers",
      {"_start_bedrock_dns", "_start_palworld_snapshots"}
      <= calls_within("main"),
      "if these moved, this test is checking a function that no longer "
      "starts anything")
inner = calls_within("_start_minecraft_transport_watch")
check("it persists its state", "_save_mc_transport_state" in inner or
      "_save_mc_transport_state" in calls_within("loop"),
      "dedupe across restarts depends on it")

src = open(os.path.join(BASE, "panel.py"), encoding="utf-8").read()
for kind in ("minecraft_updated", "minecraft_raknet_dead",
             "minecraft_nethernet_pingable", "minecraft_raknet_ok_again"):
    check(f"{kind} has a readable title in TITLES", f'"{kind}":' in src,
          "it would post as a raw key otherwise")

print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
sys.exit(1 if failures else 0)
