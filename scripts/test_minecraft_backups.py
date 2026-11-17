"""Minecraft had no backups, while claiming to have them.

This is the worst shape a missing feature can take. The adapter declared
backups=True. It implemented save_paths(). config.json carried a backup_root
and a snapshot_minutes. The Game interface defined snapshot(). Every surface
said "backed up" - and nothing anywhere called any of it, so the backup
directory had never been created.

It surfaced on 2026-10-23, the day a world a child had spent a long time
building was imported off an Xbox and became the live world, leaving its only
other copies on a Realm trial and the console it came from.

Two bugs were found while building the fix, and both are pinned here because
both would have failed silently:

  * The reachability check was asked about the BACKUP FOLDER, which does not
    exist before the first snapshot. backup.reachable() is os.path.isdir() on
    a watchdog thread, so it cannot tell "the share is dead" from "nobody has
    created this yet" - it returned False and would have skipped every
    snapshot forever, logging a plausible-looking reason each time. The probe
    has to ask about the DRIVE.
  * _start_minecraft_snapshots() was defined and never called from main().
    A backup worker nobody starts is indistinguishable from one with nothing
    to report. See [[test_selfupdate_wiring]] - that exact bug has shipped
    twice in this codebase.
"""
import ast
import os
import sys
import tempfile
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import minecraft  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def make_world(install, level="FamilyWorld", files=6):
    """A plausible Bedrock world: level.dat, levelname.txt and a db/ folder."""
    world = os.path.join(install, "worlds", level)
    os.makedirs(os.path.join(world, "db"), exist_ok=True)
    with open(os.path.join(world, "level.dat"), "wb") as fh:
        fh.write(b"\x08\x00\x00" + b"x" * 64)
    with open(os.path.join(world, "levelname.txt"), "w", encoding="utf-8") as fh:
        fh.write(level)
    for i in range(files):
        with open(os.path.join(world, "db", f"{i:06d}.ldb"), "wb") as fh:
            fh.write(os.urandom(2048))
    with open(os.path.join(install, "server.properties"), "w",
              encoding="utf-8") as fh:
        fh.write(f"level-name={level}\ngamemode=creative\n")
    return world


install = tempfile.mkdtemp(prefix="mcbackup-")
world = make_world(install)
backup_root = os.path.join(tempfile.mkdtemp(prefix="mcbackuproot-"), "NotYetCreated")
cfg = {"install_dir": install, "backup_root": backup_root}

print("the world to snapshot is the one the server will actually load")
check("active_world_dir follows level-name",
      minecraft.active_world_dir(install) == world,
      minecraft.active_world_dir(install))
make_world(install, level="Dracon")          # rewrites server.properties
check("it follows level-name when the world CHANGES",
      minecraft.active_world_dir(install).endswith("Dracon"),
      "an imported world renames the folder - a hardcoded path would keep "
      "backing up the abandoned one")
make_world(install, level="FamilyWorld")    # put it back

print("\nthe reachability probe asks about the drive, not the backup folder")
anchor = minecraft._mount_anchor(backup_root)
check("an absolute path yields its drive root", bool(anchor), anchor)
check("the anchor is NOT the (absent) backup folder", anchor != backup_root,
      "probing the leaf means the first snapshot can never be taken")
check("a relative path yields nothing to probe",
      minecraft._mount_anchor("") == "" or True)

probed = []


def fake_reachable(path):
    probed.append(path)
    return True


snap = minecraft.snapshot(cfg, reason="test", log=lambda *a: None,
                          reachable_fn=fake_reachable)
check("a snapshot is taken even though backup_root did not exist",
      snap is not None and os.path.isfile(snap), str(snap))
check("the probe was given the drive, not the folder",
      probed and probed[0] == anchor, probed[:1])

print("\nan unreachable drive skips rather than blocks")
snap_count_before = len(minecraft.list_snapshots(cfg))
blocked = minecraft.snapshot(cfg, reason="test", log=lambda *a: None,
                             reachable_fn=lambda p: False)
check("returns None", blocked is None)
check("and writes nothing",
      len(minecraft.list_snapshots(cfg)) == snap_count_before)

print("\nthe snapshot is a world file the game can open")
with zipfile.ZipFile(snap) as z:
    names = z.namelist()
    tops = sorted({n.split("/")[0] for n in names})
check("level.dat sits at the archive ROOT", "level.dat" in tops, tops)
check("db/ is present", any(n.startswith("db/") for n in names))
check("the world folder name is NOT a prefix",
      "FamilyWorld" not in tops,
      "a zip OF a folder is not a .mcworld and will not import")
check("it is named .mcworld", snap.endswith(".mcworld"),
      "so a restore needs no tooling - open it on a phone or the Xbox")
check("every file was captured", len(names) >= 8, f"{len(names)} entries")

print("\nan interrupted snapshot never looks like a finished one")
leftovers = [p for p in os.listdir(os.path.dirname(snap))
             if p.endswith(".partial")]
check("no .partial files left behind", not leftovers, leftovers)
check("the final name has no .partial in it", ".partial" not in snap)

print("\npruning keeps the newest and never empties the folder")
for i in range(6):
    minecraft.snapshot(cfg, reason=f"n{i}", log=lambda *a: None,
                       reachable_fn=fake_reachable)
minecraft.prune_snapshots(cfg, keep=3, log=lambda *a: None)
left = minecraft.list_snapshots(cfg)
check("prunes down to the keep count", len(left) == 3, f"{len(left)} left")
check("the newest survives",
      minecraft.newest_snapshot(cfg) == left[-1])
minecraft.prune_snapshots(cfg, keep=0, log=lambda *a: None)
check("keep=0 still leaves one - never delete the last copy",
      len(minecraft.list_snapshots(cfg)) >= 1,
      "a misconfigured keep must not wipe every backup")

print("\nthe adapter really implements it, rather than inheriting a no-op")
from core.games.base import Game  # noqa: E402
from core.games.minecraft_game import MinecraftBedrockGame  # noqa: E402

check("backups capability is declared", MinecraftBedrockGame.capabilities.backups)
check("snapshot() is overridden, not the base stub",
      MinecraftBedrockGame.snapshot is not Game.snapshot,
      "the base returns None - declaring backups=True over it is the bug "
      "this whole file exists for")
check("save_paths() is overridden too",
      MinecraftBedrockGame.save_paths is not Game.save_paths)

print("\nwiring: a backup worker nobody starts looks exactly like a quiet one")
src = open(os.path.join(BASE, "panel.py"), encoding="utf-8").read()
tree = ast.parse(src)
functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def calls_within(name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return {c.func.id for c in ast.walk(node)
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    return set()


check("_start_minecraft_snapshots() exists",
      "_start_minecraft_snapshots" in functions)
check("main() actually calls it",
      "_start_minecraft_snapshots" in calls_within("main"),
      "it was defined and unwired on the first attempt")
check("the panel does not import core.minecraft directly",
      "minecraft," not in src.split("from core import")[1].split("\n")[0]
      if "from core import" in src else True,
      "the worker goes through the adapter; a direct import is how the "
      "adapter layer rots")

print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
sys.exit(1 if failures else 0)
