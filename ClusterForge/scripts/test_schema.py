"""Check the wiki schema parses and that no live ini setting gets dropped."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.inifile import IniFile, KIND_KEYVALUE  # noqa: E402
from core.schema import load_schema, describe_ini, CATEGORIES  # noqa: E402

CONFIG = r"D:\Servers\TheIsland\ShooterGame\Saved\Config\WindowsServer"

schema = load_schema()
print(f"Schema settings loaded: {len(schema)}")
by_cat: dict[str, int] = {}
for s in schema.values():
    by_cat[s.category] = by_cat.get(s.category, 0) + 1
for c in CATEGORIES:
    if by_cat.get(c):
        print(f"  {c:40} {by_cat[c]}")

types: dict[str, int] = {}
for s in schema.values():
    types[s.type] = types.get(s.type, 0) + 1
print(f"  types: {types}")

print()
failures = 0
for label in ("GameUserSettings.ini", "Game.ini"):
    path = os.path.join(CONFIG, label)
    ini = IniFile.load(path)
    view = describe_ini(ini, label, schema)
    # Documented settings land in wiki categories; everything else is grouped
    # by its ini section (per-mod). Both must be counted -- the total has to
    # equal every key/value line in the file, or something was dropped.
    in_categories = sum(len(c["settings"]) for c in view["categories"])
    in_sections = sum(len(s["settings"]) for s in view["sections"])
    rows = in_categories + in_sections
    raw = sum(1 for ln in ini.lines if ln.kind == KIND_KEYVALUE and ln.key)

    print(f"{label}")
    print(f"  key/value lines in file : {raw}")
    print(f"  rows produced           : {rows}  ({in_categories} documented + {in_sections} by section)")
    print(f"  known / unknown         : {view['counts']['known']} / {view['counts']['unknown']}")
    print(f"  repeated-key rows       : {view['counts']['repeated']}")
    if rows != raw:
        print(f"  FAIL: {raw - rows} setting(s) dropped")
        failures += 1
    else:
        print("  OK: every line survived into the view")
    for c in view["categories"]:
        print(f"    {c['name']:40} {len(c['settings'])}")
    for s in view["sections"]:
        tag = "orphan" if s["orphan"] else s["kind"]
        print(f"    [{s['label']}]{'':<{max(0, 38 - len(s['label']))}} {len(s['settings'])}  ({tag})")
    print()

# The repeated tuning lines must be individually addressable, not merged.
game = IniFile.load(os.path.join(CONFIG, "Game.ini"))
view = describe_ini(game, "Game.ini", schema)
stack = [
    r for group in (view["categories"] + view["sections"])
    for r in group["settings"]
    if r["key"] == "ConfigOverrideItemMaxQuantity"
]
print(f"ConfigOverrideItemMaxQuantity rows: {len(stack)} (expected 42)")
if len(stack) == 42 and stack[0]["occurrence"] == 1 and stack[-1]["occurrence"] == 42:
    print("  OK: each line is its own editable row, numbered 1..42")
else:
    print("  FAIL: repeated lines were collapsed or misnumbered")
    failures += 1

sys.exit(1 if failures else 0)
