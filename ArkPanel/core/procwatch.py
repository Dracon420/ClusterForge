"""
Process supervision for ARK dedicated servers.

Every rule here exists because of an incident recorded in the cluster briefing.
None of it is defensive theatre:

1. Never trust the configured game port. On a bind conflict ArkAscendedServer
   silently increments (7777 -> 7778 was observed) instead of failing, leaving
   the server unreachable with no error. Read the actually-bound port.

2. Always exclude UDP 27015. ArkAscendedServer.exe opens it as a Steam legacy
   query socket on every launch regardless of configuration. Logic that picks
   "the lowest UDP port" therefore reports 27015 instead of the real game port.
   On The Island this bug is masked purely by coincidence -- its RCON port is
   also 27015, so discarding the RCON port happens to remove the Steam socket
   too. Svartalfheim (RCON 27020) is where it actually fired. Excluding 27015
   explicitly makes both correct rather than one of them lucky.

3. Re-verify the real PID after every start. DSM's crash detection compared
   against an uninitialised -1 and restarted a healthy server.

4. Confirm the old process has genuinely exited before starting a new one.
   Interrupting DSM mid-shutdown once left two ARK processes writing to the
   same save files simultaneously.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import psutil

# ArkAscendedServer.exe always binds this UDP port as a Steam legacy query
# socket, whatever the configured game port is. It is never the game port.
STEAM_LEGACY_UDP_PORT = 27015

PROCESS_NAMES = ("arkascendedserver.exe", "shootergameserver.exe")


@dataclass
class ServerProcess:
    pid: int
    started_at: float
    cmdline: str
    memory_bytes: int = 0
    private_bytes: int = 0
    cpu_percent: float = 0.0
    bound_game_port: int | None = None
    all_udp_ports: list[int] = field(default_factory=list)

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)

    @property
    def uptime_human(self) -> str:
        total = int(self.uptime_seconds)
        d, rem = divmod(total, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        return f"{d}d {h}h {m}m {s}s"

    @property
    def memory_gb(self) -> float:
        return round(self.memory_bytes / (1024 ** 3), 2)

    @property
    def private_gb(self) -> float:
        return round(self.private_bytes / (1024 ** 3), 2)


def _iter_ark_processes():
    for proc in psutil.process_iter(attrs=["pid", "name", "create_time"]):
        try:
            if (proc.info["name"] or "").lower() in PROCESS_NAMES:
                yield proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def _safe_cmdline(proc: psutil.Process) -> str:
    """Command line, or '' when the process is elevated beyond our reach.

    An unelevated reader gets an empty string for a process started elevated --
    which is exactly what happens with DSM-launched servers, and the reason the
    agent must run with highest privileges.
    """
    try:
        return " ".join(proc.cmdline())
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return ""


def bound_udp_ports(proc: psutil.Process) -> list[int]:
    try:
        conns = proc.net_connections(kind="udp")
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return []
    return sorted({c.laddr.port for c in conns if c.laddr})


def resolve_game_port(
    ports: list[int],
    rcon_port: int | None = None,
    expected: int | None = None,
    port_min: int | None = None,
    port_max: int | None = None,
) -> int | None:
    """Pick the real game port from a process's bound UDP ports.

    Order of preference: the expected port if actually bound, then anything
    inside the forwarded range, then the lowest remaining candidate. The Steam
    legacy socket and the RCON port are removed first, in that order of
    importance -- 27015 unconditionally, because relying on it coinciding with
    the RCON port is what made this bug invisible on one server and not the
    other.
    """
    candidates = set(ports)
    candidates.discard(STEAM_LEGACY_UDP_PORT)
    if rcon_port:
        candidates.discard(rcon_port)
    if not candidates:
        return None

    if expected and expected in candidates:
        return expected
    if port_min is not None and port_max is not None:
        in_range = [p for p in sorted(candidates) if port_min <= p <= port_max]
        if in_range:
            return in_range[0]
    return sorted(candidates)[0]


def find_server_process(server: dict,
                        cpu_interval: float | None = None) -> ServerProcess | None:
    """Locate the ARK process for one configured server.

    Matches on map name, install directory or configured ports appearing in the
    command line. Falls back to port matching when the command line is
    unreadable (elevated process, unelevated reader), so status still works
    before the agent is installed with privileges.
    """
    map_name = (server.get("map_name") or "").lower()
    install_dir = (server.get("install_dir") or "").lower()
    rcon_port = server.get("rcon_port")
    expected = server.get("expected_game_port")

    fallback: psutil.Process | None = None

    for proc in _iter_ark_processes():
        try:
            cmdline = _safe_cmdline(proc).lower()
            matched = False
            if cmdline:
                if map_name and map_name in cmdline:
                    matched = True
                elif install_dir and install_dir in cmdline:
                    matched = True
                elif rcon_port and str(rcon_port) in cmdline:
                    matched = True
            if not matched:
                ports = bound_udp_ports(proc)
                if expected and expected in ports:
                    matched = True
                elif rcon_port and rcon_port in ports and not cmdline:
                    fallback = fallback or proc
            if matched:
                return _describe(proc, server, cpu_interval)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if fallback is not None:
        try:
            return _describe(fallback, server, cpu_interval)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
    return None


def _describe(proc: psutil.Process, server: dict,
              cpu_interval: float | None = None) -> ServerProcess:
    ports = bound_udp_ports(proc)
    try:
        info = proc.memory_info()
        mem = info.rss
        # Private bytes: committed memory, which Windows cannot page out of the
        # number. rss is the working set, and an idle server gets trimmed -- Nyrandil
        # read 5.3 GB working against 9.2 GB committed, and Svartalfheim 0.6 against
        # 4.1. Capacity decisions made on rss are made on a flattering fiction.
        private = getattr(info, "private", 0)
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        mem = private = 0

    # Normalised across cores, so 100% means "all cores busy" rather than
    # "one core busy" -- the same convention PatchRaptor's status embed used.
    # Needs an interval: a fresh Process object has no previous sample, so
    # cpu_percent() without one always returns 0.0.
    cpu = 0.0
    if cpu_interval:
        try:
            cores = psutil.cpu_count() or 1
            cpu = round(proc.cpu_percent(interval=cpu_interval) / cores, 1)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            cpu = 0.0
    return ServerProcess(
        pid=proc.pid,
        started_at=proc.create_time(),
        cmdline=_safe_cmdline(proc),
        memory_bytes=mem,
        private_bytes=private,
        cpu_percent=cpu,
        bound_game_port=resolve_game_port(
            ports,
            rcon_port=server.get("rcon_port"),
            expected=server.get("expected_game_port"),
            port_min=server.get("port_range_min"),
            port_max=server.get("port_range_max"),
        ),
        all_udp_ports=ports,
    )


def find_duplicates(server: dict) -> list[int]:
    """PIDs of every ARK process that appears to belong to this server.

    More than one means two processes are writing to the same save files --
    the failure that once ran for several minutes unnoticed. Start sequences
    must check this before launching.
    """
    map_name = (server.get("map_name") or "").lower()
    install_dir = (server.get("install_dir") or "").lower()
    expected = server.get("expected_game_port")

    found: list[int] = []
    for proc in _iter_ark_processes():
        try:
            cmdline = _safe_cmdline(proc).lower()
            if cmdline and (
                (map_name and map_name in cmdline)
                or (install_dir and install_dir in cmdline)
            ):
                found.append(proc.pid)
            elif not cmdline and expected and expected in bound_udp_ports(proc):
                found.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def port_health(server: dict, proc: ServerProcess | None) -> dict:
    """Whether the bound port is where it should be.

    Drifting outside the forwarded range means players silently cannot connect,
    so this is what the Discord alert keys off.
    """
    expected = server.get("expected_game_port")
    lo, hi = server.get("port_range_min"), server.get("port_range_max")
    if proc is None or proc.bound_game_port is None:
        return {"state": "unknown", "bound": None, "expected": expected,
                "range": [lo, hi], "message": "server not running"}

    bound = proc.bound_game_port
    if bound == expected:
        state, message = "ok", f"bound to expected port {bound}"
    elif lo is not None and hi is not None and lo <= bound <= hi:
        state = "drifted"
        message = (f"bound to {bound}, not the expected {expected}, but still "
                   f"inside the forwarded range {lo}-{hi}")
    else:
        state = "out_of_range"
        message = (f"bound to {bound}, OUTSIDE the forwarded range {lo}-{hi} - "
                   f"players cannot connect")
    return {"state": state, "bound": bound, "expected": expected,
            "range": [lo, hi], "message": message}


# psutil.cpu_percent(interval=None) reports usage since the PREVIOUS call, so
# the very first call in a process always returns 0.0. Priming it at import
# means the first real reading is meaningful rather than a misleading zero.
psutil.cpu_percent(interval=None)


def system_stats(interval: float | None = None) -> dict:
    """Host CPU, memory and boot time.

    Pass a small interval (0.3-0.5s) when the reading is being shown to a
    person and accuracy matters more than latency -- the delta-based reading
    can otherwise report 0.0% if two callers sample in quick succession.
    """
    mem = psutil.virtual_memory()
    return {
        "cpu_percent": psutil.cpu_percent(interval=interval),
        "memory_total_gb": round(mem.total / (1024 ** 3), 1),
        "memory_used_gb": round(mem.used / (1024 ** 3), 1),
        "memory_percent": mem.percent,
        "boot_time": psutil.boot_time(),
    }


def disk_free(path: str) -> dict | None:
    try:
        usage = psutil.disk_usage(path)
    except (OSError, ValueError):
        return None
    return {
        "total_gb": round(usage.total / (1024 ** 3), 1),
        "free_gb": round(usage.free / (1024 ** 3), 1),
        "percent": usage.percent,
    }
