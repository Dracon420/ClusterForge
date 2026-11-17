"""Minecraft Bedrock, behind the Game interface.

The game that justifies the `rcon` capability flag existing. Bedrock has no
remote console at all, so this declares rcon=False and the UI stops offering
a console it could never reach - rather than showing a button that fails.

It still reports a player COUNT, because a RakNet ping gives one without any
console. Names need a console, so `players` stays None: the panel shows
"3 online" rather than claiming to know who, which is honest and is what the
in-game server browser shows too.
"""

from __future__ import annotations

import time

from .. import backup, minecraft, minecraft_gamerules, minecraft_schema
from .base import Capabilities, Game, ServerStatus


class MinecraftBedrockGame(Game):
    id = "minecraft"
    name = "Minecraft (Bedrock)"
    short = "Minecraft"
    steam_app_id = ""          # free from Mojang, not on Steam
    capabilities = Capabilities(
        rcon=False,            # no remote console exists for Bedrock
        settings=True,
        mods=False,            # add-ons are manual file drops, nothing to manage
        cluster=False,
        player_list=False,     # a count, yes; names, no - see the docstring
        backups=True,
        settings_need_stop=False,
        # Bedrock reads server.properties ONCE, at startup. A setting saved
        # while it runs is written correctly and does nothing, which looks
        # exactly like a bug: the panel said survival, the file said
        # survival, and the child still spawned in creative. Gamerules are
        # worse - they can only be set through the server's stdin, which a
        # detached server does not have. Both are handled by cycling.
        settings_need_restart=True,
    )

    def _cfg(self, server: dict) -> dict:
        cfg = dict(self.config.get("minecraft") or {})
        cfg.update(server or {})
        return cfg

    # ------------------------------------------------------------ lifecycle
    def status(self, server: dict) -> ServerStatus:
        cfg = self._cfg(server)
        raw = minecraft.status(cfg)
        return ServerStatus(
            running=bool(raw.get("running")),
            pid=raw.get("pid"),
            uptime_seconds=raw.get("uptime_seconds") or 0,
            memory_gb=raw.get("private_gb") or 0.0,
            players=raw.get("players"),          # None: names need a console
            port=raw.get("port"),
            installed=bool(raw.get("installed")),
            detail=raw.get("detail", ""),
            extra={"server_name": raw.get("server_name", ""),
                   "transport": raw.get("transport", ""),
                   "level_name": raw.get("level_name", ""),
                   "player_count": raw.get("player_count"),
                   "max_players": raw.get("max_players"),
                   "version": raw.get("version", ""),
                   "motd": raw.get("motd", "")},
        )

    def discord_info(self, server: dict) -> str:
        """The #minecraft how-to-join post.

        No live numbers here (the embed below carries them), and no internal
        details beyond what a player on the home network needs. Port
        forwarding is NOT set up (measured 2026-10-23), so the post says
        plainly that it is home-network only rather than letting people try
        from outside and conclude it is down.
        """
        cfg = self._cfg(server)
        props = minecraft.read_settings(cfg.get("install_dir", ""))
        name = props.get("server-name") or "Minecraft"
        mode = (props.get("gamemode") or "").title()
        allow = str(props.get("allow-list", "")).lower() == "true"
        redirect = (self.config.get("bedrockconnect") or {}).get("redirect_to", "")
        nethernet = (props.get("transport") or "raknet").strip().lower() == "nethernet"
        parts = [
            "# :pick: Minecraft (Bedrock)",
            "**Server**\n"
            f"- Name: `{name}`\n"
            + (f"- Mode: {mode}\n" if mode else "")
            + "- Edition: Bedrock (Xbox, Switch, phone, Windows)",
        ]
        if nethernet:
            # Measured 2026-10-20 (core/minecraft_transport): on NetherNet the
            # world shows under Worlds -> LAN, and joining by address or the
            # BedrockConnect menu does not work - so do not advertise them.
            parts.append(
                "**How to join (Xbox, PC or phone)**\n"
                "1. Be on the home Wi-Fi / network\n"
                "2. Start Minecraft -> **Play** -> **Worlds** tab\n"
                f"3. Scroll to **LAN Games** and pick `{name}`")
        else:
            parts.append(
                "**How to join on Xbox**\n"
                + (f"1. Xbox Settings -> Network -> Advanced -> DNS -> set the "
                   f"**primary** DNS to `{redirect}` (leave the secondary alone)\n"
                   if redirect else "")
                + "2. Start Minecraft -> **Play** -> **Servers** tab\n"
                "3. Open any featured server - you land on our server menu instead\n"
                "4. Pick **Dracon Minecraft**")
            parts.append(
                "**How to join on PC or phone**\n"
                "**Play** -> **Servers** -> **Add Server**, and ask an admin for "
                "the address.")
        if allow:
            parts.append(":lock: **Allowlist is on** - ask an admin to add your "
                         "gamertag before you try to join, or it will refuse you.")
        parts.append(":house: **Home network only.** It is not reachable from "
                     "outside the house, so it not showing up away from home is "
                     "normal, not an outage.")
        return "\n\n".join(parts)

    def start(self, server: dict, **kwargs) -> dict:
        return minecraft.start(self._cfg(server), log=self.log)

    def stop(self, server: dict, **kwargs) -> dict:
        return minecraft.stop(self._cfg(server), log=self.log)

    # ------------------------------------------------------------- settings
    #
    # Two sources behind one dictionary. server.properties holds the server's
    # own settings; gamerules live inside the world and are reachable only
    # through the console. Presenting them together is the whole point - the
    # split is Mojang's implementation detail, not something a parent trying
    # to make the game easier should have to know about.
    #
    # Gamerules carry a "gamerule." prefix so the panel's generic settings
    # screen, preset mechanism and unknown-key guard all work on them with no
    # changes in panel.py.
    def _world_name(self, props: dict) -> str:
        return (props.get("level-name") or "").strip() or "Bedrock level"

    def read_settings(self, server: dict) -> dict:
        install = self._cfg(server).get("install_dir", "")
        props = minecraft.read_settings(install)
        rules = minecraft_gamerules.read(install, self._world_name(props))
        out = dict(props)
        out.update({minecraft_gamerules.PREFIX + k: v for k, v in rules.items()})
        return out

    def write_settings(self, server: dict, changes: dict) -> dict:
        """Apply settings from either source, reporting before/after for each.

        Order matters. The properties file is written first and the gamerule
        pass cycles the server, so one call lands both and the restart that
        gamerules need is also the restart that makes the properties take
        effect. Writing them the other way round would need two restarts.
        """
        cfg = self._cfg(server)
        install = cfg.get("install_dir", "")
        prefix = minecraft_gamerules.PREFIX

        rule_changes = {k: v for k, v in changes.items() if k.startswith(prefix)}
        prop_changes = {k: v for k, v in changes.items() if k not in rule_changes}

        # Fail before changing anything if a gamerule name is wrong. Bedrock
        # accepts a misspelled rule silently, so a typo would otherwise be
        # reported as applied and do nothing.
        rule_commands = minecraft_gamerules.commands(rule_changes)

        applied: dict = {}
        if prop_changes:
            applied.update(minecraft.write_settings(install, prop_changes))

        if rule_commands:
            props = minecraft.read_settings(install)
            world = self._world_name(props)
            before = minecraft_gamerules.read(install, world)
            # run_console stops the server if it is running and starts it
            # again afterwards; called while already stopped it just uses a
            # temporary instance and leaves it stopped, which is what the
            # panel's cycle wants.
            result = self.run_console(server, rule_commands)
            if not result.get("ok"):
                raise RuntimeError(result.get("error")
                                   or "could not apply gamerules")
            after = minecraft_gamerules.read(install, world)
            for key in rule_changes:
                name = key[len(prefix):]
                applied[key] = (before.get(name, "?"), after.get(name, "?"))
        return applied

    def describe_settings(self, server: dict) -> list[dict]:
        install = self._cfg(server).get("install_dir", "")
        props = minecraft.read_settings(install)
        groups = minecraft_schema.describe(props)
        rules = minecraft_gamerules.read(install, self._world_name(props))
        return groups + minecraft_gamerules.describe(rules)

    def presets(self) -> dict:
        return minecraft_schema.PRESETS

    def locked_settings(self) -> dict:
        return minecraft_schema.LOCKED

    # -------------------------------------------------------------- console
    def run_console(self, server: dict, commands: list) -> dict:
        """Apply console commands by cycling the server.

        Bedrock has no remote console, so the only way in is its stdin - and
        a detached server has none. This stops it, runs a brief attached
        instance to send the commands, and starts it again. Gamerules are
        saved into the world, so they stick.

        The caller is the panel, which is elevated; an unelevated process
        cannot stop a server Task Scheduler owns.
        """
        cfg = self._cfg(server)
        was_running = bool(minecraft.find_process(cfg.get("install_dir", "")))
        if was_running:
            self.stop(server)
            for _ in range(30):
                if not minecraft.find_process(cfg.get("install_dir", "")):
                    break
                time.sleep(2)
            if minecraft.find_process(cfg.get("install_dir", "")):
                return {"ok": False,
                        "error": "could not stop the server to apply commands"}

        result = minecraft.run_console_commands(cfg, commands, log=self.log)

        if was_running:
            self.start(server)
        result["restarted"] = was_running
        return result

    # -------------------------------------------------------------- backups
    def save_paths(self, server: dict) -> list[str]:
        return [minecraft.worlds_root(self._cfg(server).get("install_dir", ""))]

    def snapshot(self, server: dict, reason: str = "manual"):
        """Copy the live world to a timestamped .mcworld.

        This capability was declared long before it existed: backups=True,
        save_paths() implemented, a backup_root and snapshot_minutes sitting
        in config.json - and nothing anywhere called any of it. The backup
        directory had never been created. It read as configured from every
        angle, which is why it went unnoticed until a world a child had spent
        a long time building became the live world with no copies of it here.

        The .mcworld format is chosen so a restore needs no tooling: it is a
        world file the game itself opens, on any device.
        """
        cfg = self._cfg(server)
        return minecraft.snapshot(
            cfg, reason=reason, log=self.log,
            keep=int(cfg.get("snapshot_keep", 30) or 30),
            # A dead network share does not raise - it blocks in kernel I/O
            # that cannot be killed or timed out, and has stalled a backup
            # here for thirteen hours. Checked before anything is written.
            reachable_fn=backup.reachable)
