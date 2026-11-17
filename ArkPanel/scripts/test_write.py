"""Phase 2 write-path gate.

Operates on COPIES of the real configs in a scratch directory. Never touches
the live server files.

The question this answers: when one of 42 identically-named tuning lines is
edited, does exactly that line change and nothing else?
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.inifile import (  # noqa: E402
    ChangeError, IniFile, KIND_KEYVALUE, apply_changes,
)

CONFIG = r"D:\Servers\TheIsland\ShooterGame\Saved\Config\WindowsServer"
SECTION = "/Script/ShooterGame.ShooterGameMode"
REPEATED = "ConfigOverrideItemMaxQuantity"

failures = 0
work = tempfile.mkdtemp(prefix="arkpanel_write_")
print(f"scratch: {work}\n")


def fail(msg):
    global failures
    failures += 1
    print(f"  FAIL {msg}")


_copies = [0]


def copy(name):
    """Fresh copy per call, in its own subdir.

    The live configs are read-only, and copy2 carries that across, so reusing
    one filename would make the second copy fail rather than overwrite.
    """
    _copies[0] += 1
    sub = os.path.join(work, f"c{_copies[0]}")
    os.makedirs(sub, exist_ok=True)
    dst = os.path.join(sub, name)
    shutil.copy2(os.path.join(CONFIG, name), dst)
    return dst


# ---- 1. edit occurrence 17 of 42 -------------------------------------------
print("1. Edit occurrence 17 of 42 repeated keys")
path = copy("Game.ini")
with open(path, "rb") as fh:
    before_bytes = fh.read()
before_lines = before_bytes.decode("utf-8").splitlines()

ini = IniFile.load(path)
target = ini.get_occurrence(SECTION, REPEATED, 17)
original_value = target.value
new_value = '(ItemClassString="ARKPANEL_TEST_C",Quantity=(MaxItemQuantity=1234,bIgnoreMultiplier=True))'
apply_changes(ini, [{"op": "set", "section": SECTION, "key": REPEATED,
                     "occurrence": 17, "value": new_value}])
ini.save(path)

after = IniFile.load(path)
with open(path, "rb") as fh:
    after_lines = fh.read().decode("utf-8").splitlines()

count = len(after.get_all(SECTION, REPEATED))
print(f"  {REPEATED} lines after edit: {count}")
if count != 42:
    fail(f"expected 42, got {count} — lines were lost or merged")

if len(before_lines) != len(after_lines):
    fail(f"line count changed: {len(before_lines)} -> {len(after_lines)}")
else:
    differing = [i for i, (a, b) in enumerate(zip(before_lines, after_lines)) if a != b]
    print(f"  lines differing: {len(differing)} (at index {differing})")
    if len(differing) != 1:
        fail(f"expected exactly 1 changed line, got {len(differing)}")
    elif new_value not in after_lines[differing[0]]:
        fail("the changed line does not contain the new value")
    else:
        print("  OK exactly one line changed, and it is the right one")

if after.get_occurrence(SECTION, REPEATED, 16).value == original_value:
    fail("occurrence 16 was overwritten — wrong line targeted")
if after.get_occurrence(SECTION, REPEATED, 18).value == new_value:
    fail("occurrence 18 also changed — edit leaked")

# ---- 2. round-trip still holds after a write --------------------------------
print("\n2. Round-trip after write")
with open(path, "rb") as fh:
    disk = fh.read()
if IniFile.load(path).to_bytes() == disk:
    print("  OK re-parses byte-identically")
else:
    fail("written file no longer round-trips")

# ---- 3. a stale occurrence is refused whole --------------------------------
print("\n3. Stale/invalid change is rejected without partial application")
path2 = copy("Game.ini")
ini2 = IniFile.load(path2)
snapshot = ini2.to_bytes()
try:
    apply_changes(ini2, [
        {"op": "set", "section": SECTION, "key": REPEATED, "occurrence": 1, "value": "X"},
        {"op": "set", "section": SECTION, "key": REPEATED, "occurrence": 999, "value": "Y"},
    ])
    fail("out-of-range occurrence was accepted")
except ChangeError as exc:
    print(f"  OK rejected: {exc}")
    if ini2.to_bytes() != snapshot:
        fail("file was mutated despite the batch being rejected")
    else:
        print("  OK nothing was mutated — batch failed whole")

# ---- 4. BOM file survives a write ------------------------------------------
print("\n4. GameUserSettings.ini (UTF-8 BOM) write")
path3 = copy("GameUserSettings.ini")
ini3 = IniFile.load(path3)
print(f"  encoding detected: {ini3.encoding}")
line = ini3.get_one("ServerSettings", "XPMultiplier")
if line is None:
    fail("XPMultiplier not found")
else:
    old = line.value
    apply_changes(ini3, [{"op": "set", "section": "ServerSettings",
                          "key": "XPMultiplier", "occurrence": 1, "value": "9.0"}])
    ini3.save(path3)
    with open(path3, "rb") as fh:
        head = fh.read(3)
    reread = IniFile.load(path3)
    ok_bom = head == b"\xef\xbb\xbf"
    ok_val = reread.get_one("ServerSettings", "XPMultiplier").value == "9.0"
    ok_rt = reread.to_bytes() == open(path3, "rb").read()
    print(f"  BOM preserved: {ok_bom} | value written: {ok_val} | round-trips: {ok_rt}")
    if not (ok_bom and ok_val and ok_rt):
        fail("BOM file write was not clean")
    sections = len(reread.sections())
    print(f"  sections intact: {sections}")
    if sections != 21:
        fail(f"expected 21 sections, got {sections}")
    print(f"  (XPMultiplier {old} -> 9.0 in the copy only)")

# ---- 5. delete removes exactly one line ------------------------------------
print("\n5. Delete one of the repeated lines")
path4 = copy("Game.ini")
ini4 = IniFile.load(path4)
apply_changes(ini4, [{"op": "delete", "section": SECTION, "key": REPEATED, "occurrence": 5}])
ini4.save(path4)
remaining = len(IniFile.load(path4).get_all(SECTION, REPEATED))
print(f"  {REPEATED} lines: 42 -> {remaining}")
if remaining != 41:
    fail(f"expected 41 after delete, got {remaining}")
else:
    print("  OK exactly one removed")

# ---- 6. read-only flag is preserved ----------------------------------------
# Both servers keep Game.ini and GameUserSettings.ini read-only deliberately:
# ARK rewrites GameUserSettings.ini on shutdown and the flag is what stops it
# discarding manual edits. A save that leaves the flag off would let the next
# restart silently wipe the operator's settings.
print("\n6. Read-only flag preserved across a write")
path5 = copy("Game.ini")
os.chmod(path5, 0o444)
was_ro_before = not os.access(path5, os.W_OK)
ini5 = IniFile.load(path5)
_, reported = ini5.save(path5)
was_ro_after = not os.access(path5, os.W_OK)
print(f"  read-only before: {was_ro_before} | reported by save: {reported} | after: {was_ro_after}")
if not (was_ro_before and reported and was_ro_after):
    fail("read-only flag was not preserved — ARK could overwrite the file on shutdown")
else:
    print("  OK flag cleared for the write and restored afterwards")

os.chmod(path5, 0o666)
print()
shutil.rmtree(work, ignore_errors=True)
if failures:
    print(f"RESULT: {failures} FAILURE(S) — do not enable writing")
    sys.exit(1)
print("RESULT: PASS — write path is safe to enable")
