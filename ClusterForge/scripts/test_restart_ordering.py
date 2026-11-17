"""A backup must never stand between a server and being back up.

On 2026-09-23 the nightly restart stopped Astraeos, Lost Colony and Genesis,
then ran the install mirror to a share on PC1 before starting them again. PC1
was at 22.8 GB committed on 15.9 GB of RAM at the time - because the Monday
restart had wrongly started the Island alongside Palworld - and its SMB server
was starved. All three robocopies wedged in kernel I/O, where they cannot be
killed: TerminateProcess does not take, so subprocess.run's own 2-hour timeout
never fired either. It sat there for thirteen hours.

Three maps stayed down overnight waiting for a backup. Nobody was playing at
23:00, so nothing alerted.

The fix is ordering, not timeouts. run_mirror excludes Saved, Logs and Cache -
the directories a running server holds - which is why its docstring says it is
safe with the server up. So the mirror runs AFTER do_start, and the worst a
hung mirror can cost is a missed mirror.

Checked structurally, by reading the source, because the bug was an ordering
of two calls that both individually worked.
"""
import ast
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(BASE, "agent.py")

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)
    return bool(ok)          # callers gate on this; returning None exits early


tree = ast.parse(open(AGENT, encoding="utf-8").read(), filename=AGENT)
target = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_scheduled_restart"), None)

print("the nightly restart")
if not check("_scheduled_restart() exists", target is not None):
    sys.exit(1)

# Only the RESTART path. _scheduled_restart also has a maintenance branch that
# mirrors and then returns without starting anything - that mirror is
# deliberately not preceded by a start, and counting it would fail this test
# for doing the right thing.
sequence = next((n for n in ast.walk(target)
                 if isinstance(n, ast.FunctionDef) and n.name == "sequence"), None)
if not check("its sequence() is found", sequence is not None):
    sys.exit(1)

restart_path = [stmt for stmt in sequence.body
                if not (isinstance(stmt, ast.If)
                        and any(isinstance(s, ast.Return) for s in stmt.body))]

calls = {}
for stmt in restart_path:
    for node in ast.walk(stmt):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None)
            if name in ("do_start", "do_stop", "run_mirror"):
                calls.setdefault(name, []).append(node.lineno)

check("it stops the server", "do_stop" in calls)
check("it starts the server", "do_start" in calls)
check("it mirrors the install", "run_mirror" in calls)

if "do_start" in calls and "run_mirror" in calls:
    first_start = min(calls["do_start"])
    first_mirror = min(calls["run_mirror"])
    check("the server is STARTED before the mirror runs",
          first_start < first_mirror,
          f"do_start line {first_start}, run_mirror line {first_mirror}"
          + ("" if first_start < first_mirror
             else " - a hung backup would hold the server down"))

print("\nthe mirror refuses an unresponsive destination")
backup_src = open(os.path.join(BASE, "core", "backup.py"), encoding="utf-8").read()
btree = ast.parse(backup_src)
bfuncs = {n.name: n for n in ast.walk(btree) if isinstance(n, ast.FunctionDef)}
check("backup.reachable() exists", "reachable" in bfuncs)
run_mirror = bfuncs.get("run_mirror")
check("run_mirror() checks reachability first",
      run_mirror is not None and any(
          isinstance(c, ast.Call) and getattr(c.func, "id", None) == "reachable"
          for c in ast.walk(run_mirror)),
      "robocopy on a dead share cannot be killed, so it must not be started")

print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
sys.exit(1 if failures else 0)
