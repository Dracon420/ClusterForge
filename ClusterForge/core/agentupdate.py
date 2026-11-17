"""Agents fetch their own code from the panel.

The recurring failure of this project, in one line: an agent silently runs old
code. Deploying has meant pushing files to each machine and restarting it
there, which needs a route to every PC -- an SMB share for one, a remote
session for another -- and when a machine has neither, the deploy reports
success having delivered nothing. PC3 was restarted three times in one evening
without receiving a single file.

So the direction is reversed. The panel PUBLISHES its agent code and each agent
PULLS it. An agent needs nothing but the panel's URL and the token it already
has, which is also what makes a customer's third PC work with no file share
between the machines.

Rules that keep this from being dangerous:

  * The file list comes from codestamp._files() - agent.py plus the core
    modules it actually imports. Not "everything in core", which would drag
    panel-only modules onto machines that never run them.

  * Every file is hashed, downloaded to a staging folder, and verified against
    the manifest BEFORE anything is replaced. A half-downloaded agent.py is an
    agent that does not start, on a machine that may be in another house.

  * config.json and secrets.json are never fetched or written. Agents
    legitimately differ - their own paths, their own servers, their own bot
    token - and overwriting those would be the worst kind of "update".

  * An agent that is busy does not update. A backup or a map switch mid-flight
    matters more than being current five minutes sooner.
"""

from __future__ import annotations

import hashlib
import os
import shutil

from . import codestamp

# Files an agent may receive, beyond its code: the map catalogue and the wiki
# data, both of which the agent reads and neither of which is machine-specific.
EXTRA_FILES = [
    os.path.join("data", "maps", "catalog.json"),
]
EXTRA_GLOBS = [
    (os.path.join("data", "wiki"), ".csv"),
]

NEVER = {"config.json", "secrets.json"}


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest(base_dir: str) -> dict:
    """What an agent should have, as {path: sha256} plus the code fingerprint.

    Paths are relative and always forward-slashed, so the same manifest reads
    the same on any machine.
    """
    files = {}

    for absolute in codestamp._files(base_dir):
        rel = os.path.relpath(absolute, base_dir).replace("\\", "/")
        files[rel] = _sha256(absolute)

    for rel in EXTRA_FILES:
        absolute = os.path.join(base_dir, rel)
        if os.path.isfile(absolute):
            files[rel.replace("\\", "/")] = _sha256(absolute)

    for folder, suffix in EXTRA_GLOBS:
        absolute_dir = os.path.join(base_dir, folder)
        if not os.path.isdir(absolute_dir):
            continue
        for name in sorted(os.listdir(absolute_dir)):
            if not name.lower().endswith(suffix):
                continue
            absolute = os.path.join(absolute_dir, name)
            rel = os.path.join(folder, name).replace("\\", "/")
            files[rel] = _sha256(absolute)

    return {"fingerprint": codestamp.fingerprint(base_dir), "files": files}


def safe_path(base_dir: str, rel: str) -> str | None:
    """Resolve a manifest path, or None if it tries to escape or is forbidden.

    A path arriving over HTTP decides where a file gets written, so this is the
    difference between an update and an arbitrary write.
    """
    rel = (rel or "").replace("\\", "/").strip()
    if not rel or ".." in rel.split("/"):
        return None
    # An ABSOLUTE path is refused, not quietly reinterpreted. Stripping the
    # leading slash turned "/etc/passwd" into a write inside the install, which
    # is not what was asked for and not something to guess about.
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        return None
    if os.path.basename(rel).lower() in NEVER:
        return None
    absolute = os.path.abspath(os.path.join(base_dir, rel))
    if not absolute.startswith(os.path.abspath(base_dir) + os.sep):
        return None
    return absolute


def differences(base_dir: str, wanted: dict) -> list[str]:
    """Which files this machine is missing or has a different version of."""
    out = []
    for rel, digest in (wanted.get("files") or {}).items():
        absolute = safe_path(base_dir, rel)
        if absolute is None:
            continue
        if not os.path.isfile(absolute) or _sha256(absolute) != digest:
            out.append(rel)
    return sorted(out)


def apply_update(base_dir: str, staged: dict, wanted: dict, log=print) -> dict:
    """Move verified files from staging into place.

    staged: {relative path -> staged file on disk}. Every one is re-hashed here
    before it is trusted, because the check that matters is the one immediately
    before the write.
    """
    applied, rejected = [], []
    for rel, temp_path in staged.items():
        expected = (wanted.get("files") or {}).get(rel)
        target = safe_path(base_dir, rel)
        if target is None or not expected:
            rejected.append(f"{rel}: not an allowed path")
            continue
        try:
            if _sha256(temp_path) != expected:
                rejected.append(f"{rel}: hash mismatch after download")
                continue
        except OSError as exc:
            rejected.append(f"{rel}: {exc}")
            continue

        os.makedirs(os.path.dirname(target), exist_ok=True)
        backup = target + ".prev"
        try:
            if os.path.isfile(target):
                shutil.copy2(target, backup)
            shutil.move(temp_path, target)
            applied.append(rel)
        except OSError as exc:
            rejected.append(f"{rel}: {exc}")
            # Put back whatever was there, so a failed update does not leave a
            # machine with half of two versions.
            if os.path.isfile(backup) and not os.path.isfile(target):
                try:
                    shutil.move(backup, target)
                except OSError:
                    pass

    for rel in applied:
        log(f"[selfupdate] updated {rel}")
    for problem in rejected:
        log(f"[selfupdate] REFUSED {problem}")
    return {"applied": applied, "rejected": rejected,
            "ok": bool(applied) and not rejected}
