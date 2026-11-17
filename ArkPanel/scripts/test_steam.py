"""Phase 7 gate: build detection must be branch-correct and never guess.

Read-only. Parses real steamcmd output and the real appmanifest files; runs no
update.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import steam  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures += 1


# Real app_info_print output shape, with the public branch listed SECOND so the
# test fails if the parser just grabs the first buildid it sees.
SAMPLE = '''
"2430930"
{
    "common" { "name" "ARK: Survival Ascended Dedicated Server" }
    "depots"
    {
        "1234" { "manifests" { "public" { "gid" "999" } } }
        "branches"
        {
            "public_test_realm"
            {
                "buildid"        "21994402"
                "description"    "Try out the new stuff"
            }
            "public"
            {
                "buildid"        "25117056"
                "timeupdated"    "1788548219"
            }
        }
        "privatebranches"  "1"
    }
}
'''

print("=" * 74)
print("1. BRANCH-CORRECT PARSING")
print("=" * 74)
pub = steam._extract_branch_buildid(SAMPLE, "public")
ptr = steam._extract_branch_buildid(SAMPLE, "public_test_realm")
check("public branch resolves to 25117056", pub == "25117056", f"got {pub}")
check("test realm resolves to 21994402", ptr == "21994402", f"got {ptr}")
check("public is NOT the first buildid in the text — order-independent",
      SAMPLE.index("21994402") < SAMPLE.index("25117056"))
check("a missing branch returns None",
      steam._extract_branch_buildid(SAMPLE, "nonexistent") is None)
check("no branches block returns None",
      steam._extract_branch_buildid('"depots" { }', "public") is None)

print()
print("=" * 74)
print("2. INSTALLED BUILD, FROM STEAMCMD'S OWN MANIFEST")
print("=" * 74)
for label, install in (("Island", r"D:\Servers\TheIsland"),
                       ("Svartalfheim", r"Z:\Svartalfhiem")):
    if not os.path.isdir(install):
        print(f"  SKIP {label} (unreachable)")
        continue
    info = steam.installed_build(install)
    print(f"  {label:14} build {info['build']}  target {info['target_build']}")
    check(f"{label}: build id found", bool(info["build"]))
    check(f"{label}: build matches target (no half-applied update)",
          info["build"] == info["target_build"], info["error"] or "")

print()
print("=" * 74)
print("3. A FAILED CHECK MUST REPORT 'unknown', NEVER 'up to date'")
print("=" * 74)
bogus = steam.compare(r"D:\Servers\TheIsland", r"C:\does\not\exist\steamcmd.exe")
check("status is unknown when steamcmd is missing", bogus["status"] == "unknown",
      f"got {bogus['status']}")
check("the reason is reported", bool(bogus["errors"]))
missing = steam.installed_build(r"C:\no\such\server")
check("missing manifest is an error, not a silent None",
      missing["error"] is not None)

print()
if failures:
    print(f"RESULT: {failures} FAILURE(S)")
    sys.exit(1)
print("RESULT: PASS — build detection is branch-correct and fails loudly")
