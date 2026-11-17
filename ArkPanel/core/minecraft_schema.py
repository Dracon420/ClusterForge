"""What each Bedrock server.properties setting means.

Mojang's own comments in server.properties are good, and this does not try to
replace them - it groups the settings so a parent can find the four that
matter without reading sixty lines of a text file while a child waits.

Kept deliberately short on the advanced ones. Anything not described still
appears under "Everything else" and is still editable.
"""

from __future__ import annotations

GROUPS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("Server identity", [
        ("server-name", "Server name", "text",
         "What shows in the Friends tab and on the server list."),
        ("server-port", "Port (IPv4)", "int",
         "Default 19132. Set it inside a range your router already forwards "
         "rather than opening more."),
        ("server-portv6", "Port (IPv6)", "int", ""),
        ("max-threads", "Max threads", "int", "0 lets it use as many as it likes."),
    ]),

    ("The world", [
        ("level-name", "World name", "text",
         "Names the folder under worlds/. Changing it starts a DIFFERENT world "
         "- the old one is still on disk under its own name."),
        ("level-seed", "World seed", "text",
         "Only used when creating a new world. Blank means random."),
        ("gamemode", "Default game mode", "choice:survival|creative|adventure",
         "What new players start in. Creative is a good way to let a young "
         "child build without being attacked."),
        ("difficulty", "Difficulty", "choice:peaceful|easy|normal|hard",
         "Peaceful removes hostile mobs entirely - no monsters at night. The "
         "single biggest setting for a nervous or very young player."),
        ("allow-cheats", "Allow commands", "bool",
         "Lets operators use /commands, including /gamemode and /time set."),
    ]),

    ("Players", [
        ("max-players", "Max players", "int",
         "Each player costs memory on the host."),
        ("online-mode", "Require Xbox Live sign-in", "bool",
         "Leave on. Off allows anyone claiming any name, on a server that is "
         "reachable from the internet."),
        ("allow-list", "Use the allow list", "bool",
         "When on, only gamertags in allowlist.json can join. The simplest "
         "way to keep a family server private."),
        ("default-player-permission-level",
         "New players join as", "choice:visitor|member|operator",
         "Operator gives full command access - fine for family, wrong for a "
         "public server."),
        ("player-idle-timeout", "Kick idle players after (minutes)", "int",
         "0 never kicks."),
    ]),

    ("Performance - this machine", [
        ("view-distance", "View distance (chunks)", "int",
         "The main memory dial. Each step up costs real memory per player; "
         "10-12 is plenty on a shared machine."),
        ("tick-distance", "Simulation distance (chunks)", "int",
         "How far the world keeps ticking. 4 is the usual minimum."),
        ("server-authoritative-movement", "Authoritative movement", "text",
         "Leave as shipped unless you are debugging cheating."),
        ("compression-threshold", "Compression threshold", "int", ""),
    ]),

    ("Content", [
        ("texturepack-required", "Require the server resource pack", "bool", ""),
        ("content-log-file-enabled", "Content log", "bool", ""),
        ("force-gamemode", "Force game mode on join", "bool",
         "Puts everyone back to the default mode each time they log in."),
    ]),
]

# Nothing here breaks ArkPanel's control of the server - Bedrock has no
# console for the panel to lose - so this is empty on purpose. Kept so the
# generic settings UI has the same shape for every game.
LOCKED: dict[str, str] = {}

DESCRIBED = {key for _g, rows in GROUPS for key, _l, _k, _h in rows}


def kind_of(value: str) -> str:
    value = (value or "").strip()
    if value.lower() in ("true", "false"):
        return "bool"
    try:
        int(value)
        return "int"
    except ValueError:
        return "text"


def describe(settings: dict) -> list[dict]:
    out = []
    for group, rows in GROUPS:
        fields = []
        for key, label, kind, help_text in rows:
            if key not in settings:
                continue
            fields.append({"key": key, "label": label, "kind": kind,
                           "help": help_text, "value": str(settings[key]),
                           "locked": key in LOCKED,
                           "locked_why": LOCKED.get(key, "")})
        if fields:
            out.append({"group": group, "fields": fields})

    # Gamerules arrive under a "gamerule." prefix and have their own groups
    # from core/minecraft_gamerules.py. Excluded here rather than trusting
    # every caller to pass only server.properties: hand this the merged
    # dictionary and they would otherwise appear twice, once described and
    # once as a raw key in "Everything else".
    rest = [{"key": k, "label": k, "kind": kind_of(str(settings[k])),
             "help": "", "value": str(settings[k]),
             "locked": False, "locked_why": ""}
            for k in sorted(settings)
            if k not in DESCRIBED and not k.startswith("gamerule.")]
    if rest:
        out.append({"group": "Everything else", "fields": rest})
    return out


