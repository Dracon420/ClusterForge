"""
The map catalogue, and what it takes to actually run each map.

The Island slot rotates: finish a map, vote on the next one, come back to The
Island later. Everything that makes a map different lives in
data/maps/catalog.json, so adding Ragnarok to the rotation is editing one JSON
file rather than touching Python.

Deliberately small, because the cluster shares one install, one pair of ini
files, one set of ports and one password across every map. ARK already keeps
each map's world in its own SavedArks folder -- the Island install has eleven
of them sitting there already -- so switching maps never moves a save and never
touches a rate. A map "profile" is only three things:

    map_name      what the server is launched with
    session_name  what players see in the server browser
    save_folder   where its world lands (usually map_name, but not always)

plus the map's own mod id, when the map is itself a mod.

The save folder is not always the map name: Svartalfheim saves into
SavedArks/Svartalfheim with no _WP suffix even though the .ark file inside is
Svartalfheim_WP.ark. Assuming map_name would point backups at an empty folder,
which is the kind of failure nobody notices until a restore.
"""

from __future__ import annotations

import json
import os

# Every map in the cluster is named the same way, so the session name is
# derived rather than repeated eleven times. A catalog entry can still override
# it with its own "session_name" if a map ever needs to break the pattern.
SESSION_PREFIX = "[ToDs]Dracon-"


def catalog_path(base_dir: str) -> str:
    return os.path.join(base_dir, "data", "maps", "catalog.json")


def load(base_dir: str) -> list[dict]:
    """Every catalogued map. Returns [] rather than raising -- a missing
    catalogue should degrade the map picker, not take the panel down."""
    try:
        with open(catalog_path(base_dir), "r", encoding="utf-8-sig") as fh:
            return json.load(fh).get("maps", [])
    except (OSError, json.JSONDecodeError):
        return []


def by_key(base_dir: str) -> dict[str, dict]:
    return {e["key"]: e for e in load(base_dir) if e.get("key")}


def find(base_dir: str, key: str) -> dict | None:
    return by_key(base_dir).get(key)


def find_by_map_name(base_dir: str, map_name: str) -> dict | None:
    """Which catalogue entry a server is currently running, matched on the
    value ARK is actually launched with."""
    if not map_name:
        return None
    for entry in load(base_dir):
        if entry.get("map_name", "").lower() == map_name.lower():
            return entry
    return None


def session_name(entry: dict) -> str:
    """'Astraeos' -> '[ToDs]Dracon-Astraeos', 'The Island' -> '[ToDs]Dracon-The-Island'.

    Spaces become dashes because that is what the two live servers already do,
    and a session name that changes shape mid-rotation makes players think they
    are looking at a different cluster.
    """
    override = (entry.get("session_name") or "").strip()
    if override:
        return override
    return SESSION_PREFIX + (entry.get("name") or entry.get("map_name", "")).replace(" ", "-")


def is_available(entry: dict) -> bool:
    """False for a map that is catalogued but cannot actually be run yet.

    Fjordur is listed because it was asked for and will land eventually, but
    launching a server on a map this build does not contain gets a server that
    will not boot. Better to carry the entry and refuse the switch than to
    leave it out and forget, or to let it through and debug a dead server.
    """
    return entry.get("available", True) is not False


def unavailable_reason(entry: dict) -> str:
    return (entry.get("unavailable_reason") or "").strip() or "not available in this build"


def save_folder(entry: dict) -> str:
    return (entry.get("save_folder") or "").strip() or entry.get("map_name", "")


def save_dir(install_dir: str, entry: dict) -> str:
    return os.path.join(install_dir, "ShooterGame", "Saved", "SavedArks",
                        save_folder(entry))


def map_mod_ids(base_dir: str) -> set[int]:
    """The mod ids that exist only to BE a map."""
    out = set()
    for entry in load(base_dir):
        try:
            out.add(int(entry["mod_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def exclusive_mod_ids(base_dir: str) -> set[int]:
    """Mods that only work on the map that lists them (catalog "exclusive_mods")."""
    out = set()
    for entry in load(base_dir):
        for mod in entry.get("exclusive_mods") or []:
            try:
                out.add(int(mod))
            except (TypeError, ValueError):
                continue
    return out


def apply_map_mods(base_dir: str, current: list[int], entry: dict) -> list[int]:
    """The mod list for a map: everything you already run, plus this map's own
    mod, minus any other map's.

    The rates and the QOL mods are identical from map to map by design, so a
    switch must not disturb them. Only the map mods move, and the incoming one
    goes first because load order is leftmost-wins and a map mod belongs at the
    front -- that is where Svartalfheim's already sits.
    """
    others = map_mod_ids(base_dir) | exclusive_mod_ids(base_dir)
    mine = {int(m) for m in entry.get("exclusive_mods") or []}
    keep = [m for m in current if m not in others or m in mine]

    front = []
    own = entry.get("mod_id")
    if own not in (None, ""):
        front.append(int(own))

    # Exclusive mods are the opposite of companions: they belong to ONE map and
    # break the others, so they come in with that map and go out with it.
    # Additional IslandDinos crashes Ragnarok ~25 s into boot (proved 2026-09-15:
    # Ragnarok boots with the Island's other 16 mods and crashes with that one
    # alone). Stripping it is safe for the map's own world, because switching
    # back puts it in the launch line before that world is ever loaded again.
    for exclusive in entry.get("exclusive_mods") or []:
        if int(exclusive) not in current and int(exclusive) not in front:
            front.append(int(exclusive))

    # Companion mods travel IN with their map but are never stripped on the way
    # OUT. Amissa's creatures live in "Amissa Additions", whose author notes it
    # is also needed on any map those creatures are transferred to -- and
    # removing a mod deletes every creature and item stored in it. Adding one
    # is harmless; taking one away automatically would destroy player tames
    # just because a map was switched.
    for companion in entry.get("companion_mods") or []:
        if int(companion) not in front:
            front.append(int(companion))

    return front + [m for m in keep if m not in front]


def profile(base_dir: str, entry: dict, install_dir: str = "") -> dict:
    """Flat view of one map, for the panel, the agent and the UI to agree on."""
    return {
        "key": entry.get("key", ""),
        "name": entry.get("name", ""),
        "map_name": entry.get("map_name", ""),
        "session_name": session_name(entry),
        "save_folder": save_folder(entry),
        "save_dir": save_dir(install_dir, entry) if install_dir else "",
        "mod_id": entry.get("mod_id"),
        "kind": entry.get("kind", ""),
        "notes": entry.get("notes", ""),
    }
