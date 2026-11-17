"""
Prove the change-triggered Discord refresh fires once, not once per edit.

The refresh re-renders three channels and talks to Discord, so a mod change
that fired it per-map -- or per mod in a batch -- would edit the same posts
repeatedly for one logical change. This checks the debounce collapses a burst
into a single publish, and that a later request after the window fires again.

Nothing here touches Discord or the servers: _publish_all is replaced with a
recorder. Importing panel is safe because everything that starts bots and
threads lives in main().

Run:  python scripts\\test_info_refresh.py
"""

import os
import sys
import threading
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import panel                                                    # noqa: E402

FAILED = []
calls = []
lock = threading.Lock()


def fake_publish():
    with lock:
        calls.append(time.time())
    return {"ok": True, "results": {"island": {"edited": 1}}}


panel._publish_all = fake_publish
panel.plog = lambda msg: None          # keep the test quiet


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got} (expected {want})")
    if not ok:
        FAILED.append(label)


print("A burst of changes collapses into one publish:")
calls.clear()
for i in range(5):
    panel.request_info_refresh(f"change {i}", delay=0.6)
    time.sleep(0.05)
time.sleep(1.6)
check("5 rapid changes -> publishes", len(calls), 1)

print("\nA later change fires again:")
calls.clear()
panel.request_info_refresh("first", delay=0.4)
time.sleep(1.0)
panel.request_info_refresh("second", delay=0.4)
time.sleep(1.0)
check("2 separated changes -> publishes", len(calls), 2)

print("\nThe publish runs off the caller's thread (a save must not block):")
calls.clear()
started = time.time()
panel.request_info_refresh("non-blocking", delay=0.5)
elapsed = time.time() - started
check("request returned immediately", elapsed < 0.2, True)
check("nothing published yet", len(calls), 0)
time.sleep(1.0)
check("published after the delay", len(calls), 1)

print("\nA crash in publishing does not escape:")
calls.clear()
panel._publish_all = lambda: (_ for _ in ()).throw(RuntimeError("discord down"))
try:
    panel.request_info_refresh("boom", delay=0.3)
    time.sleep(0.9)
    check("survived a failing publish", True, True)
except Exception as exc:                                        # noqa: BLE001
    check(f"survived a failing publish ({exc})", False, True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed - Discord was never contacted")
