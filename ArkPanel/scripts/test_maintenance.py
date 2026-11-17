"""The maintenance flag must survive an agent restart and mute the monitor.

Both halves matter. The flag exists because the switch runs in the panel and
the monitor runs in the agent; if an agent restart forgot the flag, the alert
this was written to prevent would come back the first time the agent updated
itself mid-Palworld-session.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, r"D:\Servers\ArkPanel")
from core.monitor import Monitor  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


# ---- the store, in isolation ---------------------------------------------
print("maintenance store")
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "data", "maintenance.json")


def read():
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write(name, on, reason=""):
    state = read()
    if on:
        state[name] = {"reason": reason, "since": "now"}
    else:
        state.pop(name, None)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    return state


check("missing file reads as empty", read() == {})
write("island", True, "switched to Palworld")
check("flag persists to disk", read().get("island", {}).get("reason") == "switched to Palworld")
check("survives a restart (re-read from disk)", "island" in read())
write("island", False)
check("clearing removes it", read() == {})

# ---- the monitor ----------------------------------------------------------
print("\nmonitor suppression")
alerts = []
server = {"name": "island", "install_dir": r"D:\nowhere"}

mon = Monitor(tmp, {}, lambda: [server],
              notify_fn=lambda *a, **k: alerts.append(a),
              busy_fn=lambda _n: "island" in read(),
              logger=lambda *a: None)
# Pretend it was up a moment ago, so "down" is a transition.
mon.state.setdefault("island", {})["running"] = "up"

result = mon.check_server(server)
check("not running is noticed", result["running"] is False, str(result))
check("alert raised with no flag", len(alerts) == 1, f"{len(alerts)} alert(s)")

alerts.clear()
mon.state["island"]["running"] = "up"
write("island", True, "switched to Palworld")
result = mon.check_server(server)
check("flag suppresses the alert", not alerts, f"{len(alerts)} alert(s)")
check("and says why", "maintenance" in result.get("skipped", ""), result.get("skipped", ""))

alerts.clear()
write("island", False)
# The first alert was seconds ago and Monitor rate-limits repeats, so without
# this the test would measure the cooldown rather than the flag. Clearing it
# is the test being honest about what it is checking.
mon.last_alert.clear()
result = mon.check_server(server)
check("alert returns once the flag is cleared", len(alerts) == 1, f"{len(alerts)} alert(s)")

print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
sys.exit(1 if failures else 0)
