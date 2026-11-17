"""ARK: Survival Ascended, behind the Game interface.

The lifecycle here goes through the AGENT, not through local functions: ARK
servers run on three different PCs and the panel reaches them over HTTP. So
this adapter is constructed with a caller that knows how to talk to agents,
and the game-specific part is only what to ask for.

That asymmetry with Palworld (which runs on the panel's own machine) is
exactly why the interface takes a `server` dict rather than assuming where
anything lives.
"""

from __future__ import annotations

from .base import Capabilities, Game, ServerStatus


class ArkGame(Game):
    id = "ark"
    name = "ARK: Survival Ascended"
    short = "ARK"
    steam_app_id = "2430930"
    capabilities = Capabilities(
        rcon=True,
        settings=True,
        mods=True,           # CurseForge, downloaded by the game itself
        cluster=True,        # transfers between maps, shared mod sets
        player_list=True,
        backups=True,
        settings_need_stop=False,   # ARK reads its ini at start, and does not
                                    # rewrite it on shutdown the way Palworld does
    )

    def __init__(self, config, secrets, log=print, agent_get=None, agent_post=None):
        super().__init__(config, secrets, log)
        # Supplied by the panel. Keeping these injected rather than imported
        # is what stops this module needing to know about panel.py.
        self._get = agent_get
        self._post = agent_post

    # ------------------------------------------------------------ lifecycle
    def status(self, server: dict) -> ServerStatus:
        name = server["name"]
        raw, error = self._get(server, f"/api/server/{name}/status")
        if error or not raw:
            return ServerStatus(running=False, detail=error or "no answer",
                                extra={"agent_error": error})
        return ServerStatus(
            running=bool(raw.get("running")),
            pid=raw.get("pid"),
            uptime_seconds=raw.get("uptime_seconds") or 0,
            memory_gb=raw.get("private_gb") or raw.get("memory_gb") or 0.0,
            port=(raw.get("port_health") or {}).get("bound"),
            installed=True,
            extra={"map_name": raw.get("map_name"),
                   "maintenance": raw.get("maintenance"),
                   "port_health": raw.get("port_health"),
                   "display_name": raw.get("display_name")},
        )

    def start(self, server: dict, **kwargs) -> dict:
        name = server["name"]
        body, error, _status = self._post(server, f"/api/server/{name}/start",
                                          {"wait_ready": kwargs.get("wait_ready", True)})
        return {"ok": not error, "error": error, "job": (body or {}).get("job")}

    def stop(self, server: dict, **kwargs) -> dict:
        name = server["name"]
        body, error, _status = self._post(
            server, f"/api/server/{name}/stop",
            {"warn_minutes": kwargs.get("warn_minutes", []),
             "save": kwargs.get("save", True)})
        return {"ok": not error, "error": error, "job": (body or {}).get("job")}

    def restart(self, server: dict, **kwargs) -> dict:
        name = server["name"]
        body, error, _status = self._post(server, f"/api/server/{name}/restart",
                                          dict(kwargs))
        return {"ok": not error, "error": error, "job": (body or {}).get("job")}

    # ------------------------------------------------------------- settings
    def read_settings(self, server: dict) -> dict:
        name = server["name"]
        raw, error = self._get(server, f"/api/server/{name}/ini")
        return {} if error else (raw or {})

    def write_settings(self, server: dict, changes: dict) -> dict:
        name = server["name"]
        body, error, _status = self._post(server, f"/api/server/{name}/ini",
                                          {"changes": changes})
        if error:
            raise RuntimeError(error)
        return (body or {}).get("applied", {})

    # -------------------------------------------------------------- control
    def players(self, server: dict):
        name = server["name"]
        raw, error = self._get(server, f"/api/server/{name}/players")
        if error or not raw:
            return None
        return raw.get("names") if raw.get("known") else None

    def rcon(self, server: dict, command: str) -> str:
        name = server["name"]
        body, error, _status = self._post(server, f"/api/server/{name}/rcon",
                                          {"command": command})
        if error:
            raise RuntimeError(error)
        return (body or {}).get("reply", "")

    # -------------------------------------------------------------- backups
    def save_paths(self, server: dict) -> list[str]:
        install = server.get("install_dir", "")
        return [install] if install else []
