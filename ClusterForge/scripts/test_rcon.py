"""
No shipped module may depend on PatchRaptor.

This file used to exercise PatchRaptor's RCONManager and its allowlist, back
when ClusterForge reached its servers through `rcon_wrapper.bat`. That dependency
is gone -- core/rcon.py speaks the protocol itself -- and it must not come
back, because PatchRaptor is another author's software under a licence that
forbids passing it on to anyone else. Shipping ClusterForge with it would make the
whole thing undistributable.

The protocol itself is tested in test_rcon_native.py against a fake server.

Run:  python scripts\\test_rcon.py
"""

import io
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got} (expected {want})")
    if not ok:
        FAILED.append(label)


# Everything that would land on a customer's machine.
SHIPPED = ["agent.py", "panel.py"]
for name in sorted(os.listdir(os.path.join(BASE, "core"))):
    if name.endswith(".py"):
        SHIPPED.append(os.path.join("core", name))

IMPORT = re.compile(r"^\s*(?:from|import)\s+patchraptor\b", re.M)
WRAPPER = re.compile(r"rcon_wrapper")

print(f"checking {len(SHIPPED)} shipped files")
importers, wrappers = [], []
for rel in SHIPPED:
    text = io.open(os.path.join(BASE, rel), encoding="utf-8", errors="replace").read()
    if IMPORT.search(text):
        importers.append(rel)
    # A mention in a comment is history, not a dependency. Only code counts.
    for line in text.splitlines():
        stripped = line.strip()
        if WRAPPER.search(stripped) and not stripped.startswith("#"):
            wrappers.append(f"{rel}: {stripped[:70]}")

check("no module imports patchraptor", importers, [])
check("no module builds an rcon_wrapper path", wrappers, [])

print("\nand the replacement is actually wired in:")
from core import control, rcon                                       # noqa: E402

check("control exposes rcon_command", callable(control.rcon_command), True)
check("core.rcon does the talking", control.rcon_mod is rcon, True)
check("rcon_tool argument is optional now",
      "rcon_tool" in control.rcon_command.__code__.co_varnames, True)

print("\nthe shell allowlist went with the shell:")
clean, changed = control.sanitise_message("Restarting in 5 minutes (save & log out)")
check("brackets and ampersands survive", clean,
      "Restarting in 5 minutes (save & log out)")
check("message reported unchanged", changed, False)
clean, changed = control.sanitise_message("two\nlines")
check("newlines still stripped - they would split the command", clean, "two lines")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed - ClusterForge owes nothing to PatchRaptor")
