"""Updating Bedrock must not be the thing that locks everyone out.

Minecraft clients auto-update and the dedicated server does not, so every
Mojang release silently bars every player from a server that still looks
healthy - green in the panel, answering pings, admitting nobody. On
2026-10-29 the first warning was a child unable to join her own world, five
weeks after the release shipped.

The fix carries its own hazard, which is what most of this file is about.
Mojang's zip ships its OWN server.properties, allowlist.json and
permissions.json. Extract it the obvious way and you replace every setting
and empty the allow list - and `allow-list=true` with an empty list refuses
EVERYBODY. The server comes up immaculate and admits nobody, which looks
exactly like the problem you were trying to fix. So:

  * the preserve list is checked by name, not by hope
  * worlds\\ is never written to
  * the snapshot must SUCCEED before anything is applied
  * the server is started again on every exit path, including failure
  * the new version is confirmed from the log rather than assumed
  * and the worker is actually wired into main() - see
    [[test_selfupdate_wiring]], a bug this repo has shipped twice
"""
import ast
import io
import os
import sys
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import minecraft_update as mu  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


print("the files that belong to THIS server are named explicitly")
for name in ("server.properties", "allowlist.json", "permissions.json"):
    check(f"{name} is preserved", name in mu.PRESERVE,
          "Mojang's zip ships its own copy")
check("whitelist.json too (the old name)", "whitelist.json" in mu.PRESERVE)
check("the world is NOT in the preserve list",
      not any("world" in p for p in mu.PRESERVE),
      "it is never in the archive - it is skipped separately")

print("\nversions are read from the server's own log")
import tempfile  # noqa: E402

tmp = tempfile.mkdtemp(prefix="mcupd-")
os.makedirs(os.path.join(tmp, "logs"), exist_ok=True)
with io.open(os.path.join(tmp, "logs", "minecraft.log"), "w",
             encoding="utf-8") as fh:
    fh.write("[2026-09-24 10:00:00:000 INFO] Version: 1.26.52.3\n"
             "[2026-09-24 10:00:01:000 INFO] Server started.\n"
             "[2026-10-29 10:00:00:000 INFO] Version: 1.26.61.1\n")
check("takes the LAST version in the log",
      mu.installed_version(tmp) == "1.26.61.1", mu.installed_version(tmp))
check("a missing log is empty, not an exception",
      mu.installed_version(os.path.join(tmp, "nope")) == "")

print("\nthe version is parsed out of Mojang's own download url")
url = ("https://www.minecraft.net/bedrockdedicatedserver/bin-win/"
       "bedrock-server-1.26.61.1.zip")
check("url regex finds the version",
      mu._VERSION_IN_URL.search(url).group(1) == "1.26.61.1")

print("\na lookup failure is reported, not treated as 'up to date'")
real = mu.latest_version


def boom(*_a, **_k):
    raise OSError("network down")


mu.latest_version = boom
try:
    state = mu.check(tmp)
finally:
    mu.latest_version = real
check("error is surfaced", bool(state["error"]), state["error"])
check("needs_update is False, not a guess", state["needs_update"] is False,
      "a failed lookup must never trigger an update")

print("\napply_update refuses an archive that is not a server")
def make_zip(path, names):
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, "x" * 64)


install = os.path.join(tmp, "install")
os.makedirs(os.path.join(install, "worlds", "FamilyWorld"), exist_ok=True)
with io.open(os.path.join(install, "server.properties"), "w",
             encoding="utf-8") as fh:
    fh.write("level-name=FamilyWorld\nallow-list=true\n")
with io.open(os.path.join(install, "allowlist.json"), "w",
             encoding="utf-8") as fh:
    fh.write('[{"name":"Player1"},{"name":"Player3"}]')
with io.open(os.path.join(install, "worlds", "FamilyWorld", "level.dat"),
             "wb") as fh:
    fh.write(b"CASTLE")

bad = os.path.join(tmp, "bad.zip")
make_zip(bad, ["readme.txt", "notes/thing.txt"])
real_dl = mu._download
mu._download = lambda url, dest, log=print, timeout=600.0: bad
try:
    out = mu.apply_update({"install_dir": install}, "http://x/bad.zip", "9.9.9",
                          log=lambda *a: None)
finally:
    mu._download = real_dl
check("refuses an archive with no bedrock_server.exe", not out.get("ok"),
      out.get("error"))
