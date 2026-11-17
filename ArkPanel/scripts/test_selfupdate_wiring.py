"""The agent self-update must actually be switched on.

Two separate bugs shipped in this one feature, and neither was visible from
outside:

  1. `_finish_selfupdate` called `_restarter_available()`, a name that does not
     exist. Every update wrote its files and then raised NameError before
     triggering the restart, so agents ran old code off correct files.

  2. `start_services()` never called `_start_selfupdate()` at all, so the poll
     loop was never running. `/api/selfupdate` still reported
     `"enabled": true` - because that reads config, not reality - while
     `last_check` sat at 0.0 for weeks.

Both are wiring faults, invisible to any test that exercises the functions
directly. So this checks the wiring itself, statically: no agent needs to be
running, and it cannot be fooled by a config file that merely says "enabled".

[[test_undefined_names]] covers bug 1 generally; this pins the specific call.
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


tree = ast.parse(open(AGENT, encoding="utf-8").read(), filename=AGENT)
functions = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def calls_within(func_name: str) -> set:
    node = functions.get(func_name)
    if node is None:
        return set()
    return {c.func.id for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}


print("the poll loop is started")
check("start_services() exists", "start_services" in functions)
check("start_services() calls _start_selfupdate()",
      "_start_selfupdate" in calls_within("start_services"),
      "the loop is never started otherwise")
check("_start_selfupdate() exists", "_start_selfupdate" in functions)

print("\nthe restart is triggered")
check("_finish_selfupdate() exists", "_finish_selfupdate" in functions)
called = calls_within("_finish_selfupdate")
undefined = sorted(n for n in called
                   if n not in functions and n not in dir(__builtins__)
                   and n not in {"jsonify", "Thread", "run", "sleep"})
check("every function it calls is defined", not undefined,
      f"undefined: {undefined}" if undefined else "")
check("it consults the restart task",
      "_restart_task_present" in called,
      "must refuse rather than pretend when the task is missing")

print("\nthe update path reaches the restart")
check("_selfupdate_once() calls _finish_selfupdate()",
      "_finish_selfupdate" in calls_within("_selfupdate_once"))

print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
sys.exit(1 if failures else 0)
