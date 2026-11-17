"""Bedrock gamerules, readable and writable from the panel.

Half of what makes Minecraft easier or harder is not in server.properties at
all. keepinventory, falldamage, mobgriefing and the rest are GAMERULES, which
live inside the world itself, and Bedrock offers no way to set them except a
player typing in chat or something talking to the server's stdin.

That split is invisible from outside and it bites: the panel could edit
server.properties perfectly while the settings that actually decide whether a
six-year-old has fun were unreachable, and the only way to change them was a
human with an elevated PowerShell window.

So this module gives gamerules the same shape as any other setting:

    reading   parses the values straight out of the world's level.dat
    writing   turns them into "gamerule x y" console commands

and core/games/minecraft_game.py exposes them under a "gamerule." prefix, so
the panel's existing settings screen, its preset mechanism and its
unknown-setting guard all work on them with no new endpoints.

WHY READ FROM level.dat: every gamerule is present in that file whether or
not anyone set it, so checking whether the NAME appears proves nothing. I
made exactly that mistake and concluded the QOL rules had never applied when
in fact they had. Only the value is evidence. level.dat is little-endian NBT
behind a short header, and a tag is type(1) + name_length(2, LE) + name +
payload, which is enough to read values without an NBT library and without
ever writing to the file.
"""

from __future__ import annotations

import os
import struct

# name -> (label, kind, help). The kind matches the panel's own vocabulary so
# the generic settings UI renders these exactly like server.properties rows.
#
# Deliberately a curated subset, not every gamerule Bedrock has. This is the
# screen a parent uses while a child waits; a list of forty toggles is worse
# than a list of twenty useful ones. Anything missing can still be sent as a
# console command.
RULES: dict[str, tuple[str, str, str]] = {
    # --- the ones that decide how forgiving the world is ------------------
    "keepinventory": (
        "Keep items on death", "bool",
        "Dropping everything on death is the single most upsetting thing in "
        "survival for a young child. On means they keep it all."),
    "doimmediaterespawn": (
        "Respawn instantly", "bool",
        "Skips the death screen entirely - no menu to read, they are just "
        "back."),
    "falldamage": ("Fall damage", "bool",
                   "The most common accidental death."),
    "drowningdamage": ("Drowning damage", "bool", "The second most common."),
    "firedamage": ("Fire and lava damage", "bool", "The third."),
    "freezedamage": ("Powder snow freezing damage", "bool", ""),
    "naturalregeneration": (
        "Heal from a full food bar", "bool",
        "On is normal Minecraft. Off means healing only from potions and "
        "golden apples."),

    # --- what the world is allowed to do to their things ------------------
    "mobgriefing": (
        "Mobs can change blocks", "bool",
        "Off stops creepers blowing holes in their house and endermen "
        "carrying blocks away. Mobs still exist and still fight."),
    "dofiretick": ("Fire spreads", "bool",
                   "Off means a stray fire stays where it is."),
    "tntexplodes": ("TNT explodes", "bool", ""),
    "respawnblocksexplode": (
        "Beds explode in the Nether", "bool",
        "Off removes a classic way for a child to die without warning."),
    "pvp": ("Players can hurt each other", "bool",
            "Off is the obvious choice when a parent and child play together."),

    # --- mobs and the world clock -----------------------------------------
    "domobspawning": (
        "Hostile mobs spawn", "bool",
        "A softer alternative to peaceful difficulty: mobs already placed "
        "stay, but no new ones appear."),
    "domobloot": ("Mobs drop items", "bool", ""),
    "doentitydrops": ("Broken things drop items", "bool", ""),
    "dotiledrops": (
        "Blocks drop when broken", "bool",
        "Off means mining gives nothing. Rarely what anyone wants."),
    "dodaylightcycle": (
        "Day and night pass", "bool",
        "Off freezes the clock. Leave on if they want real nights."),
    "doweathercycle": ("Weather changes", "bool", ""),
    "doinsomnia": (
        "Phantoms appear", "bool",
        "Phantoms arrive after three nights without sleep and attack from "
        "above. Off is the kindest single change for a small child."),

    # --- help on screen ---------------------------------------------------
    "showcoordinates": (
        "Show coordinates on screen", "bool",
        "Lets a child say where they are, and lets you find them. Also the "
        "easiest way for them to get home."),
    "showdeathmessages": ("Announce deaths in chat", "bool", ""),
    "showdaysplayed": ("Show days played", "bool", ""),
    "dolimitedcrafting": (
        "Only craft discovered recipes", "bool",
        "OFF means every recipe is available immediately, and the in-game "
        "recipe book will build things for them. Leave this off for anyone "
        "who cannot look recipes up."),
    "sendcommandfeedback": ("Show command results in chat", "bool", ""),

    # --- numbers ----------------------------------------------------------
    "randomtickspeed": (
        "World tick speed", "int",
        "How fast crops, saplings and grass grow. Bedrock ships 1; Java "
        "Edition's own default is 3, which makes a farm pay off in one play "
        "session. Large numbers will hurt this machine."),
    "playerssleepingpercentage": (
        "Percent who must sleep to skip night", "int",
        "100 means everyone. Set it to 1 so one person sleeping skips the "
        "night for all."),
    "spawnradius": ("Respawn scatter (blocks)", "int",
                    "0 puts them back on exactly the same spot."),
}

