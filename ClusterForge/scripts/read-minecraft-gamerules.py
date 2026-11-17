"""Print the gamerules actually stored in the Bedrock world.

Why this exists rather than grepping: every gamerule is present in level.dat
whether or not anyone ever set it, so finding the NAME proves nothing. I made
exactly that mistake once and concluded the QOL rules had never applied when
they had. Only the value is evidence.

Bedrock's level.dat is little-endian NBT behind a short header. A tag is
type(1) + name_length(2, LE) + name + payload, so the values can be read with
a targeted scan - no NBT library, nothing written back. Read-only by design:
this is run right after changing the rules, and a verifier that can corrupt
the thing it verifies is worse than no verifier.

Usage:  python -I scripts/read-minecraft-gamerules.py [path-to-level.dat]
"""

from __future__ import annotations

import json
import os
import struct
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Grouped the way a person would ask about them.
GROUPS = [
    ("Set for a young child", [
        "keepinventory", "doimmediaterespawn", "showcoordinates",
        "mobgriefing", "doinsomnia", "dofiretick",
    ]),
    ("Damage that causes accidental deaths", [
        "falldamage", "drowningdamage", "firedamage", "freezedamage", "pvp",
    ]),
    ("Kept deliberately", [
        "naturalregeneration", "dolimitedcrafting", "showdeathmessages",
        "tntexplodes", "respawnblocksexplode",
    ]),
    ("Numbers", [
        "randomtickspeed", "playerssleepingpercentage",
    ]),
]


def world_level_dat() -> str:
    """Find level.dat from the panel's own config, not a hardcoded path."""
    with open(os.path.join(BASE, "config.json"), encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    install = (cfg.get("minecraft") or {}).get("install_dir", "")
    level = ""
    try:
        with open(os.path.join(install, "server.properties"),
                  encoding="utf-8-sig") as fh:
            for line in fh:
                if line.strip().startswith("level-name="):
                    level = line.split("=", 1)[1].strip()
    except OSError:
        pass
    return os.path.join(install, "worlds", level or "Dracon", "level.dat")


def read_values(path: str, names: list[str]) -> dict[str, str | None]:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        print(f"could not read {path}: {exc}")
        return {name: None for name in names}

    out: dict[str, str | None] = {}
    for name in names:
        raw = name.encode()
        value = None
        for tag_type in (0x01, 0x03):          # TAG_Byte (bool), TAG_Int
            pattern = bytes([tag_type]) + struct.pack("<H", len(raw)) + raw
            idx = data.find(pattern)
            if idx < 0:
                continue
            at = idx + len(pattern)
            if tag_type == 0x01:
                value = "true" if data[at] else "false"
            else:
                value = str(struct.unpack_from("<i", data, at)[0])
            break
        out[name] = value
    return out


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else world_level_dat()
    if not os.path.isfile(path):
        print(f"no level.dat at {path}")
        return 1
    print(f"  {path}")
    for group, names in GROUPS:
        print(f"\n  {group}")
        for name, value in read_values(path, names).items():
            print(f"    {name:28} {value if value is not None else '(not stored)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
