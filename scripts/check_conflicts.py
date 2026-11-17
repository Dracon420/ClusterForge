"""Compare launch-argument sources for a server, without going through an agent.

Used to verify the comparison logic against Svartalfheim's files over the Z:
share before asking PC2 to restart its agent.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.importer import import_server  # noqa: E402

TARGETS = [
    ("island", r"D:\Servers\TheIsland", "TheIsland", r"D:\Servers\TheIsland\StartServer.bat"),
    ("svartalfheim", r"Z:\Svartalfhiem", "Svartalfhiem", r"Z:\StartServer.bat"),
]

for name, install, profile, bat in TARGETS:
    if not os.path.isdir(install):
        print(f"{name}: unreachable\n")
        continue
    r = import_server(name=name, install_dir=install, profile_name=profile, bat_path=bat)
    print("=" * 70)
    print(name)
    print("=" * 70)
    for v in r.views.values():
        state = "available" if v.available else "missing"
        print(f"  {v.source:24} {state:10} {len(v.mods)} mods")
    if not r.conflicts:
        print("  conflicts: none")
    else:
        print(f"  conflicts: {len(r.conflicts)}")
        for c in r.conflicts:
            vals = "  ".join(f"{k}={v}" for k, v in c["values"].items())
            print(f"    {c['field']:18} {vals}")
            for k, missing in (c.get("detail") or {}).items():
                if missing:
                    print(f"        missing from {k}: {missing}")
    print()