PREFIX = "gamerule."

# Which NBT tag type holds each kind.
_BYTE, _INT = 0x01, 0x03


def level_dat(install_dir: str, level_name: str) -> str:
    return os.path.join(install_dir, "worlds", level_name, "level.dat")


def read(install_dir: str, level_name: str) -> dict[str, str]:
    """Current gamerule values, as strings, keyed WITHOUT the prefix.

    A rule that cannot be found is left out rather than guessed at - an
    absent key is honest, a defaulted one is a fabrication that would then
    be written back.
    """
    path = level_dat(install_dir, level_name)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return {}

    out: dict[str, str] = {}
    for name, (_label, kind, _help) in RULES.items():
        raw = name.encode()
        want = _INT if kind == "int" else _BYTE
        # Try the expected type first, then the other: Mojang has moved a
        # rule between byte and int before, and a wrong guess here would
        # silently report nonsense.
        for tag_type in (want, _INT if want == _BYTE else _BYTE):
            pattern = bytes([tag_type]) + struct.pack("<H", len(raw)) + raw
            idx = data.find(pattern)
            if idx < 0:
                continue
            at = idx + len(pattern)
            try:
                if tag_type == _BYTE:
                    out[name] = "true" if data[at] else "false"
                else:
                    out[name] = str(struct.unpack_from("<i", data, at)[0])
            except (IndexError, struct.error):
                pass
            break
    return out


def normalise(name: str, value) -> str:
    """The text Bedrock's console expects for one rule."""
    kind = RULES[name][1]
    text = str(value).strip()
    if kind == "bool":
        return "true" if text.lower() in ("1", "true", "yes", "on") else "false"
    try:
        return str(int(float(text)))
    except ValueError as exc:
        raise ValueError(f"{name} needs a whole number, not {text!r}") from exc


def commands(changes: dict) -> list[str]:
    """Turn {name_or_prefixed_name: value} into console commands.

    Unknown names raise rather than being passed through. Bedrock accepts a
    misspelled gamerule without complaining, so a typo would otherwise look
    like a saved setting that quietly does nothing - the same failure that
    let a 7 Days preset target thirty settings that no longer existed.
    """
    out = []
    for key, value in changes.items():
        name = key[len(PREFIX):] if key.startswith(PREFIX) else key
        if name not in RULES:
            raise ValueError(f"no such gamerule: {name}")
        out.append(f"gamerule {name} {normalise(name, value)}")
    return out


