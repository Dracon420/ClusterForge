"""
Steam build detection and updates.

Two questions, two different sources:

  What is INSTALLED?   steamapps/appmanifest_<appid>.acf, written by steamcmd
                       itself. Authoritative. PatchRaptor instead keeps its own
                       version.txt, which on this cluster had drifted a whole
                       build behind reality (25002750 vs 25117056) -- a file you
                       maintain yourself is a file that can be wrong.

  What is AVAILABLE?   steamcmd +app_info_print, reading the buildid of a
                       NAMED BRANCH.

That second point matters more than it looks. app_info_print returns a buildid
for every branch:

    "public"             "25117056"     <- the one that matters
    "public_test_realm"  "21994402"     <- months old

Anything that greps for the first "buildid" line is relying on Steam's ordering
never changing. If it ever did, the comparison would be against the test realm:
either permanently "an update is available", or missing real updates entirely.
So the branch block is located explicitly.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

APP_ID = "2430930"
DEFAULT_BRANCH = "public"

APP_INFO_TIMEOUT = 300
UPDATE_TIMEOUT = 7200


def installed_build(install_dir: str, app_id: str = APP_ID) -> dict:
    """Read the installed build from steamcmd's own manifest."""
    path = os.path.join(install_dir, "steamapps", f"appmanifest_{app_id}.acf")
    result = {"build": None, "target_build": None, "last_updated": None,
              "path": path, "source": "appmanifest", "error": None}
    if not os.path.exists(path):
        result["error"] = f"manifest not found: {path}"
        return result
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        result["error"] = str(exc)
        return result

    def field(name):
        m = re.search(rf'"{name}"\s+"([^"]+)"', text)
        return m.group(1) if m else None

    result["build"] = field("buildid")
    result["target_build"] = field("TargetBuildID")
    updated = field("LastUpdated")
    if updated and updated.isdigit():
        result["last_updated"] = int(updated)

    # buildid != TargetBuildID means steamcmd knows about an update it has not
    # finished applying -- an interrupted or partial update.
    if result["build"] and result["target_build"] and result["build"] != result["target_build"]:
        result["error"] = (f"installed build {result['build']} does not match target "
                           f"{result['target_build']} - a previous update may not have "
                           "completed")
    return result


def _extract_branch_buildid(text: str, branch: str) -> str | None:
    """Pull one branch's buildid out of app_info_print output.

    Walks braces rather than pattern-matching loosely, so a buildid belonging
    to a different branch can never be returned by accident.
    """
    branches_at = text.find('"branches"')
    if branches_at == -1:
        return None

    open_at = text.find("{", branches_at)
    if open_at == -1:
        return None

    # Find the extent of the branches block.
    depth = 0
    end = len(text)
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    block = text[open_at:end]

    # Locate the named branch inside it, then its own sub-block.
    m = re.search(rf'"{re.escape(branch)}"\s*\{{', block)
    if not m:
        return None
    sub_start = m.end() - 1
    depth = 0
    sub_end = len(block)
    for i in range(sub_start, len(block)):
        if block[i] == "{":
            depth += 1
        elif block[i] == "}":
            depth -= 1
            if depth == 0:
                sub_end = i
                break
    sub = block[sub_start:sub_end]

    found = re.search(r'"buildid"\s+"(\d+)"', sub)
    return found.group(1) if found else None


