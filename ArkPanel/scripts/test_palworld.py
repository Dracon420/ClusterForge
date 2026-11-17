"""
Palworld settings handling.

Palworld keeps EVERY setting on one line:

    OptionSettings=(Difficulty=None,ServerName="x",ServerPassword="y",...)

so changing one value means rewriting the whole line. The two ways that goes
wrong are splitting on commas inside a quoted name, and dropping keys the panel
does not know about - Palworld adds settings between versions, and a rewrite
that silently reset them would be far worse than one that refused.

No real install is touched; everything happens in a temp folder.

Run:  python scripts\\test_palworld.py
"""

import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import palworld                                            # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r} (expected {want!r})")
    if not ok:
        FAILED.append(label)


DEFAULT = (
    "[/Script/Pal.PalGameWorldSettings]\n"
    'OptionSettings=(Difficulty=None,DayTimeSpeedRate=1.000000,'
    'ExpRate=1.000000,ServerPlayerMaxNum=32,ServerName="Default Palworld Server",'
    'ServerDescription="",AdminPassword="",ServerPassword="",PublicPort=8211,'
    'PublicIP="",RCONEnabled=False,RCONPort=25575,bUseAuth=True,'
    'CrossplayPlatforms=(Steam,Xbox,PS5,Mac),SomeFutureSetting=42)\n'
)

root = tempfile.mkdtemp(prefix="palworld-test-")
install = os.path.join(root, "Palworld")
os.makedirs(install)
with open(palworld.default_settings_path(install), "w", encoding="utf-8") as fh:
    fh.write(DEFAULT)

try:
    print("Reading before anything is written:")
    check("no live settings file yet", palworld.read_settings(install), {})

    print("\nFirst write creates it from the default, as the docs require:")
    applied = palworld.write_settings(install, {
        "ServerName": "[ToDs]Dracon",
        "ServerPassword": "123456",
        "AdminPassword": "supersecret",
        "RCONEnabled": "True",
        "ServerPlayerMaxNum": "8",
    })
    check("file now exists", os.path.isfile(palworld.settings_path(install)), True)
    check("five keys changed", len(applied), 5)

    now = palworld.read_settings(install)
    check("server name quoted", now["ServerName"], '"[ToDs]Dracon"')
    check("password quoted", now["ServerPassword"], '"123456"')
    check("RCON on", now["RCONEnabled"], "True")
    check("player cap set", now["ServerPlayerMaxNum"], "8")

    print("\nEverything else survived:")
    check("untouched setting kept", now["DayTimeSpeedRate"], "1.000000")
    check("a setting this panel has never heard of is kept",
          now["SomeFutureSetting"], "42")
    check("crossplay list kept whole - Xbox must stay in it",
          now["CrossplayPlatforms"], "(Steam,Xbox,PS5,Mac)")
    check("no keys lost", len(now), 15)

    print("\nA name containing a comma - the classic parser trap:")
    palworld.write_settings(install, {"ServerName": "Dracon, the server"})
    now = palworld.read_settings(install)
    check("name survived intact", now["ServerName"], '"Dracon, the server"')
    check("still no keys lost", len(now), 15)
    check("the key AFTER it is still itself", now["ServerDescription"], '""')

    print("\nWriting the same value twice reports no change:")
    again = palworld.write_settings(install, {"ServerName": "Dracon, the server"})
    check("nothing applied", again, {})

    print("\nA backup is kept before each write:")
    backups = [f for f in os.listdir(os.path.dirname(palworld.settings_path(install)))
               if ".bak-" in f]
    check("at least one backup", len(backups) >= 1, True)

    print("\nRefusing to guess when there is no file at all:")
    empty = os.path.join(root, "NotInstalled")
    os.makedirs(empty)
    try:
        palworld.write_settings(empty, {"ServerName": "x"})
        check("raises", False, True)
    except FileNotFoundError as exc:
        check("says what is missing", "installed" in str(exc), True)

    print("\nLaunch arguments:")
    check("community server flag present - Xbox needs it",
          "-publiclobby" in palworld.DEFAULT_ARGS, True)
    check("performance flags present",
          "-UseMultithreadForDS" in palworld.DEFAULT_ARGS, True)

finally:
    shutil.rmtree(root, ignore_errors=True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed - no real install was touched")


# ---------------------------------------------------------------- ownership
#
# Added after the world was corrupted twice by the server being KILLED rather
# than shut down. It had been launched as a child of a terminal session, and
# Windows tears down a session's whole process tree on exit. Level.sav holds
# bases, structures and containers; the player .sav files hold levels. So the
# damage always looked the same: "structures and inventory gone, levels fine".
#
# Both defences are one line each to undo by accident, so both are pinned.
import subprocess as _sp

print()
print("Ownership - the server must not die with its launcher:")
_flags = palworld.DETACHED
check("breaks out of the launcher's job object",
      bool(_flags & getattr(_sp, "CREATE_BREAKAWAY_FROM_JOB", 0)), True)
check("gets no console to be signalled through",
      bool(_flags & getattr(_sp, "DETACHED_PROCESS", 0)), True)

_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "core", "palworld.py"), encoding="utf-8").read()
check("start() prefers a scheduled task when one is configured",
      'config.get("task_name")' in _src, True)

print()
print("Backups - a bad save must never be the only save:")
check("snapshot() exists", hasattr(palworld, "snapshot"), True)
check("snapshots before starting (last moment prior damage is recoverable)",
      'reason="pre-start"' in _src, True)
check("snapshots before any forced kill",
      'reason="BEFORE-FORCED-KILL"' in _src, True)
check("snapshots after a clean stop",
      'reason="clean-stop"' in _src, True)


# ------------------------------------- settings must be written while STOPPED
#
# Palworld REWRITES PalWorldSettings.ini from its in-memory values when it
# shuts down. So a setting saved while the server is running survives only
# until the next stop, which clobbers it - and the server then starts on its
# own old values. Measured 2026-10-12: written while running, 0 of 18 survived;
# written while stopped, 18 of 18.
#
# The symptom is brutal to diagnose from outside: the file looks right
# immediately after saving, the panel says "saved", and the game behaves as if
# nothing happened. So the ORDER is the thing worth pinning.
print()
print("Settings are applied with the server stopped:")

_panel = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "panel.py"), encoding="utf-8").read()
_fn = _panel[_panel.index("def palworld_settings_set"):]
_fn = _fn[:_fn.index("@app.post(", 10)]

check("it checks whether the server is running", "find_process" in _fn, True)
check("a running server is stopped before writing",
      _fn.index("palworld.stop") < _fn.index("write_settings", _fn.index("palworld.stop")), True)
check("and started again afterwards",
      _fn.index("write_settings", _fn.index("palworld.stop"))
      < _fn.index("palworld.start", _fn.index("palworld.stop")), True)
check("it says why it has to restart", "overwrites" in _fn.lower(), True)

print()
print("all checks passed" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}")
sys.exit(1 if FAILED else 0)
