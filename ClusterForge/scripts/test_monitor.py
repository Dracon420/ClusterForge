"""Phase 6 gate: the alert state machine must not cry wolf.

Runs entirely against fake process/port readings -- no real server is touched.
What is being proven:

  - alerts fire on a state CHANGE, not on every poll
  - an uncertain reading (server still starting, no port bound yet) does NOT
    raise a false "port missing"
  - recovery is announced
  - a deliberate ClusterForge restart does not raise "server is down"
  - repeat alerts are rate-limited
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import monitor as monitor_mod  # noqa: E402
from core.monitor import Monitor  # noqa: E402

failures = 0


def check(label, condition, detail=""):
    global failures
    print(f"  {'OK  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures += 1


SERVER = {
    "name": "testmap", "install_dir": "", "log_dir": "",
    "expected_game_port": 7000, "port_range_min": 7000, "port_range_max": 7999,
    "rcon_port": 27015,
}

# --- fake the world the monitor observes -------------------------------------

class FakeProc:
    pid = 1234
    def __init__(self, port):
        self.bound_game_port = port
        self.all_udp_ports = [port] if port else []

STATE = {"running": True, "port": 7000, "dupes": [1234]}


class FakeProcwatch:
    @staticmethod
    def find_server_process(server):
        return FakeProc(STATE["port"]) if STATE["running"] else None

    @staticmethod
    def find_duplicates(server):
        return STATE["dupes"] if STATE["running"] else []

    @staticmethod
    def port_health(server, proc):
        lo, hi = server["port_range_min"], server["port_range_max"]
        bound = proc.bound_game_port if proc else None
        if bound is None:
            state, msg = "unknown", "server not running"
        elif bound == server["expected_game_port"]:
            state, msg = "ok", f"bound to expected port {bound}"
        elif lo <= bound <= hi:
            state, msg = "drifted", f"bound to {bound}, inside range"
        else:
            state, msg = "out_of_range", f"bound to {bound}, OUTSIDE range"
        return {"state": state, "bound": bound,
                "expected": server["expected_game_port"],
                "range": [lo, hi], "message": msg}

    @staticmethod
    def disk_free(path):
        return None


monitor_mod.procwatch = FakeProcwatch

work = tempfile.mkdtemp(prefix="clusterforge_monitor_")
BUSY = {"value": False}
sent = []

mon = Monitor(base_dir=work, config={}, servers_fn=lambda: [SERVER],
              busy_fn=lambda n: BUSY["value"],
              notify_fn=lambda a: sent.append(a),
              logger=lambda m: None)

print("=" * 74)
print("1. Steady state raises nothing")
print("=" * 74)
for _ in range(3):
    mon.tick()
check("three healthy ticks produce no alerts", len(sent) == 0, f"{len(sent)} sent")

print()
print("=" * 74)
print("2. Port drifts OUT of the forwarded range")
print("=" * 74)
STATE["port"] = 9999
mon.tick()
drift = [a for a in sent if a.kind == "port_drift"]
check("one port_drift alert raised", len(drift) == 1, f"{len(drift)}")
check("severity is critical", drift and drift[0].severity == "critical")
check("message says players cannot connect",
      drift and "cannot connect" in drift[0].message.lower())

before = len(sent)
for _ in range(3):
    mon.tick()
check("repeat ticks do not re-alert", len(sent) == before,
      f"{len(sent) - before} extra")

print()
print("=" * 74)
print("3. Recovery is announced")
print("=" * 74)
STATE["port"] = 7000
mon.tick()
rec = [a for a in sent if a.kind == "port_drift_recovered"]
check("recovery alert raised", len(rec) == 1)
check("recovery severity is info", rec and rec[0].severity == "info")

print()
print("=" * 74)
print("4. A still-starting server does NOT raise a false alarm")
print("=" * 74)
before = len(sent)
STATE["port"] = None          # process up, no port bound yet
mon.tick()
mon.tick()
new = [a for a in sent[before:] if "port" in a.kind]
check("no port alert while the port is unknown", len(new) == 0,
      f"{[a.kind for a in new]}")
STATE["port"] = 7000
mon.tick()

print()
print("=" * 74)
print("5. Duplicate processes are critical")
print("=" * 74)
before = len(sent)
STATE["dupes"] = [1234, 5678]
mon.tick()
dup = [a for a in sent[before:] if a.kind == "duplicate_process"]
check("duplicate_process alert raised", len(dup) == 1)
check("severity is critical", dup and dup[0].severity == "critical")
check("message names both PIDs", dup and "5678" in dup[0].message)
STATE["dupes"] = [1234]
mon.tick()

print()
print("=" * 74)
print("6. A deliberate ClusterForge restart is not reported as a crash")
print("=" * 74)
before = len(sent)
BUSY["value"] = True
STATE["running"] = False
mon.tick()
mon.tick()
down = [a for a in sent[before:] if a.kind == "server_down"]
check("no server_down while a control job is running", len(down) == 0,
      f"{[a.kind for a in sent[before:]]}")

print()
print("=" * 74)
print("7. An UNEXPECTED stop IS reported")
print("=" * 74)
before = len(sent)
BUSY["value"] = False
mon.tick()
down = [a for a in sent[before:] if a.kind == "server_down"]
check("server_down raised when ClusterForge did not stop it", len(down) == 1)
check("severity is critical", down and down[0].severity == "critical")

STATE["running"] = True
mon.tick()
back = [a for a in sent if a.kind == "server_down_recovered"]
check("recovery raised when it returns", len(back) == 1)

print()
shutil.rmtree(work, ignore_errors=True)
print(f"alerts raised in total: {len(sent)}")
if failures:
    print(f"RESULT: {failures} FAILURE(S)")
    sys.exit(1)
print("RESULT: PASS — alerts fire on real changes only")


# --------------------------------------------------------------------------
# Informational alerts are EVENTS and must age out of the active band.
#
# warn/critical describe a condition and stay until it clears. info describes
# something that already happened ("started automatically after a reboot"), so
# nothing will ever resolve it -- without a lifetime these permanently occupy
# the band where a real alert needs to stand out.
# --------------------------------------------------------------------------

import time as _time
from core.monitor import Alert, INFO_TTL_SECONDS

print()
print("=" * 66)
print("INFO ALERT LIFETIME")
print("=" * 66)

now = _time.time()
cases = [
    ("fresh info",              Alert("island", "autostart", "info", "up",
                                      at=now - 60),                      True),
    ("info past its lifetime",  Alert("island", "autostart", "info", "up",
                                      at=now - INFO_TTL_SECONDS - 1),    False),
    ("old warn still active",   Alert("island", "port_drift", "warn", "drift",
                                      at=now - 86400),                   True),
    ("old critical still active", Alert("island", "stopped", "critical", "down",
                                        at=now - 86400),                 True),
    ("resolved warn",           Alert("island", "port_drift", "warn", "drift",
                                      at=now - 60, resolved_at=now),     False),
]

failures = 0
for label, alert, expected in cases:
    got = alert.is_active(now)
    ok = got is expected
    failures += 0 if ok else 1
    print(f"  [{'ok ' if ok else 'FAIL'}] {label:28} active={got}")

# An aged-out info must be distinguishable from a condition that cleared.
expired = Alert("island", "autostart", "info", "up",
                at=now - INFO_TTL_SECONDS - 1).to_dict()
resolved = Alert("island", "port_drift", "warn", "drift",
                 at=now - 60, resolved_at=now).to_dict()
if not expired["expired"]:
    print("  [FAIL] an aged-out info alert is not flagged as expired")
    failures += 1
if resolved["expired"]:
    print("  [FAIL] a genuinely resolved alert is wrongly flagged as expired")
    failures += 1
if not failures:
    print("  [ok ] expired and resolved are reported distinctly")

# to_dict() adds derived keys; feeding them back to Alert() must not explode,
# because that is how the saved history is reloaded on restart.
fields = set(Alert.__dataclass_fields__)
try:
    Alert(**{k: v for k, v in expired.items() if k in fields})
    print("  [ok ] a saved alert round-trips back into Alert()")
except TypeError as exc:
    print(f"  [FAIL] saved alerts cannot be reloaded: {exc}")
    failures += 1

print()
if failures:
    print(f"FAILED: {failures} case(s)")
    raise SystemExit(1)
print("PASS: info alerts age out, conditions persist, history reloads")
