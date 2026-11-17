"""
Record what a server actually costs while people are playing.

Built to answer one question honestly: can PC2 -- an Intel N100, 4 cores, 16 GB --
take a second map? The live readings could not answer it, for three reasons
that each looked fine while being wrong:

  * Idle numbers lie about RAM. Once a server has sat empty for a while Windows
    trims its working set, and Svartalfheim read 0.64 GB against the ~3.9 GB it
    really needs. So memory here is PRIVATE bytes -- committed memory, which
    paging cannot hide -- with the working set kept alongside for contrast.

  * Whole-machine CPU hides a saturated game thread. An ARK server leans on one
    main thread; on a 4-core chip a thread pinned at 100% shows as only 25% of
    the machine, which reads as "loads of headroom" while the game is actually
    bottlenecked. So the busiest single thread is tracked too, in units of one
    core, and that is the number that matters for rubber-banding.

  * Nothing was stored. CPU was only ever sampled live for the Discord embed,
    so there was no record of a real play session to look back at.

Samples are split into idle and active (anyone online) when summarised, because
an average diluted by twenty idle hours says nothing about an evening of play.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

import psutil

from . import procwatch

SAMPLE_SECONDS = 15
# RCON is a process spawn per query, so players are checked once a minute and
# the last known count is carried onto the samples in between.
PLAYERS_EVERY = 4
KEEP_DAYS = 7


def _gb(n) -> float:
    return round((n or 0) / (1024 ** 3), 2)


class PerfRecorder:
    def __init__(self, base_dir: str, servers_fn, players_fn, logger=print):
        self.base_dir = base_dir
        self.servers_fn = servers_fn        # () -> [server dict] on this machine
        self.players_fn = players_fn        # (server) -> int | None
        self.log = logger
        self._stop = threading.Event()
        self._thread = None
        # Per server: the psutil handle plus last per-thread CPU times, so each
        # sample can diff against the previous one.
        self._state: dict[str, dict] = {}

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        psutil.cpu_percent(interval=None)   # prime: the first call always returns 0
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="clusterforge-perf")
        self._thread.start()
        self.log(f"[perf] recording every {SAMPLE_SECONDS}s, keeping {KEEP_DAYS} days")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        tick = 0
        last_prune = 0.0
        while not self._stop.is_set():
            for server in self.servers_fn() or []:
                try:
                    sample = self._sample(server, check_players=(tick % PLAYERS_EVERY == 0))
                    if sample:
                        self._append(server["name"], sample)
                except Exception as exc:  # noqa: BLE001 - a recorder must never die
                    self.log(f"[perf] {server.get('name')}: sample failed: {exc}")
            if time.time() - last_prune > 3600:
                self._prune()
                last_prune = time.time()
            tick += 1
            self._stop.wait(SAMPLE_SECONDS)

    # ---------- sampling ----------

    def _sample(self, server: dict, check_players: bool) -> dict | None:
        name = server["name"]
        state = self._state.setdefault(name, {"proc": None, "pid": None,
                                              "threads": {}, "at": None,
                                              "players": None})
        found = procwatch.find_server_process(server)
        if found is None:
            state.update(proc=None, pid=None, threads={}, at=None)
            return None

        now = time.time()
        if state["pid"] != found.pid:
            # New process (a restart): start clean, or the first diff would span
            # two different processes and report nonsense.
            state.update(proc=psutil.Process(found.pid), pid=found.pid,
                         threads={}, at=None)
            state["proc"].cpu_percent(interval=None)

        proc = state["proc"]
        cores = psutil.cpu_count() or 1

        with proc.oneshot():
            mem = proc.memory_info()
            # Average since the previous call, so a 15s sample reflects the whole
            # 15s rather than one instant.
            proc_cores = proc.cpu_percent(interval=None) / 100.0
            threads = {t.id: t.user_time + t.system_time for t in proc.threads()}

        busiest = None
        if state["at"] is not None and state["threads"]:
            elapsed = now - state["at"]
            if elapsed > 0:
                deltas = [threads[t] - state["threads"][t]
                          for t in threads if t in state["threads"]]
                if deltas:
                    busiest = round(max(deltas) / elapsed * 100.0, 1)
        state["threads"], state["at"] = threads, now

        if check_players:
            try:
                state["players"] = self.players_fn(server)
            except Exception:  # noqa: BLE001
                state["players"] = None

        vm = psutil.virtual_memory()
        return {
            "t": round(now, 1),
            "players": state["players"],
            # Whole machine, 0-100.
            "cpu_machine": round(proc_cores / cores * 100.0, 1),
            # This process across all threads, in cores (1.0 = one full core).
            "cpu_cores": round(proc_cores, 2),
            # The single busiest thread, % of ONE core. ~100 means the game
            # thread is pinned, however low cpu_machine looks.
            "busiest_thread": busiest,
            "private_gb": _gb(getattr(mem, "private", 0)),
            "working_gb": _gb(mem.rss),
            "system_cpu": round(psutil.cpu_percent(interval=None), 1),
            "system_ram_pct": round(vm.percent, 1),
            "system_avail_gb": _gb(vm.available),
        }

    # ---------- storage ----------

    def _dir(self, name: str) -> str:
        return os.path.join(self.base_dir, "data", "perf", name)

    def _append(self, name: str, sample: dict) -> None:
        folder = self._dir(name)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, datetime.now().strftime("%Y-%m-%d") + ".jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(sample) + "\n")

    def _prune(self) -> None:
        """Drop day files older than KEEP_DAYS. Failures are LOGGED -- a silent
        prune is exactly how the backups filled D: to 97%."""
        cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        root = os.path.join(self.base_dir, "data", "perf")
        if not os.path.isdir(root):
            return
        for name in os.listdir(root):
            folder = os.path.join(root, name)
            if not os.path.isdir(folder):
                continue
            for fname in os.listdir(folder):
                if fname.endswith(".jsonl") and fname[:10] < cutoff:
                    try:
                        os.remove(os.path.join(folder, fname))
                    except OSError as exc:
                        self.log(f"[perf] could not prune {name}/{fname}: {exc}")


def load(base_dir: str, name: str, hours: float = 24) -> list[dict]:
    folder = os.path.join(base_dir, "data", "perf", name)
    if not os.path.isdir(folder):
        return []
    since = time.time() - hours * 3600
    out = []
    for fname in sorted(os.listdir(folder)):
        if not fname.endswith(".jsonl"):
            continue
        with open(os.path.join(folder, fname), "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                if s.get("t", 0) >= since:
                    out.append(s)
    return out


def _stats(values: list) -> dict | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    p95 = vals[min(len(vals) - 1, int(round(0.95 * (len(vals) - 1))))]
    return {"avg": round(sum(vals) / len(vals), 2), "p95": p95, "peak": vals[-1]}


def summarise(samples: list[dict]) -> dict:
    """Idle and active reported separately -- never blended."""
    fields = ("cpu_machine", "cpu_cores", "busiest_thread", "private_gb",
              "working_gb", "system_ram_pct", "system_avail_gb")

    def block(rows):
        if not rows:
            return {"samples": 0}
        span = rows[-1]["t"] - rows[0]["t"] if len(rows) > 1 else 0
        out = {"samples": len(rows), "minutes": round(len(rows) * SAMPLE_SECONDS / 60, 1),
               "span_minutes": round(span / 60, 1)}
        for f in fields:
            out[f] = _stats([r.get(f) for r in rows])
        counts = [r.get("players") for r in rows if r.get("players") is not None]
        out["max_players"] = max(counts) if counts else None
        return out

    active = [s for s in samples if (s.get("players") or 0) >= 1]
    idle = [s for s in samples if s.get("players") == 0]
    return {"active": block(active), "idle": block(idle),
            "unknown_players": sum(1 for s in samples if s.get("players") is None)}
