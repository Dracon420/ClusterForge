"""What each Palworld setting means, in words a person can act on.

PalWorldSettings.ini is one 6000-character line of 122 keys with names like
`bAllowEnemyCampSpawnNearBaseCamp` and no documentation anywhere near it.
Editing it by hand means finding the key in that wall of text, getting the
type right (quoted string, bare bool, float with six decimal places) and
rewriting the whole tuple without breaking the other 121 values.

So this is the same idea as core/schema.py for ARK: the panel shows grouped,
labelled, explained fields and writes the file properly.

Only the settings worth a human decision are described. Everything else still
appears, under "Everything else", editable as raw text - an undescribed
setting must never become an uneditable one, because the game adds keys
between versions and this file will always lag.

HINT is deliberately opinionated where a value has a right answer for a
particular situation. "Playing with a young child" is a real use case here
and the defaults are tuned for adults.
"""

from __future__ import annotations

# group -> [(key, label, kind, help)]
# kind: "float" | "int" | "bool" | "text" | "choice:a|b|c"
GROUPS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("Server identity", [
        ("ServerName", "Server name", "text",
         "What shows in the in-game server list."),
        ("ServerDescription", "Description", "text", "Shown under the name."),
        ("ServerPlayerMaxNum", "Max players", "int",
         "Total slots. Each player costs memory on the host."),
        ("CoopPlayerMaxNum", "Max co-op players", "int",
         "Players in a single co-op world. Separate from the server slot count."),
    ]),

    ("Difficulty - the big ones", [
        ("DeathPenalty", "What you lose on death", "choice:None|Item|ItemAndEquipment|All",
         "None = lose nothing. The single biggest frustration for a new or "
         "young player is losing a full inventory to a mistake."),
        ("bEnableInvaderEnemy", "Raids attack your base", "bool",
         "Off means nothing turns up to wreck what you have built while you "
         "are elsewhere."),
        ("PlayerDamageRateDefense", "Damage YOU take", "float",
         "Below 1.0 means you take less. 0.5 is half damage."),
        ("PlayerDamageRateAttack", "Damage you DEAL", "float",
         "Above 1.0 means you hit harder."),
        ("PalDamageRateDefense", "Damage your pals take", "float",
         "Below 1.0 keeps your pals alive through mistakes."),
        ("PalDamageRateAttack", "Damage your pals deal", "float", ""),
        ("bHardcore", "Hardcore", "bool",
         "Permanent death. Leave off unless you mean it."),
        ("bPalLost", "Pals die permanently", "bool",
         "On means a fainted pal is gone for good."),
    ]),

    ("Pace and progress", [
        ("ExpRate", "Experience rate", "float",
         "How fast you level. 2.0 is twice as fast."),
        ("PalCaptureRate", "Pal catch rate", "float",
         "Higher means spheres succeed more often. Catching is the fun part; "
         "failing throws is deflating."),
        ("PalSpawnNumRate", "How many wild pals", "float", ""),
        ("CollectionDropRate", "Resources from gathering", "float",
         "Wood, stone and ore per hit."),
        ("CollectionObjectHpRate", "Toughness of trees and rocks", "float",
         "Below 1.0 means fewer swings to fell a tree."),
        ("CollectionObjectRespawnSpeedRate", "How fast they grow back", "float", ""),
        ("EnemyDropItemRate", "Loot from defeated enemies", "float", ""),
        ("PalEggDefaultHatchingTime", "Egg hatching time", "float",
         "Lower is faster. This is in HOURS; 0 hatches almost instantly."),
        ("WorkSpeedRate", "Base work speed", "float",
         "How fast pals do jobs at your base."),
    ]),

    ("Survival chores", [
        ("PlayerStomachDecreaceRate", "How fast you get hungry", "float",
         "Lower means fewer interruptions to eat."),
        ("PalStomachDecreaceRate", "How fast pals get hungry", "float",
         "Lower means less time spent managing food."),
        ("PlayerStaminaDecreaceRate", "How fast stamina drains", "float", ""),
        ("PlayerAutoHPRegeneRate", "Health regeneration", "float",
         "Higher means you heal up without items."),
        ("PlayerAutoHpRegeneRateInSleep", "Health regen while sleeping", "float", ""),
        ("ItemWeightRate", "How heavy items are", "float",
         "Lower means you carry more before being slowed."),
        ("EquipmentDurabilityDamageRate", "Gear wear", "float",
         "0 means equipment never breaks."),
    ]),

    ("Time of day", [
        ("DayTimeSpeedRate", "Daytime speed", "float",
         "Lower makes days LONGER. Below 1.0 gives more daylight."),
        ("NightTimeSpeedRate", "Night speed", "float",
         "Higher makes nights pass QUICKER. Night is dark, cold and full of "
         "things that kill you - worth speeding up for a nervous player."),
    ]),

    ("Building", [
        ("BuildObjectHpRate", "Structure health", "float", ""),
        ("BuildObjectDamageRate", "Damage to structures", "float",
         "0 means your base cannot be damaged."),
        ("BuildObjectDeteriorationDamageRate", "Structure decay", "float",
         "0 means buildings never rot. Worth setting for a server that is "
         "only up some of the time."),
        ("BaseCampMaxNum", "Max base camps", "int", ""),
        ("BaseCampWorkerMaxNum", "Pals working per base", "int", ""),
        ("MaxBuildingLimitNum", "Building piece limit", "int",
         "0 means no limit."),
        ("bBuildAreaLimit", "Limit where you can build", "bool", ""),
        ("bAllowEnemyCampSpawnNearBaseCamp", "Enemy camps near your base", "bool", ""),
    ]),

    ("Players and guilds", [
        ("bIsPvP", "Player versus player", "bool", ""),
        ("bEnablePlayerToPlayerDamage", "Players can hurt each other", "bool",
         "Off stops accidents between people playing together."),
        ("bEnableFriendlyFire", "Friendly fire", "bool", ""),
        ("bEnableFastTravel", "Fast travel", "bool", ""),
        ("bExistPlayerAfterLogout", "Body stays after logout", "bool", ""),
        ("GuildPlayerMaxNum", "Max players per guild", "int", ""),
        ("bEnableNonLoginPenalty", "Penalty for not logging in", "bool", ""),
        ("ServerPassword", "Join password", "text", ""),
    ]),

    ("World and saving", [
        ("AutoSaveSpan", "Autosave interval (seconds)", "float",
         "Already 30 by default. Lowering it does not make the world safer - "
         "ClusterForge's own snapshots are what protect you."),
        ("bIsUseBackupSaveData", "Keep the game's own backups", "bool",
         "Leave on."),
        ("SupplyDropSpan", "Supply drop interval", "int", ""),
        ("EnablePredatorBossPal", "Predator bosses roam the world", "bool",
         "Off removes the roaming high-level threats."),
        ("DropItemMaxNum", "Max dropped items in the world", "int", ""),
        ("DropItemAliveMaxHours", "How long drops last (hours)", "float", ""),
        ("FishingDifficultyRate", "Fishing difficulty", "float", ""),
    ]),
]

