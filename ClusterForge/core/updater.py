"""
Cluster-coordinated auto-update.

Runs in the PANEL, not the agents, because it is the only component that can
see both machines. An agent updating on its own initiative is the failure this
is designed to avoid.

Why coordination matters: both maps must run the same build. A version-mismatched
cluster breaks cross-server transfers, and a map behind Steam's current build
cannot be joined at all once clients auto-update. So:

  ONE MAP AT A TIME (updates.stagger, default on). This reverses the original
  design, which patched both at once to shorten the window where the maps
  differ. That window turned out not to be the real cost: during ANY patch the
  cluster is unjoinable anyway, so there is no split worth protecting. What
  simultaneous patching did cost was blast radius -- on 2026-09-08 both maps
  went down, both failed, and both stayed down. Sequentially, the first failure
  stops the run and the untouched map keeps serving.

  A settle delay after a build appears (updates.settle_minutes, default 15).
  Steam publishes the build id before the content is reliably downloadable;
  patching instantly patches into that gap.

  If ANY agent is unreachable, NOTHING updates. Holding leaves a working
  cluster on an old build; proceeding leaves a split one. Holding is better,
  and it alerts rather than failing silently.

  An unknown version check is never treated as "up to date". Steam being
  unreachable must not quietly park a patch forever.

Timing is player-aware rather than clock-aware, because Steam does not tell us
how urgent a patch is:

  nobody online   patch immediately, no countdown
  anyone online   full warning ladder on BOTH maps first

Most patches land when nobody is on, so in practice this is genuinely instant.
"""

from __future__ import annotations

import threading
import time

POLL_JITTER = 0.15   # spread polls so both PCs never hit Steam in lockstep


# How long to wait before retrying a build that has already failed, and how
# many attempts before giving up and waiting for a human. Tuned so a genuine
# Steam-side blip clears on its own, but a broken patch cannot restart the
# cluster over and over unattended.
BUILD_RETRY_COOLDOWN = 3600      # 1 hour
MAX_BUILD_ATTEMPTS = 3


def _target_build(state: dict) -> str:
    """The build every map is trying to reach.

    check() reports availability per server rather than cluster-wide, and the
    two agents ask Steam independently. Normally they agree; if they briefly do
    not, take the highest, since that is the one that will still be pending on
    the next poll.
    """
    seen = []
    for info in (state.get("servers") or {}).values():
        available = (info or {}).get("available")
        if available:
            seen.append(str(available))
    if not seen:
        return ""
    try:
        return max(seen, key=int)
    except (TypeError, ValueError):
        return sorted(seen)[-1]


