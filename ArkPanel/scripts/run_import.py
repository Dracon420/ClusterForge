"""Phase 0: ingest the existing DSM setup and report any drift between sources.

Read-only against the servers and against DSM. Writes only into
D:\\Servers\\ArkPanel\\data\\servers\\<name>\\import.json.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.importer import import_server, to_record, dsm_profiles  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    with open(os.path.join(BASE, "config.json"), "r", encoding="utf-8") as fh:
        config = json.load(fh)

    print("=" * 78)
    print("DSM PROFILES VISIBLE ON THIS PC")
    print("=" * 78)
    profiles = dsm_profiles()
    if profiles:
        for p in profiles:
            print(f"  {p.get('Name'):20} {p.get('ServerPath')}")
    else:
        print("  none (DSM not installed on this machine, or no profiles)")

    for server in config["servers"]:
        name = server["name"]
        print()
        print("=" * 78)
        print(f"{server['display_name']}  ({server['host']})")
        print("=" * 78)

        if not os.path.isdir(server["install_dir"]):
            print(f"  install dir unreachable: {server['install_dir']}  -- SKIPPED")
            continue

        result = import_server(
            name=name,
            install_dir=server["install_dir"],
            profile_name=server["dsm_profile"],
            bat_path=server.get("start_bat"),
        )

        print("  Sources:")
        for view in result.views.values():
            mark = "OK  " if view.available else "--  "
            extra = f"{len(view.mods)} mods" if view.available else view.detail
            print(f"    {mark}{view.source:24} {extra:12} {view.detail if view.available else ''}")

        auth = result.authoritative
        if auth:
            s = auth.summary
            print()
            print(f"  Authoritative: {auth.source}")
            print(f"    map        : {s.get('map_name')}")
            print(f"    game port  : {s.get('port')}")
            print(f"    rcon port  : {s.get('rcon_port')}")
            print(f"    platform   : {s.get('platform')}   max players: {s.get('max_players')}")
            print(f"    cluster    : {s.get('cluster_id')} @ {s.get('cluster_dir')}")
            print(f"    mods       : {s.get('mod_count')}")
            print(f"    switches   : {', '.join(s.get('switches') or []) or '(none)'}")
        else:
            print("  No launch source available -- cannot determine args")

        installed = sum(1 for m in result.mods if m.installed)
        in_args = sum(1 for m in result.mods if m.in_load_order)
        named = sum(1 for m in result.mods if not m.name.startswith("Mod"))
        print()
        print(f"  Mod table: {len(result.mods)} total | {in_args} in launch args | "
              f"{installed} installed | {named} with real names")
        drifted = [m for m in result.mods if m.drift]
        if drifted:
            print("  Mod drift:")
            for m in drifted:
                print(f"    - {m.name} ({m.mod_id}): {m.drift}")
        else:
            print("  Mod drift: none")

        if result.conflicts:
            print()
            print("  SOURCE CONFLICTS:")
            for c in result.conflicts:
                vals = "  ".join(f"{k}={v}" for k, v in c["values"].items())
                print(f"    {c['field']:20} {vals}")
        else:
            print("  Source conflicts: none")

        out_dir = os.path.join(BASE, "data", "servers", name)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "import.json"), "w", encoding="utf-8") as fh:
            json.dump(to_record(result), fh, indent=2)
        print(f"\n  -> data/servers/{name}/import.json")

    return 0


if __name__ == "__main__":
    sys.exit(main())