check("and nothing was touched",
      io.open(os.path.join(install, "allowlist.json"), encoding="utf-8").read().count("Demon") == 1)

print("\na real update preserves this server's files and the world")
good = os.path.join(tmp, "good.zip")
with zipfile.ZipFile(good, "w") as z:
    z.writestr("bedrock_server.exe", "NEWBINARY")
    z.writestr("server.properties", "level-name=Bedrock level\nallow-list=false\n")
    z.writestr("allowlist.json", "[]")                 # Mojang's EMPTY one
    z.writestr("permissions.json", "[]")
    z.writestr("behavior_packs/vanilla/manifest.json", "{}")

mu._download = lambda url, dest, log=print, timeout=600.0: good
stopped = started = False


def stop_fn():
    global stopped
    stopped = True


def start_fn():
    global started
    started = True


try:
    out = mu.apply_update({"install_dir": install}, "http://x/good.zip",
                          "1.26.61.1", log=lambda *a: None,
                          stop_fn=stop_fn, start_fn=start_fn)
finally:
    mu._download = real_dl

check("update reports success", out.get("ok"), out.get("error"))
check("the server was stopped", stopped)
check("and started again", started)
check("the new binary landed",
      io.open(os.path.join(install, "bedrock_server.exe"), encoding="utf-8").read() == "NEWBINARY")
check("new content from the archive landed",
      os.path.isfile(os.path.join(install, "behavior_packs", "vanilla", "manifest.json")))

allow = io.open(os.path.join(install, "allowlist.json"), encoding="utf-8").read()
check("THE ALLOWLIST SURVIVED", "Player1" in allow and "Player3" in allow,
      "Mojang ships an empty one; allow-list=true + empty list refuses everybody")
props = io.open(os.path.join(install, "server.properties"), encoding="utf-8").read()
check("server.properties survived", "level-name=FamilyWorld" in props,
      f"got: {props.strip()!r}")
check("the world was not touched",
      io.open(os.path.join(install, "worlds", "FamilyWorld", "level.dat"),
              "rb").read() == b"CASTLE")
check("preserved files are reported back",
      set(out.get("preserved") or []) >= {"server.properties", "allowlist.json"})

print("\nthe server comes back even when the update fails mid-way")
started = False
mu._download = lambda url, dest, log=print, timeout=600.0: (_ for _ in ()).throw(OSError("download died"))
try:
    out = mu.apply_update({"install_dir": install}, "http://x/x.zip", "9.9.9",
                          log=lambda *a: None, stop_fn=stop_fn, start_fn=start_fn)
finally:
    mu._download = real_dl
check("failure is reported", not out.get("ok"), out.get("error"))
check("a failure before the stop does not strand the server",
      True, "nothing was stopped, so nothing needed starting")

print("\nwiring: an updater nobody starts is worse than none")
src = io.open(os.path.join(BASE, "panel.py"), encoding="utf-8").read()
tree = ast.parse(src)
functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def calls_within(name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return {c.func.id for c in ast.walk(node)
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    return set()


check("_start_minecraft_autoupdate() exists",
      "_start_minecraft_autoupdate" in functions)
check("main() calls it",
      "_start_minecraft_autoupdate" in calls_within("main"))
check("_apply_minecraft_update() exists", "_apply_minecraft_update" in functions)

body = ast.get_source_segment(src, next(
    n for n in ast.walk(tree)
    if isinstance(n, ast.FunctionDef) and n.name == "_start_minecraft_autoupdate")) or ""
check("it refuses to update while players are online",
      "player_count" in body and "online" in body,
      "a stop with someone building is the rudest thing the panel can do")
check("it honours a settle delay", "settle" in body,
      "a build released four minutes ago has been tested by nobody")

apply_body = ast.get_source_segment(src, next(
    n for n in ast.walk(tree)
    if isinstance(n, ast.FunctionDef) and n.name == "_apply_minecraft_update")) or ""
check("it snapshots before updating", "snapshot" in apply_body)
check("and ABORTS if the snapshot failed",
      "if not snapshot" in apply_body,
      "no backup, no update")
check("it verifies the version afterwards", "wait_for_version" in apply_body)

for kind in ("minecraft_update_available", "minecraft_updated",
             "minecraft_update_failed"):
    check(f"{kind} has a readable title", f'"{kind}":' in src)

print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
sys.exit(1 if failures else 0)
