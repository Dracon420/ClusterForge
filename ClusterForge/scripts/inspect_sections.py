"""Diagnostic: where do a server's ini settings actually live, and which of
them does the wiki schema know about?"""

import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.inifile import IniFile, KIND_KEYVALUE  # noqa: E402
from core.schema import load_schema  # noqa: E402

CONFIG = r"D:\Servers\TheIsland\ShooterGame\Saved\Config\WindowsServer"
schema = load_schema()

for label in ("GameUserSettings.ini", "Game.ini"):
    ini = IniFile.load(os.path.join(CONFIG, label))
    by_section = collections.Counter()
    known_by_section = collections.Counter()
    unknown_core: list[str] = []

    core_sections = {"ServerSettings", "SessionSettings", "/Script/ShooterGame.ShooterGameMode"}

    for line in ini.lines:
        if line.kind != KIND_KEYVALUE or not line.key:
            continue
        sec = line.section or "(root)"
        by_section[sec] += 1
        if f"{label}::{line.key}".lower() in schema:
            known_by_section[sec] += 1
        elif line.section in core_sections:
            unknown_core.append(line.key)

    print("=" * 70)
    print(label)
    print("=" * 70)
    for sec, count in by_section.most_common():
        known = known_by_section[sec]
        print(f"  {sec:38} {count:4}  ({known} known)")
    print(f"\n  Unrecognised inside core sections: {len(unknown_core)}")
    for key in unknown_core[:20]:
        print(f"    {key}")
    print()
