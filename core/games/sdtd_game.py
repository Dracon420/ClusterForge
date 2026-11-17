"""7 Days to Die, behind the Game interface.

The point of this adapter is what it does NOT need. It speaks telnet rather
than Source RCON, stores config as XML rather than ini, and keeps its saves
outside the install directory - three ways of being unlike the first two
games - and it still needed no change to panel.py. That is the adapter layer
earning its keep.
"""

from __future__ import annotations

from .. import sdtd, sdtd_schema
from .base import Capabilities, Game, ServerStatus


class SevenDaysGame(Game):
    id = "sdtd"
    name = "7 Days to Die"
    short = "7 Days"
    steam_app_id = sdtd.STEAM_APP_ID
    capabilities = Capabilities(
        rcon=True,            # telnet console rather than Source RCON, but the
                              # panel only cares that commands can be sent
        settings=True,
        mods=False,           # mods exist, but they are manual file drops with
                              # no server-side distribution to manage
        cluster=False,
        player_list=True,
        backups=True,
        # 7DtD writes serverconfig.xml only when told to, not on shutdown, so
        # settings can be saved while it runs. It still needs a restart to
        # take effect - that is a different thing and the UI says so.
        settings_need_stop=False,
    )

    def _cfg(self, server: dict) -> dict:
        cfg = dict(self.config.get("sdtd") or {})
        cfg.update(server or {})
        cfg.setdefault("telnet_password",
                       (self.secrets.get("sdtd_telnet_password")
                        or self.secrets.get("rcon_password") or "").strip())
        return cfg

    # ------------------------------------------------------------ lifecycle
    def status(self, server: dict) -> ServerStatus:
        cfg = self._cfg(server)
        raw = sdtd.status(cfg)
        return ServerStatus(
            running=bool(raw.get("running")),
            pid=raw.get("pid"),
            uptime_seconds=raw.get("uptime_seconds") or 0,
            memory_gb=raw.get("private_gb") or 0.0,
            players=raw.get("players"),
            port=raw.get("port"),
            installed=bool(raw.get("installed")),
            extra={"server_name": raw.get("server_name", ""),
                   "password": raw.get("password", "")},
        )

    def start(self, server: dict, **kwargs) -> dict:
        return sdtd.start(self._cfg(server), log=self.log)

    def stop(self, server: dict, warn_seconds: int = 30, **kwargs) -> dict:
        return sdtd.stop(self._cfg(server), warn_seconds=warn_seconds,
                         log=self.log)

    # ------------------------------------------------------------- settings
    def read_settings(self, server: dict) -> dict:
        return sdtd.read_settings(self._cfg(server).get("install_dir", ""))

    def write_settings(self, server: dict, changes: dict) -> dict:
        return sdtd.write_settings(
            self._cfg(server).get("install_dir", ""), changes)

    def describe_settings(self, server: dict) -> list[dict]:
        return sdtd_schema.describe(self.read_settings(server))

    def presets(self) -> dict:
        return sdtd_schema.PRESETS

    def locked_settings(self) -> dict:
        return sdtd_schema.LOCKED

    # -------------------------------------------------------------- control
    def players(self, server: dict):
        return sdtd.players(self._cfg(server))

    def rcon(self, server: dict, command: str) -> str:
        cfg = self._cfg(server)
        return sdtd.telnet("127.0.0.1", int(cfg.get("telnet_port", 8081)),
                           cfg.get("telnet_password") or "", command)

    # -------------------------------------------------------------- backups
    def save_paths(self, server: dict) -> list[str]:
        cfg = self._cfg(server)
        return [sdtd.save_root(cfg.get("install_dir", ""), cfg)]
