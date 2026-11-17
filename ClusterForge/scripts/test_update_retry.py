"""Gate: a failing patch must not cycle the cluster, and a fake success must fail.

Written after the first real patch (2026-09-08) turned a Steam-side refusal
into a rolling outage: the updater retried every 7 minutes, stopping both maps
each time. Three behaviours are pinned here.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import steam                                      # noqa: E402
from core.updater import _target_build, BUILD_RETRY_COOLDOWN, MAX_BUILD_ATTEMPTS  # noqa: E402

failures = 0


def check(label, got, expected):
    global failures
    ok = got == expected
    if not ok:
        failures += 1
    print(f"  [{'ok ' if ok else 'FAIL'}] {label:52} -> {got!r}")


print("1. The real Steam refusal is recognised as retryable")
# Verbatim from content_log.txt during the incident.
real = ("CDepotDownloadMgr::BYldRequestDepotManifest(App: 2430930, Depot: 2430931, "
        "Manifest: 000000000000000000, branch: ): Failed to get manifest request "
        "code, 'Access Denied'")
check("manifest request code / Access Denied", steam.looks_transient(real), True)
check("update canceled, No connection",
      steam.looks_transient("AppID 2430930 update canceled : Failed downloading 1 manifests (No connection)"), True)
check("console 'state is 0x6'",
      steam.looks_transient("Error! App '2430930' state is 0x6 after update job."), True)

print("\n2. A genuinely broken install is NOT retried forever")
check("disk full", steam.looks_transient("Not enough disk space"), False)
check("missing steamcmd", steam.looks_transient("could not run steamcmd: file not found"), False)
check("corrupt files", steam.looks_transient("Manifest corrupt, please reinstall"), False)

print("\n3. update_with_retries retries transient, gives up on permanent")
calls = {"n": 0}
slept = []


def fake_transient(*_a, **_k):
    calls["n"] += 1
    return {"ok": False, "exit_code": 8, "tail": ["Error! App '2430930' state is 0x6"],
            "error": "steamcmd exited 8 without reporting success"}


def fake_permanent(*_a, **_k):
    calls["n"] += 1
    return {"ok": False, "exit_code": 1, "tail": ["Not enough disk space"],
            "error": "Not enough disk space"}


def fake_success_on_third(*_a, **_k):
    calls["n"] += 1
    if calls["n"] < 3:
        return {"ok": False, "exit_code": 8, "tail": ["state is 0x6"], "error": "x"}
    return {"ok": True, "exit_code": 0, "seconds": 5, "tail": ["Success! App fully installed"]}


original = steam.run_update
steam._content_log_tail = lambda *_a, **_k: []
try:
    steam.run_update = fake_transient
    calls["n"] = 0; slept.clear()
    r = update = steam.update_with_retries("x", "y", attempts=3, sleep_fn=slept.append)
    check("transient: attempted 3 times", calls["n"], 3)
    check("transient: backed off twice", len(slept), 2)
    check("transient: backoff increases", slept == sorted(slept) and slept[0] < slept[-1], True)
    check("transient: still reports failure", r["ok"], False)

    steam.run_update = fake_permanent
    calls["n"] = 0; slept.clear()
    r = steam.update_with_retries("x", "y", attempts=3, sleep_fn=slept.append)
    check("permanent: attempted only once", calls["n"], 1)
    check("permanent: never slept", len(slept), 0)

    steam.run_update = fake_success_on_third
    calls["n"] = 0; slept.clear()
    r = steam.update_with_retries("x", "y", attempts=3, sleep_fn=slept.append)
    check("recovers on the third attempt", r["ok"], True)
    check("reports which attempt won", r["attempt"], 3)
finally:
    steam.run_update = original

print("\n4. The target build is derived from per-server availability")
check("both agents agree",
      _target_build({"servers": {"a": {"available": "25176147"},
                                 "b": {"available": "25176147"}}}), "25176147")
check("agents disagree -> highest wins",
      _target_build({"servers": {"a": {"available": "25176147"},
                                 "b": {"available": "25117056"}}}), "25176147")
check("nothing known -> empty, so no cooldown key",
      _target_build({"servers": {"a": {}, "b": {}}}), "")

print("\n5. Cooldown constants are sane")
# The property that matters is that a failed build is not retried on the next
# poll -- that is what turned one Steam refusal into a rolling outage. 30
# minutes is also roughly how long Steam's manifest refusals take to clear, so
# the cooldown is long enough to be worth waiting out rather than fighting.
POLL_SECONDS = 420
check("cooldown far exceeds one poll interval",
      BUILD_RETRY_COOLDOWN >= 4 * POLL_SECONDS, True)
check("cooldown is at least 30 minutes", BUILD_RETRY_COOLDOWN >= 1800, True)
check("gives up after a few attempts", 2 <= MAX_BUILD_ATTEMPTS <= 5, True)
check("worst case holds for hours, not days",
      BUILD_RETRY_COOLDOWN * MAX_BUILD_ATTEMPTS <= 6 * 3600, True)



# ---------------------------------------------------------------------------
# Added after the 2026-09-08 post-mortem: the real cause was a STUCK LOCAL
# MANIFEST, not Steam, and the real cost was refusing to restart afterwards.
# ---------------------------------------------------------------------------

import tempfile
import shutil

print("\n6. Pre-flight detects a stuck manifest BEFORE anything is stopped")

def make_install(state_flags, build="25117056", target=None, downloaded=None):
    d = tempfile.mkdtemp(prefix="clusterforge_t_")
    os.makedirs(os.path.join(d, "steamapps"))
    body = '"AppState"\n{\n'
    body += f'\t"StateFlags"\t\t"{state_flags}"\n'
    body += f'\t"buildid"\t\t"{build}"\n'
    if target:
        body += f'\t"TargetBuildID"\t\t"{target}"\n'
    if downloaded is not None:
        body += f'\t"BytesDownloaded"\t\t"{downloaded}"\n'
    body += "}\n"
    with open(os.path.join(d, "steamapps", "appmanifest_2430930.acf"), "w") as fh:
        fh.write(body)
    return d

fake_steamcmd = tempfile.NamedTemporaryFile(suffix=".exe", delete=False).name

# The exact state Island and Svartalfheim were stuck in.
stuck = make_install(6, target="25176147")
healthy = make_install(4)
try:
    p = steam.preflight(stuck, fake_steamcmd)
    check("stuck manifest (StateFlags 6) is caught", p["ok"], False)
    check("and it is auto-fixable", "clear_manifest" in p["fixable"], True)
    check("the pending build is reported", p["target"], "25176147")

    p2 = steam.preflight(healthy, fake_steamcmd)
    check("healthy install passes pre-flight", p2["ok"], True)
    check("healthy install needs no fixing", p2["fixable"], [])

    p3 = steam.preflight(healthy, os.path.join(healthy, "nope.exe"))
    check("missing steamcmd fails pre-flight", p3["ok"], False)

    moved = steam.clear_stuck_manifest(stuck)
    check("clearing keeps the old manifest as a backup", bool(moved) and os.path.exists(moved), True)
    check("and removes the live one",
          os.path.exists(steam.manifest_path(stuck)), False)

    print("\n7. After a failure, is it safe to restart the server?")
    untouched = make_install(6, target="25176147", downloaded=0)
    check("nothing downloaded -> safe to restart",
          steam.wrote_anything(untouched), False)

    partial = make_install(1026, build="0", downloaded=165075264)
    check("partial download -> stay stopped", steam.wrote_anything(partial), True)

    midway = make_install(6, downloaded=0)
    os.makedirs(os.path.join(midway, "steamapps", "downloading", "2430931"))
    with open(os.path.join(midway, "steamapps", "downloading", "2430931", "chunk"), "wb") as fh:
        fh.write(b"x" * 16)
    check("files in downloading/ -> stay stopped", steam.wrote_anything(midway), True)

    gone = make_install(4)
    os.remove(steam.manifest_path(gone))
    check("manifest missing mid-reacquire -> stay stopped (cautious)",
          steam.wrote_anything(gone), True)
finally:
    for d in (stuck, healthy):
        shutil.rmtree(d, ignore_errors=True)
    try:
        os.unlink(fake_steamcmd)
    except OSError:
        pass



# ---------------------------------------------------------------------------
# Staggering and the settle delay, both added at the user's suggestion after
# the 2026-09-08 outage. Sequential patching halves the blast radius of a
# failure; the settle delay stops us patching into Steam's publish gap.
# ---------------------------------------------------------------------------

import time  # noqa: E402
from core.updater import Updater  # noqa: E402

print("\n8. Staggered updates stop at the first failure")

def build_updater(cfg, job_states, started):
    servers = [{"name": "island"}, {"name": "svartalfheim"}]

    def version(server):
        return {"status": "update_available", "installed": "25117056",
                "available": "25176147"}

    def update(server, warn, target=None):
        started.append(server["name"])
        return {"ok": True, "job": {"id": f"job-{server['name']}"}}

    def job_status(name, job_id):
        return {"state": job_states.get(name, "done"), "elapsed": 1,
                "error": "steamcmd refused" if job_states.get(name) == "failed" else None}

    return Updater(config={"updates": cfg}, servers_fn=lambda: servers,
                   version_fn=version, players_fn=lambda s: 0,
                   update_fn=update, reachable_fn=lambda s: True,
                   job_status_fn=job_status, logger=lambda *a: None)

# Island fails -> Svartalfheim must never be touched.
started = []
u = build_updater({"enabled": True, "auto_apply": True, "stagger": True,
                   "settle_minutes": 0},
                  {"island": "failed"}, started)
u.apply(force=True)
for _ in range(60):
    if not u._applying:
        break
    time.sleep(0.05)
check("first map failed -> only one map attempted", started, ["island"])
check("the untouched map stayed up", "svartalfheim" in started, False)
check("the failure is recorded against the build",
      u.failed_builds.get("25176147", (0,))[0], 1)

# Both succeed -> both patched, in order.
started = []
u2 = build_updater({"enabled": True, "auto_apply": True, "stagger": True,
                    "settle_minutes": 0}, {}, started)
u2.apply(force=True)
for _ in range(60):
    if not u2._applying:
        break
    time.sleep(0.05)
check("all good -> both patched sequentially", started, ["island", "svartalfheim"])
check("success clears any failure record", "25176147" in u2.failed_builds, False)

print("\n9. The settle delay holds off an automatic patch, but never a manual one")
started = []
u3 = build_updater({"enabled": True, "auto_apply": True, "stagger": True,
                    "settle_minutes": 15}, {}, started)
r = u3.apply()                      # automatic: build only just seen
check("automatic patch waits out the settle delay", r.get("error"), "settling")
check("and nothing was started", started, [])

r2 = u3.apply(force=True)           # manual: go now
for _ in range(60):
    if not u3._applying:
        break
    time.sleep(0.05)
check("a forced update ignores the settle delay", started, ["island", "svartalfheim"])

# Once the delay has elapsed, the automatic path proceeds.
started = []
u4 = build_updater({"enabled": True, "auto_apply": True, "stagger": True,
                    "settle_minutes": 15}, {}, started)
u4.check()
u4.first_seen["25176147"] = time.time() - (16 * 60)
r3 = u4.apply()
for _ in range(60):
    if not u4._applying:
        break
    time.sleep(0.05)
check("after the delay elapses it proceeds", started, ["island", "svartalfheim"])

print("\n10. settle_minutes is tunable from config")
u5 = build_updater({"enabled": True, "auto_apply": True, "settle_minutes": 0}, {}, [])
check("settle_minutes 0 disables the wait",
      u5.apply().get("error") != "settling", True)

print()
if failures:
    print(f"FAILED: {failures} check(s)")
    sys.exit(1)
print("PASS: staggered, settled, and a failure never takes the whole cluster down")
