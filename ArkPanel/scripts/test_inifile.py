"""Round-trip gate for core/inifile.py, run against the REAL server configs.

Read-only: loads each file, re-renders it in memory and compares bytes. Never
writes to the server directories.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.inifile import IniFile, selftest_roundtrip  # noqa: E402

ISLAND = r"D:\Servers\TheIsland\ShooterGame\Saved\Config\WindowsServer"
SVART = r"Z:\Svartalfhiem\ShooterGame\Saved\Config\WindowsServer"

TARGETS = [
    ("Island  Game.ini", os.path.join(ISLAND, "Game.ini")),
    ("Island  GameUserSettings.ini", os.path.join(ISLAND, "GameUserSettings.ini")),
    ("Island  Engine.ini", os.path.join(ISLAND, "Engine.ini")),
    ("Svart   Game.ini", os.path.join(SVART, "Game.ini")),
    ("Svart   GameUserSettings.ini", os.path.join(SVART, "GameUserSettings.ini")),
    ("Svart   Engine.ini", os.path.join(SVART, "Engine.ini")),
]

# The tuning lines that a dict-based parser would silently eat.
CRITICAL_REPEATS = {
    "ConfigOverrideItemMaxQuantity": 42,
    "HarvestResourceItemAmountClassMultipliers": 25,
}

failures = 0

print("=" * 78)
print("ROUND-TRIP GATE")
print("=" * 78)
for label, path in TARGETS:
    if not os.path.exists(path):
        print(f"  {label:32} SKIP (not found)")
        continue
    ok, detail = selftest_roundtrip(path)
    print(f"  {label:32} {detail}")
    if not ok:
        failures += 1

print()
print("=" * 78)
print("REPEATED-KEY PRESERVATION  (Island Game.ini)")
print("=" * 78)
game = IniFile.load(os.path.join(ISLAND, "Game.ini"))
section = "/Script/ShooterGame.ShooterGameMode"
for key, expected in CRITICAL_REPEATS.items():
    got = len(game.get_all(section, key))
    status = "OK " if got == expected else "FAIL"
    if got != expected:
        failures += 1
    print(f"  {status} {key:48} {got} lines (expected {expected})")

print()
print("=" * 78)
print("STRUCTURE")
print("=" * 78)
gus = IniFile.load(os.path.join(ISLAND, "GameUserSettings.ini"))
print(f"  Island GUS sections : {len(gus.sections())}")
print(f"  Island Game entries : {sum(1 for _ in game.entries())}")

# Mod sections carry real tuning and are absent from the wiki schema.
mod_sections = [
    s for s in gus.sections()
    if s and not s.startswith("/Script/") and s not in (
        "ServerSettings", "ScalabilityGroups", "SessionSettings", "Startup",
    )
]
print(f"  Mod-specific sections: {len(mod_sections)}")
print(f"    {', '.join(mod_sections)}")

top = sorted(game.key_counts(section).items(), key=lambda kv: -kv[1])[:5]
print("  Most-repeated keys in Game.ini:")
for key, count in top:
    print(f"    {count:4}  {key}")

print()
if failures:
    print(f"RESULT: {failures} FAILURE(S) -- write path stays disabled")
    sys.exit(1)
print("RESULT: PASS -- safe to build editing on top")