def describe(current: dict[str, str]) -> list[dict]:
    """Panel settings groups for the gamerules we could read."""
    groups = [
        ("Gamerules - how forgiving the world is",
         ["keepinventory", "doimmediaterespawn", "falldamage",
          "drowningdamage", "firedamage", "freezedamage",
          "naturalregeneration", "pvp"]),
        ("Gamerules - protecting what they build",
         ["mobgriefing", "dofiretick", "tntexplodes",
          "respawnblocksexplode"]),
        ("Gamerules - mobs, time and weather",
         ["domobspawning", "doinsomnia", "domobloot", "doentitydrops",
          "dotiledrops", "dodaylightcycle", "doweathercycle"]),
        ("Gamerules - help on screen",
         ["showcoordinates", "dolimitedcrafting", "showdeathmessages",
          "showdaysplayed", "sendcommandfeedback"]),
        ("Gamerules - numbers",
         ["randomtickspeed", "playerssleepingpercentage", "spawnradius"]),
    ]
    out = []
    for title, names in groups:
        fields = []
        for name in names:
            if name not in current:
                continue
            label, kind, help_text = RULES[name]
            fields.append({"key": PREFIX + name, "label": label, "kind": kind,
                           "help": help_text, "value": current[name],
                           "locked": False, "locked_why": ""})
        if fields:
            out.append({"group": title, "fields": fields})
    return out


def compare(before: dict[str, str], after: dict[str, str],
            presets: dict | None = None) -> dict:
    """What swapping one world for another silently did to the gamerules.

    Importing a world replaces level.dat, and the gamerules live INSIDE
    level.dat - so they travel with the world. The panel's settings screen
    shows server.properties and gamerules side by side as though they were
    one set of settings, which they are not: an import leaves the properties
    alone and silently reverts every gamerule to whatever the incoming world
    was saved with.

    That is not hypothetical. On 2026-10-24 a world was imported off an Xbox
    onto this server, bringing vanilla gamerules with it, and nobody noticed
    for five days: mobgriefing and dofiretick were back on, so creepers and
    fire were free to take apart a six-floor castle that had taken hours to
    build. The byte count was verified that day. The gamerules were not.

    `presets` is passed in rather than imported so this module keeps no
    dependency on the schema - and so a caller can ask about one preset.

    Returns a report, never raises. A world whose level.dat cannot be read
    yields compared=False, which is an honest "do not know" rather than an
    all-clear.
    """
    report = {
        "compared": bool(before) and bool(after),
        "readable": bool(after),
        "changed": {},
        "preset": "", "preset_matched": 0, "preset_total": 0,
        "gap": 0, "gap_keys": [],
    }
    if not after:
        return report

    if before:
        for name in sorted(set(before) | set(after)):
            was, now = before.get(name, "(absent)"), after.get(name, "(absent)")
            if was != now:
                report["changed"][name] = [was, now]

    if not presets:
        return report

    # Which preset the OUTGOING world looked like. Inferred from the world
    # that was working rather than assumed, because the panel does not
    # record which preset was last applied - and a preset applied months
    # ago then hand-tweaked is still the best description of "how it was".
    best, best_hits, best_total = "", -1, 0
    for pid, preset in presets.items():
        rules = {k[len(PREFIX):]: str(v)
                 for k, v in (preset.get("values") or {}).items()
                 if k.startswith(PREFIX)}
        if not rules:
            continue
        hits = sum(1 for n, v in rules.items() if before.get(n) == v)
        # Most rules matched wins; ties go to the preset that got the FEWEST
        # wrong. Ranking by size instead picks a big preset that matched
        # half of itself over a small one that matched all of itself, which
        # is the opposite of "this is what the world looked like".
        if (hits, hits - len(rules)) > (best_hits, best_hits - best_total):
            best, best_hits, best_total = pid, hits, len(rules)
    if not best:
        return report

    rules = {k[len(PREFIX):]: str(v)
             for k, v in presets[best]["values"].items()
             if k.startswith(PREFIX)}
    gap = sorted(n for n, v in rules.items() if after.get(n) != v)
    report.update({"preset": best, "preset_matched": max(best_hits, 0),
                   "preset_total": best_total,
                   "gap": len(gap), "gap_keys": gap})
    return report
