"""
Dracon Cluster Bouncer -- one onboarding channel instead of two.

Replaces Carlbot's split of #welcome-rules and #reaction-roles, which made new
members read rules in one place and then go hunting for a second channel to
become anything. Everything now lives in one channel: rules, an Agree button
that grants the survivor role, and the role pickers underneath it.

Why dropdowns instead of reactions:

  A reaction role needs the member to find a tiny emoji, and the bot needs
  Manage Messages plus a permanent reaction listener. A select menu shows the
  role NAMES, works on mobile, and tells the member what happened.

  Twelve zodiac reactions is a wall of emoji. Twelve options in a dropdown is
  one tidy list.

  Reactions cannot be made exclusive. A dropdown can be "pick exactly one",
  which is what a region or a platform actually is.

Views are PERSISTENT: they carry fixed custom_ids and are re-registered on
every connect, so the buttons keep working after a restart without reposting
anything. A non-persistent view silently dies when the process does, which
looks exactly like a broken bot.
"""

from __future__ import annotations

import json
import os
import time

from .discord_bot import BotRunner, DISCORD_AVAILABLE, discord

if DISCORD_AVAILABLE:
    from discord import ui
else:  # pragma: no cover - only when discord.py is absent
    ui = None


def load_groups(path: str) -> dict:
    """Role groups from data/bouncer/roles.json."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


class RoleSelect(ui.Select if DISCORD_AVAILABLE else object):
    """One dropdown for one group of roles."""

    def __init__(self, group: dict, bot_state: dict):
        self.group_key = group["key"]
        self.role_ids = {str(o["role_id"]): o for o in group["options"]}
        self.exclusive = bool(group.get("exclusive"))
        self.state = bot_state

        options = [
            discord.SelectOption(
                label=o["label"],
                value=str(o["role_id"]),
                emoji=o.get("emoji") or None,
                description=o.get("description") or None,
            )
            for o in group["options"]
        ]
        super().__init__(
            placeholder=group.get("placeholder", "Choose..."),
            min_values=0,
            # Exclusive groups (region, platform) allow one; the rest allow any.
            max_values=1 if self.exclusive else len(options),
            options=options,
            custom_id=f"bouncer:roles:{group['key']}",
        )

    async def callback(self, interaction):
        member = interaction.user
        guild = interaction.guild
        wanted = {int(v) for v in self.values}
        managed = {int(r) for r in self.role_ids}

        add, remove = [], []
        for role_id in managed:
            role = guild.get_role(role_id)
            if role is None:
                continue
            has = role in member.roles
            if role_id in wanted and not has:
                add.append(role)
            # Deselecting removes the role, which is the behaviour people
            # expect from a menu and the thing reactions handle badly.
            elif role_id not in wanted and has:
                remove.append(role)

        try:
            if add:
                await member.add_roles(*add, reason="Bouncer role picker")
            if remove:
                await member.remove_roles(*remove, reason="Bouncer role picker")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I do not have permission to change your roles. My role has to sit "
                "ABOVE the roles I hand out in Server Settings > Roles.",
                ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            await interaction.response.send_message(
                f"Could not update your roles: {exc}", ephemeral=True)
            return

        bits = []
        if add:
            bits.append("added " + ", ".join(r.name for r in add))
        if remove:
            bits.append("removed " + ", ".join(r.name for r in remove))
        await interaction.response.send_message(
            "✅ " + ("; ".join(bits).capitalize() if bits else "No change."),
            ephemeral=True)


class RolePickerView(ui.View if DISCORD_AVAILABLE else object):
    """Up to five dropdowns per message -- Discord's component-row limit."""

    def __init__(self, groups: list, bot_state: dict):
        super().__init__(timeout=None)
        for group in groups[:5]:
            self.add_item(RoleSelect(group, bot_state))


