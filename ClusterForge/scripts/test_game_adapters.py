"""Adding a game must not require editing panel.py.

That is the whole point of core/games. Before it, `palworld` appeared 136
times in panel.py: a second game bolted alongside the first, with its own
endpoints, its own UI branch and its own path through every control call.
A third game added the same way costs another 136, a fourth another, and by
the fifth nobody can change anything without breaking two games they were not
touching.

So this test registers a FAKE game that panel.py has never heard of, and
drives the generic API against it. If that works, the interface holds. If it
needs a special case in panel.py to pass, the interface is wrong - and the
fix belongs in core/games/base.py, not in another branch out there.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import games                                    # noqa: E402
from core.games.base import Capabilities, Game, ServerStatus   # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r} (expected {want!r})")
    if not ok:
        FAILED.append(label)


# --------------------------------------------------- a game nobody wrote for
class FakeSurvivalGame(Game):
    """Stands in for the next game someone adds - Valheim, 7 Days, V Rising.

    Written only against base.py. It touches no panel code and no existing
    game module, which is exactly the position a new contributor is in.
    """
    id = "fakesurvival"
    name = "Fake Survival Game"
    short = "Fake"
    steam_app_id = "999999"
    capabilities = Capabilities(rcon=True, settings=True, player_list=True,
                                settings_need_stop=True)

    def __init__(self, config, secrets, log=print):
        super().__init__(config, secrets, log)
        self.state = {"running": False, "settings": {"Difficulty": "Normal",
                                                     "DropLoot": "True"}}
        self.calls = []

    def status(self, server):
        return ServerStatus(running=self.state["running"], pid=4242,
                            uptime_seconds=60, players=[], port=1234)

    def start(self, server, **kw):
        self.calls.append("start")
        self.state["running"] = True
        return {"ok": True}

    def stop(self, server, **kw):
        self.calls.append("stop")
        self.state["running"] = False
        return {"ok": True}

    def read_settings(self, server):
        return dict(self.state["settings"])

    def write_settings(self, server, changes):
        self.calls.append("write")
        applied = {}
        for key, value in changes.items():
            before = self.state["settings"].get(key)
            if before != value:
                self.state["settings"][key] = value
                applied[key] = (before, value)
        return applied

    def describe_settings(self, server):
        return [{"group": "Fake", "fields": [
            {"key": k, "label": k, "kind": "text", "help": "", "value": v,
             "locked": False, "locked_why": ""}
            for k, v in self.state["settings"].items()]}]

    def presets(self):
        return {"easy": {"label": "Easy", "why": "test",
                         "values": {"DropLoot": "False"}}}


print("The registry takes a game it has never seen:")
games.REGISTRY[FakeSurvivalGame.id] = FakeSurvivalGame
try:
    built = games.build(FakeSurvivalGame.id, {}, {}, log=lambda *a: None)
    check("it can be built", isinstance(built, FakeSurvivalGame), True)
    check("it describes itself", built.describe()["short"], "Fake")
    check("capabilities are reported",
          built.describe()["capabilities"]["settings_need_stop"], True)

    print()
    print("panel.py contains no mention of it:")
    panel = open(os.path.join(BASE, "panel.py"), encoding="utf-8").read()
    check("no special case in panel.py", FakeSurvivalGame.id in panel, False)
    check("panel.py does not import any game module directly",
          "from core.games.palworld_game" in panel
          or "from core.games.ark_game" in panel, False)

    print()
    print("The generic endpoints are game-agnostic:")
    for route in ("/api/games/<game_id>/servers/<name>/status",
                  "/api/games/<game_id>/servers/<name>/<action>",
                  "/api/games/<game_id>/servers/<name>/settings"):
        check(f"panel.py serves {route}", route in panel, True)

    print()
    print("A game that rewrites its config on shutdown is stopped first:")
    # The trap this generalises: Palworld rewrites PalWorldSettings.ini from
    # memory when it shuts down, so a write while running is silently undone.
    fn = panel[panel.index("def game_settings_set"):]
    fn = fn[:fn.index("@app.", 10)]
    check("it consults settings_need_stop", "settings_need_stop" in fn, True)
    check("it stops before writing",
          fn.index("game.stop") < fn.index("write_settings", fn.index("game.stop")),
          True)
    check("and starts again after",
          fn.index("write_settings", fn.index("game.stop"))
          < fn.index("game.start", fn.index("game.stop")), True)

    print()
    print("Real games still answer:")
    for gid, short in (("ark", "ARK"), ("palworld", "Palworld")):
        g = games.build(gid, {}, {}, log=lambda *a: None)
        check(f"{gid} builds", g.short, short)
    try:
        games.build("definitely-not-a-game", {}, {}, log=lambda *a: None)
        raised = None
    except KeyError:
        raised = "KeyError"
    except Exception as exc:  # noqa: BLE001
        raised = type(exc).__name__
    check("an unknown game raises rather than guessing", raised, "KeyError")
finally:
    games.REGISTRY.pop(FakeSurvivalGame.id, None)

print()
print("all checks passed" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}")
sys.exit(1 if FAILED else 0)