def latest_build(steamcmd_path: str, app_id: str = APP_ID,
                 branch: str = DEFAULT_BRANCH) -> dict:
    """Ask Steam for the current buildid of a branch."""
    result = {"build": None, "branch": branch, "error": None,
              "checked_at": time.time()}
    if not os.path.exists(steamcmd_path):
        result["error"] = f"steamcmd not found: {steamcmd_path}"
        return result

    # app_info_update 1 forces a refresh from Steam. WITHOUT it steamcmd happily
    # answers from its local appinfo cache, and the two agents then disagree
    # about what build exists -- which is exactly what happened on 2026-09-08:
    # PC1 knew about the new build and tried to fetch it, while PC2's steamcmd
    # still thought the old build was current, did nothing, and reported
    # "Success! App fully installed" for a patch it had never applied.
    args = [os.path.abspath(steamcmd_path), "+login", "anonymous",
            "+app_info_update", "1", "+app_info_print", str(app_id), "+quit"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=APP_INFO_TIMEOUT,
            creationflags=flags, cwd=os.path.dirname(os.path.abspath(steamcmd_path)),
            stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        result["error"] = f"steamcmd timed out after {APP_INFO_TIMEOUT}s"
        return result
    except OSError as exc:
        result["error"] = f"could not run steamcmd: {exc}"
        return result

    build = _extract_branch_buildid(proc.stdout or "", branch)
    if not build:
        result["error"] = (f"could not find a buildid for branch '{branch}'. "
                           "Steam may be unreachable, or the output format changed.")
        return result
    result["build"] = build
    return result


def compare(install_dir: str, steamcmd_path: str, app_id: str = APP_ID,
            branch: str = DEFAULT_BRANCH) -> dict:
    """Installed vs available, with an explicit unknown when either is missing."""
    have = installed_build(install_dir, app_id)
    want = latest_build(steamcmd_path, app_id, branch)

    if have["build"] and want["build"]:
        status = "up_to_date" if have["build"] == want["build"] else "update_available"
    else:
        # Never guess. A failed check must not read as "up to date" and let a
        # patch sit unapplied, nor as "update available" and trigger a restart.
        status = "unknown"

    return {
        "status": status,
        "installed": have["build"],
        "available": want["build"],
        "branch": branch,
        "installed_at": have["last_updated"],
        "errors": [e for e in (have["error"], want["error"]) if e],
    }


# Steam refusing to hand out a download manifest is COMMON and transient. The
# first real patch here died on it repeatedly:
#
#   Failed to get manifest request code, 'Access Denied'
#   update canceled : Failed downloading 1 manifests (No connection)
#
# and none of it appears in steamcmd's console output -- only in
# <install>/logs/content_log.txt, while the console just says "state is 0x6".
# These are matched against the console tail AND that log so a retryable
# refusal is not mistaken for a broken install.
TRANSIENT_MARKERS = (
    "failed to get manifest request code",
    "access denied",
    "no connection",
    "failed downloading",
    "state is 0x6",
    "rate limit",
    "connection to steam servers lost",
    "timeout downloading",
)

UPDATE_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (60, 180)


def _content_log_tail(install_dir: str, lines: int = 40) -> list[str]:
    """steamcmd's real error lives here, not on stdout."""
    path = os.path.join(install_dir, "logs", "content_log.txt")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip() for ln in fh.readlines()[-lines:]]
    except OSError:
        return []


def looks_transient(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in TRANSIENT_MARKERS)


def manifest_path(install_dir: str, app_id: str = APP_ID) -> str:
    return os.path.join(install_dir, "steamapps", f"appmanifest_{app_id}.acf")


# StateFlags is a bitfield. 0x2 = UpdateRequired. A manifest sitting in
# "Update Required" with a TargetBuildID it never reached is STUCK: steamcmd
# keeps asking Steam for one specific old manifest id, Steam answers
# 'Access Denied', and it fails in ~25 seconds forever. Clearing the manifest
# makes steamcmd re-acquire the current one and it downloads normally.
#
# This cost hours of downtime on 2026-09-08 while it was misread as Steam
# being down -- a scratch install dir downloaded the same build perfectly.
STATE_UPDATE_REQUIRED = 0x2


def preflight(install_dir: str, steamcmd_path: str, app_id: str = APP_ID) -> dict:
    """Check, WITHOUT stopping anything, whether an update can even proceed.

    The whole point is to run this before the servers go down. Discovering a
    blocked update after shutting the cluster off is what turns a patch into an
    outage.
    """
    out = {"ok": True, "problems": [], "fixable": [], "state_flags": None,
           "installed": None, "target": None}

    if not os.path.exists(steamcmd_path):
        out["ok"] = False
        out["problems"].append(f"steamcmd not found: {steamcmd_path}")
        return out

    path = manifest_path(install_dir, app_id)
    if not os.path.exists(path):
        # No manifest is fine -- steamcmd treats it as a fresh install.
        return out

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        out["ok"] = False
        out["problems"].append(f"cannot read {path}: {exc}")
        return out

    def field(name):
        found = re.search(rf'"{name}"\s+"([^"]+)"', text)
        return found.group(1) if found else None

    flags_raw = field("StateFlags")
    out["installed"] = field("buildid")
    out["target"] = field("TargetBuildID")
    try:
        flags = int(flags_raw)
    except (TypeError, ValueError):
        flags = None
    out["state_flags"] = flags

    if flags is not None and flags & STATE_UPDATE_REQUIRED:
        out["ok"] = False
        out["problems"].append(
            f"steamapps manifest is stuck in 'Update Required' (StateFlags {flags}"
            + (f", pending build {out['target']}" if out["target"] else "")
            + "). steamcmd will keep requesting a manifest Steam refuses.")
        out["fixable"].append("clear_manifest")

    return out


