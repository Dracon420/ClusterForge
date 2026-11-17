"""
The weekly backup of his PC's irreplaceable files.

The property that matters most: a file deleted at the source must STAY in the
backup. This exists because an AI with admin rights made a mess of the PC it
was developed on; a backup that mirrors deletions would carry the mess across
at the next scheduled run.

Run:  python scripts\\test_filebackup.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import filebackup                                          # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got} (expected {want})")
    if not ok:
        FAILED.append(label)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


root = tempfile.mkdtemp(prefix="arkpanel-filebackup-")
src = os.path.join(root, "profile")
dst = os.path.join(root, "backup")

write(os.path.join(src, "Documents", "taxes.txt"), "keep me")
write(os.path.join(src, "Pictures", "holiday.jpg"), "photo")
write(os.path.join(src, "AppData", "Local", "Temp", "junk.tmp"), "throwaway")
write(os.path.join(src, "AppData", "Local", "BigApp", "Cache", "blob.bin"), "cache")

try:
    print("First run:")
    out = filebackup.run_jobs({"destination": dst,
                               "jobs": [{"name": "profile", "source": src}]},
                              log=lambda *_a: None)
    check("the copy succeeded", out["ok"], True)
    check("documents came across",
          os.path.isfile(os.path.join(dst, "profile", "Documents", "taxes.txt")), True)
    check("pictures came across",
          os.path.isfile(os.path.join(dst, "profile", "Pictures", "holiday.jpg")), True)
    check("Temp was skipped",
          os.path.exists(os.path.join(dst, "profile", "AppData", "Local", "Temp")), False)
    check("Cache was skipped",
          os.path.exists(os.path.join(dst, "profile", "AppData", "Local", "BigApp", "Cache")), False)

    print("\nHe edits one file and DELETES another, then the weekly run fires:")
    write(os.path.join(src, "Documents", "taxes.txt"), "edited version")
    os.remove(os.path.join(src, "Pictures", "holiday.jpg"))
    out = filebackup.run_jobs({"destination": dst,
                               "jobs": [{"name": "profile", "source": src}]},
                              log=lambda *_a: None)
    check("second run succeeded", out["ok"], True)
    with open(os.path.join(dst, "profile", "Documents", "taxes.txt"), encoding="utf-8") as fh:
        check("the edit came across", fh.read(), "edited version")
    check("THE DELETED FILE IS STILL IN THE BACKUP",
          os.path.isfile(os.path.join(dst, "profile", "Pictures", "holiday.jpg")), True)

    print("\nA job whose source does not exist:")
    out = filebackup.run_jobs(
        {"destination": dst,
         "jobs": [{"name": "gone", "source": os.path.join(root, "nope")},
                  {"name": "profile", "source": src}]},
        log=lambda *_a: None)
    check("the missing one is marked skipped", out["jobs"][0]["skipped"], True)
    check("the other job still ran", out["jobs"][1]["ok"], True)
    check("overall still ok - a missing source is not a failed backup",
          out["ok"], True)

    print("\nUnreachable destination (his PC offline, share down):")
    out = filebackup.run_jobs({"destination": r"\\NO-SUCH-HOST\backup",
                               "jobs": [{"name": "profile", "source": src}]},
                              log=lambda *_a: None)
    check("refuses rather than pretending", out["ok"], False)
    check("says why", "not reachable" in out["error"], True)
    check("copies nothing", out["jobs"], [])

    print("\nNo destination configured at all:")
    out = filebackup.run_jobs({"jobs": []}, log=lambda *_a: None)
    check("refuses", out["ok"], False)

    print("\nA profile with Windows' legacy junctions in it:")
    # This is the bug that ate the first real run. A Windows profile contains
    # "Local Settings" -> AppData\Local, and inside that an "Application Data"
    # that points at its own parent. robocopy without /XJ walks in and copies
    # the same tree over and over: 19.9 GB of a 16 GB profile, eleven levels
    # deep, still climbing when it was killed.
    loopy = os.path.join(root, "profile2")
    write(os.path.join(loopy, "AppData", "Local", "app", "real.txt"), "the only copy")
    made_junction = False
    for link, target in ((os.path.join(loopy, "Local Settings"),
                          os.path.join(loopy, "AppData", "Local")),
                         (os.path.join(loopy, "AppData", "Local", "Application Data"),
                          os.path.join(loopy, "AppData", "Local"))):
        rc = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                            capture_output=True, text=True)
        made_junction = made_junction or rc.returncode == 0

    if not made_junction:
        print("  [skip] could not create a junction on this filesystem")
    else:
        out2 = os.path.join(root, "backup2")
        res = filebackup.copy_tree(loopy, out2)
        check("the copy finished rather than looping", res["ok"], True)
        check("the real file came across",
              os.path.isfile(os.path.join(out2, "AppData", "Local", "app", "real.txt")), True)
        check("the junction was NOT followed",
              os.path.exists(os.path.join(out2, "Local Settings", "app")), False)
        deepest = max((len(d.split(os.sep)) for d, _, _ in os.walk(out2)), default=0)
        check("no runaway nesting", deepest - len(out2.split(os.sep)) <= 4, True)

    print("\nrobocopy arguments (exactly as copy_tree builds them):")
    args = filebackup.robocopy_args(src, r"\\HOME-SERVER-1\HubmasterBackup\profile",
                                    mirror=False, exclude_dirs=filebackup.SKIP_DIRS,
                                    throttle=True, exclude_junctions=True)
    check("never mirrors - deletions must not propagate", "/MIR" in args, False)
    check("network tuning applied", "/COMPRESS" in args, True)
    check("throttled when players are on", "/IPG:20" in args, True)
    check("Temp excluded", "Temp" in args, True)
    check("junctions excluded - the bug that ate the first run", "/XJ" in args, True)

    print("\nThings a copy of a RUNNING Windows can never read:")
    # The first real run went red over three of these and nothing else. A
    # backup that always reports failure is one nobody reads.
    res = filebackup.copy_tree(src, os.path.join(root, "backup3"))
    built = " ".join(filebackup.robocopy_args(
        src, os.path.join(root, "backup3"), exclude_junctions=True))
    check("registry hives are not attempted",
          "NTUSER.DAT*" in filebackup.SKIP_FILES, True)
    check("locked user class hive not attempted",
          "UsrClass.dat*" in filebackup.SKIP_FILES, True)
    check("database journals not attempted", "*-journal" in filebackup.SKIP_FILES, True)
    check("a clean copy reports nothing unreadable", res["unreadable"], 0)
    check("and it is ok", res["ok"], True)

finally:
    shutil.rmtree(root, ignore_errors=True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed - nothing outside the temp folder was touched")

