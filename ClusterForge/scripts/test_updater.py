"""Phase 7 gate: the update orchestrator must never split the cluster.

Entirely fake agents -- no server is touched and steamcmd is never run. What is
being proven:

  - both maps update TOGETHER, never one alone
  - an unreachable agent holds the WHOLE update, not just its own
  - an unknown version is never treated as "up to date"
  - nobody online  -> immediate, no countdown
  - players online -> full warning ladder
  - player count UNKNOWN -> warn anyway, never assume empty
  - two maps on different builds is flagged in its own right
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.updater import Updater  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures += 1


SERVERS = [{"name": "island"}, {"name": "svartalfheim"}]

CONFIG = {
    # settle_minutes 0: these tests are about cluster semantics, not the
    # publish-gap delay, which has its own gate in test_update_retry.py.
    "updates": {"enabled": True, "auto_apply": True, "poll_seconds": 420,
                "settle_minutes": 0},
    "schedule": {"warn_minutes": [30, 15, 10, 5, 4, 3, 2, 1]},
}

W = {}          # per-server world state the fakes read
started = []    # servers an update was actually started on


def reset(**kw):
    started.clear()
    W.clear()
    W.update({
        "versions": {"island": ("25117056", "25117056"),
                     "svartalfheim": ("25117056", "25117056")},
        "players": {"island": 0, "svartalfheim": 0},
        "reachable": {"island": True, "svartalfheim": True},
    })
    W.update(kw)


def version_fn(server):
    entry = W["versions"].get(server["name"])
    if entry is None:
        return None
    installed, available = entry
    if installed is None or available is None:
        status = "unknown"
    elif installed == available:
        status = "up_to_date"
    else:
        status = "update_available"
    return {"status": status, "installed": installed, "available": available,
            "errors": []}


def make(config=None):
    return Updater(
        config=config or CONFIG,
        servers_fn=lambda: SERVERS,
        version_fn=version_fn,
        players_fn=lambda s: W["players"].get(s["name"]),
        # takes target_build too: the agent needs it to verify the patch
        # actually landed rather than trusting steamcmd's exit code.
        update_fn=lambda s, warn, target=None: (
            started.append((s["name"], tuple(warn)))
            or {"ok": True, "job": {"id": "job-" + s["name"]}}),
        reachable_fn=lambda s: W["reachable"].get(s["name"], True),
        # Updates now run ONE MAP AT A TIME in a background thread, so the
        # runner needs a job status source and the assertions below have to wait
        # for it rather than reading `started` immediately.
        job_status_fn=lambda name, job_id: {"state": "done", "elapsed": 1},
        logger=lambda m: None,
    )


def settle(updater, seconds=5):
    """Wait for a staggered run to finish before asserting on `started`."""
    import time as _t
    deadline = _t.time() + seconds
    while _t.time() < deadline:
        if not getattr(updater, "_applying", False):
            return
        _t.sleep(0.05)


print("=" * 74)
print("1. Everything current -> nothing happens")
print("=" * 74)
reset()
r = make().apply()
check("no updates started", started == [], f"{started}")
check("reported as up to date", "up to date" in (r.get("message") or "").lower())

print()
print("=" * 74)
print("2. One map needs a patch -> BOTH are updated together")
print("=" * 74)
reset(versions={"island": ("25117056", "25200000"),
                "svartalfheim": ("25117056", "25117056")})
u = make()
r = u.apply()
settle(u)
names = sorted(n for n, _ in started)
check("both maps updated, not just the one that lagged",
      names == ["island", "svartalfheim"], f"{names}")

print()
print("=" * 74)
print("3. An unreachable agent holds the WHOLE update")
print("=" * 74)
reset(versions={"island": ("25117056", "25200000"),
                "svartalfheim": ("25117056", "25200000")},
      reachable={"island": True, "svartalfheim": False})
r = make().apply()
check("nothing was updated", started == [], f"{started}")
check("error is agent_unreachable", r.get("error") == "agent_unreachable")
check("message explains a split cluster is worse",
      "every map or none" in (r.get("message") or ""))

print()
print("=" * 74)
print("4. An unknown version is NOT treated as up to date")
print("=" * 74)
reset(versions={"island": ("25117056", "25200000"),
                "svartalfheim": (None, None)})
r = make().apply()
check("nothing was updated", started == [], f"{started}")
check("error is version_unknown", r.get("error") == "version_unknown")

print()
print("=" * 74)
print("5. Timing keys off players, not the clock")
print("=" * 74)
reset(versions={"island": ("1", "2"), "svartalfheim": ("1", "2")},
      players={"island": 0, "svartalfheim": 0})
u = make(); u.apply(); settle(u)
warns = {w for _, w in started}
check("nobody online -> no countdown", warns == {()}, f"{warns}")

reset(versions={"island": ("1", "2"), "svartalfheim": ("1", "2")},
      players={"island": 3, "svartalfheim": 0})
u = make(); u.apply(); settle(u)
warns = {w for _, w in started}
check("players online -> full ladder on BOTH maps",
      warns == {(30, 15, 10, 5, 4, 3, 2, 1)}, f"{warns}")

reset(versions={"island": ("1", "2"), "svartalfheim": ("1", "2")},
      players={"island": None, "svartalfheim": 0})
u = make()
plan = u.apply(dry_run=True)["plan"]
check("unknown player count -> warn anyway, never assume empty",
      tuple(plan["warn_minutes"]) == (30, 15, 10, 5, 4, 3, 2, 1),
      plan["reason"])
check("the reason says so", "unknown" in plan["reason"])

print()
print("=" * 74)
print("6. Two maps on different builds is flagged on its own")
print("=" * 74)
reset(versions={"island": ("25117056", "25117056"),
                "svartalfheim": ("25002750", "25117056")})
state = make().check()
check("build_mismatch detected", state["build_mismatch"] is True)
check("the lagging map is listed as needing an update",
      state["needs_update"] == ["svartalfheim"], f"{state['needs_update']}")

print()
print("=" * 74)
print("7. dry_run changes nothing")
print("=" * 74)
reset(versions={"island": ("1", "2"), "svartalfheim": ("1", "2")})
r = make().apply(dry_run=True)
check("no updates started", started == [], f"{started}")
check("a plan is returned", bool(r.get("plan")))

print()
if failures:
    print(f"RESULT: {failures} FAILURE(S)")
    sys.exit(1)
print("RESULT: PASS — updates apply to the whole cluster or not at all")
