"""
Turn the ARK wiki CSVs into a typed, categorised settings schema.

Source: ArkWIKI_GameUserSettings_EN.csv (197 rows) and ArkWIKI_Game_EN.csv
(137 rows), columns ASA,ASE,Variable,Description. Type and default are buried
in the description prose ("Default value: 1.0Value type: float..."), so they
get regexed out.

Two things this must never do:

  - Drop a setting that exists in the user's ini but not in the schema. On this
    cluster that is the COMMON case, not an edge case: GameUserSettings.ini
    carries 15 mod-specific sections ([OmegaTeleporters], [Shiny],
    [CybersStructures], [BetterBreeding] and so on) that no wiki knows about,
    and they hold real tuning. Unknown settings become plain typed rows in
    "Mod Settings Or Unclassified Settings" and stay fully editable.

  - Auto-sync settings that appear in both files. ItemStackSizeMultiplier is
    300 in GameUserSettings.ini and 500.0 in Game.ini here; Game.ini governs.
    Both are shown, the relationship is flagged, neither is quietly rewritten.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIKI_DIR = os.path.join(BASE, "data", "wiki")

GUS_CSV = "ArkWIKI_GameUserSettings_EN.csv"
GAME_CSV = "ArkWIKI_Game_EN.csv"

# DSM's category names, kept identical so the UI reads the same way.
CATEGORIES = [
    "Basic Server Settings",
    "Full Server Settings",
    "Rcon Server Settings",
    "Gameplay Settings",
    "Chat and Message Settings",
    "World and Environment Settings",
    "Structures and Building Settings",
    "Player Settings",
    "Dino Settings",
    "Resource Management Settings",
    "Cryopod Settings",
    "Tribe Settings",
    "Download and Upload Settings",
    "Ragnarok Settings",
    "Deprecated Options Settings",
    "Mod Settings Or Unclassified Settings",
]
UNCLASSIFIED = CATEGORIES[-1]

# Explicit placements win over keyword matching below.
_EXPLICIT = {
    "serverpassword": "Basic Server Settings",
    "serveradminpassword": "Basic Server Settings",
    "sessionname": "Basic Server Settings",
    "maxplayers": "Basic Server Settings",
    "serverpve": "Basic Server Settings",
    "servercrosshair": "Basic Server Settings",
    "overrideofficialdifficulty": "Basic Server Settings",
    "difficultyoffset": "Basic Server Settings",
    "activemods": "Basic Server Settings",
    "rconenabled": "Rcon Server Settings",
    "rconport": "Rcon Server Settings",
    "rconservergamelogbuffer": "Rcon Server Settings",
    "adminlogging": "Rcon Server Settings",
    "serverrconoutputtribelogs": "Rcon Server Settings",
    "xpmultiplier": "Gameplay Settings",
    "tamingspeedmultiplier": "Dino Settings",
    "harvestamountmultiplier": "Resource Management Settings",
    "harvesthealthmultiplier": "Resource Management Settings",
    "itemstacksizemultiplier": "Resource Management Settings",
    "allowcryofridgeoutsidecryopod": "Cryopod Settings",
    "cryopodnuderfdamagemult": "Cryopod Settings",
    "noteryopodenabled": "Cryopod Settings",
}

# Ordered: first match wins, so more specific patterns come first.
_KEYWORDS: list[tuple[str, str]] = [
    (r"cryo|cryopod|cryofridge", "Cryopod Settings"),
    (r"tribe|alliance", "Tribe Settings"),
    (r"rcon", "Rcon Server Settings"),
    (r"ragnarok", "Ragnarok Settings"),
    (r"download|upload|transfer|crossark|cluster", "Download and Upload Settings"),
    (r"chat|message|motd|broadcast|globalvoice|proximity", "Chat and Message Settings"),
    (r"structure|building|platform|foundation|turret|pillar|snap|decay",
     "Structures and Building Settings"),
    (r"dino|tame|baby|imprint|breed|mating|egg|hatch|wild|creature|raid",
     "Dino Settings"),
    (r"harvest|resource|stack|craft|item|supplycrate|loot|fishing|quantity",
     "Resource Management Settings"),
    (r"player|character|stamina|oxygen|hunger|thirst|health|xp|level|engram|respawn",
     "Player Settings"),
    (r"day|night|time|weather|temperature|season|meteor|environment|world|map",
     "World and Environment Settings"),
    (r"pve|pvp|difficulty|damage|resist|gamemode|hardcore|singleplayer",
     "Gameplay Settings"),
]

_DEFAULT_RE = re.compile(r"Default\s*value\s*:\s*([^\n]*?)(?=Value\s*type\s*:|$)", re.I)
_TYPE_RE = re.compile(r"Value\s*type\s*:\s*([A-Za-z ]+)", re.I)

_TYPE_MAP = {
    "float": "float", "integer": "integer", "int": "integer",
    "boolean": "boolean", "bool": "boolean", "string": "string",
}


@dataclass
class Setting:
    name: str
    file: str                 # 'GameUserSettings.ini' or 'Game.ini'
    section: str = ""
    type: str = "string"
    default: str = ""
    description: str = ""
    category: str = UNCLASSIFIED
    known: bool = True        # False = discovered in the ini, absent from wiki
    asa_confirmed: bool = True  # wiki's ASA column; unreliable, display only

    def to_dict(self) -> dict:
        return asdict(self)


def _classify(name: str) -> str:
    lower = name.lower()
    if lower in _EXPLICIT:
        return _EXPLICIT[lower]
    for pattern, category in _KEYWORDS:
        if re.search(pattern, lower):
            return category
    return "Full Server Settings"


def _parse_type(description: str, name: str) -> tuple[str, str]:
    """(type, default) inferred from the wiki prose, with name-based fallback."""
    default = ""
    match = _DEFAULT_RE.search(description)
    if match:
        default = match.group(1).strip().rstrip(".").strip()

    kind = ""
    match = _TYPE_RE.search(description)
    if match:
        # The capture can be whitespace-only when the prose reads
        # "Value type: <newline>", so guard before indexing.
        words = match.group(1).strip().lower().split()
        if words:
            kind = _TYPE_MAP.get(words[0], "")

    if not kind:
        # ARK's own naming is a reliable hint: b-prefixed names are booleans,
        # *Multiplier / *Scale are floats.
        if re.match(r"^b[A-Z]", name):
            kind = "boolean"
        elif re.search(r"multiplier|scale|interval|speed|modifier", name, re.I):
            kind = "float"
        elif default.lower() in ("true", "false"):
            kind = "boolean"
        elif re.fullmatch(r"-?\d+", default or ""):
            kind = "integer"
        elif re.fullmatch(r"-?\d*\.\d+", default or ""):
            kind = "float"
        else:
            kind = "string"
    return kind, default


def _load_csv(path: str, file_label: str) -> list[Setting]:
    if not os.path.exists(path):
        return []
    out: list[Setting] = []
    current_section = ""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            variable = (row.get("Variable") or "").strip()
            if not variable:
                continue
            # Rows like "[ServerSettings]" mark the section for what follows.
            if variable.startswith("[") and variable.endswith("]"):
                current_section = variable[1:-1]
                continue
            # Deliberately NOT filtering on the ASA column. It is unreliable:
            # MatingIntervalMultiplier is marked ASA=None yet is actively set on
            # both servers here (0.1 on Island, 0.0001 on Svartalfheim), as are
            # bAllowFlyerCarryPvE and DinoCountMultiplier. A setting present in
            # a live ASA config is proof it is relevant, whatever the wiki says.
            # Filtering on it would hide real settings from the editor, so the
            # flag is recorded for display instead.
            asa = (row.get("ASA") or "").strip().lower()
            description = (row.get("Description") or "").strip()
            kind, default = _parse_type(description, variable)
            out.append(Setting(
                name=variable,
                file=file_label,
                section=current_section,
                type=kind,
                default=default,
                description=description,
                category=_classify(variable),
                asa_confirmed=asa in ("true", "1", "yes"),
            ))
    return out


def load_schema(wiki_dir: str = WIKI_DIR) -> dict[str, Setting]:
    """All known ASA settings, keyed 'file::name' (lowercased).

    Keyed per file rather than per name, because ItemStackSizeMultiplier
    legitimately exists in both with different values and different meanings.
    """
    settings: dict[str, Setting] = {}
    for filename, label in (
        (GUS_CSV, "GameUserSettings.ini"),
        (GAME_CSV, "Game.ini"),
    ):
        for setting in _load_csv(os.path.join(wiki_dir, filename), label):
            settings[f"{label}::{setting.name}".lower()] = setting
    return settings


_INDEX_SUFFIX = re.compile(r"\[\d+\]$")

# Sections that belong to the engine or the server itself, not to a mod.
ENGINE_SECTIONS = {
    "serversettings": "Server Settings (undocumented)",
    "sessionsettings": "Session Settings",
    "scalabilitygroups": "Scalability Groups",
    "startup": "Startup",
    "/script/shootergame.shootergameusersettings": "Client & User Settings",
    "/script/engine.gameusersettings": "Engine User Settings",
    "/script/shootergame.shootergamemode": "Game Mode (undocumented)",
    "": "Top of file",
}

# Section names that don't resemble their mod's title closely enough for the
# normalised match below to find them.
SECTION_ALIASES = {
    "arkautomatedsettings": "Automated Ark",
    "customleveldistrib": "Custom Dino Levels",
    "magashop": "MagaArk",
    "awesomeadmintools": "Awesome ARK Tools",  # section says Admin, mod says ARK
    "rsm": "Resonant Shop Mod",
    "s dinos": "S-Dino Variants",
    "sdinos": "S-Dino Variants",
}

# Similarity floor for the fuzzy fallback. 0.82 catches near-misses like
# [NanohsPotionMod] vs "Nanoh's Potions Mod (No Longer Supported)" without
# pairing genuinely unrelated names.
_FUZZY_FLOOR = 0.82


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def match_section_to_mod(section: str, mods: list[dict]) -> dict | None:
    """Best guess at which mod owns an ini section.

    Section names are chosen by mod authors and only loosely track their
    CurseForge titles: [OmegaTeleporters] matches cleanly, [Shiny] is a prefix
    of "Shiny! Dinos Ascended", and [ArkAutomatedSettings] shares no words at
    all with "Automated Ark" so it needs an alias.
    """
    if not section or not mods:
        return None
    target = _norm(section)
    if not target:
        return None

    alias = SECTION_ALIASES.get(section.lower()) or SECTION_ALIASES.get(target)
    if alias:
        target = _norm(alias)

    best = None
    for mod in mods:
        name = _norm(mod.get("name", ""))
        if not name:
            continue
        if name == target:
            return mod
        # Prefix either way catches [Shiny] -> "Shiny! Dinos Ascended" and
        # [CybersStructures] -> "Cybers Structures QoL+ (Crossplay)".
        if len(target) >= 4 and (name.startswith(target) or target.startswith(name)):
            if best is None or len(name) < len(_norm(best.get("name", ""))):
                best = mod
    if best is not None:
        return best

    # Fuzzy fallback for singular/plural and punctuation drift, comparing
    # against the leading portion of the mod name so trailing suffixes like
    # "(No Longer Supported)" don't sink an otherwise clear match.
    scored = []
    for mod in mods:
        name = _norm(mod.get("name", ""))
        if not name:
            continue
        head = name[:max(len(target), 6)]
        ratio = SequenceMatcher(None, target, head).ratio()
        if ratio >= _FUZZY_FLOOR:
            scored.append((ratio, mod))
    if scored:
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[0][1]
    return None


def lookup(schema: dict[str, Setting], file_label: str, key: str) -> tuple[Setting | None, str]:
    """Find a setting's documentation, tolerating two real-world wrinkles.

    Returns (setting, match_kind) where match_kind is 'exact', 'indexed',
    'other_file', 'indexed_other_file' or 'none'.

    - Indexed keys: the ini holds PerLevelStatsMultiplier_Player[0] through
      [11], but the wiki documents the un-indexed name. Strip the subscript for
      lookup and keep the index for display.
    - Cross-file: some settings are documented under one file but legitimately
      used in the other. ItemStackSizeMultiplier is the notable case here -- it
      exists in BOTH of this server's files with different values (300 in
      GameUserSettings.ini, 500.0 in Game.ini, and Game.ini governs). Falling
      back gives the user type and description; it must never merge the two.
    """
    other = "Game.ini" if file_label == "GameUserSettings.ini" else "GameUserSettings.ini"
    base = _INDEX_SUFFIX.sub("", key)

    found = schema.get(f"{file_label}::{key}".lower())
    if found:
        return found, "exact"
    if base != key:
        found = schema.get(f"{file_label}::{base}".lower())
        if found:
            return found, "indexed"
    found = schema.get(f"{other}::{key}".lower())
    if found:
        return found, "other_file"
    if base != key:
        found = schema.get(f"{other}::{base}".lower())
        if found:
            return found, "indexed_other_file"
    return None, "none"


def describe_ini(
    ini,
    file_label: str,
    schema: dict[str, Setting],
    mods: list[dict] | None = None,
) -> dict:
    """Merge a live ini file with the schema into a categorised view.

    Every key/value line in the file appears exactly once in the output. Lines
    whose key is not in the schema are still returned, typed by inspection and
    filed under Mod Settings Or Unclassified -- dropping them would hide most
    of this cluster's actual mod configuration.
    """
    from .inifile import KIND_KEYVALUE  # local import avoids a cycle

    buckets: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    section_rows: dict[str, list[dict]] = {}
    counts = {"known": 0, "unknown": 0, "repeated": 0}

    per_section_key: dict[tuple[str, str], int] = {}
    for line in ini.lines:
        if line.kind != KIND_KEYVALUE or not line.key:
            continue
        sig = (line.section or "", line.key.lower())
        per_section_key[sig] = per_section_key.get(sig, 0) + 1

    seen: dict[tuple[str, str], int] = {}
    for line in ini.lines:
        if line.kind != KIND_KEYVALUE or not line.key:
            continue
        sig = (line.section or "", line.key.lower())
        seen[sig] = seen.get(sig, 0) + 1
        occurrence = seen[sig]
        total = per_section_key[sig]

        setting, match_kind = lookup(schema, file_label, line.key)
        if setting:
            counts["known"] += 1
            category = setting.category
            kind = setting.type
            default = setting.default
            description = setting.description
            if match_kind in ("other_file", "indexed_other_file"):
                counts["cross_file"] = counts.get("cross_file", 0) + 1
                description = (
                    f"[documented under {setting.file}] {description}"
                ).strip()
        else:
            counts["unknown"] += 1
            category = UNCLASSIFIED
            kind = _infer_type(line.value or "")
            default = ""
            description = ""

        if total > 1:
            counts["repeated"] += 1

        row = {
            "key": line.key,
            "value": (line.value or ""),
            "section": line.section or "",
            "type": kind,
            "default": default,
            "description": description,
            "known": setting is not None,
            "match": match_kind,
            "occurrence": occurrence,
            "occurrences": total,
            # Repeated keys are per-item tuning (42 stack overrides, 25 harvest
            # multipliers). They are editable individually and never merged.
            "repeated": total > 1,
        }

        # Documented settings go to their wiki category. Everything else is
        # grouped by its ini SECTION, because on this cluster those sections are
        # per-mod ([OmegaTeleporters], [Shiny], [CybersStructures]...). Lumping
        # them into one "unclassified" pile buries several hundred real settings
        # behind a single expander.
        if setting is not None:
            buckets[category].append(row)
        else:
            section_rows.setdefault(line.section or "", []).append(row)

    return {
        "file": file_label,
        "categories": [
            {"name": name, "settings": rows}
            for name, rows in buckets.items() if rows
        ],
        "sections": _build_sections(section_rows, mods or []),
        "counts": counts,
    }


def _build_sections(section_rows: dict[str, list[dict]], mods: list[dict]) -> list[dict]:
    """Turn per-section rows into labelled groups, resolving mod identity.

    Sorted mods-first (largest first), then engine sections, so the settings a
    person actually tunes are at the top rather than buried under 288 client
    settings.
    """
    in_load_order = {
        m["mod_id"] for m in mods if m.get("in_load_order")
    }

    groups: list[dict] = []
    for section, rows in section_rows.items():
        key = section.lower()
        engine_label = ENGINE_SECTIONS.get(key)
        mod = None if engine_label else match_section_to_mod(section, mods)

        if engine_label:
            kind, label, orphan = "engine", engine_label, False
        elif mod:
            kind = "mod"
            label = mod["name"]
            # A section whose mod is no longer loaded is leftover config. It is
            # harmless but worth surfacing rather than hiding -- it is the trace
            # a removed mod leaves behind.
            orphan = mod["mod_id"] not in in_load_order
        else:
            # No mod in the load order claims this section. On this cluster that
            # reliably means leftover config from a removed mod -- [RSM] and
            # [DeathInventoryKeeper] are exactly that. Say so rather than
            # filing it under a vague "unclassified".
            kind, label, orphan = "unknown", section or "(top of file)", True

        groups.append({
            "section": section,
            "label": label,
            "kind": kind,
            "orphan": orphan,
            "mod_id": mod.get("mod_id") if mod else None,
            "logo": mod.get("logo") if mod else None,
            "mod_url": mod.get("url") if mod else None,
            "settings": rows,
        })

    order = {"mod": 0, "unknown": 1, "engine": 2}
    groups.sort(key=lambda g: (order[g["kind"]], -len(g["settings"])))
    return groups


def _infer_type(value: str) -> str:
    v = value.strip()
    if v.lower() in ("true", "false"):
        return "boolean"
    if re.fullmatch(r"-?\d+", v):
        return "integer"
    if re.fullmatch(r"-?\d*\.\d+", v):
        return "float"
    return "string"