# Never offer these in the UI. Changing them from the panel either breaks the
# panel's own control of the server or is a footgun with no upside.
LOCKED = {
    "RCONEnabled": "ClusterForge controls the server over RCON - turning this off "
                   "would make every button stop working.",
    "RCONPort": "ClusterForge connects on this port.",
    "AdminPassword": "Change this in secrets.json, not here - the panel uses it.",
    "PublicPort": "Changing this needs a matching router port-forward.",
    "CrossplayPlatforms": "Edit with care; removing a platform locks those "
                          "players out.",
}

DESCRIBED = {key for _group, rows in GROUPS for key, _l, _k, _h in rows}


def kind_of(key: str, raw_value: str) -> str:
    """Best guess at a type for a setting nobody has described yet."""
    value = (raw_value or "").strip()
    if value.startswith('"'):
        return "text"
    if value in ("True", "False"):
        return "bool"
    if "." in value:
        try:
            float(value)
            return "float"
        except ValueError:
            return "text"
    try:
        int(value)
        return "int"
    except ValueError:
        return "text"


def describe(settings: dict) -> list[dict]:
    """Grouped, labelled view of the live settings, including unknown keys."""
    out = []
    for group, rows in GROUPS:
        fields = []
        for key, label, kind, help_text in rows:
            if key not in settings:
                continue                # the game version does not have it
            fields.append({"key": key, "label": label, "kind": kind,
                           "help": help_text,
                           "value": str(settings[key]).strip('"'),
                           "locked": key in LOCKED,
                           "locked_why": LOCKED.get(key, "")})
        if fields:
            out.append({"group": group, "fields": fields})

    # Everything the list above does not mention, so a new game version never
    # hides a setting behind this panel.
    rest = []
    for key in sorted(settings):
        if key in DESCRIBED:
            continue
        rest.append({"key": key, "label": key,
                     "kind": kind_of(key, str(settings[key])),
                     "help": LOCKED.get(key, ""),
                     "value": str(settings[key]).strip('"'),
                     "locked": key in LOCKED,
                     "locked_why": LOCKED.get(key, "")})
    if rest:
        out.append({"group": "Everything else", "fields": rest})
    return out


# A gentler world, for playing with a young child. Values chosen to remove
# frustration rather than to remove the game: things still take effort, they
# just stop punishing mistakes.
PRESETS = {
    "gentle": {
        "label": "Playing with a young child",
        "why": "Removes the things that punish mistakes - lost inventories, "
               "wrecked bases, long dark nights - without turning off the game.",
        "values": {
            "DeathPenalty": "None",
            "bEnableInvaderEnemy": "False",
            "PlayerDamageRateDefense": "0.500000",
            "PalDamageRateDefense": "0.500000",
            "PalCaptureRate": "2.000000",
            "ExpRate": "2.000000",
            "CollectionDropRate": "2.000000",
            "CollectionObjectHpRate": "0.500000",
            "EnemyDropItemRate": "2.000000",
            "PlayerStomachDecreaceRate": "0.500000",
            "PalStomachDecreaceRate": "0.500000",
            "PlayerAutoHPRegeneRate": "2.000000",
            "NightTimeSpeedRate": "2.000000",
            "DayTimeSpeedRate": "0.700000",
            "BuildObjectDeteriorationDamageRate": "0.000000",
            "PalEggDefaultHatchingTime": "0.500000",
            "bEnablePlayerToPlayerDamage": "False",
            "bEnableFriendlyFire": "False",
        },
    },
}
