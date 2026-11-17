"""
Prove the Guardian 'Clear login' button only kicks a player who is logged in.

Never contacts Discord or a server.

Run:  python scripts\\test_clear_login.py
"""

import asyncio
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import control                                            # noqa: E402
from core.discord_bot import (ControlPanelView, GuardianBot, OnlineView,  # noqa: E402
                              RescueView, ServerControlView, UnstickView)

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got} (expected {want})")
    if not ok:
        FAILED.append(label)


print("Parsing ListPlayers:")
out = "0. Player3, 00028f47060544019508a37690637d6e\n1. Some Body, 0002ea87df624442aa13d5a768ad0075"
players = control.parse_player_ids(out)
check("two players with ids", [p["name"] for p in players], ["Player3", "Some Body"])
check("id kept exactly", players[0]["id"], "00028f47060544019508a37690637d6e")
check("'No Players Connected' is empty", control.parse_player_ids("No Players Connected"), [])
check("garbled line is NOT guessed at", control.parse_player_ids("0. Weird line"), [])


SIX = [{"name": n, "display_name": d} for n, d in [
    ("island", "The Island"), ("svartalfheim", "Svartalfheim"), ("nyrandil", "Scorched Earth"),
    ("astraeos", "Astraeos"), ("lostcolony", "Lost Colony"), ("genesis", "Genesis Part 1")]]


async def views():
    # discord.py views need a running loop to be constructed.
    kicked = []
    g = GuardianBot("Guardian", "", 1, 1, servers_fn=lambda: SIX,
                    action_fn=lambda *a: {}, job_fn=None, update_fn=None, logger=lambda m: None,
                    players_fn=lambda name: players if name == "astraeos" else [],
                    kick_fn=lambda name, pid: kicked.append((name, pid)) or {"ok": True, "name": "x"})

    print("\nThe control panel with all six maps:")
    panel = ControlPanelView(g, g.servers_fn())
    ids = [getattr(c, "custom_id", None) for c in panel.children]
    check("fits Discord's 25-component limit", len(panel.children) <= 25, True)
    check("every row is within 5 components",
          max(sum(1 for c in panel.children if c.row == r) for r in range(5)) <= 5, True)
    check("has a map picker", "guardian:ctl:pick" in ids, True)
    check("picker lists all six maps", len(panel.children[ids.index("guardian:ctl:pick")].options), 6)
    check("has the online-players button", "guardian:ctl:players:all" in ids, True)

    print("\nPer-map controls (opened from the picker):")
    one = ServerControlView(g, "astraeos", "Astraeos")
    labels = [c.label for c in one.children]
    check("start/restart/stop/save/clear login", labels,
          ["Start Astraeos", "Restart", "Stop", "Save", "Clear login"])

    print("\nOnline list and disconnect:")
    rows, problems = await g.online_now()
    check("finds the connected player", [(r[1], r[2]["name"]) for r in rows],
          [("Astraeos", "Player3"), ("Astraeos", "Some Body")])
    check("quiet maps are not reported as problems", problems, [])
    online = OnlineView(g, rows)
    check("a button per player, showing name and map",
          [c.label for c in online.children],
          ["Player3 - Astraeos", "Some Body - Astraeos"])
    check("stale list expires", online.timeout, 120)
    check("nothing kicked just by listing", kicked, [])

    print("\nThe panel embed names who is online:")
    g.status_fn = lambda: {"servers": [{"name": "astraeos", "state": "online"}]}
    embed = g.control_embed(rows, problems)
    field = [f for f in embed.fields if f.name == "Online now"]
    check("embed has an Online now section", bool(field), True)
    check("it names the player", "Player3" in (field[0].value if field else ""), True)

    print("\nRescuing a character from a saved copy:")
    saved = [
        {"id": "00028f47060544019508a37690637d6e", "name": "Player3",
         "versions": [{"name": "20260916-132512__7327224b", "when": "2026-09-16T13:25:12",
                       "size": 7327224},
                      {"name": "20260916-130513__9414b", "when": "2026-09-16T13:05:13",
                       "size": 9414}]},
        {"id": "0002ea87df624442aa13d5a768ad0075", "name": None,
         "versions": [{"name": "20260911-155644__0b", "when": "2026-09-11T15:56:44", "size": 0}]},
    ]
    g.characters_fn = lambda: saved
    g.rescue_fn = lambda account, version: {"ok": True}
    panel_ids = [getattr(c, "custom_id", None) for c in ControlPanelView(g, g.servers_fn()).children]
    check("panel has the rescue button", "guardian:ctl:rescue:all" in panel_ids, True)
    view = RescueView(g, [c for c in saved if any(v["size"] > 0 for v in c["versions"])])
    options = view.children[0].options
    check("only characters with a real copy are offered", [o.label for o in options], ["Player3"])
    check("it shows when that copy was taken", "2026-09-16 13:25" in options[0].description, True)
    check("stale list expires", view.timeout, 180)

    v = UnstickView(g, "svartalfheim", "Svartalfheim", players)
    check("select lists names, not ids", [o.label for o in v.children[0].options],
          ["Player3", "Some Body"])

asyncio.run(views())

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed")