class Updater:
    def __init__(self, config, servers_fn, version_fn, players_fn,
                 update_fn, reachable_fn, notify_fn=None, job_status_fn=None,
                 logger=print):
        self.config = config
        self.servers_fn = servers_fn        # () -> list[server dict]
        self.version_fn = version_fn        # (server) -> compare() dict | None
        self.players_fn = players_fn        # (server) -> int | None  (None = unknown)
        self.update_fn = update_fn          # (server, warn_minutes, target_build) -> job dict
        self.reachable_fn = reachable_fn    # (server) -> bool
        self.notify_fn = notify_fn
        self.job_status_fn = job_status_fn  # (server_name, job_id) -> job dict
        self.log = logger

        self.last_check = 0.0
        self.last_result: dict = {}
        self.history: list[dict] = []
        # target build -> (failure count, epoch of last attempt). Keeps a patch
        # that cannot succeed from restarting the cluster on every poll.
        self.failed_builds: dict[str, tuple[int, float]] = {}
        # target build -> epoch first seen, for the settle delay below
        self.first_seen: dict[str, float] = {}
        self._applying = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = threading.Lock()

    # ---------------------------------------------------------------- naming

    def label(self, name: str) -> str:
        """The map's name as a PERSON knows it, for anything people read.

        Internally a server is its slot: 'nyrandil'. That slot has run Amissa
        and now runs Scorched Earth, because a map switch renames the server
        without renaming the slot. Discord kept announcing updates for
        "nyrandil", a map nobody has been able to join since 2026-09-14.

        Read live rather than cached: the next map switch renames it again.
        """
        try:
            for server in self.servers_fn():
                if server.get("name") == name:
                    return server.get("display_name") or name
        except Exception:  # noqa: BLE001 - a label must never break an alert
            pass
        return name

    def labels(self, names) -> str:
        return ", ".join(f"**{self.label(n)}**" for n in names)

    # ---------------------------------------------------------------- config

    @property
    def _cfg(self) -> dict:
        return self.config.get("updates", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.get("enabled", False))

    @property
    def auto_apply(self) -> bool:
        return bool(self._cfg.get("auto_apply", False))

    # ----------------------------------------------------------------- check

    def check(self) -> dict:
        """Ask every agent what it has and what Steam is offering."""
        servers = self.servers_fn()
        per_server = {}
        unreachable = []
        needs = []
        unknown = []

        for server in servers:
            name = server["name"]
            if not self.reachable_fn(server):
                unreachable.append(name)
                per_server[name] = {"status": "unreachable"}
                continue
            info = self.version_fn(server)
            if not info:
                unknown.append(name)
                per_server[name] = {"status": "unknown", "errors": ["no response"]}
                continue
            per_server[name] = info
            if info.get("status") == "update_available":
                needs.append(name)
            elif info.get("status") != "up_to_date":
                unknown.append(name)

        builds = {n: v.get("installed") for n, v in per_server.items()
                  if v.get("installed")}
        mismatched = len(set(builds.values())) > 1

        result = {
            "checked_at": time.time(),
            "servers": per_server,
            "needs_update": needs,
            "unknown": unknown,
            "unreachable": unreachable,
            "builds": builds,
            # A split cluster is a problem in its own right, whether or not an
            # update is pending.
            "build_mismatch": mismatched,
            "can_proceed": bool(needs) and not unreachable and not unknown,
        }
        # Remember when each build FIRST appeared, so the settle delay has
        # something to measure from.
        found = _target_build(result)
        if found and found not in self.first_seen:
            self.first_seen[found] = result["checked_at"]
            self.log(f"[updater] build {found} first seen")
        result["target_build"] = found
        result["first_seen"] = self.first_seen.get(found)

        self.last_check = result["checked_at"]
        self.last_result = result
        return result

    # ---------------------------------------------------------------- apply

    def apply(self, force: bool = False, dry_run: bool = False) -> dict:
        """Update every server that needs it, together."""
        if self._applying or not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "busy",
                    "message": "An update is already in progress."}
        try:
            return self._apply_locked(force=force, dry_run=dry_run)
        finally:
            self._busy.release()

    def _apply_locked(self, force: bool, dry_run: bool) -> dict:
        state = self.check()

        if state["unreachable"]:
            msg = (f"Not updating: cannot reach {', '.join(state['unreachable'])}. "
                   "A cluster-wide update is applied to every map or none - a split "
                   "cluster breaks transfers and locks players out of the stale map.")
            self._alert("update_held", "warn", msg)
            return {"ok": False, "error": "agent_unreachable", "message": msg,
                    "state": state}

        if state["unknown"] and not force:
            msg = (f"Not updating: could not determine the build for "
                   f"{', '.join(state['unknown'])}. An unknown check is not treated "
                   "as up to date, so this holds rather than guessing.")
            self._alert("update_held", "warn", msg)
            return {"ok": False, "error": "version_unknown", "message": msg,
                    "state": state}

        if not state["needs_update"] and not force:
            return {"ok": True, "updated": [], "message": "Everything is up to date.",
                    "state": state}

        # A patch that CANNOT succeed must not cycle the cluster.
        #
        # The first real patch failed on Steam's side, and with a 7-minute poll
        # the updater simply tried again: stop both maps, fail, leave them
        # stopped, repeat. A failed update became a rolling outage. So a build
        # that has already failed is not retried until the cooldown expires,
        # and after enough failures it holds until a human intervenes.
        target = _target_build(state)

        # SETTLE DELAY. Steam publishes the new build id in its app metadata
        # before the depot content is reliably downloadable -- patching the
        # instant we notice is patching into that gap, and a failure there costs
        # a full outage. Waiting a little makes the first attempt far more
        # likely to be the only attempt.
        #
        # Manual updates (force) skip this: if a human is asking, they have
        # decided the timing themselves.
        delay_minutes = float(self._cfg.get("settle_minutes", 15) or 0)
        # dry_run is 'show me the plan' -- it must never be blocked by a
        # scheduling gate, or the panel cannot tell you what it intends.
        if not force and not dry_run and target and delay_minutes > 0:
            seen = self.first_seen.get(target)
            if seen:
                waited = time.time() - seen
                if waited < delay_minutes * 60:
                    remaining = int((delay_minutes * 60 - waited) / 60) + 1
                    return {"ok": False, "error": "settling",
                            "message": (f"Build {target} was spotted "
                                        f"{int(waited / 60)} min ago; waiting "
                                        f"{remaining} more min before patching, so "
                                        "Steam has finished publishing it. "
                                        "Force an update to go now."),
                            "state": state}

        if not force and not dry_run and target:
            failures = self.failed_builds.get(target)
            if failures:
                count, last_try = failures
                if count >= MAX_BUILD_ATTEMPTS:
                    msg = (f"Not retrying build {target}: it has failed "
                           f"{count} times. Fix the cause and update manually, "
                           "or force it. The servers are left alone.")
                    self._alert("update_held", "warn", msg)
                    return {"ok": False, "error": "too_many_failures",
                            "message": msg, "state": state}
                waited = time.time() - last_try
                if waited < BUILD_RETRY_COOLDOWN:
                    remaining = int(BUILD_RETRY_COOLDOWN - waited)
                    return {"ok": False, "error": "cooling_down",
                            "message": (f"Build {target} failed {count} time(s); "
                                        f"waiting {remaining}s before trying again "
                                        "rather than restarting the cluster on a loop."),
                            "state": state}

        # When ANY map needs a patch, EVERY map is updated -- not just the ones
        # flagged. Patching only the laggard is how a cluster ends up split:
        # the two maps would finish on different builds, which breaks transfers
        # and locks players out of the stale one.
        #
        # Normally both agents query the same public branch and report the same
        # available build, so this changes nothing. The case where only one is
        # behind means the cluster has ALREADY diverged, and that is precisely
        # when bringing both to the same build matters most. steamcmd on an
        # already-current install verifies and exits, so the cost is one
        # restart of a server that was going to be restarted anyway.
        targets = list(self.servers_fn())

        # Player-aware timing: the ladder only earns its disruption if anyone
        # is actually online.
        counts = {s["name"]: self.players_fn(s) for s in self.servers_fn()}
        known = [c for c in counts.values() if c is not None]
        unknown_players = any(c is None for c in counts.values())
        total = sum(known) if known else 0

        warn_cfg = self.config.get("schedule", {}).get("warn_minutes", [])
        if unknown_players:
            # Never assume an empty server just because the query failed.
            warn = warn_cfg
            reason = "player count unknown - warning as if players are online"
        elif total > 0:
            warn = warn_cfg
            reason = f"{total} player(s) online - full warning ladder"
        else:
            warn = []
            reason = "nobody online - updating immediately"

        plan = {
            "targets": [s["name"] for s in targets],
            "players": counts,
            "warn_minutes": warn,
            "reason": reason,
            # Carried so _follow can record success or failure against the
            # specific build, and so the agent can verify the patch actually
            # landed rather than trusting steamcmd's exit code.
            "target_build": target,
            "builds": state["builds"],
            "available": {n: v.get("available") for n, v in state["servers"].items()},
        }
        if dry_run:
            return {"ok": True, "dry_run": True, "plan": plan, "state": state}

        self._alert("update_starting", "info",
                    f"Updating {self.labels(plan['targets'])} - {reason}."
                    # The one thing a staggered update cannot survive: a survivor
                    # uploaded against the old build trying to land on the new
                    # one. That cost a player their gear and tames on
                    # 2026-09-17, and the build-mismatch alert only fires once
                    # the maps have already split.
                    "\n\n:warning: **Do not transfer between maps until this "
                    "finishes** - maps are patched one at a time, and a transfer "
                    "that starts on one build and lands on another can lose "
                    "what you are carrying.")

        # ONE MAP AT A TIME, by default.
        #
        # The original design patched both at once to minimise the window where
        # the maps sit on different builds. That reasoning does not survive
        # contact with reality: a server behind Steam's build cannot be joined
        # at all, so during ANY patch window the cluster is unusable regardless
        # of whether the two maps match each other. There is no split to protect.
        #
        # What simultaneous updating does cost is blast radius. On 2026-09-08
        # both maps were taken down, both failed, and both stayed down. Done
        # sequentially, the first failure stops the sequence and the second map
        # is never touched -- half the outage, and a working map left running.
        # It also stops the two PCs fighting over one WAN link for 9 GB each.
        if self._cfg.get("stagger", True) and len(targets) > 1:
            self._applying = True
            threading.Thread(target=self._run_staggered, args=(targets, warn, target, plan),
                             daemon=True, name="clusterforge-update-stagger").start()
            record = {"at": time.time(), "plan": plan, "results": {}, "errors": [],
                      "mode": "staggered"}
            self.history.append(record)
            self.history = self.history[-50:]
            return {"ok": True, "plan": plan, "staggered": True,
                    "message": (f"Updating {len(targets)} maps one at a time, "
                                f"starting with {targets[0]['name']}. If one fails "
                                "the rest are left alone.")}

        results, errors = {}, []
        threads = []

        def run(server):
            try:
                job = self.update_fn(server, warn, target)
                results[server["name"]] = job
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{server['name']}: {exc}")
                results[server["name"]] = {"error": str(exc)}

        for server in targets:
            t = threading.Thread(target=run, args=(server,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=60)

        record = {"at": time.time(), "plan": plan, "results": results,
                  "errors": errors}
        self.history.append(record)
        self.history = self.history[-50:]

        # Follow the jobs to completion and report the OUTCOME. Firing an
        # update and never saying whether it worked is the worst possible
        # behaviour for something that runs unattended: a failed patch leaves
        # the servers stopped, and silence looks identical to success.
        if self.job_status_fn:
            jobs = {name: (res.get("job") or {}).get("id")
                    for name, res in results.items()
                    if isinstance(res, dict) and (res.get("job") or {}).get("id")}
            if jobs:
                threading.Thread(target=self._follow, args=(jobs, plan),
                                 daemon=True, name="clusterforge-update-follow").start()

        return {"ok": not errors, "plan": plan, "results": results,
                "errors": errors}

    def _run_staggered(self, targets: list, warn: list, target: str, plan: dict) -> None:
        """Patch one map, confirm it worked, then move to the next.

        Stops at the first failure. The whole value of doing this sequentially
        is that a map which has not been touched yet stays up and playable.
        """
        done, failed = [], []
        try:
            for index, server in enumerate(targets, start=1):
                name = server["name"]
                self.log(f"[updater] staggered {index}/{len(targets)}: {name}")
                try:
                    job = self.update_fn(server, warn, target)
                except Exception as exc:  # noqa: BLE001
                    failed.append(name)
                    self._alert("update_finished", "critical",
                                f"Could not start the update on **{self.label(name)}**: {exc}. "
                                f"Stopping here - {len(targets) - index} map(s) were "
                                "left untouched and are still running.")
                    return

                job_id = (job or {}).get("job", {}).get("id")
                if not job_id or not self.job_status_fn:
                    # Nothing to follow, so nothing can be confirmed. Do not
                    # blindly march on to the next map.
                    self._alert("update_finished", "warn",
                                f"Started an update on **{self.label(name)}** but cannot track "
                                "it, so the remaining maps were not touched.")
                    return

                outcome = self._await_job(name, job_id)
                if outcome.get("state") == "done":
                    done.append(name)
                    self.log(f"[updater] {name} updated")
                    continue

                failed.append(name)
                remaining = [s["name"] for s in targets[index:]]
                self._alert(
                    "update_finished", "critical",
                    f":rotating_light: **{self.label(name)}** failed to update: "
                    f"{str(outcome.get('error') or 'no result')[:300]}\n\n"
                    + (f"{self.labels(remaining)} was NOT touched and is still "
                       "running on the old build.\n\n" if remaining else "")
                    + "Note the old build is not joinable once clients update, so "
                      "this still needs fixing - but nothing else was taken down.")
                return

            builds = (self.check().get("builds") or {})
            self.failed_builds.pop(target, None)
            self._alert("update_finished", "info",
                        ":white_check_mark: Updated " + self.labels(done)
                        + " one at a time.\n\nBuilds now: "
                        + ", ".join(f"**{self.label(k)}** `{v}`" for k, v in builds.items()))
        finally:
            if failed and target:
                count, _ = self.failed_builds.get(target, (0, 0.0))
                self.failed_builds[target] = (count + 1, time.time())
            for record in reversed(self.history):
                if record.get("plan") is plan:
                    record["results"] = {"done": done, "failed": failed}
                    break
            self._applying = False

    def _await_job(self, name: str, job_id: str, timeout: int = 5400) -> dict:
        """Block until one agent job finishes. Returns its final state."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop.is_set():
                return {"state": "aborted", "error": "updater stopping"}
            try:
                job = self.job_status_fn(name, job_id) or {}
            except Exception:  # noqa: BLE001
                job = {}
            if job.get("state") in ("done", "failed"):
                return job
            self._stop.wait(15)
        return {"state": "timeout",
                "error": f"no result within {timeout // 60} minutes"}

    def _follow(self, jobs: dict, plan: dict, timeout: int = 5400) -> None:
        """Poll the update jobs and announce how they ended."""
        deadline = time.time() + timeout
        outcomes: dict[str, dict] = {}

        while time.time() < deadline and len(outcomes) < len(jobs):
            for name, job_id in jobs.items():
                if name in outcomes:
                    continue
                try:
                    job = self.job_status_fn(name, job_id) or {}
                except Exception:  # noqa: BLE001
                    continue
                if job.get("state") in ("done", "failed"):
                    outcomes[name] = job
            if len(outcomes) < len(jobs):
                self._stop.wait(15)

        if not outcomes:
            self._alert("update_finished", "warn",
                        "The update jobs did not report a result within "
                        f"{timeout // 60} minutes. Check the panel.")
            return

        failed = [n for n, j in outcomes.items() if j.get("state") != "done"]

        # Record the outcome against the build we were aiming at, so a patch
        # that keeps failing backs off instead of cycling the cluster, and a
        # patch that finally lands clears its own history.
        target = plan.get("target_build") or ""
        if target:
            if failed:
                count, _ = self.failed_builds.get(target, (0, 0.0))
                self.failed_builds[target] = (count + 1, time.time())
                self.log(f"[updater] build {target} has now failed "
                         f"{count + 1} time(s)")
            else:
                self.failed_builds.pop(target, None)

        lines = []
        for name, job in outcomes.items():
            mark = ":white_check_mark:" if job.get("state") == "done" else ":rotating_light:"
            mins = round((job.get("elapsed") or 0) / 60, 1)
            lines.append(f"{mark} **{self.label(name)}** - "
                         f"{job.get('state')} after {mins} min")
            if job.get("error"):
                lines.append(f"> {str(job['error'])[:300]}")

        state = self.check()
        builds = state.get("builds") or {}
        if builds:
            lines.append("")
            lines.append("Builds now: " + ", ".join(f"**{k}** `{v}`"
                                                    for k, v in builds.items()))
        if state.get("build_mismatch"):
            lines.append(":rotating_light: The maps are on DIFFERENT builds - "
                         "transfers will not work until they match.")

        if failed:
            lines.append("")
            lines.append("A failed update leaves that server STOPPED on purpose - "
                         "starting on a half-applied install would be worse. "
                         "A pre-update backup was taken before patching.")

        self._alert("update_finished",
                    "critical" if failed else "info",
                    "\n".join(lines))

    # ------------------------------------------------------------ background

    def _alert(self, kind: str, severity: str, message: str) -> None:
        self.log(f"[updater] {severity}: {message}")
        if self.notify_fn:
            try:
                self.notify_fn(kind, severity, message)
            except Exception:  # noqa: BLE001
                pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            self._stop.wait(120)
            while not self._stop.is_set():
                interval = float(self._cfg.get("poll_seconds", 420))
                try:
                    if self.enabled:
                        state = self.check()
                        if state["build_mismatch"]:
                            self._alert(
                                "build_mismatch", "critical",
                                "The maps are on DIFFERENT builds: "
                                + ", ".join(
                                    f"**{self.label(k)}** `{v}`"
                                    for k, v in (state["builds"] or {}).items())
                                + ". Cross-server transfers will not work "
                                  "until they match.")
                        if state["needs_update"]:
                            if self.auto_apply:
                                self.apply()
                            else:
                                # CRITICAL, not "warn". A server behind Steam's
                                # current build is not merely out of date -- ARK
                                # clients auto-update, so nobody can join it. The
                                # process being alive is cosmetic. Treating this
                                # as informational hid the fact that the cluster
                                # was effectively down for an hour on 2026-09-08.
                                self._alert(
                                    "update_available", "critical",
                                    f"{', '.join(state['needs_update'])} is behind "
                                    "Steam's current build. Updated clients CANNOT "
                                    "JOIN a server on the old build, so this is an "
                                    "outage, not a to-do. Auto-apply is off, so "
                                    "nothing was changed - patch it.")
                except Exception as exc:  # noqa: BLE001 - the poller must not die
                    self.log(f"[updater] poll failed: {exc}")
                # Jitter so both machines never query Steam in lockstep.
                self._stop.wait(interval * (1 + POLL_JITTER * (time.time() % 1)))

        self._thread = threading.Thread(target=loop, daemon=True, name="clusterforge-updater")
        self._thread.start()
        self.log(f"[updater] started - enabled={self.enabled} "
                 f"auto_apply={self.auto_apply} "
                 f"every {self._cfg.get('poll_seconds', 420)}s")

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "enabled": self.enabled,
            "auto_apply": self.auto_apply,
            "poll_seconds": self._cfg.get("poll_seconds", 420),
            "branch": self._cfg.get("branch", "public"),
            # The two knobs added after the first bad patch. They were working
            # but invisible -- nothing in the panel or its API reported them,
            # so the only way to confirm auto-update was configured safely was
            # to read config.json. A safety setting you cannot see is one you
            # cannot trust.
            "settle_minutes": float(self._cfg.get("settle_minutes", 15) or 0),
            "stagger": bool(self._cfg.get("stagger", True)),
            "last_check": self.last_check,
            "last_result": self.last_result,
            "history": self.history[-10:],
        }
