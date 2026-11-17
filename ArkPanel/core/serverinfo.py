"""
Keep the Discord #<map>-server-info posts true.

Those channels are hand-written prose plus a pile of numbers, and the numbers
rot: the Island post advertised "Stack Size 100x" while the server actually ran
500, and listed mods that had been removed. Anything a human has to remember to
update after every change will drift, so the numbers are generated from the
live config and the prose stays hand-written.

Two rules shape this:

  The bot owns ONE message and edits it. It cannot edit the posts already there
  -- Discord does not let a bot edit a human's message -- so it posts its own
  and keeps editing that forever.

  The template is the user's. Everything between the {{placeholders}} is theirs
  to write; this module only fills the placeholders in. A regenerated post must
  never silently rewrite someone's welcome text.
"""

from __future__ import annotations

import re

# Where each advertised number really lives. Game.ini wins where a key appears
# in both files -- ItemStackSizeMultiplier is in BOTH (300 in GameUserSettings,
# 500 in Game.ini) and the engine takes Game.ini, which is exactly the sort of
# detail a hand-written post gets wrong.
GUS_KEYS = (
    "SessionName", "ServerPassword", "MaxPlayers",
    "XPMultiplier", "TamingSpeedMultiplier", "HarvestAmountMultiplier",
    "OverrideOfficialDifficulty", "DifficultyOffset",
    "ItemStackSizeMultiplier",
    "AllowThirdPersonPlayer", "ServerPVE", "ShowMapPlayerLocation",
)
GAME_KEYS = (
    "MatingIntervalMultiplier", "EggHatchSpeedMultiplier",
    "BabyMatureSpeedMultiplier", "BabyCuddleIntervalMultiplier",
    "BabyImprintAmountMultiplier", "BabyImprintingStatScaleMultiplier",
    "ItemStackSizeMultiplier",
    "bAutoUnlockAllEngrams", "bAllowSpeedLeveling", "bAllowFlyerSpeedLeveling",
)


def _scan(text: str, keys) -> dict:
    """Pull `Key=Value` lines. Last occurrence wins, matching engine behaviour
    for the single-valued settings listed above."""
    found = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#", "[")):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in keys:
            found[key] = value.strip()
    return found


def extract(gus_text: str, game_text: str) -> dict:
    """Merge both ini files into one flat view of the advertised settings."""
    values = _scan(gus_text, GUS_KEYS)
    game = _scan(game_text, GAME_KEYS)
    # Game.ini governs on conflict. Keep the loser visible so the panel can warn
    # about a post that quotes the wrong one.
    for key, value in game.items():
        if key in values and values[key] != value:
            values[f"{key}__gus"] = values[key]
        values[key] = value
    return values


def _num(raw):
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else f


def multiplier(raw, suffix="x") -> str:
    """1.0 -> '1x', 0.0001 -> '0.0001x'. Trailing '.0' on a rate reads badly in
    a post that is trying to advertise '100x taming'."""
    value = _num(raw)
    if value is None:
        return str(raw) if raw is not None else "?"
    return f"{value}{suffix}"


def mods_list(mods, style: str = "names") -> str:
    """Mods in LOAD ORDER, which is the order that matters and the order the
    launch line already stores them in."""
    names = []
    for mod in mods or []:
        name = (mod.get("name") or "").strip() or f"Mod {mod.get('mod_id') or '?'}"
        if style == "ids":
            names.append(f"{name} ({mod.get('mod_id')})")
        else:
            names.append(name)
    return ", ".join(names) if names else "none"


def loaded_only(mods) -> list:
    """Mods the server actually loads, not every folder left on disk.

    The agent's mod table also lists downloads no longer in -mods=: a map
    switch leaves the old map's mod behind, and a removed mod keeps its folder.
    Posting those told players Scorched Earth ran Amissa, Nyrandil and Club ARK.
    Rows without the flag are kept, so a plain list still renders.
    """
    return [m for m in (mods or []) if m.get("in_load_order", True)]


def key_mods(mods, key_ids, display_names=None) -> list:
    """The mods worth naming in an info post, in load order.

    'Key' is an editorial call -- CurseForge categories put Der Dino Finder
    under Creatures and Awesome ARK Tools under Weapons, so inferring it from
    metadata gets the wrong answer. The list is curated in
    data/serverinfo/key_mods.json and each map shows only what it actually runs.
    """
    wanted = {str(i) for i in (key_ids or [])}
    names = {str(k): v for k, v in (display_names or {}).items()
             if not k.startswith("_")}
    out = []
    for mod in (mods or []):
        mid = str(mod.get("mod_id"))
        if mid not in wanted:
            continue
        if mid in names:
            # Shorter label for the info post only. A mod whose CurseForge title
            # carries "(No Longer Supported)" should not eat 41 characters of a
            # 2000-character budget.
            mod = {**mod, "name": names[mid]}
        out.append(mod)
    return out


