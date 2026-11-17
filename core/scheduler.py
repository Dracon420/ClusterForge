"""
Background scheduling for the agent: periodic save backups and the
Mon/Wed/Fri restart.

Runs in a daemon thread inside each agent, so each machine schedules its own
servers. Nothing here reaches across the network.

Two deliberate defaults:

  backups  ENABLED by default. Retiring DSM removed a backup layer, so the
           replacement should not need opting into.
  restart  DISABLED by default. An unattended restart is disruptive and should
           be a conscious choice, made after the manual restart path has been
           watched working.

Last-run times are persisted, so restarting an agent does not re-fire a job
that already ran, and a machine that was off does not immediately fire every
missed job at once.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

STATE_FILE = "scheduler_state.json"
TICK_SECONDS = 60

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"]


class Scheduler:
    def __init__(self, base_dir: str, config: dict, servers_fn,
                 backup_fn, restart_fn, last_restart_fn, logger=print,
                 file_backup_fn=None):
        self.base_dir = base_dir
        self.config = config
        self.servers_fn = servers_fn        # () -> list[dict]
        self.backup_fn = backup_fn          # (server) -> job or None
        self.restart_fn = restart_fn        # (server) -> job or None
        self.last_restart_fn = last_restart_fn  # (server) -> epoch or None
        # () -> job or None. Not per server: this one copies a PERSON's files
        # (his profile, the ARK saves) to another machine's drive, weekly.
        self.file_backup_fn = file_backup_fn
        self.log = logger
        self.state_path = os.path.join(base_dir, "data", STATE_FILE)
        self.state = self._load()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- state

    def _load(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8-sig") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {"last_backup": {}, "last_restart": {}}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, indent=2)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            self.log(f"[scheduler] could not save state: {exc}")

    # ----------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="clusterforge-scheduler")
        self._thread.start()
        b = self.config.get("backup", {})
        s = self.config.get("schedule", {})
        self.log(f"[scheduler] started - backups every "
                 f"{b.get('save_interval_hours', 4)}h "
                 f"({'on' if b.get('enabled', True) else 'OFF'}), "
                 f"restart {'on' if s.get('enabled', False) else 'OFF'}")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # Settle before the first tick so a boot-started agent is not doing
        # backups while the machine is still coming up.
        self._stop.wait(120)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - a scheduler must not die
                self.log(f"[scheduler] tick failed: {exc}")
            self._stop.wait(TICK_SECONDS)

    # -------------------------------------------------------------- checks

    def tick(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now()
        fired: list[str] = []
        for server in self.servers_fn():
            if self._backup_due(server, now):
                fired.append(f"backup:{server['name']}")
            if self._restart_due(server, now):
                fired.append(f"restart:{server['name']}")
        if self._file_backup_due():
            fired.append("file-backup")
        if fired:
            self._save()
        return fired

    def _file_backup_due(self) -> bool:
        """The weekly copy of his personal files and the ARK saves to PC1.

        Interval-based rather than day-and-time, because the machine it runs on
        is a home PC that is not always awake at 3am on a Sunday. Asking "has it
        been a week" means a PC that was off on the day still catches up the
        moment it comes back, instead of silently skipping until the next week.
        """
        cfg = self.config.get("file_backup", {})
        if not cfg.get("enabled", False) or self.file_backup_fn is None:
            return False
        interval = float(cfg.get("interval_hours", 168)) * 3600
        last = self.state.get("last_file_backup", 0)
        if time.time() - last < interval:
            return False

        self.log("[scheduler] weekly file backup due")
        job = self.file_backup_fn()
        if job is None:
            return False  # something else is running; try again next tick
        self.state["last_file_backup"] = time.time()
        return True

    def _backup_due(self, server: dict, now: datetime) -> bool:
        cfg = self.config.get("backup", {})
        if not cfg.get("enabled", True):
            return False
        interval = float(cfg.get("save_interval_hours", 4)) * 3600
        last = self.state.get("last_backup", {}).get(server["name"], 0)
        if time.time() - last < interval:
            return False

        self.log(f"[scheduler] backup due for {server['name']}")
        job = self.backup_fn(server)
        if job is None:
            return False  # something already running; try again next tick
        self.state.setdefault("last_backup", {})[server["name"]] = time.time()
        return True

    def _restart_due(self, server: dict, now: datetime) -> bool:
        cfg = self.config.get("schedule", {})
        if not cfg.get("enabled", False):
            return False

        if WEEKDAYS[now.weekday()] not in cfg.get("restart_days", []):
            return False

        target = str(cfg.get("restart_time", "23:00"))
        try:
            hh, mm = (int(x) for x in target.split(":", 1))
        except ValueError:
            return False
        # Fire within the minute the target lands in, not before it.
        if (now.hour, now.minute) != (hh, mm):
            return False

        key = f"{server['name']}:{now:%Y-%m-%d}"
        if self.state.get("last_restart", {}).get(key):
            return False

        # Skip if the cluster already restarted recently -- a patch that landed
        # at 6pm should not cause a second disruption at 11pm.
        skip_hours = float(cfg.get("skip_if_restarted_within_hours", 8))
        started = self.last_restart_fn(server)
        if started and (time.time() - started) < skip_hours * 3600:
            hours = round((time.time() - started) / 3600, 1)
            self.log(f"[scheduler] skipping {server['name']} restart - it already "
                     f"restarted {hours}h ago (threshold {skip_hours}h)")
            self.state.setdefault("last_restart", {})[key] = "skipped"
            return False

        self.log(f"[scheduler] scheduled restart for {server['name']}")
        job = self.restart_fn(server)
        if job is None:
            return False
        self.state.setdefault("last_restart", {})[key] = time.time()
        return True

    # --------------------------------------------------------------- status

    def status(self) -> dict:
        cfg_b = self.config.get("backup", {})
        cfg_s = self.config.get("schedule", {})
        interval = float(cfg_b.get("save_interval_hours", 4)) * 3600
        out = []
        for server in self.servers_fn():
            last = self.state.get("last_backup", {}).get(server["name"], 0)
            out.append({
                "server": server["name"],
                "last_backup": last or None,
                "next_backup": (last + interval) if last else None,
                "last_restart_seen": self.last_restart_fn(server),
            })
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "backups_enabled": cfg_b.get("enabled", True),
            "backup_interval_hours": cfg_b.get("save_interval_hours", 4),
            "restart_enabled": cfg_s.get("enabled", False),
            "restart_days": cfg_s.get("restart_days", []),
            "restart_time": cfg_s.get("restart_time"),
            "skip_if_restarted_within_hours": cfg_s.get("skip_if_restarted_within_hours"),
            "servers": out,
        }
