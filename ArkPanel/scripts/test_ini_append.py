"""
Prove adding a key never glues itself onto the previous line.

An ini file whose last line has no trailing newline used to produce
"bAutoUnlockAllEngrams=TrueDinoClassDamageMultipliers=(...)" - one broken line that
silently disabled automatic engram unlocking on The Island and Scorched Earth (2026-09-14).

Run:  python scripts\\test_ini_append.py
"""

import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.inifile import IniFile, apply_changes                     # noqa: E402

FAILED = []
SECTION = "/Script/ShooterGame.ShooterGameMode"


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r} (expected {want!r})")
    if not ok:
        FAILED.append(label)


def run(original: str, changes: list) -> str:
    tmp = tempfile.mktemp(suffix=".ini")
    with open(tmp, "wb") as fh:
        fh.write(original.encode("utf-8"))
    ini = IniFile.load(tmp)
    check("parses back byte-identically", ini.to_bytes(), original.encode("utf-8"))
    apply_changes(ini, changes)
    ini.save(tmp)
    # newline="" or Python turns every \r\n into \n and the line-ending checks lie.
    text = open(tmp, encoding="utf-8", newline="").read()
    os.remove(tmp)
    return text


add = [{"op": "add", "section": SECTION, "key": "DinoClassDamageMultipliers",
        "value": '(ClassName="SpiderL_Character_BP_C",Multiplier=1.25)'}]

print("File with NO trailing newline (the case that broke):")
out = run(f"[{SECTION}]\r\nbAutoUnlockAllEngrams=True", add)
check("engram line intact", "bAutoUnlockAllEngrams=True\r\n" in out, True)
check("new key on its own line", out.rstrip().endswith("Multiplier=1.25)"), True)
check("nothing glued together", "TrueDino" in out, False)

print("\nFile that already ends with a newline:")
out = run(f"[{SECTION}]\r\nbAutoUnlockAllEngrams=True\r\n", add)
check("engram line intact", "bAutoUnlockAllEngrams=True\r\n" in out, True)
check("nothing glued together", "TrueDino" in out, False)
check("no blank line inserted", "\r\n\r\n" in out, False)

print("\nAdding to a section that does not exist yet:")
out = run("[OtherSection]\r\nFoo=1", [{"op": "add", "section": SECTION,
                                       "key": "bAutoUnlockAllEngrams", "value": "True"}])
check("previous line kept", "Foo=1\r\n" in out, True)
check("section created", f"[{SECTION}]" in out, True)
check("nothing glued together", "1[" in out, False)

print("\nSeveral keys appended in one go stay on separate lines:")
many = [{"op": "add", "section": SECTION, "key": f"Key{i}", "value": str(i)} for i in range(3)]
out = run(f"[{SECTION}]\r\nbAutoUnlockAllEngrams=True", many)
check("three new lines", sum(1 for line in out.splitlines() if line.startswith("Key")), 3)
check("nothing glued together", "TrueKey" in out, False)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed")