# Priority order for filing a mod that carries several categories, most
# specific first. Creatures sits near the top deliberately: CurseForge tags
# creature mods with Structures or General as well (Moro's Indomitable Duo is
# ['Creatures', 'Structures'], MarniiMods Griffins is ['Creatures', 'General'])
# and with Structures ranked higher a dino mod was filed under Structures,
# where nobody looking for a new dino would ever find it. If a mod adds
# creatures, that is the thing players care about.
CATEGORY_ORDER = ("Maps", "Creatures", "Structures", "Rebalancing", "General",
                  "Weapons", "Visuals and Sounds", "Game Modes")


def catalogue(mods_by_server: dict, key_ids=None) -> list[tuple[str, list]]:
    """Every mod in the cluster, grouped for the #mods-we-use channel.

    Each mod appears exactly once -- most carry several categories, and listing
    a mod three times is worse than filing it imperfectly.

    Which one wins is CATEGORY_ORDER's priority, NOT the order CurseForge
    happens to list them in. Going by the mod's own first category was tried
    and is worse: it puts "Cybers Structures QoL+" under Rebalancing and
    "Shiny! Dinos Ascended" under Visuals and Sounds.
    """
    merged = {}
    for server, mods in (mods_by_server or {}).items():
        for mod in loaded_only(mods):
            entry = merged.setdefault(str(mod.get("mod_id")), {**mod, "servers": []})
            entry["servers"].append(server)

    groups: dict[str, list] = {}
    for mod in merged.values():
        cats = mod.get("categories") or []
        bucket = next((c for c in CATEGORY_ORDER if c in cats),
                      cats[0] if cats else "Other")
        groups.setdefault(bucket, []).append(mod)

    ordered = []
    for name in CATEGORY_ORDER:
        if name in groups:
            ordered.append((name, sorted(groups.pop(name),
                                         key=lambda m: (m.get("name") or "").lower())))
    for name in sorted(groups):
        ordered.append((name, sorted(groups[name],
                                     key=lambda m: (m.get("name") or "").lower())))
    return ordered


def render_catalogue(groups, servers_label: dict | None = None) -> str:
    """Markdown for #mods-we-use: name as a link, who runs it, one-line summary."""
    labels = servers_label or {}
    out = []
    for category, mods in groups:
        out.append(f"\n**{category}**")
        for mod in mods:
            name = (mod.get("name") or "").strip() or f"Mod {mod.get('mod_id')}"
            url = mod.get("url")
            title = f"[{name}]({url})" if url else name
            on = mod.get("servers") or []
            # "both maps" was right with two servers and quietly wrong at three:
            # a mod on all of them, or on just two of three, got the same label.
            if not on:
                where = "?"
            elif labels and len(on) >= len(labels):
                where = "all maps"
            elif len(on) == 1:
                where = labels.get(on[0], on[0])
            else:
                where = " & ".join(labels.get(s, s) for s in on)
            line = f"• {title} — *{where}*"
            summary = (mod.get("summary") or "").strip()
            if summary:
                short = summary.split(". ")[0].strip().rstrip(".")
                if len(short) > 90:
                    short = short[:87].rstrip() + "..."
                line += f"\n  {short}"
            out.append(line)
    return "\n".join(out).strip()


