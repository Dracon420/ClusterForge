"""
Agents fetching their own code from the panel.

The point of this feature is that a machine with no file share still gets
updates - PC3 was restarted three times in one evening by a deploy that had no
way to deliver a file to it. The point of this TEST is that a path arriving
over HTTP decides what gets written, so the refusals matter more than the
happy path.

Nothing here touches a real install: everything happens in a temp folder.

Run:  python scripts\\test_agentupdate.py
"""

import hashlib
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import agentupdate                                         # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got!r} (expected {want!r})")
    if not ok:
        FAILED.append(label)


def write(path, text):
    """Binary, deliberately. Text mode on Windows turns every \\n into \\r\\n,
    which changes the hash - and the agent writes the panel's bytes verbatim,
    so a test that writes text is testing something the real thing never does."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))
    return path


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


root = tempfile.mkdtemp(prefix="clusterforge-selfupdate-")
agent_dir = os.path.join(root, "agent")
os.makedirs(agent_dir)

print("Paths a malicious or buggy manifest might ask for:")
for bad in ("../../Windows/System32/evil.dll", "..\\..\\escape.py", "/etc/passwd",
            "config.json", "secrets.json", "core/../../outside.py", ""):
    check(f"refuses {bad!r}", agentupdate.safe_path(agent_dir, bad), None)

check("allows a normal core module",
      agentupdate.safe_path(agent_dir, "core/rcon.py") is not None, True)
check("allows the map catalogue",
      agentupdate.safe_path(agent_dir, "data/maps/catalog.json") is not None, True)
check("backslashes are normalised, not rejected",
      agentupdate.safe_path(agent_dir, "core\\rcon.py") is not None, True)

print("\nWorking out what a machine is missing:")
write(os.path.join(agent_dir, "agent.py"), "old version\n")
write(os.path.join(agent_dir, "core", "rcon.py"), "same\n")
wanted = {"fingerprint": "abc123", "files": {
    "agent.py": hashlib.sha256(b"new version\n").hexdigest(),
    "core/rcon.py": sha(os.path.join(agent_dir, "core", "rcon.py")),
    "core/brand_new.py": hashlib.sha256(b"fresh\n").hexdigest(),
}}
behind = agentupdate.differences(agent_dir, wanted)
check("changed and missing files only", behind, ["agent.py", "core/brand_new.py"])

print("\nApplying a verified update:")
staging = os.path.join(root, "staging")
os.makedirs(staging)
staged = {
    "agent.py": write(os.path.join(staging, "agent.py"), "new version\n"),
    "core/brand_new.py": write(os.path.join(staging, "brand_new.py"), "fresh\n"),
}
result = agentupdate.apply_update(agent_dir, staged, wanted, log=lambda *_a: None)
check("both applied", sorted(result["applied"]), ["agent.py", "core/brand_new.py"])
check("nothing rejected", result["rejected"], [])
with open(os.path.join(agent_dir, "agent.py"), encoding="utf-8") as fh:
    check("agent.py really replaced", fh.read(), "new version\n")
check("previous version kept as .prev",
      os.path.isfile(os.path.join(agent_dir, "agent.py.prev")), True)

print("\nA file that arrives corrupted:")
staged2 = {"agent.py": write(os.path.join(staging, "corrupt.py"), "TRUNCATED")}
before = open(os.path.join(agent_dir, "agent.py"), encoding="utf-8").read()
result = agentupdate.apply_update(agent_dir, staged2, wanted, log=lambda *_a: None)
check("refused", result["applied"], [])
check("says why", "hash mismatch" in result["rejected"][0], True)
after = open(os.path.join(agent_dir, "agent.py"), encoding="utf-8").read()
check("the good file was NOT overwritten", after, before)

print("\nA manifest entry pointing outside the install:")
staged3 = {"../escape.py": write(os.path.join(staging, "escape.py"), "nope\n")}
result = agentupdate.apply_update(agent_dir, staged3,
                                  {"files": {"../escape.py": sha(staged3["../escape.py"])}},
                                  log=lambda *_a: None)
check("refused", result["applied"], [])
check("nothing written outside",
      os.path.isfile(os.path.join(root, "escape.py")), False)

print("\nThe manifest of this real install:")
real = agentupdate.manifest(BASE)
check("has a fingerprint", len(real["fingerprint"]), 12)
check("includes agent.py", "agent.py" in real["files"], True)
check("includes core modules",
      any(p.startswith("core/") for p in real["files"]), True)
check("NEVER includes config.json", "config.json" in real["files"], False)
check("NEVER includes secrets.json", "secrets.json" in real["files"], False)
check("includes the map catalogue",
      "data/maps/catalog.json" in real["files"], True)
print(f"       {len(real['files'])} files, fingerprint {real['fingerprint']}")

shutil.rmtree(root, ignore_errors=True)
print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed - no real install was modified")
