"""
One-shot fresh install of the ASA dedicated server into a new directory.

Used to stand up a second server on a PC. It deliberately does NOT copy an
existing install: a copied steamapps/appmanifest is exactly the kind of stale
Steam state that stuck the first real patch for an hour. A clean steamcmd
install writes its own correct manifest.

It goes through core.steam.update_with_retries -- the same hardened path the
auto-updater uses -- so a Steam refusal partway through a ~26 GB download is
retried rather than failing the whole install.

Progress is appended to logs/fresh_install_<name>.log so it can be read over
the network share while it runs.

Usage:  python scripts\\fresh_install.py <name> <install_dir> <steamcmd.exe>
"""

import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import steam                                           # noqa: E402


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    name, install_dir, steamcmd = sys.argv[1], sys.argv[2], sys.argv[3]

    log_path = os.path.join(BASE, "logs", f"fresh_install_{name}.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    def log(message: str) -> None:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")

    log(f"fresh install of app {steam.APP_ID} into {install_dir}")
    os.makedirs(install_dir, exist_ok=True)

    pre = steam.preflight(install_dir, steamcmd)
    log(f"preflight: {json.dumps(pre)}")
    if not pre.get("ok"):
        log("FAILED - preflight refused, nothing downloaded")
        return 1

    started = time.time()
    result = steam.update_with_retries(install_dir, steamcmd, validate=True, on_line=log)
    minutes = round((time.time() - started) / 60, 1)
    summary = {k: v for k, v in result.items() if k not in ("tail", "content_log")}
    log(f"result after {minutes} min: {json.dumps(summary)}")

    if result.get("ok"):
        build = steam.installed_build(install_dir)
        log(f"installed build: {json.dumps(build)}")
        log("DONE")
        return 0

    for line in (result.get("tail") or [])[-15:]:
        log(f"  steamcmd: {line}")
    log("FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
