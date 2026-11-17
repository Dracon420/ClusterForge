"""Nothing automatic may start a server that is down on purpose.

The maintenance flag was added to stop the monitor crying "island is NOT
running" while Palworld held PC1's memory. It did that, and nothing else - so
on Monday 2026-09-21 the nightly scheduled restart stopped an already-stopped
Island and dutifully started it again. Both games then sat on a 15.9 GB
machine for two days, 22.8 GB committed, each paging the other out, with no
alert because the flag was busy suppressing exactly the alert that would have
said so.

Three paths can start a server without anyone asking:

  * the scheduler's nightly restart
  * the updater, after patching
  * the boot autostart, after a power cut

All three must refuse. The updater must still PATCH - a server that is off
should come back current, which is the whole point of updating it while it is
down - it just must not come back UP.

And the human must always win: an explicit start overrides the flag and
clears it, because a running server marked "stopped on purpose" would leave
the monitor ignoring it the next time it really died.

This reads the source rather than running an agent, for the same reason as
[[test_selfupdate_wiring]]: these are guard clauses, and a guard that was
never wired in is invisible to a test that calls the function it guards.
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
funcs = {n.name: n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def consults_flag(func_name: str) -> bool:
    """Does this function (or anything nested in it) ask about maintenance?"""
    node = funcs.get(func_name)
    if node is None:
        return False
    return any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
               and c.func.id in ("_maintenance_reason", "_maintenance")
               for c in ast.walk(node))


def calls(func_name: str, wanted: str) -> bool:
    node = funcs.get(func_name)
    if node is None:
        return False
    for c in ast.walk(node):
        if isinstance(c, ast.Call):
            f = c.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name == wanted:
                return True
    return False


print("the helper exists")
check("_maintenance_reason() is defined", "_maintenance_reason" in funcs)

print("\nautomatic starts consult it")
for fn, why in (("_scheduled_restart", "the nightly restart - this is what broke"),
                ("update_server", "the updater, after patching"),
                ("_autostart_worker", "the boot autostart, after a power cut")):
    check(f"{fn}() consults the flag", consults_flag(fn), why)

print("\nthe updater still patches while held")
# It must call do_start conditionally, not never: a normal update still
# restarts. The guard is what makes it conditional.
check("update_server() can still start a server", calls("update_server", "do_start"),
      "a held server is the exception, not the rule")

print("\nthe human overrides it")
check("start() consults the flag", consults_flag("start"))
check("start() clears the flag", calls("start", "_set_maintenance"),
      "a running server must not stay marked stopped")

print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
sys.exit(1 if failures else 0)
