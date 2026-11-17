"""
Build the mod table from the three sources that already exist on disk.

No CurseForge API key is needed and no downloading happens here. The ARK server
installs CurseForge mods itself on boot, driven purely by the -mods= launch
argument (visible as LogCFCore lines). DSM's "Download & install" button is
unreliable -- it has been observed dropping a .zip into the Windows Downloads
folder instead of installing anything. So ClusterForge maintains the list and
nothing more.

Sources, in order of trustworthiness:

  library.json   written by the ARK server itself. Real names, authors,
                 summaries, categories, logo URLs, install/update timestamps.
                 This is what's actually on disk.
  modInfos.json  DSM's cache. Carries the Enable/Passive/Dev flags and download
                 URLs, but its ModName is usually an unresolved placeholder
                 ("Mod928597") and LastUpdated is typically null.
  -mods=         load order, which is significant: leftmost wins on conflicts.

Because they can disagree, every mod reports three independent booleans --
in_load_order, installed, enabled -- rather than one blurred "status".
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict

ARK_GAME_ID = 83374


def _library_path(install_dir: str) -> str:
    return os.path.join(
        install_dir, "ShooterGame", "Binaries", "Win64", "ShooterGame",
        "ModsUserData", str(ARK_GAME_ID), "library.json",
    )


def _name_from_slug(url: str) -> str | None:
    """Derive a readable name from a CurseForge URL slug.

    'https://www.curseforge.com/ark-survival-ascended/mods/automated-ark/download/8449295'
    -> 'Automated Ark'

    Used for mods added by URL that aren't installed yet, so the table shows
    something meaningful before the server's first boot fills in real metadata.
    """
    match = re.search(r"/mods/([a-z0-9][a-z0-9\-]*)", url or "", re.I)
    if not match:
        return None
    slug = match.group(1)
    return " ".join(word.capitalize() for word in slug.split("-") if word) or None


@dataclass
class Mod:
    mod_id: int
    name: str
    author: str = ""
    url: str = ""
    summary: str = ""
    logo: str = ""
    categories: list[str] = field(default_factory=list)
    last_updated: str = ""
    file_name: str = ""

    # The three-way state. These are deliberately separate.
    in_load_order: bool = False
    installed: bool = False
    enabled: bool = True

    load_index: int | None = None
    passive: bool = False
    dev: bool = False
    path_on_disk: str = ""

    @property
    def drift(self) -> str | None:
        """Human-readable problem, or None when consistent."""
        if self.in_load_order and not self.installed:
            return "in launch args but not installed on disk"
        if self.installed and not self.in_load_order:
            return "installed on disk but missing from launch args"
        if self.in_load_order and not self.enabled:
            return "in launch args but marked disabled"
        return None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["drift"] = self.drift
        return data


def load_library(install_dir: str) -> dict[int, dict]:
    """Installed-mod metadata written by the ARK server. {mod_id: entry}."""
    path = _library_path(install_dir)
    if not os.path.exists(path):
        return {}
    try:
        # utf-8-sig: library.json ships with a BOM, and plain utf-8 raises on it.
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}

    out: dict[int, dict] = {}
    for entry in data.get("installedMods") or []:
        details = entry.get("details") or {}
        mod_id = details.get("iD") or details.get("id")
        if mod_id is None:
            continue
        out[int(mod_id)] = entry
    return out


def load_modinfos(install_dir: str) -> dict[int, dict]:
    """DSM's per-server mod cache. {mod_id: entry}."""
    path = os.path.join(install_dir, "Profile", "modInfos.json")
    if not os.path.exists(path):
        return {}
    try:
        # utf-8-sig: library.json ships with a BOM, and plain utf-8 raises on it.
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}

    out: dict[int, dict] = {}
    for entry in data or []:
        raw_id = entry.get("ProjectId")
        if raw_id is None:
            continue
        try:
            out[int(raw_id)] = entry
        except (TypeError, ValueError):
            continue
    return out


def _from_library(mod_id: int, entry: dict) -> Mod:
    details = entry.get("details") or {}
    logo = details.get("logo") or {}
    authors = details.get("authors") or []
    links = details.get("links") or {}
    return Mod(
        mod_id=mod_id,
        name=details.get("name") or f"Mod{mod_id}",
        author=(authors[0].get("name") if authors else "") or "",
        url=links.get("websiteUrl") or "",
        summary=details.get("summary") or "",
        logo=logo.get("thumbnailUrl") or logo.get("uRL") or "",
        categories=[c.get("name", "") for c in (details.get("categories") or [])],
        last_updated=entry.get("dateUpdated") or details.get("dateModified") or "",
        path_on_disk=entry.get("pathOnDisk") or "",
        installed=True,
        enabled=bool(entry.get("enabled", True)),
    )


def build_mod_table(install_dir: str, load_order: list[int]) -> list[Mod]:
    """Merge the three sources into one ordered table.

    Mods present in the launch args come first, in load order, because that
    order is what the game actually honours. Anything installed but absent from
    the args is appended afterwards and flagged as drift rather than hidden.
    """
    library = load_library(install_dir)
    modinfos = load_modinfos(install_dir)

    mods: dict[int, Mod] = {}

    for mod_id, entry in library.items():
        mods[mod_id] = _from_library(mod_id, entry)

    for mod_id, entry in modinfos.items():
        mod = mods.get(mod_id)
        if mod is None:
            url = entry.get("DownloadUrl") or ""
            raw_name = entry.get("ModName") or ""
            # DSM leaves ModName as a placeholder; prefer a slug-derived name.
            name = (
                _name_from_slug(url)
                or (raw_name if raw_name and not re.fullmatch(r"Mod\d+", raw_name) else None)
                or f"Mod{mod_id}"
            )
            mod = Mod(mod_id=mod_id, name=name, url=url)
            mods[mod_id] = mod
        if not mod.author:
            mod.author = entry.get("AuthorName") or ""
        if not mod.url:
            mod.url = entry.get("DownloadUrl") or ""
        if not mod.file_name:
            mod.file_name = entry.get("FileName") or ""
        mod.passive = bool(entry.get("IsPassiveMod", False))
        mod.dev = bool(entry.get("IsDevMod", False))
        if "IsSelected" in entry:
            mod.enabled = bool(entry["IsSelected"])

    for index, mod_id in enumerate(load_order):
        mod = mods.get(mod_id)
        if mod is None:
            mod = Mod(mod_id=mod_id, name=f"Mod{mod_id}")
            mods[mod_id] = mod
        mod.in_load_order = True
        mod.load_index = index

    ordered = [mods[m] for m in load_order if m in mods]
    extras = sorted(
        (m for m in mods.values() if not m.in_load_order), key=lambda m: m.name.lower()
    )
    return ordered + extras


def diff_maps(table_a: list[Mod], table_b: list[Mod]) -> dict[str, list[dict]]:
    """Compare two servers' mod tables, for the cluster-wide view.

    The two maps intentionally run different mod sets, so this reports the
    difference without implying either side is wrong.
    """
    a_ids = {m.mod_id for m in table_a if m.in_load_order}
    b_ids = {m.mod_id for m in table_b if m.in_load_order}
    by_id = {m.mod_id: m for m in list(table_a) + list(table_b)}

    def rows(ids):
        return [
            {"mod_id": i, "name": by_id[i].name if i in by_id else f"Mod{i}"}
            for i in sorted(ids)
        ]

    return {
        "shared": rows(a_ids & b_ids),
        "only_a": rows(a_ids - b_ids),
        "only_b": rows(b_ids - a_ids),
    }