def wrote_anything(install_dir: str, app_id: str = APP_ID) -> bool:
    """Did a failed steamcmd run actually modify the install?

    Decides whether it is safe to restart the server after a failed update. A
    refusal at the manifest stage downloads nothing and leaves the install
    byte-for-byte intact, so keeping the server down buys nothing but downtime.
    Anything partially written means stay stopped.

    Errs toward caution: if this cannot tell, it says yes.
    """
    downloading = os.path.join(install_dir, "steamapps", "downloading")
    try:
        if os.path.isdir(downloading):
            for _root, _dirs, files in os.walk(downloading):
                if files:
                    return True
    except OSError:
        return True

    path = manifest_path(install_dir, app_id)
    if not os.path.exists(path):
        # The manifest is gone, so steamcmd was mid-reacquire. Not safe to call
        # the install untouched.
        return True
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return True

    found = re.search(r'"BytesDownloaded"\s+"(\d+)"', text)
    if found and int(found.group(1)) > 0:
        return True
    return False


def clear_stuck_manifest(install_dir: str, app_id: str = APP_ID) -> str | None:
    """Move a stuck appmanifest aside so steamcmd re-acquires a fresh one.

    Renamed rather than deleted -- it is the only record of what the install
    thought it was, and it costs nothing to keep.
    """
    path = manifest_path(install_dir, app_id)
    if not os.path.exists(path):
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = f"{path}.stuck_{stamp}"
    try:
        os.replace(path, target)
        return target
    except OSError:
        return None


def update_with_retries(install_dir: str, steamcmd_path: str, app_id: str = APP_ID,
                        validate: bool = True, on_line=None,
                        attempts: int = UPDATE_ATTEMPTS, sleep_fn=time.sleep) -> dict:
    """run_update, but retrying the failures that are Steam's fault.

    One refused manifest used to fail the whole job and leave the server
    stopped. Retrying costs a couple of minutes; not retrying costs an outage.
    A failure that does NOT look transient is returned immediately -- a genuinely
    broken install should not be hammered.
    """
    last = None
    for attempt in range(1, max(1, attempts) + 1):
        last = run_update(install_dir, steamcmd_path, app_id, validate, on_line)
        last["attempt"] = attempt
        if last.get("ok"):
            return last

        detail = "\n".join(last.get("tail") or []) + "\n" + \
                 "\n".join(_content_log_tail(install_dir))
        last["content_log"] = _content_log_tail(install_dir, 12)
        last["transient"] = looks_transient(detail)

        if not last["transient"] or attempt >= attempts:
            return last

        delay = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        if on_line:
            on_line(f"Steam refused the download (attempt {attempt}/{attempts}); "
                    f"retrying in {delay}s")
        sleep_fn(delay)
    return last


def run_update(install_dir: str, steamcmd_path: str, app_id: str = APP_ID,
               validate: bool = True, on_line=None) -> dict:
    """Run steamcmd app_update for the dedicated server.

    validate=True adds +validate, which checks every file. Slower, but an
    update that half-applied is worse than one that took longer.
    """
    if not os.path.exists(steamcmd_path):
        return {"ok": False, "error": f"steamcmd not found: {steamcmd_path}"}

    args = [os.path.abspath(steamcmd_path),
            "+force_install_dir", os.path.abspath(install_dir),
            "+login", "anonymous",
            "+app_update", str(app_id)]
    if validate:
        args.append("validate")
    args.append("+quit")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    started = time.time()
    tail: list[str] = []
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=flags, cwd=os.path.dirname(os.path.abspath(steamcmd_path)),
            stdin=subprocess.DEVNULL)
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            tail[:] = tail[-40:]
            if on_line and ("%" in line or "Success" in line or "ERROR" in line
                            or "Error" in line):
                on_line(line)
        code = proc.wait(timeout=UPDATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"ok": False, "error": f"steamcmd update exceeded {UPDATE_TIMEOUT}s",
                "tail": tail}
    except OSError as exc:
        return {"ok": False, "error": f"could not run steamcmd: {exc}"}

    joined = "\n".join(tail)
    success = ("Success! App" in joined and "fully installed" in joined) or code == 0
    return {
        "ok": success,
        "exit_code": code,
        "seconds": round(time.time() - started),
        "tail": tail[-15:],
        "error": None if success else
                 f"steamcmd exited {code} without reporting success",
    }
