"""config.json and secrets.json must not carry a byte-order mark.

ArkPanel.py reads config.json as plain utf-8. A BOM makes json.load fail with
"Unexpected UTF-8 BOM" and the desktop app will not open at all - while the
panel and agent, which read utf-8-sig, carry on working perfectly. So the
symptom is "the app is broken" with every service healthy and the web UI
loading fine, which is a miserable thing to debug.

It got there because Windows PowerShell 5.1 writes a BOM for -Encoding utf8,
and scripts/install-game-task.ps1 rewrote config.json that way. Any future
script that touches these files can do it again, so this checks rather than
trusts - and the launcher is now BOM-tolerant as well, because defending in
one place only works until someone writes another script.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOM = bytes([0xEF, 0xBB, 0xBF])
FAILED = []

for name in ("config.json", "secrets.json"):
    path = os.path.join(BASE, name)
    if not os.path.isfile(path):
        continue
    raw = open(path, "rb").read()
    ok = not raw.startswith(BOM)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name} has no BOM")
    if not ok:
        FAILED.append(f"{name} has a BOM")

# And the launcher should survive one anyway - belt as well as braces.
launcher = open(os.path.join(BASE, "ArkPanel.py"), encoding="utf-8").read()
tolerant = 'encoding="utf-8-sig"' in launcher
print(f"  [{'ok  ' if tolerant else 'FAIL'}] ArkPanel.py reads utf-8-sig, so a "
      "BOM cannot stop it opening")
if not tolerant:
    FAILED.append("ArkPanel.py reads plain utf-8")

# No script should write these files with PowerShell's BOM-adding default.
scripts_dir = os.path.join(BASE, "scripts")
offenders = []
for name in sorted(os.listdir(scripts_dir)):
    if not name.endswith(".ps1"):
        continue
    text = open(os.path.join(scripts_dir, name), encoding="utf-8",
                errors="replace").read()
    for line in text.splitlines():
        if "config.json" in line and "-Encoding utf8" in line:
            offenders.append(f"{name}: {line.strip()[:70]}")
print(f"  [{'ok  ' if not offenders else 'FAIL'}] no .ps1 writes config.json "
      "with PowerShell's BOM-adding -Encoding utf8")
for line in offenders:
    print(f"          {line}")
FAILED.extend(offenders)

# Stray control characters in scripts.
#
# Written after a generated line in install-game-task.ps1 ended up containing
# a literal BACKSPACE (0x08): a Python heredoc turned a backslash-b
# escape into the control character it names, so a path meant to end in
# bin/java.exe came out with an invisible 0x08 in it and PowerShell
# refused it with "Illegal characters in path".
#
# The first run of this check found a backspace in its OWN comment, put
# there the same way while describing the bug. Which is the argument for
# having the check.
# so the path read jre\jdk-...-jre<BS>in\java.exe and PowerShell refused it
# with "Illegal characters in path". Invisible in an editor, obvious here.
#
# This is the third time escaping has silently corrupted a file in this
# project, after PowerShell mojibake in source and a newline baked into a JS
# string literal. Cheap to check, miserable to debug.
print()
print("No stray control characters in scripts:")
ALLOWED = {9, 10, 13}        # tab, newline, carriage return
dirty = []
for folder in (BASE, scripts_dir):
    for name in sorted(os.listdir(folder)):
        if not name.endswith((".ps1", ".py", ".js", ".json", ".html", ".css")):
            continue
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        try:
            text = open(path, encoding="utf-8", errors="strict").read()
        except (OSError, UnicodeDecodeError):
            continue
        bad = {ord(ch) for ch in text if ord(ch) < 32 and ord(ch) not in ALLOWED}
        if bad:
            dirty.append(f"{name}: {sorted(hex(b) for b in bad)}")
print(f"  [{'ok  ' if not dirty else 'FAIL'}] every script is free of control characters")
for line in dirty:
    print(f"          {line}")
FAILED.extend(dirty)

print()
print("all checks passed" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}")
sys.exit(1 if FAILED else 0)