def load_registry(path: str) -> dict:
    """Who is survivor number what.

    A live count of the Survivors role looked fine until you consider someone
    LEAVING: the next arrival would silently reuse a number already given out.
    Numbers are assigned once and kept, so #7 stays #7 forever and nobody is
    ever greeted as a duplicate.
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("survivors", [])
    return data


def save_registry(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def assign_number(data: dict, member_id, name: str) -> tuple[int, bool]:
    """Number for this member. Returns (number, is_new)."""
    member_id = str(member_id)
    for entry in data["survivors"]:
        if str(entry.get("id")) == member_id:
            return int(entry["number"]), False
    number = max((int(e["number"]) for e in data["survivors"]), default=0) + 1
    data["survivors"].append({"number": number, "id": member_id,
                              "name": name, "at": time.time()})
    return number, True


def _ordinal(n: int) -> str:
    """1 -> 1st, 2 -> 2nd, 11 -> 11th."""
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class RulesView(ui.View if DISCORD_AVAILABLE else object):
    """The Agree button. Grants the survivor role and announces the arrival."""

    def __init__(self, runner):
        super().__init__(timeout=None)
        self.runner = runner
        self.survivor_role_id = int(runner.config["survivor_role_id"])
        button = ui.Button(
            label=runner.config.get("agree_label", "I agree to the rules"),
            style=discord.ButtonStyle.success,
            emoji="✅", custom_id="bouncer:agree")
        button.callback = self.agree
        self.add_item(button)

    def _survivor_count(self, guild, role) -> int:
        """How many PEOPLE are survivors.

        Counting the role is the honest answer, but it needs the privileged
        Members intent; without it discord.py caches only the bot itself, so
        this falls back to the guild's member count.

        Bots are excluded either way. The Survivors role currently includes the
        watchers and the bouncer itself, so a raw count would greet the next
        human as the 6th survivor when two people have actually joined.
        """
        try:
            if role is not None:
                humans = [m for m in role.members if not m.bot]
                # >1 means the member cache is genuinely populated, not just
                # this bot looking at itself.
                if len(role.members) > 1:
                    return len(humans)
        except Exception:  # noqa: BLE001
            pass
        # Fallback: total members, minus the bots we can see.
        total = guild.member_count or 0
        try:
            bots = len([m for m in guild.members if m.bot])
            if bots and total > bots:
                return total - bots
        except Exception:  # noqa: BLE001
            pass
        return total

    async def agree(self, interaction):
        role = interaction.guild.get_role(self.survivor_role_id)
        if role is None:
            await interaction.response.send_message(
                "The survivor role is missing or misconfigured - tell an admin.",
                ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.response.send_message(
                "You already have access. Pick your roles below whenever you like.",
                ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="Agreed to the rules")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot grant that role - my role must sit ABOVE it in "
                "Server Settings > Roles.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Welcome in! You now have **{role.name}** and can see the rest of "
            "the server. Pick your roles below.", ephemeral=True)

        # Announce it. Deliberately AFTER the reply and wrapped: a failure to
        # post a welcome must never make it look like agreeing failed.
        try:
            await self._announce(interaction, role)
        except Exception as exc:  # noqa: BLE001
            self.runner.log(f"[bouncer] welcome post failed: {exc}")

    async def _announce(self, interaction, role):
        config = self.runner.config
        channel_id = config.get("welcome_channel_id")
        if not channel_id or not config.get("welcome_enabled", True):
            return
        guild = interaction.guild
        channel = guild.get_channel(int(channel_id)) or \
            await self.runner.bot.fetch_channel(int(channel_id))

        member = interaction.user
        registry = load_registry(self.runner.registry_path)
        count, is_new = assign_number(registry, member.id, member.display_name)
        if is_new:
            save_registry(self.runner.registry_path, registry)
        text = config.get(
            "welcome_message",
            "Hello {mention}, welcome to {guild}! You are the {ordinal} Survivor to join."
        ).format(mention=member.mention, name=member.display_name,
                 guild=guild.name, count=count, ordinal=_ordinal(count),
                 role=role.name)

        await channel.send(
            content=text,
            # Ping the newcomer, nobody else. A welcome that @everyone's the
            # server is how a welcome channel gets muted.
            allowed_mentions=discord.AllowedMentions(
                users=True, everyone=False, roles=False))


class BouncerBot(BotRunner):
    """Onboarding bot: rules, agreement, and role pickers in one channel."""

    def __init__(self, name, token, channel_id, config: dict, logger=print,
                 registry_path: str = "data/bouncer/survivors.json"):
        super().__init__(name, token, channel_id, logger)
        self.config = config or {}
        self.state: dict = {}
        self.registry_path = registry_path

    def build(self):
        intents = discord.Intents.default()
        # Members is a PRIVILEGED intent and has two halves: granted in the
        # Developer Portal AND requested here. Granting it alone changes
        # nothing -- discord.py keeps sending Intents.default(), the member
        # cache stays empty, and role counts silently read as 1 (just the bot).
        #
        # Message Content is deliberately NOT requested: this bot never reads
        # what anyone types, and asking for an intent you do not use is how a
        # bot ends up needing verification it should not need.
        if self.config.get("use_members_intent", True):
            intents.members = True
        bot = discord.Client(intents=intents)
        outer = self

        @bot.event
        async def on_ready():
            outer.ready = True
            outer.last_error = None
            # Re-register the views so the buttons posted days ago still work.
            # Without this every restart leaves dead components behind.
            try:
                if outer.config.get("survivor_role_id"):
                    bot.add_view(RulesView(outer))
                groups = outer.config.get("groups") or []
                for chunk in [groups[i:i + 5] for i in range(0, len(groups), 5)]:
                    bot.add_view(RolePickerView(chunk, outer.state))
                outer.log(f"[discord] {outer.name}: ready, "
                          f"{len(groups)} role group(s) live")
            except Exception as exc:  # noqa: BLE001
                outer.log(f"[discord] {outer.name}: could not register views: {exc}")

        return bot

    # ------------------------------------------------------------- publishing

    def publish(self, channel_id: int, rules_text: str, picker_text: str,
                timeout: int = 60) -> dict:
        """Post or edit the rules message and the role pickers.

        Reuses BotRunner.published so a republish edits rather than stacking a
        second copy underneath the first.
        """
        if not (self.bot and self.loop and self.ready):
            return {"ok": False, "error": "bot not connected"}
        import asyncio

        groups = self.config.get("groups") or []
        chunks = [groups[i:i + 5] for i in range(0, len(groups), 5)] or [[]]

        # The rules run past Discord's 2000-character limit, so they span
        # several messages. The Agree button goes on the LAST of them -- a
        # button above the text it refers to invites agreeing without reading.
        from .serverinfo import split_for_discord
        rules_chunks = split_for_discord(rules_text) or [rules_text]

        async def run(existing):
            result = {"message_ids": [], "posted": 0, "edited": 0, "deleted": 0}
            channel = self.bot.get_channel(int(channel_id)) or \
                await self.bot.fetch_channel(int(channel_id))

            wanted = []
            for index, chunk in enumerate(rules_chunks):
                last = index == len(rules_chunks) - 1
                view = RulesView(self) if last else None
                wanted.append((chunk, view))
            for index, chunk in enumerate(chunks):
                text = picker_text if index == 0 else ""
                wanted.append((text, RolePickerView(chunk, self.state)))

            for index, (text, view) in enumerate(wanted):
                message = None
                if index < len(existing):
                    try:
                        message = await channel.fetch_message(int(existing[index]))
                    except Exception:  # noqa: BLE001
                        message = None
                # discord.py treats view=None as "leave it alone" on an edit, so
                # a message that should have no components needs an explicit
                # empty view or an old button would linger on it.
                edit_view = view if view is not None else ui.View()
                if message is not None:
                    await message.edit(content=text or None, view=edit_view)
                    result["edited"] += 1
                    result["message_ids"].append(str(message.id))
                else:
                    sent = await channel.send(
                        content=text or None, view=view or discord.utils.MISSING,
                        allowed_mentions=discord.AllowedMentions.none())
                    result["posted"] += 1
                    result["message_ids"].append(str(sent.id))

            for stale in existing[len(wanted):]:
                try:
                    old = await channel.fetch_message(int(stale))
                    await old.delete()
                    result["deleted"] += 1
                except Exception:  # noqa: BLE001
                    pass
            return result

        existing = list(self.published.get(str(channel_id), []))
        try:
            future = asyncio.run_coroutine_threadsafe(run(existing), self.loop)
            out = future.result(timeout=timeout)
            out["ok"] = True
            self.published[str(channel_id)] = out["message_ids"]
            return out
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def backfill(self, timeout: int = 90) -> dict:
        """Seed the registry from existing survivors and post their welcomes.

        Numbers follow JOIN DATE, so the people already here get the numbers
        they earned rather than whatever order Discord hands the role list
        back in. Bots are skipped. Anyone already in the registry is left
        alone, so running this twice does not double-post.
        """
        if not (self.bot and self.loop and self.ready):
            return {"ok": False, "error": "bot not connected"}
        import asyncio

        async def run():
            config = self.config
            guild = self.bot.guilds[0]
            role = guild.get_role(int(config["survivor_role_id"]))
            if role is None:
                return {"error": "survivor role not found"}

            humans = [m for m in role.members if not m.bot]
            if len(role.members) <= 1:
                return {"error": "member cache empty - the Members intent is "
                                 "not active, so existing survivors cannot be listed"}
            humans.sort(key=lambda m: m.joined_at or m.created_at)

            registry = load_registry(self.registry_path)
            channel = guild.get_channel(int(config["welcome_channel_id"])) or                 await self.bot.fetch_channel(int(config["welcome_channel_id"]))

            posted, skipped = [], []
            for member in humans:
                number, is_new = assign_number(registry, member.id,
                                               member.display_name)
                if not is_new:
                    skipped.append(f"{member.display_name} (already #{number})")
                    continue
                text = config.get("welcome_message", "").format(
                    mention=member.mention, name=member.display_name,
                    guild=guild.name, count=number, ordinal=_ordinal(number),
                    role=role.name)
                await channel.send(
                    content=text,
                    allowed_mentions=discord.AllowedMentions(
                        users=True, everyone=False, roles=False))
                posted.append(f"{member.display_name} = #{number}")
            save_registry(self.registry_path, registry)
            return {"posted": posted, "skipped": skipped,
                    "next_number": max((int(e["number"]) for e in registry["survivors"]),
                                       default=0) + 1}

        try:
            future = asyncio.run_coroutine_threadsafe(run(), self.loop)
            out = future.result(timeout=timeout)
            out.setdefault("ok", "error" not in out)
            return out
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def inventory(self, timeout: int = 30) -> dict:
        """List the guild's roles and channels, so config can use real ids
        instead of someone copying them out of the client by hand."""
        if not (self.bot and self.loop and self.ready):
            return {"ok": False, "error": "bot not connected"}
        import asyncio

        async def run():
            out = {"guilds": []}
            for guild in self.bot.guilds:
                me = guild.me
                out["guilds"].append({
                    "name": guild.name,
                    "id": str(guild.id),
                    "my_top_role": me.top_role.name if me else None,
                    "my_top_role_position": me.top_role.position if me else None,
                    "can_manage_roles": bool(me and me.guild_permissions.manage_roles),
                    "roles": [
                        {"name": r.name, "id": str(r.id), "position": r.position,
                         "managed": r.managed,
                         # A role above the bot's own top role cannot be granted,
                         # which is the single most common cause of "the button
                         # does nothing".
                         "assignable": bool(me and not r.managed
                                            and r.position < me.top_role.position)}
                        for r in sorted(guild.roles, key=lambda x: -x.position)
                        if r.name != "@everyone"
                    ],
                    "channels": [{"name": c.name, "id": str(c.id)}
                                 for c in guild.text_channels],
                })
            return out

        try:
            future = asyncio.run_coroutine_threadsafe(run(), self.loop)
            out = future.result(timeout=timeout)
            out["ok"] = True
            return out
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
