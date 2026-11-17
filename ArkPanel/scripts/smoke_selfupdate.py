"""Prove an agent updates itself, unattended, end to end.

    python scripts/smoke_selfupdate.py --agent http://127.0.0.1:8768

THE ONE RULE: this never calls /api/selfupdate/check.

That endpoint runs the update directly and bypasses the poll loop, so it
passes whether or not auto-update is switched on. Both bugs found on
2026-09-20 shipped behind exactly that blind spot - the endpoint worked, the
loop had never been started, and every status field said "enabled": true
because it was reading config rather than behaviour. So this bumps a version
string on the panel and then LEAVES THE AGENT ALONE, and the agent has to
notice by itself.

What it asserts, in order:

  1. the agent is reachable and currently matches the panel
  2. the panel's fingerprint changes (core/buildstamp.py is bumped)
  3. the agent's last_check advances WITHOUT being asked   <- catches a dead loop
  4. the agent ends up running the new fingerprint         <- catches a dead restart
  5. it reports itself not stale
  6. every game server it manages still has the SAME pid   <- an agent restart
                                                              must not disturb
                                                              a running server

The stamp is always put back, including on failure, so a run leaves no trace.

Point it at a THROWAWAY agent instance (scripts/install-smoke-agent.ps1), not
a production one. It refuses the panel's own embedded agent, which has
self-update deliberately disabled and would just sit there.

KNOWN SIDE EFFECT, and the reason to run this deliberately rather than often:
the stamp is bumped on the LIVE panel, and every agent in the cluster watches
that panel. So a run makes the real agents update and restart too, twice -
once for the bump and once when it is put back. That is safe (an agent
restart does not touch a running game server, which this test asserts), but
it is not invisible: the panel will show OLD CODE for the other machines for
a few minutes afterwards while they roll back.

Isolating it properly means a second panel serving its own copy of the code,
so the smoke agent is the only thing watching the stamp that moves. Worth
doing before this ships to customers; not worth it to test one machine here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAMP_FILE = os.path.join(BASE, "core", "buildstamp.py")
sys.path.insert(0, BASE)
from core import codestamp  # noqa: E402

failures: list[str] = []


def step(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}",
          flush=True)
    if not ok:
        failures.append(label)
    return ok


def get(url: str, path: str, token: str, timeout: int = 20) -> dict:
    request = urllib.request.Request(url.rstrip("/") + path)
    if token:
        request.add_header("X-ArkPanel-Token", token)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def bump_stamp() -> str:
    """Change STAMP to something new. Returns the original file contents."""
    original = open(STAMP_FILE, encoding="utf-8").read()
    marker = f"smoke-{int(time.time())}"
    changed = False
    out = []
    for line in original.splitlines(keepends=True):
        if line.startswith("STAMP =") and not changed:
            out.append(f'STAMP = "{marker}"\n')
            changed = True
        else:
            out.append(line)
    if not changed:
        raise RuntimeError(f"no STAMP = line in {STAMP_FILE}")
    open(STAMP_FILE, "w", encoding="utf-8", newline="").write("".join(out))
    return original


def server_pids(agent: str, token: str) -> dict:
    """pid per managed server, so we can prove the restart did not touch them."""
    try:
        data = get(agent, "/api/status", token, timeout=30)
    except Exception:  # noqa: BLE001
        return {}
    rows = data.get("servers") if isinstance(data, dict) else data
    pids = {}
    for row in rows or []:
        if isinstance(row, dict) and row.get("pid"):
            pids[row.get("name")] = row["pid"]
    return pids


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True,
                        help="base URL of the THROWAWAY agent, e.g. http://127.0.0.1:8768")
    parser.add_argument("--timeout", type=int, default=420,
                        help="seconds to wait for the agent to notice (default 420)")
    parser.add_argument("--allow-production", action="store_true",
                        help="permit running against an agent that manages servers")
    args = parser.parse_args()

    secrets = json.load(open(os.path.join(BASE, "secrets.json"), encoding="utf-8-sig"))
    token = (secrets.get("agent_token") or "").strip()

    print(f"smoke test: {args.agent}\n")

    print("before")
    try:
        health = get(args.agent, "/api/health", token)
        state = get(args.agent, "/api/selfupdate", token)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  agent is not reachable  {type(exc).__name__}: {exc}")
        return 1

    step("agent is reachable", True, f"{health.get('host')} stamp={health.get('stamp')}")
    if not step("self-update is enabled on it", bool(state.get("enabled")),
                "a disabled agent can never pass this"):
        return 1

    pids_before = server_pids(args.agent, token)
    if pids_before and not args.allow_production:
        print(f"  FAIL  this agent is running {len(pids_before)} live server(s): "
              f"{', '.join(pids_before)}")
        print("        Point at a throwaway instance, or pass --allow-production "
              "if you really mean it.")
        return 1

    start_fp = health.get("code")
    panel_fp = codestamp.fingerprint(BASE)
    step("agent matches the panel to start with", start_fp == panel_fp,
         f"{start_fp} == {panel_fp}" if start_fp == panel_fp
         else f"agent={start_fp} panel={panel_fp} - update it first")
    last_check_before = state.get("last_check") or 0

    print("\nbumping the panel's build stamp")
    original = bump_stamp()
    try:
        new_fp = codestamp.fingerprint(BASE)
        step("panel fingerprint changed", new_fp != panel_fp, f"{panel_fp} -> {new_fp}")

        print(f"\nwaiting up to {args.timeout}s - NOT touching the agent")
        deadline = time.time() + args.timeout
        noticed = restarted = False
        while time.time() < deadline:
            time.sleep(10)
            left = round(deadline - time.time())
            try:
                state = get(args.agent, "/api/selfupdate", token)
                health = get(args.agent, "/api/health", token)
            except Exception as exc:  # noqa: BLE001
                print(f"    {left:4}s left  agent down ({type(exc).__name__}) "
                      "- restarting?", flush=True)
                restarted = True
                continue
            if not noticed and (state.get("last_check") or 0) > last_check_before:
                noticed = True
                print(f"    {left:4}s left  agent checked on its own: "
                      f"{state.get('last_result')!r}", flush=True)
            if health.get("code") == new_fp:
                print(f"    {left:4}s left  running {new_fp}", flush=True)
                break
            print(f"    {left:4}s left  running {health.get('code')} "
                  f"last_check={'yes' if noticed else 'not yet'}", flush=True)

        print()
        step("the agent checked by itself (poll loop alive)", noticed,
             "" if noticed else "last_check never advanced - _start_selfupdate() "
                                "is not being called")
        step("the agent restarted itself", health.get("code") == new_fp,
             f"running {health.get('code')}, wanted {new_fp}")
        step("it reports itself up to date",
             get(args.agent, "/api/selfupdate", token).get("stale") is False)
        step("the new stamp is live", health.get("stamp", "").startswith("smoke-"),
             f"stamp={health.get('stamp')}")
        if restarted:
            step("it went away and came back", True, "a real restart, not a no-op")

        pids_after = server_pids(args.agent, token)
        if pids_before:
            moved = [n for n, p in pids_before.items() if pids_after.get(n) != p]
            step("managed servers were not disturbed", not moved,
                 f"pid changed for: {', '.join(moved)}" if moved else
                 f"{len(pids_before)} server(s) same pid")
    finally:
        open(STAMP_FILE, "w", encoding="utf-8", newline="").write(original)
        print(f"\nstamp restored ({codestamp.fingerprint(BASE)})")
        print("NOTE: the agent is now one build AHEAD of the panel and will "
              "roll itself back on its next check.")

    print("\n" + ("SMOKE TEST PASSED" if not failures
                  else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
