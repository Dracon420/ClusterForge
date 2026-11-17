"""Gate: boot auto-start must fire after a reboot and never after a deploy.

The dangerous failure is not "it didn't start" -- it is an agent restart during
a deliberate outage silently bringing a server back up while someone is
removing a mod or restoring a save. That is what the boot window prevents, so
it is what this gate pins down.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.control import should_autostart  # noqa: E402

ON = {"enabled": True, "boot_window_seconds": 900, "delay_seconds": 60}

CASES = [
    # (label, cfg, seconds since boot, expected go)
    ("just booted, agent up in 95s",        ON,                        95,    True),
    ("booted, right at the window edge",    ON,                        899,   True),
    ("one second past the window",          ON,                        901,   False),
    ("deploy restart, 6h uptime",           ON,                        21600, False),
    ("deploy restart, 3 days uptime",       ON,                        259200, False),
    ("disabled outright",                   {"enabled": False},        60,    False),
    ("missing enabled key",                 {},                        60,    False),
    ("boot time unreadable",                ON,                        -1,    False),
    ("garbage window value",                {"enabled": True, "boot_window_seconds": "soon"}, 60, False),
    ("custom short window, inside",         {"enabled": True, "boot_window_seconds": 300}, 120, True),
    ("custom short window, outside",        {"enabled": True, "boot_window_seconds": 300}, 400, False),
]

failures = 0
for label, cfg, since_boot, expected in CASES:
    go, reason = should_autostart(cfg, since_boot)
    ok = go is expected
    if not ok:
        failures += 1
    print(f"  [{'ok ' if ok else 'FAIL'}] {label:36} -> {go}  ({reason})")

# Every refusal must explain itself; a silent stand-down is what left the
# cluster down without anyone knowing why.
for label, cfg, since_boot, expected in CASES:
    go, reason = should_autostart(cfg, since_boot)
    if not reason.strip():
        print(f"  [FAIL] {label}: refused with no reason given")
        failures += 1

print()
if failures:
    print(f"FAILED: {failures} case(s)")
    sys.exit(1)
print(f"PASS: {len(CASES)} autostart decisions correct")