# The gentle gamerules, shared by every preset that is meant to be kind.
# Kept in one place so "creative for a child" and "survival for a child"
# cannot drift apart - the difference between them should be the game mode
# and nothing else.
_KIND_TO_A_CHILD = {
    "gamerule.keepinventory": "true",        # never lose the inventory
    "gamerule.doimmediaterespawn": "true",   # no death screen to read
    "gamerule.showcoordinates": "true",      # can say where they are
    "gamerule.mobgriefing": "false",         # creepers cannot wreck the house
    "gamerule.dofiretick": "false",          # fire stays put
    "gamerule.doinsomnia": "false",          # no phantoms
    "gamerule.respawnblocksexplode": "false",
    "gamerule.pvp": "false",                 # cannot be hurt by each other
    "gamerule.dolimitedcrafting": "false",   # every recipe available at once
    "gamerule.randomtickspeed": "3",         # farms grow within one session
}

# The damage sources that kill a small child without a mob being involved.
_NO_ACCIDENTAL_DEATHS = {
    "gamerule.falldamage": "false",
    "gamerule.drowningdamage": "false",
    "gamerule.firedamage": "false",
    "gamerule.freezedamage": "false",
}

PRESETS = {
    # The two that answer "she wants hearts" and "that was too much",
    # which is a switch that gets flipped more than once in a family.
    "creative_child": {
        "label": "Creative - for a young child",
        "why": ("Creative mode: she cannot be hurt, cannot starve and has "
                "every block. Mobs and night still happen, so the world "
                "still feels alive. This is the one to come back to when "
                "survival turns out to be too much."),
        "values": {
            "gamemode": "creative",
            "force-gamemode": "true",     # every join, without anyone typing
            "difficulty": "easy",
            "allow-list": "true",
            "player-idle-timeout": "0",
            **_KIND_TO_A_CHILD,
        },
    },
    "survival_child": {
        "label": "Survival - as gentle as it gets",
        "why": ("Hearts, hunger, mobs and night - but she keeps her items on "
                "death, respawns instantly, and cannot die from falling, "
                "drowning, fire or cold. Only a mob she can see can kill "
                "her. Try this if she asks for hearts; switch back to "
                "Creative in one click if it is too much."),
        "values": {
            "gamemode": "survival",
            "force-gamemode": "true",
            "difficulty": "easy",
            "allow-list": "true",
            "player-idle-timeout": "0",
            **_KIND_TO_A_CHILD,
            **_NO_ACCIDENTAL_DEATHS,
        },
    },
    "survival_normal": {
        "label": "Survival - ordinary Minecraft",
        "why": ("Standard survival with nothing softened: items drop on "
                "death, every damage type is on, normal difficulty. For "
                "older players who want the real game."),
        "values": {
            "gamemode": "survival",
            "difficulty": "normal",
            "gamerule.keepinventory": "false",
            "gamerule.doimmediaterespawn": "false",
            "gamerule.mobgriefing": "true",
            "gamerule.dofiretick": "true",
            "gamerule.doinsomnia": "true",
            "gamerule.falldamage": "true",
            "gamerule.drowningdamage": "true",
            "gamerule.firedamage": "true",
            "gamerule.freezedamage": "true",
            "gamerule.respawnblocksexplode": "true",
            "gamerule.randomtickspeed": "1",
        },
    },
    "young_child": {
        "label": "Playing with a young child",
        "why": ("Peaceful difficulty - no monsters at all - plus a private "
                "allow list and no idle kick. Everything else is left alone, "
                "so it is still ordinary Minecraft."),
        "values": {
            "difficulty": "peaceful",
            "allow-list": "true",
            "player-idle-timeout": "0",
            "default-player-permission-level": "member",
        },
    },
    "lighter_host": {
        "label": "Easier on this PC",
        "why": ("Shorter view and simulation distance. Worth it on a machine "
                "already running another game server."),
        "values": {
            "view-distance": "10",
            "tick-distance": "4",
            "max-players": "8",
        },
    },
}
