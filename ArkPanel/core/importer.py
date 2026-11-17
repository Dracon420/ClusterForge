"""
One-time ingest of the existing DSM setup.

Read-only with respect to DSM: this reads its files and never writes to them.

Source ranking, established during planning:

  1. DSM's management log  -- the exact command DSM executed, timestamped. This
     is what is actually running, not what a file claims. Ground truth.
  2. StartServer.bat       -- a manual-start fallback nothing depended on, which
     is precisely why it drifted out of date unnoticed.
  3. serverProfil.json     -- DSM's stored settings, useful for the operational
     knobs (schedule, backup, timeouts) that never appear on a command line.

Where they disagree, the reconciliation report shows all three side by side
rather than silently picking one.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from .launch import LaunchConfig, extract_from_bat
from .mods import build_mod_table

LAUNCH_MARKER = "Launching server with arguments:"


def dsm_appdata() -> str:
    return os.path.join(os.environ.get("APPDATA", ""), "ASADedicatedManager")


def dsm_profiles() -> list[dict]:
    """Server profiles DSM knows about: [{Name, ServerPath, IsSelected}, ...]."""
    path = os.path.join(dsm_appdata(), "profils.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh) or []
    except (json.JSONDecodeError, OSError):
        return []


def _unescape_log_value(text: str) -> str:
    r"""DSM writes the command line into its log with escaped quotes (\"), so
    the raw line is not directly parseable. Undo that."""
    return text.replace('\\"', '"')


def latest_launch_line(profile_name: str) -> tuple[str, str] | None:
    """Most recent launch command DSM executed for this profile.

    Returns (timestamp, cmdline) or None. Reads the tail of the file rather
    than the whole thing -- these logs reach hundreds of KB.
    """
    path = os.path.join(dsm_appdata(), "logs", "servers", f"{profile_name}_management.log")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None

    for line in reversed(lines):
        if LAUNCH_MARKER not in line:
            continue
        _, _, tail = line.partition(LAUNCH_MARKER)
        stamp = ""
        match = re.match(r"\s*\[([^\]]+)\]", line)
        if match:
            stamp = match.group(1)
        return stamp, _unescape_log_value(tail.strip())
    return None


def read_server_profile(install_dir: str) -> dict:
    """DSM's serverProfil.json -- the operational settings behind its UI."""
    path = os.path.join(install_dir, "Profile", "serverProfil.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def read_start_bat(install_dir: str, bat_path: str | None = None) -> tuple[str, str] | None:
    """(exe_path, cmdline) from StartServer.bat, if one exists."""
    path = bat_path or os.path.join(install_dir, "StartServer.bat")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return extract_from_bat(fh.read())
    except OSError:
        return None


@dataclass
class SourceView:
    """One view of a server's launch args, from one source."""

    source: str
    available: bool = False
    detail: str = ""
    cmdline: str = ""
    summary: dict = field(default_factory=dict)

    @property
    def mods(self) -> list[int]:
        return self.summary.get("mods", [])


@dataclass
class ImportResult:
    name: str
    install_dir: str
    profile_name: str
    views: dict[str, SourceView] = field(default_factory=dict)
    profile: dict = field(default_factory=dict)
    mods: list = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)

    @property
    def authoritative(self) -> SourceView | None:
        for key in ("dsm_log", "bat"):
            view = self.views.get(key)
            if view and view.available:
                return view
        return None


# Launch settings compared across sources. Mods are handled separately because
# they need order-aware diffing.
_COMPARED = [
    ("map_name", "Map"),
    ("port", "Game port"),
    ("rcon_port", "RCON port"),
    ("max_players", "Max players"),
    ("platform", "Server platform"),
    ("cluster_id", "Cluster ID"),
    ("cluster_dir", "Cluster dir"),
]


def _compare(views: dict[str, SourceView]) -> list[dict]:
    """Every disagreement between available sources, field by field."""
    live = [(k, v) for k, v in views.items() if v.available and v.summary]
    if len(live) < 2:
        return []

    conflicts: list[dict] = []
    for field_key, label in _COMPARED:
        values = {k: (v.summary.get(field_key) or "") for k, v in live}
        if len(set(values.values())) > 1:
            conflicts.append({"field": label, "values": values})

    mod_sets = {k: v.mods for k, v in live}
    keys = list(mod_sets)
    baseline = mod_sets[keys[0]]
    if any(mod_sets[k] != baseline for k in keys[1:]):
        union: list[int] = []
        for mods in mod_sets.values():
            for mod_id in mods:
                if mod_id not in union:
                    union.append(mod_id)
        conflicts.append({
            "field": "Mods",
            "values": {k: f"{len(v)} mods" for k, v in mod_sets.items()},
            "detail": {
                k: sorted(set(union) - set(v)) for k, v in mod_sets.items()
            },
        })

    # The same setting can be written three ways, and they mean the same thing:
    #   bare switch   -RCONEnabled
    #   value flag    -RCONPort=27020
    #   query param   ?RCONPort=27020   (inside the quoted map segment)
    # Island uses flags; Svartalfheim's bat packs several into its map segment.
    # So compare a normalised name -> value map rather than a list of switches,
    # otherwise identical configurations are reported as differing.
    maps = {k: _settings_map(v.summary) for k, v in live}
    already = {"port", "rconport", "winlivemaxplayers", "serverplatform",
               "clusterid", "clusterdiroverride", "mods", "gamemodids", "sessionname"}

    every_key: set[str] = set()
    for m in maps.values():
        every_key |= set(m)

    for key in sorted(every_key - already):
        values = {k: m.get(key, None) for k, m in maps.items()}
        if len({_render_setting(v) for v in values.values()}) > 1:
            conflicts.append({
                "field": f"-{key}",
                "values": {k: _render_setting(v) for k, v in values.items()},
            })
    return conflicts


def _norm_setting(value) -> object:
    """Normalise a launch setting so equivalent spellings compare equal."""
    if value is True:
        return True
    text = str(value).strip().strip('"')
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return text


def _settings_map(summary: dict) -> dict:
    """Every launch setting from one source, as name -> normalised value.

    A bare switch becomes True, so `-RCONEnabled` and `?RCONEnabled=True`
    resolve to the same thing.
    """
    out: dict[str, object] = {}
    for qk, qv in (summary.get("query") or []):
        out[qk.lower()] = _norm_setting(qv if qv != "" else True)
    for flag in (summary.get("all_flags") or []):
        raw = flag.get("value")
        out[flag["name"].lower()] = _norm_setting(True if raw is None else raw)
    return out


def _render_setting(value) -> str:
    if value is None:
        return "absent"
    if value is True:
        return "on"
    if value is False:
        return "off"
    return str(value)


def import_server(
    name: str,
    install_dir: str,
    profile_name: str,
    bat_path: str | None = None,
) -> ImportResult:
    """Gather every view of one server and diff them."""
    result = ImportResult(name=name, install_dir=install_dir, profile_name=profile_name)

    log_entry = latest_launch_line(profile_name)
    view = SourceView(source="DSM management log")
    if log_entry:
        stamp, cmdline = log_entry
        cfg = LaunchConfig.parse(cmdline)
        view.available = True
        view.detail = f"launched {stamp}"
        view.cmdline = cmdline
        view.summary = cfg.summary()
    else:
        view.detail = "no launch line found"
    result.views["dsm_log"] = view

    bat = read_start_bat(install_dir, bat_path)
    view = SourceView(source="StartServer.bat")
    if bat:
        exe, cmdline = bat
        cfg = LaunchConfig.parse(cmdline)
        view.available = True
        view.detail = exe
        view.cmdline = cmdline
        view.summary = cfg.summary()
    else:
        view.detail = "not found"
    result.views["bat"] = view

    result.profile = read_server_profile(install_dir)

    auth = result.authoritative
    load_order = auth.mods if auth else []
    result.mods = build_mod_table(install_dir, load_order)
    result.conflicts = _compare(result.views)
    return result


def to_record(result: ImportResult) -> dict:
    """Serialisable snapshot written to data/servers/<name>/import.json."""
    auth = result.authoritative
    profile = result.profile
    return {
        "name": result.name,
        "install_dir": result.install_dir,
        "dsm_profile": result.profile_name,
        "authoritative_source": auth.source if auth else None,
        "cmdline": auth.cmdline if auth else "",
        "launch": auth.summary if auth else {},
        "sources": {
            key: {
                "source": v.source,
                "available": v.available,
                "detail": v.detail,
                "mod_count": len(v.mods),
                "port": v.summary.get("port"),
                "rcon_port": v.summary.get("rcon_port"),
            }
            for key, v in result.views.items()
        },
        "conflicts": result.conflicts,
        "mods": [m.to_dict() for m in result.mods],
        "operational": {
            "auto_start": profile.get("AutoStartServer"),
            "auto_update": profile.get("AutoUpdate"),
            "max_players": profile.get("MaxPlayers"),
            "server_port": profile.get("ServerPort"),
            "rcon_port": profile.get("RCONPort"),
            "cluster_id": profile.get("ClutserId"),
            "cluster_path": profile.get("ClutserPath"),
            "cpu_priority": profile.get("SelectedCpuPriority"),
            "warn_before_stop": profile.get("StopWarnPlayer"),
            "warn_message": profile.get("Personalwarnmessage"),
            "warn_times": profile.get("AdvancedRestartWarnTimes"),
            "shutdown_time": profile.get("AdvancedShutdownTime"),
            "restart_after_shutdown": profile.get("RestartAfterShutdown"),
            "schedule_days": [
                day for day in
                ("Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday")
                if profile.get(day)
            ],
            "backup_path": profile.get("BackupPath"),
            "backup_interval": profile.get("BackupInterval"),
            "backup_keep": profile.get("BackupFilesToKeep"),
            "startup_timeout_min": profile.get("ServerStartupTimeoutMinutes"),
            "zombie_detection": profile.get("ZombieDetectionEnabled"),
            "map_folder": profile.get("MapFolder"),
            "map_file": profile.get("MapFile"),
        },
    }
