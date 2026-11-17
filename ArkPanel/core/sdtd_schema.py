"""What each 7 Days to Die setting means, in words a person can act on.

Written against the ACTUAL serverconfig.xml of the installed version, after a
first attempt built from web guides described thirty settings that do not
exist. Those guides covered Alpha 20; this version moved every gameplay
value - difficulty, zombie speed, loot, XP, death penalty - out of the config
file and into `SandboxCode`, an encoded string produced by the game's own
world-creation menu.

That matters beyond tidiness: a preset writing settings the server has never
heard of does not fail, it silently adds them to the XML and changes nothing.
Which is the same class of bug as Palworld quietly reverting its config on
shutdown, and just as hard to see from outside.

So the rule for any game added after this one: build the schema from a real
config file, and check that every key you name is in it.
"""

from __future__ import annotations

GROUPS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("Server identity", [
        ("ServerName", "Server name", "text", "What shows in the server browser."),
        ("ServerDescription", "Description", "text", ""),
        ("ServerPassword", "Join password", "text", "Empty means anyone can join."),
        ("ServerMaxPlayerCount", "Max players", "int",
         "Crossplay servers are reported to cap at 8."),
        ("ServerVisibility", "Visibility", "choice:0|1|2",
         "2 = listed publicly, 1 = friends only, 0 = hidden (join by IP)."),
        ("ServerPort", "Game port", "int",
         "Uses this port and the two above it. Set it inside a range your "
         "router already forwards rather than opening more."),
        ("Region", "Region", "text", "Which regional server list it appears in."),
        ("Language", "Language", "text", ""),
    ]),

    ("Crossplay and network", [
        ("ServerAllowCrossplay", "Allow crossplay", "bool",
         "Lets console and Microsoft Store players join. Reported to need a "
         "public, vanilla, EAC-enabled server."),
        ("EACEnabled", "Easy Anti-Cheat", "bool",
         "Turning this off blocks crossplay and stops console players joining."),
        ("ServerDisabledNetworkProtocols", "Disabled protocols", "text",
         "SteamNetworking is disabled by default so LiteNetLib is used, which "
         "is what crossplay runs over."),
        ("IgnoreEOSSanctions", "Ignore EOS sanctions", "bool", ""),
    ]),

    ("The world", [
        ("GameWorld", "World", "text",
         "Navezgane is the handmade map. RWG generates a random one."),
        ("GameName", "Save name", "text",
         "Names the save folder. Changing it starts a DIFFERENT world."),
        ("WorldGenSeed", "World seed", "text",
         "Only used for generated worlds. Changing it starts a new one."),
        ("WorldGenSize", "World size", "int",
         "Bigger costs more memory and takes far longer to generate."),
        ("GameMode", "Game mode", "text", ""),
        # The important one, and the reason this file is shorter than it looks
        # like it should be.
        ("SandboxCode", "Gameplay settings code", "text",
         "This version keeps difficulty, zombie speed, loot, XP and death "
         "penalty in this ENCODED STRING, not as separate settings. It is "
         "produced when you create a world in the game's own menus - copy it "
         "from there and paste it here. Those values cannot be edited "
         "individually from a config file any more."),
    ]),

    ("Players", [
        ("PlayerKillingMode", "Player killing", "choice:0|1|2|3",
         "0 = nobody can hurt anyone, 3 = everyone can. 0 stops accidents "
         "between people playing together."),
        ("PlayerSafeZoneLevel", "New players safe until level", "int",
         "Zombies leave them alone below this level."),
        ("PlayerSafeZoneHours", "Safe zone hours", "int",
         "How long that protection lasts, in game hours."),
        ("AllowSpawnNearFriend", "Spawn near friends", "choice:0|1|2",
         "Lets you respawn next to the people you are playing with."),
        ("BedrollDeadZoneSize", "Bedroll safe area", "int",
         "How far around a bedroll zombies will not spawn."),
        ("BedrollExpiryTime", "Bedroll expires after (days)", "int", ""),
        ("PersistentPlayerProfiles", "Persistent profiles", "bool", ""),
        ("PartySharedKillRange", "Shared XP range", "int", ""),
    ]),

    ("Land claims - protecting your base", [
        ("LandClaimCount", "Claims per player", "int", ""),
        ("LandClaimSize", "Claim size (blocks)", "int", ""),
        ("LandClaimExpiryTime", "Claim expires after (days)", "int",
         "How long a base stays protected while nobody plays. Worth raising "
         "on a server that is only up some of the time."),
        ("LandClaimDecayMode", "Decay mode", "choice:0|1|2", ""),
        ("LandClaimOnlineDurabilityModifier", "Block strength while online", "int",
         "How much tougher claimed blocks are. 0 disables the bonus."),
        ("LandClaimOfflineDurabilityModifier", "Block strength while offline", "int",
         "Protects your base while you are away."),
        ("LandClaimOfflineDelay", "Offline protection delay (minutes)", "int", ""),
        ("LandClaimDeadZone", "Minimum gap between claims", "int", ""),
    ]),

    ("Performance - this machine", [
        ("MaxSpawnedZombies", "Max zombies at once", "int",
         "The main CPU and memory dial. Lower it on a machine already "
         "running another game server."),
        ("MaxSpawnedAnimals", "Max animals at once", "int", ""),
        ("ServerMaxAllowedViewDistance", "Max view distance", "int",
         "Each step up costs the server real memory per player."),
        ("MaxQueuedMeshLayers", "Max queued mesh layers", "int", ""),
        ("MaxChunkAge", "Max chunk age", "int", "-1 means chunks never reset."),
        ("SaveDataLimit", "Save size limit (MB)", "int", "-1 means no limit."),
        ("DynamicMeshEnabled", "Dynamic mesh", "bool",
         "Makes destruction smoother and costs memory."),
        ("DynamicMeshLandClaimOnly", "Dynamic mesh only in claims", "bool",
         "Leave on - whole-world dynamic mesh is expensive."),
        ("ServerMaxWorldTransferSpeedKiBs", "Max transfer speed (KiB/s)", "int", ""),
    ]),

    ("Admin and monitoring", [
        ("WebDashboardEnabled", "Web dashboard", "bool",
         "The game's own web UI. ArkPanel does not need it."),
        ("WebDashboardPort", "Web dashboard port", "int", ""),
        ("EnableMapRendering", "Render the map for the web dashboard", "bool", ""),
        ("TerminalWindowEnabled", "Terminal window", "bool", ""),
        ("HideCommandExecutionLog", "Hide command log", "choice:0|1|2|3", ""),
        ("AdminFileName", "Admin list file", "text", ""),
        ("ServerAdminSlots", "Reserved admin slots", "int", ""),
        ("ServerReservedSlots", "Reserved slots", "int", ""),
    ]),
]

