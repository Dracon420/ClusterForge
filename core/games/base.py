"""What every game ClusterForge manages has to be able to do.

The reason this exists, measured: `palworld` appeared 136 times in panel.py.
It was not "a game the panel supports", it was a second game bolted alongside
the first, with its own endpoints, its own UI tab and its own branches through
the control code. Adding a third game that way costs another 136 branches, a
fourth another, and by the fifth nobody can change anything without breaking
two games they were not touching.

So the panel stops knowing what ARK or Palworld ARE. It knows about a Game,
and asks it questions.

THE TEST OF THIS INTERFACE: adding a new game must not require editing
panel.py. If it does, the interface is wrong and should change here rather
than growing another special case out there.

Deliberately NOT abstracted:

  * Clustering. ARK's cluster - transfers, shared mod sets, cluster-wide
    updates across three PCs - is the thing this panel is actually good at,
    and it is ARK-shaped. Forcing Valheim to pretend it has a cluster would
    make both worse. Games that cluster say so with `supports_cluster`.

  * Mods. ARK downloads them from CurseForge itself; Palworld has no server
    mods at all; Minecraft has four incompatible ecosystems. A single mod
    abstraction over that is a lie. `supports_mods` gates the UI instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Capabilities:
    """What this game can do, so the UI offers only what is real.

    A greyed-out button with a reason beats a button that does nothing, and
    both beat a button the game cannot support appearing at all.
    """
    rcon: bool = False              # live control and player lists
    settings: bool = False          # editable config through the panel
    mods: bool = False              # the panel can install/manage mods
    cluster: bool = False           # multiple maps sharing transfers
    player_list: bool = False       # can name who is online
    backups: bool = True            # snapshot the save
    # Some games REWRITE their own config on shutdown (Palworld does), so a
    # setting saved while running is silently undone. Games that do this must
    # be stopped before writing - see settings_need_stop.
    settings_need_stop: bool = False
    # A different failure with the same remedy: the write SUCCEEDS and the
    # file is correct, but the server only read its config at startup, so
    # nothing changes until it restarts. Minecraft Bedrock does this, and it
    # is indistinguishable from a broken save - the panel reported survival,
    # the file said survival, and the player still spawned in creative.
    # Declaring it gets the save-restart-report order for free.
    settings_need_restart: bool = False


@dataclass
class ServerStatus:
    """One running (or not running) server, in game-agnostic terms."""
    running: bool = False
    pid: int | None = None
    uptime_seconds: int = 0
    memory_gb: float = 0.0
    players: list[str] | None = None     # None = could not ask, [] = nobody
    port: int | None = None
    installed: bool = True
    detail: str = ""
    extra: dict = field(default_factory=dict)


class Game:
    """One game ClusterForge can manage.

    Subclasses are registered in core/games/__init__.py. Everything the panel
    needs comes through here; nothing in panel.py should import a specific
    game module.
    """

    # Identity -------------------------------------------------------------
    id: str = ""                    # "ark", "palworld" - stable, used in URLs
    name: str = ""                  # "ARK: Survival Ascended"
    short: str = ""                 # "ARK" - for buttons and tabs
    steam_app_id: str = ""          # dedicated server app id, "" if not Steam
    capabilities = Capabilities()

    def __init__(self, config: dict, secrets: dict, log=print):
        self.config = config or {}
        self.secrets = secrets or {}
        self.log = log

    # Lifecycle ------------------------------------------------------------
    def status(self, server: dict) -> ServerStatus:
        raise NotImplementedError

    def start(self, server: dict, **kwargs) -> dict:
        raise NotImplementedError

    def stop(self, server: dict, **kwargs) -> dict:
        raise NotImplementedError

    def restart(self, server: dict, **kwargs) -> dict:
        """Default: stop then start. Override if the game can do better."""
        self.stop(server, **kwargs)
        return self.start(server, **kwargs)

    # Settings -------------------------------------------------------------
    def read_settings(self, server: dict) -> dict:
        raise NotImplementedError

    def write_settings(self, server: dict, changes: dict) -> dict:
        """Returns {key: (before, after)} for what actually changed."""
        raise NotImplementedError

    def describe_settings(self, server: dict) -> list[dict]:
        """Grouped, labelled, explained settings for the UI.

        Shape: [{"group": str, "fields": [{key,label,kind,help,value,locked,
        locked_why}]}]. A game with no description for a setting must still
        return it - an undescribed setting must never become an uneditable
        one, because games add keys between versions.
        """
        return []

    def presets(self) -> dict:
        """Named bundles of settings, e.g. a gentler world for young players."""
        return {}

    # Control --------------------------------------------------------------
    def players(self, server: dict) -> list[str] | None:
        """Who is online. None means the question could not be asked."""
        return None

    def rcon(self, server: dict, command: str) -> str:
        raise NotImplementedError

    def run_console(self, server: dict, commands: list) -> dict:
        """Run console commands, restarting the server if that is the only way.

        Separate from rcon() because some games have no live console at all.
        Bedrock is the case this exists for: gamerules can normally only be
        set by a player typing them in chat, but the server reads its own
        stdin - so the commands can be applied by briefly running an attached
        instance, with nobody needing to join the game.

        Games that can do it live should just use their console and not
        restart anything.
        """
        raise NotImplementedError(f"{self.name} has no console ClusterForge can use")

    # Backups --------------------------------------------------------------
    def save_paths(self, server: dict) -> list[str]:
        """Directories worth snapshotting. Empty means this game has none."""
        return []

    def snapshot(self, server: dict, reason: str = "manual"):
        """Take a backup. Returns the path, or None if there was nothing."""
        return None

    # Discord ---------------------------------------------------------------
    def discord_info(self, server: dict) -> str:
        """The how-to-join post for this game's Discord channel.

        Part of the game-channel standard (see GameView in discord_bot): each
        game writes its own join instructions, because joining is what
        differs between games. This generic fallback only names the game.
        """
        return f"# {self.short}\n\nAsk an admin how to join."

    # Introspection --------------------------------------------------------
    def describe(self) -> dict:
        caps = self.capabilities
        return {
            "id": self.id, "name": self.name, "short": self.short,
            "steam_app_id": self.steam_app_id,
            "capabilities": {
                "rcon": caps.rcon, "settings": caps.settings,
                "mods": caps.mods, "cluster": caps.cluster,
                "player_list": caps.player_list, "backups": caps.backups,
                "settings_need_stop": caps.settings_need_stop,
            },
        }
