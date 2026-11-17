"""Palworld, behind the Game interface.

Deliberately a thin wrapper over core/palworld.py rather than a rewrite. That
module holds a lot of hard-won behaviour - the shipping binary rather than the
wrapper stub, graceful RCON shutdown, snapshots before any forced kill, the
scheduled-task launch that stops the process dying with whoever started it -
and none of that should be re-derived for the sake of a tidier shape.

What this adds is the part the interface cares about: Palworld REWRITES its
settings file on shutdown, so writing settings while it runs silently undoes
them. That is declared as a capability (settings_need_stop) instead of being
a comment somewhere in panel.py that the next game's author will not read.
"""

from __future__ import annotations

from .. import palworld, palworld_schema
from .base import Capabilities, Game, ServerStatus


class PalworldGame(Game):
    id = "palworld"
    name = "Palworld"
    short = "Palworld"
    steam_app_id = "2394010"
    capabilities = Capabilities(
        rcon=True,
        settings=True,
        mods=False,          # no server-side mod support exists; see below
        cluster=False,       # one world, no transfers
        player_list=True,
        backups=True,
        # The one that matters. Measured 2026-10-12: settings written while
        # running survived 0 of 18 restarts; written while stopped, 18 of 18.
        settings_need_stop=True,
    )

    # Palworld has no server mod system at all. `bAllowClientMod` only lets
    # CLIENTS run their own local mods - nothing is distributed by the server
    # - so a mod UI here would be a button that cannot work.
    mods_unavailable_because = (
        "Palworld has no server-side mod support. There is no mod loader and "
        "no server mod distribution; bAllowClientMod only permits players to "
        "run mods locally."
    )

    def _cfg(self, server: dict) -> dict:
        """Merge the panel's palworld config with the RCON password."""
        cfg = dict(self.config.get("palworld") or {})
        cfg.update(server or {})
        cfg["rcon_password"] = (self.secrets.get("rcon_password") or "").strip()
        return cfg

    # ------------------------------------------------------------ lifecycle
    def status(self, server: dict) -> ServerStatus:
        cfg = self._cfg(server)
        raw = palworld.status(cfg)
        return ServerStatus(
            running=bool(raw.get("running")),
            pid=raw.get("pid"),
            uptime_seconds=raw.get("uptime_seconds") or 0,
            memory_gb=raw.get("private_gb") or 0.0,
            players=raw.get("players"),
            port=raw.get("port"),
            installed=bool(raw.get("installed")),
            extra={"server_name": cfg.get("server_name", ""),
                   "search_name": palworld.search_name(cfg.get("server_name", "")),
                   "password": cfg.get("password", "")},
        )

    def start(self, server: dict, **kwargs) -> dict:
        return palworld.start(self._cfg(server), log=self.log)

    def stop(self, server: dict, warn_seconds: int = 15, **kwargs) -> dict:
        return palworld.stop(self._cfg(server), warn_seconds=warn_seconds,
                             log=self.log)

    # ------------------------------------------------------------- settings
    def read_settings(self, server: dict) -> dict:
        return palworld.read_settings(self._cfg(server).get("install_dir", ""))

    def write_settings(self, server: dict, changes: dict) -> dict:
        return palworld.write_settings(
            self._cfg(server).get("install_dir", ""), changes)

    def describe_settings(self, server: dict) -> list[dict]:
        return palworld_schema.describe(self.read_settings(server))

    def presets(self) -> dict:
        return palworld_schema.PRESETS

    def locked_settings(self) -> dict:
        return palworld_schema.LOCKED

    # -------------------------------------------------------------- control
    def players(self, server: dict):
        try:
            return palworld.players(self._cfg(server))
        except Exception:  # noqa: BLE001 - unreachable is not empty
            return None

    def rcon(self, server: dict, command: str) -> str:
        from .. import rcon as rcon_mod
        cfg = self._cfg(server)
        return rcon_mod.execute("127.0.0.1", int(cfg.get("rcon_port", 25575)),
                                cfg.get("rcon_password") or "", command)

    # -------------------------------------------------------------- backups
    def save_paths(self, server: dict) -> list[str]:
        return [palworld.save_root(self._cfg(server).get("install_dir", ""))]

    def snapshot(self, server: dict, reason: str = "manual"):
        return palworld.snapshot(self._cfg(server), reason=reason, log=self.log)
