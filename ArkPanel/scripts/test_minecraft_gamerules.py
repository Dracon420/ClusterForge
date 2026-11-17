"""Gamerules have to behave like any other setting, or they are a trap.

Half of what makes Minecraft easier or harder is not in server.properties -
keepinventory, falldamage, mobgriefing and the rest live inside the world and
are reachable only through the server's stdin. Until this existed, the panel
could edit server.properties flawlessly while the settings that actually
decided whether a six-year-old enjoyed herself needed a human with an
elevated PowerShell window.

The failure modes worth pinning:

  * A misspelled gamerule must be REFUSED. Bedrock accepts "gamerule
    keepinvetory true" without complaining, so a typo would be reported as
    applied and silently do nothing - the same class of bug as the 7 Days
    preset that targeted thirty settings which had been moved years earlier.
  * Reading must report VALUES, not the presence of names. Every gamerule
    exists in level.dat whether or not anyone set it; I once concluded the
    QOL rules had never applied because I checked for the name.
  * Every preset key must be a real setting on the live server, or applying
    the preset half-works and the panel's own unknown-key guard rejects it.
  * Minecraft must declare settings_need_restart, and the panel must act on
    it. Bedrock reads server.properties once at startup, so a save without a
    restart is written correctly and changes nothing - indistinguishable from
    a broken save, and the exact confusion that produced "the panel says
    survival but she spawned in creative".
"""
import ast
import os
import struct
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import minecraft_gamerules as gr  # noqa: E402
from core import minecraft_schema  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


# ---------------------------------------------------------------- reading
def fake_level_dat(path, values):
    """A believable level.dat: LE-NBT tags, byte for bools, int for numbers."""
    blob = b"\x08\x00\x00"                       # junk header, as the real one has
    for name, value in values.items():
        raw = name.encode()
        if isinstance(value, bool):
            blob += bytes([0x01]) + struct.pack("<H", len(raw)) + raw + bytes([1 if value else 0])
        else:
            blob += bytes([0x03]) + struct.pack("<H", len(raw)) + raw + struct.pack("<i", value)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(blob)


tmp = tempfile.mkdtemp(prefix="mcrules-")
world = "Dracon"
fake_level_dat(os.path.join(tmp, "worlds", world, "level.dat"), {
    "keepinventory": True, "falldamage": False, "mobgriefing": False,
    "randomtickspeed": 3, "playerssleepingpercentage": 100,
})

print("reading values, not names")
got = gr.read(tmp, world)
check("a true bool reads as true", got.get("keepinventory") == "true", got.get("keepinventory"))
check("a false bool reads as FALSE, not as present",
      got.get("falldamage") == "false", got.get("falldamage"))
check("an int reads as its number", got.get("randomtickspeed") == "3")
check("a rule that is not stored is omitted, not defaulted",
      "doinsomnia" not in got,
      "an invented default would be written back as if someone chose it")
check("a missing world is empty, not an exception", gr.read(tmp, "NoSuchWorld") == {})

# ------------------------------------------------------------ validation
print("\na misspelled rule is refused, not silently applied")
try:
    gr.commands({"gamerule.keepinvetory": "true"})
    check("raises on an unknown rule name", False, "it was accepted")
except ValueError as exc:
    check("raises on an unknown rule name", True, str(exc))
try:
    gr.commands({"gamerule.randomtickspeed": "fast"})
    check("raises on a non-number for an int rule", False, "accepted 'fast'")
except ValueError as exc:
    check("raises on a non-number for an int rule", True, str(exc))

print("\nvalues are normalised to what the console expects")
check("true/on/1/yes all become true",
      all(gr.normalise("keepinventory", v) == "true"
          for v in ("true", "TRUE", "on", "1", "yes")))
check("anything else becomes false",
      gr.normalise("keepinventory", "nope") == "false")
check("ints are coerced to whole numbers",
      gr.normalise("randomtickspeed", "3.0") == "3")
check("commands come out in Bedrock's syntax",
      gr.commands({"gamerule.keepinventory": "true"}) == ["gamerule keepinventory true"])
check("the prefix is optional on input",
      gr.commands({"keepinventory": "true"}) == ["gamerule keepinventory true"])

# --------------------------------------------------------------- presets
print("\nevery preset targets settings that actually exist")
props = set()
for _group, rows in minecraft_schema.GROUPS:
    props |= {key for key, _l, _k, _h in rows}
rule_keys = {gr.PREFIX + name for name in gr.RULES}

for pid, preset in minecraft_schema.PRESETS.items():
    unknown = sorted(k for k in preset["values"]
                     if k not in props and k not in rule_keys)
    check(f"{pid}: all keys are known", not unknown, f"unknown: {unknown}")
    check(f"{pid}: has a label and a reason",
          bool(preset.get("label")) and bool(preset.get("why")))

print("\nthe child presets differ only in the things they should")
child_c = minecraft_schema.PRESETS["creative_child"]["values"]
child_s = minecraft_schema.PRESETS["survival_child"]["values"]
check("creative preset sets creative", child_c["gamemode"] == "creative")
check("survival preset sets survival", child_s["gamemode"] == "survival")
check("both force the mode on join, so nobody has to type anything",
      child_c.get("force-gamemode") == "true" and child_s.get("force-gamemode") == "true")
check("both keep mobs (difficulty is not peaceful)",
      child_c["difficulty"] != "peaceful" and child_s["difficulty"] != "peaceful",
      "she asked for mobs; peaceful deletes them")
check("both keep the inventory on death",
      child_c["gamerule.keepinventory"] == "true"
      and child_s["gamerule.keepinventory"] == "true")
check("only the survival one bothers turning off accidental-death damage",
      "gamerule.falldamage" in child_s and "gamerule.falldamage" not in child_c,
      "in creative she is invulnerable anyway")
check("the ordinary-survival preset puts the softening back",
      minecraft_schema.PRESETS["survival_normal"]["values"]["gamerule.keepinventory"] == "false")

# ------------------------------------------------------------- the panel
print("\nthe adapter declares what it needs and the panel acts on it")
from core.games.minecraft_game import MinecraftBedrockGame  # noqa: E402

caps = MinecraftBedrockGame.capabilities
check("settings_need_restart is declared", caps.settings_need_restart,
      "without it a save is written and does nothing")
check("settings_need_stop stays FALSE",
      not caps.settings_need_stop,
      "Bedrock does not rewrite its config on shutdown - that is Palworld")

tree = ast.parse(open(os.path.join(BASE, "panel.py"), encoding="utf-8").read())
src = open(os.path.join(BASE, "panel.py"), encoding="utf-8").read()
check("panel.py reads settings_need_restart", "settings_need_restart" in src,
      "declaring it means nothing if the panel ignores it")


def func(name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


setter = func("game_settings_set")
check("game_settings_set() exists", setter is not None)
if setter:
    body = ast.get_source_segment(src, setter) or ""
    check("it cycles for need_restart as well as need_stop",
          "need_restart" in body and "need_stop" in body)
    check("it restarts the server after a failed apply",
          body.count("game.start(server)") >= 2,
          "a server left off because a save failed is worse than the old value")

print("\ngamerules do not leak into the properties screen twice")
described = minecraft_schema.describe({"gamemode": "creative",
                                       "gamerule.keepinventory": "true"})
everything_else = [g for g in described if g["group"] == "Everything else"]
leaked = [f["key"] for g in everything_else for f in g["fields"]
          if f["key"].startswith(gr.PREFIX)]
check("a prefixed key is not dumped into 'Everything else'", not leaked, leaked)

print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
sys.exit(1 if failures else 0)
