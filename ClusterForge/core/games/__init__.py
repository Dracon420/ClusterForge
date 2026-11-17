"""The games ClusterForge can manage.

Adding one means writing an adapter and adding a line to REGISTRY. If it ever
means editing panel.py as well, the Game interface in base.py is missing
something - fix it there rather than growing another special case.
"""

from __future__ import annotations

from .ark_game import ArkGame
from .base import Capabilities, Game, ServerStatus
from .minecraft_game import MinecraftBedrockGame
from .palworld_game import PalworldGame
from .sdtd_game import SevenDaysGame

REGISTRY: dict[str, type[Game]] = {
    ArkGame.id: ArkGame,
    PalworldGame.id: PalworldGame,
    SevenDaysGame.id: SevenDaysGame,
    MinecraftBedrockGame.id: MinecraftBedrockGame,
}

# What a server is, if its config does not say. Every ARK server predates the
# `game` key, so the absence of one means ARK rather than an error.
DEFAULT_GAME = ArkGame.id


def game_id_for(server: dict) -> str:
    return (server or {}).get("game") or DEFAULT_GAME


def build(game_id: str, config: dict, secrets: dict, log=print, **extra) -> Game:
    """Construct one game adapter. Raises KeyError for an unknown game."""
    cls = REGISTRY[game_id]
    try:
        return cls(config, secrets, log, **extra)
    except TypeError:
        # Adapters that do not take the panel's agent callers (anything
        # running on the panel's own machine) simply do not accept them.
        return cls(config, secrets, log)


def available(config: dict, secrets: dict, log=print, **extra) -> list[dict]:
    """Every registered game, described, for the UI to build itself from."""
    out = []
    for game_id in REGISTRY:
        described = build(game_id, config, secrets, log, **extra).describe()
        out.append(described)
    return out


__all__ = ["REGISTRY", "DEFAULT_GAME", "Game", "Capabilities", "ServerStatus",
           "ArkGame", "PalworldGame", "SevenDaysGame", "MinecraftBedrockGame", "game_id_for", "build", "available"]
