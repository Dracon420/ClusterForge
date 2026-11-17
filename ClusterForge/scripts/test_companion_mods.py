"""
Prove map switching handles companion mods safely.

Amissa needs 'Amissa Additions' for its creatures. A switch INTO Amissa must add
it; a switch AWAY must NOT remove it, because removing a mod deletes the tames
stored in it and the author says it is also needed wherever those creatures are
transferred. Runs against the real catalogue; writes nothing.

Run:  python scripts\\test_companion_mods.py
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import maps                                             # noqa: E402

FAILED = []
AMISSA, ADDITIONS, NYRANDIL, SVART = 965379, 1007416, 965599, 942355
QOL = [940975, 975282, 1079535]          # stand-ins for ordinary cluster mods


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got} (expected {want})")
    if not ok:
        FAILED.append(label)


amissa = maps.find(BASE, "amissa")
island = maps.find(BASE, "theisland")
assert amissa and island, "catalogue entries missing"

print("Into Amissa from Nyrandil:")
out = maps.apply_map_mods(BASE, [NYRANDIL] + QOL, amissa)
check("map mod first, companion second", out[:2], [AMISSA, ADDITIONS])
check("Nyrandil's map mod stripped", NYRANDIL in out, False)
check("cluster mods kept, in order", out[2:], QOL)

print("\nInto Amissa when the companion is already loaded:")
out = maps.apply_map_mods(BASE, QOL + [ADDITIONS], amissa)
check("no duplicate companion", out.count(ADDITIONS), 1)
check("still map mod first", out[0], AMISSA)

print("\nAway from Amissa to The Island:")
out = maps.apply_map_mods(BASE, [AMISSA, ADDITIONS] + QOL, island)
check("Amissa's map mod removed", AMISSA in out, False)
check("companion KEPT (tames stored in it survive)", ADDITIONS in out, True)
check("cluster mods kept", [m for m in out if m in QOL], QOL)

print("\nUnrelated switches are unchanged by companion support:")
svart = maps.find(BASE, "svartalfheim")
out = maps.apply_map_mods(BASE, [NYRANDIL] + QOL, svart)
check("plain map-mod swap", out, [SVART] + QOL)

print("\nIsland-only mod (Additional IslandDinos) leaves with The Island and comes back:")
ISLANDDINOS = 947442
ragnarok = maps.find(BASE, "ragnarok")
island_mods = [940975, ISLANDDINOS, 1079535, 912815]
out = maps.apply_map_mods(BASE, island_mods, ragnarok)
check("stripped when leaving The Island", ISLANDDINOS in out, False)
check("everything else kept in order", out, [940975, 1079535, 912815])
back = maps.apply_map_mods(BASE, out, island)
check("added back on return", ISLANDDINOS in back, True)
check("nothing else changed", [m for m in back if m != ISLANDDINOS], [940975, 1079535, 912815])
same = maps.apply_map_mods(BASE, island_mods, island)
check("Island -> Island leaves the order alone", same, island_mods)
bjarn = maps.find(BASE, "bjarnheim")
out = maps.apply_map_mods(BASE, island_mods, bjarn)
check("stripped for a mod map too, map mod first", out[:1] + [ISLANDDINOS in out], [1376189, False])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed")
