"""
Guardian's live embed for the PC that has no watcher of its own.

His PC runs Astraeos, Lost Colony and Genesis but no watcher bot: a Discord
token allows one gateway session, and his agent holds none. Guardian already
has a session and already reaches his agent over Tailscale, so it keeps a third
embed in #cluster-status beside the two watchers'.

What this pins down:
  * the embed matches a watcher's layout, because both call the same builders
  * an unreachable agent reads as "unknown", never as "Offline" -- an ARK map
    full of people must not be advertised as down because Tailscale blipped
  * RCON is asked once per RUNNING map and not at all of a stopped one

Run:  python scripts\\test_remote_status.py
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import discord_bot as db                                   # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got} (expected {want})")
    if not ok:
        FAILED.append(label)


def status(running=True, players_port=7100, uptime=7200, ram=9.2, cpu=12.0):
    return {"running": running, "host": "HUBMASTER", "uptime_seconds": uptime,
            "private_gb": ram, "memory_gb": 1.1, "cpu_percent": cpu,
            "port_health": {"bound": players_port, "state": "ok",
                            "range": (7100, 7199)}}


SYSTEM = {"cpu_percent": 31.0, "memory_used_gb": 12.4, "memory_total_gb": 32,
          "disk": {"free_gb": 400.0, "total_gb": 900.0}}


def fields(embed):
    return [(f.name, f.value) for f in embed.fields]


print("All three of his maps up:")
rows = [("Astraeos", status(), 2), ("Lost Colony", status(), 1),
        ("Genesis Part 1", status(), 0)]
embed = db.live_status_embed(rows, SYSTEM, "HUBMASTER", 60)
check("a field per map, then host health",
      [n for n, _ in fields(embed)],
      ["Astraeos", "Lost Colony", "Genesis Part 1", ":desktop: Host PC Health"])
check("green while all are up", embed.colour.value, db.COLOUR_OK)
check("players summed", "Players online: 3" in embed.footer.text, True)
check("says whose PC it is", "HUBMASTER" in embed.footer.text, True)
check("refresh interval is the remote one", "every 60s" in embed.footer.text, True)

print("\nOne map stopped:")
embed = db.live_status_embed([("Astraeos", status(), 2),
                              ("Lost Colony", status(running=False), 0)],
                             SYSTEM, "HUBMASTER", 60)
check("amber, not green", embed.colour.value, db.COLOUR_WARN)
check("the stopped one reads Offline", fields(embed)[1][1], ":red_circle: Offline")

print("\nHis agent unreachable (Tailscale down, PC asleep, agent restarting):")
embed = db.live_status_embed([("Astraeos", {"agent_error": "cannot reach agent"}, None)],
                             None, "HUBMASTER", 60)
text = fields(embed)[0][1]
check("NOT reported as Offline", "Offline" in text, False)
check("says the status is unknown", "Status unknown" in text, True)
check("names the reason", "cannot reach agent" in text, True)
check("player total is honest about not knowing",
      "Players online: unknown" in embed.footer.text, True)

print("\nA map that did not answer RCON:")
embed = db.live_status_embed([("Astraeos", status(), None),
                              ("Genesis Part 1", status(), 4)], SYSTEM, "", 60)
check("footer is a lower bound", "Players online: 4+" in embed.footer.text, True)

print("\nWhat the panel gathers (agent calls stubbed):")
CALLS = []


class FakeConfig(dict):
    pass


def fake_agent_get(server, path, **_params):
    CALLS.append((server["name"], path))
    if path == "/api/status":
        return ({"system": SYSTEM,
                 "servers": [{"name": "astraeos", **status()},
                             {"name": "lostcolony", **status()},
                             {"name": "genesis", **status(running=False)}]}, None)
    return ({"count": 2}, None)


# The gatherer, lifted out of panel.py's import chain: panel.py starts a web
# app on import, so the logic is exercised through the same shape of inputs
# rather than by importing the module.
def gather(servers, wanted, agent_get):
    hosts, rows, system, host_name = {}, [], None, ""
    for server in [s for s in servers if s["name"] in set(wanted)]:
        key = server.get("host") or server["name"]
        if key not in hosts:
            hosts[key] = agent_get(server, "/api/status")
        payload, error = hosts[key]
        label = server.get("display_name", server["name"])
        if error:
            rows.append((label, {"agent_error": error}, None))
            continue
        system = system or payload.get("system")
        st = next((s for s in payload.get("servers", [])
                   if s["name"] == server["name"]), None) or {}
        host_name = host_name or st.get("host") or server.get("host", "")
        players = 0
        if st.get("running"):
            who, who_error = agent_get(server, f"/api/server/{server['name']}/players")
            players = None if who_error else (who or {}).get("count")
        rows.append((label, st, players))
    return {"rows": rows, "system": system, "host": host_name}


SERVERS = [{"name": "island", "display_name": "The Island", "host": "HOME-SERVER-1"},
           {"name": "astraeos", "display_name": "Astraeos", "host": "HUBMASTER"},
           {"name": "lostcolony", "display_name": "Lost Colony", "host": "HUBMASTER"},
           {"name": "genesis", "display_name": "Genesis Part 1", "host": "HUBMASTER"}]

out = gather(SERVERS, ["astraeos", "lostcolony", "genesis"], fake_agent_get)
check("only his maps are reported", [r[0] for r in out["rows"]],
      ["Astraeos", "Lost Colony", "Genesis Part 1"])
check("one /api/status for the whole machine, not one per map",
      len([c for c in CALLS if c[1] == "/api/status"]), 1)
check("RCON asked only of the two running maps",
      sorted(c[0] for c in CALLS if c[1].endswith("/players")),
      ["astraeos", "lostcolony"])
check("the host's health came back", out["system"] is not None, True)

print("\nHis whole PC unreachable:")
CALLS.clear()
out = gather(SERVERS, ["astraeos", "lostcolony", "genesis"],
             lambda s, p, **k: (CALLS.append((s["name"], p)), (None, "agent asleep"))[1])
check("one attempt, not three", len(CALLS), 1)
check("every map reads unknown rather than offline",
      all(r[1].get("agent_error") for r in out["rows"]), True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed - Discord was never contacted")
