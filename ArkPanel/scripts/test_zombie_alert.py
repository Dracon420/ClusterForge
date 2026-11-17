"""
A quiet log alone must not raise "may be hung".

An idle ARK map writes nothing for hours. That only looked like proof of a hang while
Cybers Structures logged a garbage-collection line every 5 minutes; removing that mod
from three maps turned every idle server into a warning (2026-09-16).

Run:  python scripts\\test_zombie_alert.py
"""

import os
import sys
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import monitor as monitor_mod                              # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got} (expected {want})")
    if not ok:
        FAILED.append(label)


SERVER = {"name": "astraeos", "log_dir": r"C:\fake\Logs", "install_dir": r"C:\fake"}


AGE = {"seconds": 5}


def run(alive):
    """One server seen healthy, then quiet for 20 minutes. Returns its hung alerts."""
    m = monitor_mod.Monitor(base_dir=BASE, config={}, servers_fn=lambda: [SERVER],
                            logger=lambda *_a: None, alive_fn=alive)
    # Stub the parts that would touch a real machine.
    monitor_mod.logtail.summarise = lambda _d: {"active_age_seconds": AGE["seconds"]}
    monitor_mod.procwatch.find_server_process = lambda _s: types.SimpleNamespace(pid=1)
    monitor_mod.procwatch.disk_free = lambda _p: None
    monitor_mod.procwatch.find_duplicates = lambda _s: [1]
    monitor_mod.procwatch.port_health = lambda *_a, **_k: {
        "bound": 7100, "expected": 7100, "state": "ok", "range": (7100, 7199)}

    AGE["seconds"] = 5          # writing normally
    m.check_server(SERVER)
    AGE["seconds"] = 1200       # 20 minutes of silence
    m.check_server(SERVER)
    return [a for a in m.alerts if a.kind == "log_silent"]


print("Quiet for 20 minutes but answering RCON:")
check("no hung alert - an idle map is not a hung one", len(run(lambda _s: True)), 0)

print("\nQuiet for 20 minutes and NOT answering:")
hung = run(lambda _s: False)
check("raises the alert", len(hung), 1)
check("says RCON is dead too", "not answering RCON" in (hung[0].message if hung else ""), True)

print("\nQuiet, and the probe itself failed:")
hung = run(lambda _s: (_ for _ in ()).throw(RuntimeError("rcon tool missing")))
check("still warns, since silence is unexplained", len(hung), 1)
check("admits it could not check", "could not be checked" in (hung[0].message if hung else ""), True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed")