# Changing these from the panel either breaks the panel's own control of the
# server or needs a router change to mean anything.
LOCKED = {
    "TelnetEnabled": "ArkPanel controls this server over telnet - turning it "
                     "off would make every button stop working.",
    "TelnetPort": "ArkPanel connects on this port.",
    "TelnetPassword": "Change this in secrets.json, not here - the panel uses it.",
}

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

    rest = [{"key": k, "label": k, "kind": kind_of(str(settings[k])),
             "help": LOCKED.get(k, ""), "value": str(settings[k]),
             "locked": k in LOCKED, "locked_why": LOCKED.get(k, "")}
            for k in sorted(settings) if k not in DESCRIBED]
    if rest:
        out.append({"group": "Everything else", "fields": rest})
    return out


PRESETS = {
    "gentle": {
        "label": "Kinder to a new player",
        "why": ("Nobody can hurt anyone, new players get a long safe period, "
                "and your base survives while you are offline. Note what is "
                "NOT here: difficulty, zombie speed and loot live in the "
                "Gameplay settings code, which comes from the game's own "
                "world-creation menu."),
        "values": {
            "PlayerKillingMode": "0",
            "PlayerSafeZoneLevel": "15",
            "PlayerSafeZoneHours": "20",
            "AllowSpawnNearFriend": "2",
            "BedrollDeadZoneSize": "30",
            "LandClaimExpiryTime": "30",
            "LandClaimOfflineDurabilityModifier": "16",
            "LandClaimOnlineDurabilityModifier": "8",
        },
    },
    "lighter_host": {
        "label": "Easier on this PC",
        "why": ("Fewer spawned entities and a shorter view distance. Worth it "
                "on a machine already running another game server."),
        "values": {
            "MaxSpawnedZombies": "32",
            "MaxSpawnedAnimals": "16",
            "ServerMaxAllowedViewDistance": "8",
            "DynamicMeshLandClaimOnly": "true",
        },
    },
}