def facts(server_cfg: dict, gus_text: str, game_text: str, mods,
          schedule: dict, build: str | None = None, key_ids=None,
          display_names=None) -> dict:
    """Everything a post can reference, as flat {{placeholder}} values."""
    mods = loaded_only(mods)
    values = extract(gus_text, game_text)

    days = schedule.get("restart_days") or []
    short = {"Monday": "Mon", "Tuesday": "Tue", "Wednesday": "Wed",
             "Thursday": "Thu", "Friday": "Fri", "Saturday": "Sat", "Sunday": "Sun"}
    day_text = " / ".join(short.get(d, d) for d in days) if days else "no fixed days"
    time_raw = schedule.get("restart_time") or ""
    try:
        hour, minute = (int(x) for x in time_raw.split(":"))
        ampm = "AM" if hour < 12 else "PM"
        hour12 = hour % 12 or 12
        time_text = f"{hour12}:{minute:02d} {ampm}"
    except (ValueError, AttributeError):
        time_text = time_raw or "unscheduled"

    out = {
        "map": server_cfg.get("display_name") or server_cfg.get("name", ""),
        # The Island slot rotates, so a post that spells its map into a heading
        # has to get it from here rather than having it typed in.
        "map_upper": (server_cfg.get("display_name")
                      or server_cfg.get("name", "")).upper(),
        "map_name": server_cfg.get("map_name", ""),
        "session_name": values.get("SessionName", ""),
        "cluster_id": (server_cfg.get("cluster_id") or ""),
        "join_password": values.get("ServerPassword", ""),
        "max_players": values.get("MaxPlayers", ""),
        "build": build or "unknown",

        "xp": multiplier(values.get("XPMultiplier")),
        "taming": multiplier(values.get("TamingSpeedMultiplier")),
        "harvesting": multiplier(values.get("HarvestAmountMultiplier")),
        "stack_size": multiplier(values.get("ItemStackSizeMultiplier")),
        "difficulty": _num(values.get("OverrideOfficialDifficulty")) or
                      values.get("OverrideOfficialDifficulty", "?"),
        "max_dino_level": "",

        "mating_interval": multiplier(values.get("MatingIntervalMultiplier")),
        "hatch_speed": multiplier(values.get("EggHatchSpeedMultiplier")),
        "mature_speed": multiplier(values.get("BabyMatureSpeedMultiplier")),
        "cuddle_interval": multiplier(values.get("BabyCuddleIntervalMultiplier")),
        "imprint_stat": multiplier(values.get("BabyImprintingStatScaleMultiplier")
                                   or values.get("BabyImprintAmountMultiplier")),

        "restart_days": day_text,
        "restart_time": time_text,
        "restart_schedule": f"{day_text} at {time_text}",

        "mod_count": str(len(mods or [])),
        "mods": mods_list(mods),
        "mods_with_ids": mods_list(mods, style="ids"),
        "mod_bullets": "\n".join(
            f"• {(m.get('name') or '').strip() or 'Mod ' + str(m.get('mod_id'))}"
            for m in (mods or [])),
    }

    # The curated shortlist, so the info post stays inside Discord's 2000
    # characters and reads as a highlight reel rather than an inventory.
    featured = key_mods(mods, key_ids, display_names)
    out["key_mods"] = mods_list(featured)
    out["key_mod_count"] = str(len(featured))
    out["key_mod_bullets"] = "\n".join(
        f"• {(m.get('name') or '').strip()}" for m in featured)

    # A dino level cap is usually a mod setting, so only claim it if present.
    level = values.get("MaxDinoLevel") or values.get("DinoLevelMax")
    if level:
        out["max_dino_level"] = str(level)

    return out


PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}", re.I)


def render(template: str, values: dict) -> tuple[str, list[str]]:
    """Fill {{placeholders}}. Returns (text, unknown placeholders).

    An unknown placeholder is left visible in the output rather than blanked:
    a post that silently loses a line is worse than one that obviously has a
    typo in it.
    """
    unknown = []

    def swap(match):
        key = match.group(1).lower()
        if key in values:
            return str(values[key])
        unknown.append(key)
        return match.group(0)

    return PLACEHOLDER.sub(swap, template), unknown


DISCORD_LIMIT = 2000


def split_for_discord(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Break a post into message-sized chunks on paragraph boundaries.

    A full server-info post runs past Discord's 2000-character limit once the
    mod list is real, so the bot owns a SEQUENCE of messages and edits each in
    place. Splitting on blank lines keeps sections whole rather than cutting a
    bullet list in half.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []

    chunks, current = [], ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # A single paragraph over the limit (a very long mod list) still has to
        # go somewhere, so fall back to splitting it on line boundaries.
        if len(para) <= limit:
            current = para
            continue
        current = ""
        for line in para.split("\n"):
            trial = f"{current}\n{line}" if current else line
            if len(trial) <= limit:
                current = trial
            else:
                if current:
                    chunks.append(current)
                current = line[:limit]
    if current:
        chunks.append(current)
    return chunks


def conflicts(gus_text: str, game_text: str) -> list[str]:
    """Settings advertised from one file that the OTHER file overrides."""
    values = extract(gus_text, game_text)
    out = []
    for key in list(values):
        if key.endswith("__gus"):
            real = key[:-5]
            out.append(f"{real}: Game.ini sets {values[real]}, "
                       f"GameUserSettings.ini sets {values[key]} - Game.ini wins")
    return out
