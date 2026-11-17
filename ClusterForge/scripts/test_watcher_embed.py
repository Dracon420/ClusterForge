"""
Prove the watcher embed reports every server on a machine correctly.

PC2 runs Svartalfheim and Nyrandil, and one watcher now reports both. This
builds the real embed with fake servers -- no Discord connection -- and checks
the things that would be embarrassing to get wrong in front of players:

  * each server gets its own field, under its own name (a lambda closing over a
    loop variable would make both report the LAST server)
  * the player count is asked ONCE per running server, not twice
  * RAM shows committed memory, not the flattering working set
  * the colour turns amber when one map is down, not green
  * the old one-server arguments still produce the same single-field embed

Run:  python scripts\\test_watcher_embed.py
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import discord_bot as db                                # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r} (expected {want!r})")
    if not ok:
        FAILED.append(label)


def status(name, running, private_gb=0.0, working_gb=0.0):
    return {"name": name, "running": running, "host": "ChaoticControl",
            "uptime_seconds": 3600, "cpu_percent": 12,
            "private_gb": private_gb, "memory_gb": working_gb,
            "port_health": {"bound": 8000, "state": "ok", "range": [8000, 8999]}}


def target(name, display, running, players, private_gb, working_gb, calls):
    def players_fn():
        calls[name] = calls.get(name, 0) + 1
        return players
    return {"name": name, "display_name": display,
            "status_fn": lambda: status(name, running, private_gb, working_gb),
            "players_fn": players_fn}


def embed_for(targets=None, **legacy):
    bot = db.WatcherBot("t", "token", 1, targets=targets,
                        system_fn=lambda: {"cpu_percent": 5, "memory_used_gb": 12.7,
                                           "memory_total_gb": 15.8},
                        logger=lambda m: None, **legacy)
    bot.build()                      # creates the client object; does not connect
    return bot._build_embed()


print("Two servers, both up:")
calls = {}
e = embed_for([target("svartalfheim", "Svartalfheim", True, 1, 4.2, 0.6, calls),
               target("nyrandil", "Nyrandil", True, 2, 9.22, 5.26, calls)])
names = [f.name for f in e.fields]
check("field names", names, ["Svartalfheim", "Nyrandil", ":desktop: Host PC Health"])
check("RCON asked once per server", calls, {"svartalfheim": 1, "nyrandil": 1})
check("Nyrandil RAM is committed, not working set", "9.22 GB" in e.fields[1].value, True)
check("working set not shown", "5.26" in e.fields[1].value, False)
check("players summed in footer", "Players online: 3 " in e.footer.text, True)
check("green when all up", e.colour.value, db.COLOUR_OK)

print("\nOne up, one down:")
calls = {}
e = embed_for([target("svartalfheim", "Svartalfheim", True, 1, 4.2, 0.6, calls),
               target("nyrandil", "Nyrandil", False, 0, 0, 0, calls)])
check("amber, not green", e.colour.value, db.COLOUR_WARN)
check("offline server shown as Offline", ":red_circle: Offline" in e.fields[1].value, True)
check("no RCON asked of a stopped server", calls, {"svartalfheim": 1})

print("\nA server whose RCON failed:")
e = embed_for([target("svartalfheim", "Svartalfheim", True, 2, 4.2, 0.6, {}),
               target("nyrandil", "Nyrandil", True, None, 9.2, 5.2, {})])
check("footer is an honest lower bound", "Players online: 2+ " in e.footer.text, True)

print("\nBoth down:")
e = embed_for([target("a", "A", False, 0, 0, 0, {}), target("b", "B", False, 0, 0, 0, {})])
check("red", e.colour.value, db.COLOUR_BAD)

print("\nLegacy single-server arguments (the Island's watcher on PC1):")
calls = {}
e = embed_for(server_name="island", display_name="The Island",
              status_fn=lambda: status("island", True, 11.3, 3.4),
              players_fn=lambda: (calls.__setitem__("island", calls.get("island", 0) + 1) or 1))
check("one server field + host", [f.name for f in e.fields],
      ["The Island", ":desktop: Host PC Health"])
check("RCON asked once (was twice)", calls, {"island": 1})
check("green", e.colour.value, db.COLOUR_OK)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed - Discord was never contacted")


# ------------------------------------------- down on purpose is not an outage
#
# The Island spends most of its life deliberately stopped so Palworld can have
# PC1's memory. Before this, that looked identical to a crash: red circle,
# "Offline", and an embed stuck permanently amber - so nobody could tell at a
# glance whether anything was actually wrong, and an amber that never clears
# is an amber nobody reads.
print()
print("A server stopped on purpose:")

_held = {"running": False, "maintenance": "switched to Palworld"}
_line = db.status_lines(_held, None)
check("does not say Offline", "Offline" not in _line, True)
check("is not a red circle", ":red_circle:" not in _line, True)
check("says why it is down", "switched to Palworld" in _line, True)
check("says nothing is wrong", "Nothing is wrong" in _line, True)

_name = {db.COLOUR_OK: "green", db.COLOUR_WARN: "amber",
         db.COLOUR_BAD: "red"}
def _colour(rows):
    return _name.get(db.live_status_embed(rows).colour.value, "?")

check("one map held for Palworld keeps the embed green",
      _colour([("a", {"running": True}, 0), ("b", dict(_held), None)]), "green")
check("a genuinely down map still turns it amber",
      _colour([("a", {"running": True}, 0), ("b", {"running": False}, None)]), "amber")
check("everything down for real is still red",
      _colour([("a", {"running": False}, None), ("b", {"running": False}, None)]), "red")
