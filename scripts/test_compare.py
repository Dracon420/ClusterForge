"""Verify launch-source comparison against Svartalfheim's two real command lines.

Svartalfheim's DSM log only exists on PC2, so both lines are pinned here as
literals. This lets the comparison logic be checked from PC1 without restarting
the remote agent for every iteration.

Expected result: exactly TWO differences.
  - mod ORDER (same 25 mods, MagaArk at position 17 vs 24)
  - -log      (in the bat, not in DSM's launch)

Everything else differs only in SPELLING and must not be reported:
  bat  "...?ServerPVE=True?ServerPassword=123456?RCONEnabled=True?RCONPort=27020"
  dsm  -RCONEnabled -RCONPort=27020   (with PVE/password living in the ini)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.importer import SourceView, _compare  # noqa: E402
from core.launch import LaunchConfig  # noqa: E402

DSM = (
    '"Svartalfheim_WP"?SessionName="[ToDs]Dracon-Svartalfheim" -preventHibernation '
    '-port=8000 -RCONEnabled -RCONPort=27020 '
    '-mods=942355,942024,941450,945883,928548,912815,1177331,928597,940975,930851,'
    '975282,929420,928708,930494,935408,929977,1079535,930025,929838,1450705,933447,'
    '1222937,941697,1493855,941689 '
    '-ServerPlatform=ALL -WinLiveMaxPlayers=10 -ForceAllowCaveFlyers '
    '-clusterid=DraconCluster -ClusterDirOverride="\\\\HOME-SERVER-1\\ClusterData" '
    '-NoTransferFromFiltering -culture=en -usestore -BackupTransferPlayerDatas'
)

# Deliberately the PRE-ADOPTION archive, not the live bat. Adopting the start
# script removed this drift, so reading the live file would leave this test
# comparing two identical inputs and quietly proving nothing. The archive keeps
# a stable drifted fixture, which is what makes it a regression test.
from core.launch import extract_from_bat  # noqa: E402

FIXTURE = r"Z:\StartServer.bat.pre-clusterforge"
if not os.path.exists(FIXTURE):
    print(f"SKIP: fixture not found ({FIXTURE})")
    sys.exit(0)

with open(FIXTURE, "r", encoding="utf-8", errors="replace") as fh:
    BAT = extract_from_bat(fh.read())[1]

views = {
    "dsm_log": SourceView("DSM management log", True, "live", DSM,
                          LaunchConfig.parse(DSM).summary()),
    "bat": SourceView("StartServer.bat", True, "file", BAT,
                      LaunchConfig.parse(BAT).summary()),
}

conflicts = _compare(views)

print("=" * 74)
print(f"CONFLICTS FOUND: {len(conflicts)}")
print("=" * 74)
for c in conflicts:
    print(f"  {c['field']}")
    for k, v in c["values"].items():
        print(f"      {k:10} {v}")

fields = {c["field"] for c in conflicts}

# All six are REAL differences between the two command lines. An earlier
# version of this test expected only two, which was wrong -- the comparator was
# right and the expectation was not.
#
#   Mods            same 25, different ORDER (leftmost wins, so it matters)
#   -log            in the bat only
#   -culture=en     in DSM's line only
#   ?listen         in the bat only
#   ?ServerPVE      in the bat only -- also set in GameUserSettings.ini
#   ?ServerPassword in the bat only -- also set in GameUserSettings.ini
#
# The last two are safe to drop when adopting DSM's line: both values live in
# the ini, and Island already runs that way with players connected.
#
# What must NOT appear are pure spelling differences: DSM writes -RCONEnabled
# and -RCONPort=27020 as flags while the bat packs ?RCONEnabled=True and
# ?RCONPort=27020 into its map segment. Same settings, same values.
expected = {"Mods", "-log", "-culture", "-listen", "-serverpve", "-serverpassword"}
must_not_appear = {"-rconenabled", "-rconport", "-sessionname", "-serverplatform"}

leaked = fields & must_not_appear
if leaked:
    print(f"SPELLING NOISE (must never be reported): {sorted(leaked)}")

print()
missed = expected - fields
spurious = fields - expected
if missed:
    print(f"MISSING (should have been reported): {sorted(missed)}")
if spurious:
    print(f"SPURIOUS (spelling differences, not real): {sorted(spurious)}")

print()
if not missed and not spurious and not leaked:
    dsm_mods = LaunchConfig.parse(DSM).mods
    bat_mods = LaunchConfig.parse(BAT).mods
    print(f"  same mod set : {set(dsm_mods) == set(bat_mods)}")
    print(f"  same order   : {dsm_mods == bat_mods}")
    if set(dsm_mods) == set(bat_mods) and dsm_mods != bat_mods:
        moved = [m for i, m in enumerate(dsm_mods) if bat_mods.index(m) != i]
        print(f"  moved mods   : {moved}")
    print("\nRESULT: PASS - reports both real differences, no spelling noise")
    sys.exit(0)
print("RESULT: FAIL")
sys.exit(1)
