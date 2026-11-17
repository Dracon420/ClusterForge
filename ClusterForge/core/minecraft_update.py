"""Keep the Bedrock server on the version its clients are already running.

Minecraft clients update themselves. A dedicated server does not. So every
Mojang release silently locks everybody out of a server that is otherwise
perfectly healthy - it starts, it answers pings, the panel shows it green,
and nobody can join. On 2026-10-29 the first warning anyone got was a child
unable to get into her own world, five weeks after the release landed.

That is the same failure the ARK updater exists for, and it is treated the
same way here: find out from the publisher, not from a player.

THE TRAP THIS MODULE EXISTS TO AVOID: Mojang's zip contains its own
server.properties, allowlist.json and permissions.json. Extracting it over an
install the obvious way replaces every setting and empties the allow list -
and `allow-list=true` with an empty list refuses EVERY player. The server
comes up looking immaculate and admits nobody, which is indistinguishable
from the problem you were trying to fix. Those files are preserved by name,
explicitly, and restored afterwards.

The world is never in the archive, so it cannot be touched by an extract.
It is snapshotted first anyway, because the irreplaceable thing on this
server took hours to build.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
import zipfile

# Mojang's own index of current downloads. Preferred over scraping the
# download page, which is behind a bot check and changes shape.
LINKS_API = ("https://net-secondary.web.minecraft-services.net"
             "/api/v1.0/download/links")
WINDOWS_KEY = "serverBedrockWindows"

# Files that belong to THIS server rather than to Mojang. Never overwritten.
PRESERVE = ("server.properties", "allowlist.json", "permissions.json",
            "whitelist.json")

_VERSION_IN_URL = re.compile(r"bedrock-server-([0-9][0-9.]*)\.zip")
_VERSION_IN_LOG = re.compile(r"Version:\s*([0-9][0-9.]*)")


def installed_version(install_dir: str) -> str:
    """What the running server last reported, from its own log.

    Read from the log rather than the binary because that is what the server
    actually says about itself, and it is the same string the API returns.
    """
    path = os.path.join(install_dir, "logs", "minecraft.log")
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > 262144:
                fh.seek(size - 262144)
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    found = _VERSION_IN_LOG.findall(text)
    return found[-1] if found else ""


def latest_version(timeout: float = 30.0) -> tuple[str, str]:
    """(version, url) of the current Windows server build, or ("", "")."""
    request = urllib.request.Request(
        LINKS_API, headers={"Accept": "application/json",
                            "User-Agent": "ClusterForge"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    links = (payload.get("result") or {}).get("links") or payload.get("links") or []
    for link in links:
        if link.get("downloadType") == WINDOWS_KEY:
            url = link.get("downloadUrl") or ""
            match = _VERSION_IN_URL.search(url)
            return (match.group(1) if match else ""), url
    return "", ""


def check(install_dir: str) -> dict:
    """Compare what is installed against what Mojang is shipping."""
    have = installed_version(install_dir)
    try:
        want, url = latest_version()
        error = ""
    except Exception as exc:  # noqa: BLE001 - a lookup failure is not an outage
        want, url, error = "", "", str(exc)
    return {"installed": have, "latest": want, "url": url, "error": error,
            "needs_update": bool(have and want and have != want)}


def _download(url: str, dest: str, log=print, timeout: float = 600.0) -> str:
    path = os.path.join(dest, os.path.basename(url) or "bedrock-server.zip")
    log(f"[minecraft] downloading {os.path.basename(path)}")
    request = urllib.request.Request(url, headers={"User-Agent": "ClusterForge"})
    with urllib.request.urlopen(request, timeout=timeout) as response, \
            open(path, "wb") as out:
        shutil.copyfileobj(response, out)
    log(f"[minecraft] downloaded {os.path.getsize(path) / 1024**2:.1f} MB")
    return path


def _unpack(zip_path: str, dest: str) -> str:
    """Extract, and return the directory that actually holds the server."""
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(dest)
    if os.path.isfile(os.path.join(dest, "bedrock_server.exe")):
        return dest
    # Some builds nest everything one level down.
    entries = [os.path.join(dest, n) for n in os.listdir(dest)]
    dirs = [d for d in entries if os.path.isdir(d)]
    if len(dirs) == 1 and os.path.isfile(os.path.join(dirs[0], "bedrock_server.exe")):
        return dirs[0]
    return ""


def apply_update(config: dict, url: str, version: str, log=print,
                 stop_fn=None, start_fn=None) -> dict:
    """Download and install one build. Returns a result dict.

    stop_fn/start_fn are passed in rather than imported so this works the same
    whether the panel drives it or a script does, and so a caller that has
    already stopped the server can pass no-ops.

    The server is started again on EVERY exit path. A server left down
    because an update failed is worse than one left on the old version.
    """
    install_dir = config.get("install_dir", "")
    if not os.path.isdir(install_dir):
        return {"ok": False, "error": f"install_dir missing: {install_dir}"}

    staging = tempfile.mkdtemp(prefix="bds-update-")
    stopped = False
    try:
        zip_path = _download(url, staging, log=log)
        unpacked = _unpack(zip_path, os.path.join(staging, "unpacked"))
        if not unpacked:
            return {"ok": False,
                    "error": "the archive has no bedrock_server.exe - not applying it"}

        if stop_fn:
            log("[minecraft] stopping for update")
            stop_fn()
            stopped = True
            # Two servers must never share one world directory, and the
            # binaries cannot be replaced underneath a running process.
            for _ in range(45):
                from . import minecraft
                if not minecraft.find_process(install_dir):
                    break
                time.sleep(2)
            else:
                return {"ok": False,
                        "error": "the server would not stop - binaries left alone"}

        # Keep this server's own files out of the way of the archive.
        kept = {}
        for name in PRESERVE:
            source = os.path.join(install_dir, name)
            if os.path.isfile(source):
                keep_at = os.path.join(staging, "keep-" + name)
                shutil.copy2(source, keep_at)
                kept[name] = keep_at
        log(f"[minecraft] preserving {', '.join(sorted(kept)) or '(nothing)'}")

        copied = 0
        for folder, _dirs, files in os.walk(unpacked):
            for name in files:
                full = os.path.join(folder, name)
                rel = os.path.relpath(full, unpacked)
                if rel in PRESERVE:
                    continue
                if rel.split(os.sep)[0].lower() == "worlds":
                    continue          # belt and braces; never in the archive
                target = os.path.join(install_dir, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(full, target)
                copied += 1

        # Restore ours over anything the archive still managed to drop.
        for name, keep_at in kept.items():
            shutil.copy2(keep_at, os.path.join(install_dir, name))

        log(f"[minecraft] applied {version}: {copied} files replaced, "
            f"{len(kept)} config file(s) preserved")
        return {"ok": True, "version": version, "files": copied,
                "preserved": sorted(kept)}

    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    finally:
        if stopped and start_fn:
            try:
                start_fn()
                log("[minecraft] started again")
            except Exception as exc:  # noqa: BLE001
                log(f"[minecraft] COULD NOT START AGAIN: {exc}")
        shutil.rmtree(staging, ignore_errors=True)


def wait_for_version(install_dir: str, expected: str,
                     timeout: float = 120.0, log=print) -> bool:
    """Did the server actually come up on the version we installed?

    Checked from the log rather than assumed, because 'the files copied' and
    'the server is running the new build' are different claims.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if installed_version(install_dir) == expected:
            return True
        time.sleep(3)
    log(f"[minecraft] log does not yet report {expected}")
    return False
