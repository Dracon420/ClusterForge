"""
Health monitoring and alerting.

Ports the custom port-drift alerting from PatchRaptor's `.live` command, which
is the one piece of the watchers worth keeping, and adds the other failure
modes recorded in the cluster briefing.

Design rules, most of them learned the hard way elsewhere:

  Alert on TRANSITIONS, not on every poll. PatchRaptor tracks a per-server
  state and only speaks when it changes. Anything else becomes noise that
  gets muted, and a muted alert is worse than none.

  Never alert on an uncertain read. A server that is still starting has no
  bound port yet; treating that as "port missing" produces a false alarm every
  restart. Uncertain reads leave the state alone.

  Never claim zero when the answer is unknown. PatchRaptor returns -1 from a
  failed ListPlayers rather than 0 for exactly this reason.

  DETECT, DO NOT ACT. Nothing here restarts a server. DSM's crash detection
  compared against an uninitialised PID and restarted a healthy server; an
  auto-restart driven by a false positive is worse than the fault it thinks it
  is fixing. Alerts go to a human.

  Stay quiet during our own operations. If ArkPanel is deliberately restarting
  a server, "server is down" is not news.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict

from . import logtail, procwatch

# A server whose process is alive but whose log has not been written for this
# long is probably hung -- DSM called this a "zombie server".
ZOMBIE_LOG_SILENCE_SECONDS = 900

# Do not repeat the same alert more often than this, even if it keeps flapping.
REALERT_SECONDS = 1800

MAX_ALERTS = 300

SEVERITY_ORDER = {"info": 0, "warn": 1, "critical": 2}

# How long an informational alert stays "active".
#
# warn and critical describe a CONDITION -- a port has drifted, a server is
# down -- and stay active until the condition clears. info describes an EVENT
# that already finished: "the server was started automatically after a reboot".
# Nothing will ever resolve an event, so without a lifetime these pile up and
# permanently occupy the alert band, which is exactly where a real alert needs
# to stand out. They age out of the band and remain in the history.
INFO_TTL_SECONDS = 3600


@dataclass
class Alert:
    server: str
    kind: str
    severity: str          # info | warn | critical
    message: str
    at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    acknowledged: bool = False

    def is_active(self, now: float | None = None) -> bool:
        if self.resolved_at is not None:
            return False
        if self.severity == "info":
            return ((now or time.time()) - self.at) < INFO_TTL_SECONDS
        return True

    def is_shown(self, now: float | None = None) -> bool:
        """Active AND not acknowledged: what the alert band and counts show.

        Acknowledging used to only tag an alert, so it sat in the band until its
        condition cleared and warn/critical never went away (user, 2026-11-11).
        Now an acknowledged alert is dismissed. The condition is still tracked
        (is_active) and a NEW occurrence raises a fresh, unacknowledged alert."""
        return self.is_active(now) and not self.acknowledged

    def to_dict(self) -> dict:
        d = asdict(self)
        d["condition_active"] = self.is_active()
        d["active"] = self.is_shown()
        # The history needs to tell "aged out" apart from "the condition
        # cleared" -- they look identical once active is False.
        d["expired"] = (self.resolved_at is None and not d["active"])
        return d


class Monitor:
    """Evaluates server health and raises alerts on state changes."""

    def __init__(self, base_dir: str, config: dict, servers_fn,
                 busy_fn=None, notify_fn=None, logger=print, alive_fn=None):
        self.base_dir = base_dir
        self.config = config
        self.servers_fn = servers_fn
        # alive_fn(server) -> True/False/None: does the server answer RCON? A
        # quiet log is weak evidence on its own -- an idle map with no players
        # writes nothing for hours. It only looked like proof while a chatty mod
        # (Cybers Structures, every 5 minutes) was keeping the log warm; removing
        # it turned every idle server into a "may be hung" alert.
        self.alive_fn = alive_fn
        # busy_fn(server_name) -> True while ArkPanel is running a control job,
        # so a deliberate restart does not raise "server is down".
        self.busy_fn = busy_fn or (lambda _name: False)
        self.notify_fn = notify_fn          # (Alert) -> None, e.g. webhook
        self.log = logger

        self.state: dict[str, dict] = {}    # server -> {check: state}
        self.last_alert: dict[str, float] = {}
        self.alerts: list[Alert] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.path = os.path.join(base_dir, "data", "alerts.json")
        self._load()

    # ----------------------------------------------------------- persistence

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        self.state = data.get("state", {})
        # to_dict() adds derived keys ("active", "expired") that are not fields,
        # so filter to the real ones. Passing them through raises TypeError and
        # would silently discard the entire saved history one alert at a time.
        fields = set(Alert.__dataclass_fields__)
        for raw in data.get("alerts", []):
            try:
                self.alerts.append(Alert(**{k: v for k, v in raw.items() if k in fields}))
            except TypeError:
                continue

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"state": self.state,
                           "alerts": [a.to_dict() for a in self.alerts[-MAX_ALERTS:]]},
                          fh, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            self.log(f"[monitor] could not save alerts: {exc}")

    # --------------------------------------------------------------- raising

    def note(self, server: str, kind: str, severity: str, message: str) -> None:
        """Record an alert raised outside the periodic health checks.

        Boot auto-start uses this so an unattended recovery -- and especially a
        FAILED one -- lands in the panel's alert list and Discord like any other
        event. A server that silently failed to come back after a power cut is
        the exact failure this whole feature exists to prevent.
        """
        self._raise(server, kind, severity, message)

    def _raise(self, server: str, kind: str, severity: str, message: str) -> None:
        key = f"{server}:{kind}"
        now = time.time()
        last = self.last_alert.get(key, 0)
        if now - last < REALERT_SECONDS:
            return
        self.last_alert[key] = now

        alert = Alert(server=server, kind=kind, severity=severity, message=message)
        with self._lock:
            self.alerts.append(alert)
            self.alerts = self.alerts[-MAX_ALERTS:]
        self.log(f"[monitor] {severity.upper()} {server}/{kind}: {message}")
        if self.notify_fn:
            try:
                self.notify_fn(alert)
            except Exception as exc:  # noqa: BLE001 - never let a sink break monitoring
                self.log(f"[monitor] notify failed: {exc}")

    def _resolve(self, server: str, kind: str, message: str) -> None:
        now = time.time()
        with self._lock:
            for alert in reversed(self.alerts):
                if alert.server == server and alert.kind == kind and alert.resolved_at is None:
                    alert.resolved_at = now
                    break
        self.last_alert.pop(f"{server}:{kind}", None)
        recovery = Alert(server=server, kind=f"{kind}_recovered",
                         severity="info", message=message, resolved_at=now)
        with self._lock:
            self.alerts.append(recovery)
        self.log(f"[monitor] RECOVERED {server}/{kind}: {message}")
        if self.notify_fn:
            try:
                self.notify_fn(recovery)
            except Exception:  # noqa: BLE001
                pass

    def _transition(self, server: str, check: str, new_state: str | None) -> str | None:
        """Record an observation. Returns the PREVIOUS state only when this is a
        genuine change worth alerting about, otherwise None.

        Three cases return None:
          new_state is None   the reading is uncertain (server still starting,
                              query failed) - never alert on a guess
          no previous state   the first observation just establishes a
                              baseline. Alerting here would announce
                              "recovered" for a server that was healthy all
                              along, every time the agent restarts
          unchanged           nothing happened

        State is persisted, so a baseline survives an agent restart and real
        transitions across one are still caught.
        """
        if new_state is None:
            return None
        server_state = self.state.setdefault(server, {})
        previous = server_state.get(check)
        server_state[check] = new_state
        if previous is None or previous == new_state:
            return None
        return previous

    # ---------------------------------------------------------------- checks

    def check_server(self, server: dict) -> dict:
        name = server["name"]
        proc = procwatch.find_server_process(server)
        busy = self.busy_fn(name)
        result = {"server": name, "busy": busy}

        # --- running / down ---
        running = proc is not None
        result["running"] = running
        if busy:
            # Either a control job is running, or the server is flagged as
            # down on purpose (the Island, while Palworld has PC1's memory).
            # Either way its state is not news.
            #
            # Return here rather than only skipping the up/down test: a server
            # part-way through a shutdown is still a live process, and the
            # checks below then read a teardown as a fault. That is exactly
            # what happened switching to Palworld - the listen socket had
            # already gone, port_health picked up an ephemeral socket instead,
            # and it raised "island is bound to port 61288, OUTSIDE the
            # forwarded range" one minute before the stop completed. Nothing
            # was wrong; the server was doing as it was told.
            result["skipped"] = "deliberate: job in progress or maintenance"
            return result
        else:
            prev = self._transition(name, "running", "up" if running else "down")
            if prev is not None:
                if running:
                    self._resolve(name, "server_down", f"{name} is running again")
                else:
                    self._raise(name, "server_down", "critical",
                                f"{name} is NOT running, and ArkPanel did not stop it.")

        if not running:
            return result

        # --- duplicate processes ---
        dupes = procwatch.find_duplicates(server)
        result["duplicates"] = dupes
        prev = self._transition(name, "duplicates",
                                "many" if len(dupes) > 1 else "one")
        if prev is not None:
            if len(dupes) > 1:
                self._raise(name, "duplicate_process", "critical",
                            f"{name} has {len(dupes)} server processes "
                            f"(PIDs {', '.join(str(p) for p in dupes)}) writing to the "
                            "same save files. Stop the extras immediately.")
            else:
                self._resolve(name, "duplicate_process",
                              f"{name} is back to a single process")

        # --- port drift ---
        health = procwatch.port_health(server, proc)
        result["port"] = health
        # A server that has not bound a port yet is still starting. Reporting
        # that as drift produces a false alarm on every restart, so it is
        # treated as uncertain and left alone.
        port_state = None if health["bound"] is None else health["state"]
        prev = self._transition(name, "port", port_state)
        if prev is not None:
            lo, hi = health["range"]
            if health["state"] == "out_of_range":
                self._raise(name, "port_drift", "critical",
                            f"{name} is bound to port {health['bound']}, OUTSIDE the "
                            f"forwarded range {lo}-{hi}. Players cannot connect.")
            elif health["state"] == "drifted":
                self._raise(name, "port_drift", "warn",
                            f"{name} is bound to port {health['bound']} instead of "
                            f"{health['expected']}, but still inside the forwarded "
                            f"range {lo}-{hi}, so players can still connect.")
            elif prev in ("out_of_range", "drifted"):
                self._resolve(name, "port_drift",
                              f"{name} is back on its expected port {health['bound']}")

        # --- zombie: process alive but the log has gone silent ---
        log_dir = server.get("log_dir", "")
        if log_dir:
            summary = logtail.summarise(log_dir)
            age = summary.get("active_age_seconds")
            result["log_age_seconds"] = age
            zombie = None if age is None else (
                "silent" if age > ZOMBIE_LOG_SILENCE_SECONDS else "writing")
            answering = None
            if zombie == "silent" and self.alive_fn:
                try:
                    answering = self.alive_fn(server)
                except Exception:       # noqa: BLE001 - a failed probe is not proof of death
                    answering = None
                if answering:
                    zombie = "writing"  # quiet but responsive: that is just an idle map
                    result["log_silent_but_answering"] = True
            prev = self._transition(name, "log", zombie)
            if prev is not None:
                if zombie == "silent":
                    detail = (" It is not answering RCON either." if answering is False
                              else " RCON could not be checked.")
                    self._raise(name, "log_silent", "warn",
                                f"{name} is running but has not written to its log for "
                                f"{round(age / 60)} minutes.{detail} It may be hung.")
                else:
                    self._resolve(name, "log_silent", f"{name} is writing logs again")

        # --- disk space ---
        disk = procwatch.disk_free(server.get("install_dir", ""))
        if disk:
            result["disk_free_gb"] = disk["free_gb"]
            low = "low" if disk["free_gb"] < 20 else "ok"
            prev = self._transition(name, "disk", low)
            if prev is not None:
                if low == "low":
                    self._raise(name, "disk_low", "warn",
                                f"Only {disk['free_gb']} GB free on {name}'s drive. "
                                "Backups and updates need room.")
                else:
                    self._resolve(name, "disk_low",
                                  f"Disk space recovered ({disk['free_gb']} GB free)")
        return result

    def tick(self) -> list[dict]:
        results = [self.check_server(s) for s in self.servers_fn()]
        self._save()
        return results

    # ------------------------------------------------------------ lifecycle

    def start(self, interval: int = 60) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.interval = interval

        def loop():
            self._stop.wait(90)  # let a boot-started agent settle
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - monitoring must not die
                    self.log(f"[monitor] tick failed: {exc}")
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, daemon=True, name="arkpanel-monitor")
        self._thread.start()
        self.log(f"[monitor] started, checking every {interval}s")

    def stop(self) -> None:
        self._stop.set()

    # --------------------------------------------------------------- reading

    def recent(self, limit: int = 50, active_only: bool = False) -> list[dict]:
        with self._lock:
            items = [a for a in self.alerts if not active_only or a.is_shown()]
            return [a.to_dict() for a in reversed(items[-limit:])]

    def summary(self) -> dict:
        with self._lock:
            active = [a for a in self.alerts if a.is_shown()]
        worst = max((SEVERITY_ORDER.get(a.severity, 0) for a in active), default=-1)
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "active_alerts": len(active),
            "worst_severity": {0: "info", 1: "warn", 2: "critical"}.get(worst),
            "state": self.state,
            "alerts": [a.to_dict() for a in reversed(active)],
        }

    def acknowledge(self, index_from_end: int | None = None, at: float | None = None,
                    kind: str | None = None, server: str | None = None) -> int:
        """Acknowledge (dismiss) alerts: all of them, or just the one matching
        `at` (+ optional kind/server), which is how a single alert is dismissed."""
        count = 0
        with self._lock:
            for alert in self.alerts:
                if alert.acknowledged or not alert.is_active():
                    continue
                if at is not None and abs(alert.at - float(at)) > 0.001:
                    continue
                if kind and alert.kind != kind:
                    continue
                if server and alert.server != server:
                    continue
                alert.acknowledged = True
                count += 1
        self._save()
        return count
