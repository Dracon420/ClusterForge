"""
Prove a map switch rewrites exactly the three things it should, on the REAL
launch lines rather than a hand-typed approximation of them.

The two servers quote their map segment differently and carry different extra
settings, so a swap that works on one can quietly mangle the other. This reads
both live StartServer.bat files and checks that after a switch:

  the map name changed
  the session name changed
  the map mods changed
  EVERY other token is byte-identical

That last check is the point. Losing -preventHibernation or -ForceAllowCaveFlyers
in a map switch would look like a mod problem weeks later.

Run:  python scripts\\test_map_switch.py
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import maps                                    # noqa: E402
from core.importer import read_start_bat                 # noqa: E402
from core.launch import LaunchConfig                     # noqa: E402

FAILED = []


def check(label, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -- {detail}" if detail else ""))
    if not condition:
        FAILED.append(label)


def tokens_of(cfg):
    return [t.raw for t in cfg.tokens]


def try_switch(name, install_dir, bat_path, target_key):
    entry = maps.find(BASE, target_key)
    if not entry:
        print(f"  no catalog entry for {target_key}")
        FAILED.append(f"{name}: missing catalog entry {target_key}")
        return

    bat = read_start_bat(install_dir, bat_path)
    if not bat:
        print(f"  could not read {bat_path} -- skipped")
        return

    before = LaunchConfig.parse(bat[1])
    after = LaunchConfig.parse(bat[1])

    old_tokens = tokens_of(before)
    old_map = after.set_map(entry["map_name"])
    old_session = after.set_session_name(maps.session_name(entry))
    after.set_mods(maps.apply_map_mods(BASE, before.mods, entry))

    print(f"\n{name}: {old_map} -> {entry['map_name']}")
    print(f"  session : {old_session} -> {maps.session_name(entry)}")
    print(f"  segment : {after.map_segment}")

    check("map name changed", after.map_name == entry["map_name"], after.map_name)
    check("session name changed",
          maps.session_name(entry) in after.map_segment)
    check("still parses back to the same map",
          LaunchConfig.parse(after.render()).map_name == entry["map_name"])

    # The map's own mod must be present and first; no other map's may survive.
    map_mods = maps.map_mod_ids(BASE)
    survivors = [m for m in after.mods if m in map_mods]
    expected = [int(entry["mod_id"])] if entry.get("mod_id") else []
    check("only this map's map-mod remains", survivors == expected,
          f"{survivors} expected {expected}")

    # Every non-map, non-mod token untouched.
    new_tokens = tokens_of(after)
    check("token count unchanged", len(old_tokens) == len(new_tokens),
          f"{len(old_tokens)} -> {len(new_tokens)}")
    drift = [(a, b) for a, b in zip(old_tokens, new_tokens)
             if a != b and not a.startswith("-mods=") and a != before.map_segment]
    check("no other token changed", not drift, str(drift[:3]))

    # Round trip: switching back restores the original line exactly.
    home = maps.find_by_map_name(BASE, old_map)
    if home:
        back = LaunchConfig.parse(after.render())
        back.set_map(old_map)
        back.set_session_name(old_session or maps.session_name(home))
        back.set_mods(maps.apply_map_mods(BASE, back.mods, home))
        if back.render() == before.render():
            check("switching back restores the original line", True)
        else:
            # A map's exclusive mods (The Island's Additional IslandDinos) are
            # stripped on the way out and re-added at the FRONT on the way back,
            # so they may return in a different slot. Allow exactly that: same
            # mods, same order for everything else, rest of the line identical.
            excl = {int(m) for m in home.get("exclusive_mods") or []}
            same_set = sorted(back.mods) == sorted(before.mods)
            others_order = [m for m in back.mods if m not in excl] == \
                [m for m in before.mods if m not in excl]
            back_rest = LaunchConfig.parse(back.render())
            back_rest.set_mods(before.mods)
            rest_same = back_rest.render() == before.render()
            check("switching back restores the line (exclusive mods may move to the front)",
                  bool(excl) and same_set and others_order and rest_same,
                  f"set={same_set} order={others_order} rest={rest_same} exclusive={sorted(excl)}")


def main():
    import json
    with open(os.path.join(BASE, "config.json"), "r", encoding="utf-8-sig") as fh:
        config = json.load(fh)

    print("Catalogued maps:")
    for entry in maps.load(BASE):
        print(f"  {entry['key']:<15} {entry['map_name']:<20} "
              f"{maps.session_name(entry):<32} saves in {maps.save_folder(entry)}")

    for server in config["servers"]:
        install = server.get("install_dir", "")
        bat = server.get("start_bat")
        if not os.path.isdir(install):
            print(f"\n{server['name']}: {install} not reachable from here -- skipped")
            continue
        # An official map, then a MOD map. The second is the interesting case:
        # the outgoing map's own mod has to leave and the incoming one has to
        # arrive at the front, without touching the QOL mods in between.
        for target in ("astraeos", "bjarnheim"):
            if maps.find(BASE, target) and \
                    server["map_name"] != maps.find(BASE, target)["map_name"]:
                try_switch(server["name"], install, bat, target)

    print("\nUnavailable maps (switching to these is refused):")
    for entry in maps.load(BASE):
        if not maps.is_available(entry):
            print(f"  {entry['key']}: {maps.unavailable_reason(entry)[:90]}")

    print()
    if FAILED:
        print(f"{len(FAILED)} check(s) FAILED:")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    print("all checks passed -- no file was written, this is a dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
