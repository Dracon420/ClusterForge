"""
Discord bots, reusing the existing identities.

Three bots, keeping the names, avatars and history already in the server:

  The Island Watcher     runs in PC1's agent  - status and players for Island
  Svartalfhiem Watcher   runs in PC2's agent  - status and players for Svartalfheim
  Guardian               runs in the panel    - control across the whole cluster

The split follows the architecture rather than fighting it: a watcher only ever
reports on the machine it lives on, and Guardian is on the panel because that
is the only component that can see both PCs.

Slash commands, not a message prefix. They are self-documenting in the client
and, more usefully, need no Message Content privileged intent -- a status bot
should not be able to read every message in the server.

Control commands are restricted to the configured owner. A bot that anyone in
the channel can use to stop a server is not a control bot, it is a hazard.

A Discord token permits ONE live gateway session. PatchRaptor's watchers must
be stopped before these connect, or the two fight over the same session.
"""

from __future__ import annotations

import asyncio
import threading
import time
import traceback

# Imported at MODULE level, not inside the builders, and deliberately so.
#
# discord.py inspects command callbacks with typing.get_type_hints(), which
# evaluates annotations in the module's globals. Combined with
# `from __future__ import annotations` (which turns annotations into strings),
# a `server: app_commands.Choice[str]` parameter therefore resolves against
# THIS namespace -- not the local scope of the function that built the command.
# Importing app_commands only inside build() produced:
#     NameError: name 'app_commands' is not defined
# at command-registration time, with the bot appearing simply not to start.
try:
    import discord
    from discord import app_commands, ui
    DISCORD_AVAILABLE = True
    DISCORD_IMPORT_ERROR = None
except ImportError as _exc:  # pragma: no cover - only when discord.py is absent
    discord = None
    app_commands = None
    ui = None
    DISCORD_AVAILABLE = False
    DISCORD_IMPORT_ERROR = str(_exc)

COLOUR_OK = 0x2ECC71
COLOUR_WARN = 0xE8A33D
COLOUR_BAD = 0xE04F5F
COLOUR_INFO = 0x3C8CFF

# Backoff between reconnect attempts. Discord returning a 503 on one connect
# once left a watcher permanently dead; it now retries, but backs off so a
# genuine outage is not hammered.
RECONNECT_BASE_SECONDS = 15
RECONNECT_MAX_SECONDS = 300


def _fmt_uptime(seconds) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def status_lines(st: dict, players) -> str:
    """The body of one server's field in a live-status embed.

    Module level because two bots render it now: a watcher, which reads the
    status off its own machine, and Guardian, which reads it over the agent API
    for a machine that has no watcher of its own. The same function for both is
    what keeps the three embeds in #cluster-status looking like one thing.
    """
    st = st or {}
    planned = st.get("planned")
    if planned and not st.get("running"):
        # The machine is off and we KNOW why. Reporting three maps as
        # unreachable when a house is having building work done is noise that
        # teaches people to ignore the channel.
        until = planned.get("until")
        return (f":blue_circle: Planned downtime - {planned.get('reason')}"
                + (f"\n> Expected back {until}" if until else
                   "\n> That PC is switched off on purpose. Nothing is broken."))
    if st.get("agent_error"):
        # Not the same as "offline": the map may well be up and full of people.
        # Saying Offline because a Tailscale hop blipped would send players away
        # from a running server.
        return (":grey_question: Status unknown - the panel cannot reach that "
                f"PC right now\n> {st['agent_error']}")
    if not st.get("running"):
        # Down on purpose is not an outage, and a red circle says it is. The
        # Island spends most of its time deliberately stopped so Palworld can
        # have PC1's memory; showing that identically to a crash means nobody
        # can tell at a glance whether anything is actually wrong.
        held = st.get("maintenance")
        if held:
            return (f":blue_circle: Stopped on purpose - {held}\n"
                    "> Nothing is wrong with this server. It starts again when "
                    "you switch back.")
        return ":red_circle: Offline"

    health = st.get("port_health") or {}
    port = health.get("bound")
    state = health.get("state")
    mark = {"ok": ":white_check_mark:",
            "drifted": ":warning:",
            "out_of_range": ":rotating_light:"}.get(state, ":grey_question:")
    lo, hi = (health.get("range") or [None, None])

    # Committed memory, not working set. An idle server gets paged out, so
    # working set told us Nyrandil was 5.3 GB when it really held 9.2 --
    # on the machine where RAM is the thing that runs out.
    ram = st.get("private_gb") or st.get("memory_gb", 0)

    lines = [f":green_circle: Online - up {_fmt_uptime(st.get('uptime_seconds'))}",
             ":busts_in_silhouette: Players: "
             + ("unknown" if players is None else str(players))]
    cpu = st.get("cpu_percent")
    lines.append(f":computer: CPU: {cpu}%   :zap: RAM: {ram} GB" if cpu
                 else f":zap: RAM: {ram} GB")
    rng = f" (in range {lo}-{hi})" if lo and hi else ""
    lines.append(f":electric_plug: Port: {port} {mark}{rng}" if port
                 else ":electric_plug: Port: not bound yet")
    if state and state not in ("ok", None):
        lines.append(f"> {health.get('message')}")
    return "\n".join(lines)


def live_status_embed(rows, sysinfo=None, host="", refresh_seconds=30):
    """Live status: a field per server, then the host's health once.

    rows: [(label, status_dict, players)], players None meaning "did not answer".
    Laid out to match the embed these bots already posted under PatchRaptor, so
    a one-server watcher looks exactly as it did.
    """
    fields, up, total, unknown, unreachable, held = [], 0, 0, False, 0, 0
    for label, st, players in rows:
        st = st or {}
        host = host or st.get("host", "")
        fields.append((label, status_lines(st, players)))
        if st.get("planned") and not st.get("running"):
            # Off on purpose, like a server stopped for Palworld: not up, not
            # a problem, and must not colour the embed.
            held += 1
        elif st.get("agent_error"):
            # Its players are not zero, they are unknown. Counting them as zero
            # made an unreachable PC read "Players online: 0" -- the one number
            # a player checks before deciding the cluster is dead.
            unreachable += 1
            unknown = True
        elif not st.get("running") and st.get("maintenance"):
            # Down on purpose. Not up, but not a problem either - so it must
            # not turn the embed amber and send someone looking for a fault.
            held += 1
        elif st.get("running"):
            up += 1
            if players is None:
                unknown = True
            else:
                total += players

    # Amber when some are up and some are not: a two-server embed that
    # stays green while one map is down would be lying by omission. Red is
    # reserved for servers we can actually SEE are down -- a machine we cannot
    # reach is amber, because red would claim knowledge we do not have.
    count = len(rows)
    # Servers stopped on purpose do not count against "all up". Otherwise the
    # embed sits permanently amber for the whole time Palworld has the
    # machine, and an amber that never goes green is an amber nobody reads.
    expected = count - held
    colour = (COLOUR_OK if up == expected and not unreachable
              else COLOUR_BAD if up == 0 and not unreachable and not held
              else COLOUR_WARN)
    embed = discord.Embed(title=":t_rex: Cluster Live Status", colour=colour)
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)
    # "3+" when a map did not answer RCON: a number that silently drops a
    # server is worse than an honest lower bound.
    players_text = (f"{total}+" if unknown and total
                    else "unknown" if unknown else str(total))

    if sysinfo:
        host_lines = []
        if sysinfo.get("cpu_percent") is not None:
            host_lines.append(f":computer: System CPU: {sysinfo['cpu_percent']}%")
        if sysinfo.get("memory_used_gb") is not None:
            host_lines.append(f":zap: RAM: {sysinfo['memory_used_gb']}/"
                              f"{sysinfo.get('memory_total_gb', '?')} GB")
        disk = sysinfo.get("disk") or {}
        if disk.get("free_gb") is not None:
            used = round(disk.get("total_gb", 0) - disk["free_gb"], 1)
            host_lines.append(f":dvd: Disk: {used}/{disk.get('total_gb')} GB")
        if host_lines:
            embed.add_field(name=":desktop: Host PC Health",
                            value="\n".join(host_lines), inline=False)

    embed.set_footer(text=f"Players online: {players_text} - refreshes every "
                          f"{refresh_seconds}s | {host}")
    embed.timestamp = discord.utils.utcnow()
    return embed


async def maintain_live_message(bot, channel_id, build_embed, refresh_seconds,
                                state, log=print, name="bot"):
    """Keep ONE message current instead of posting a new one each cycle.

    The rule that matters: post a replacement ONLY when Discord says the old
    message is genuinely gone. Everything else -- a network blip, a 5xx, a rate
    limit, DNS not up yet after a reboot -- is temporary, so it retries against
    the SAME message id.

    An earlier version could not tell those apart. It fetched the message at
    startup and treated any failure as "deleted", and any failed edit set
    message = None to repost next cycle. So one transient error permanently
    orphaned the embed and started maintaining a new one, which is exactly what
    Svartalfheim did after each reboot while the Island (on the panel's own
    machine, with the network already up) happened not to trip it.

    There is no fetch at all: a PartialMessage can be edited from the id alone.
    That removes the startup fetch as a failure mode entirely, and drops the
    need for Read Message History.

    `state` is anything carrying `.message_id` and `._save_message_id(id)` --
    the watcher itself, or Guardian's holder for the machine it reports on
    remotely.
    """
    await bot.wait_until_ready()

    channel = None
    while channel is None and not bot.is_closed():
        try:
            channel = bot.get_channel(channel_id) or \
                await bot.fetch_channel(channel_id)
        except Exception as exc:  # noqa: BLE001
            # Was 'return', which killed the loop for the whole run of the
            # agent if the channel was unreachable for one moment.
            log(f"[discord] {name}: cannot reach channel yet ({exc}); "
                f"retrying in {refresh_seconds}s")
            await asyncio.sleep(refresh_seconds)

    while not bot.is_closed():
        try:
            embed = build_embed()
            if state.message_id:
                try:
                    await channel.get_partial_message(
                        state.message_id).edit(embed=embed)
                except discord.NotFound:
                    # Really deleted -- someone removed it, or it aged out.
                    # This is the ONLY case that earns a new post.
                    log(f"[discord] {name}: status message {state.message_id} "
                        "no longer exists - posting a replacement")
                    state.message_id = None
            if state.message_id is None:
                posted = await channel.send(embed=embed)
                state.message_id = posted.id
                state._save_message_id(posted.id)
                log(f"[discord] {name}: live status posted as {posted.id}")
        except Exception as exc:  # noqa: BLE001
            # Deliberately keeps message_id. Retrying against the same message
            # is always right for a temporary fault; abandoning it is what left
            # stale embeds behind.
            log(f"[discord] {name}: live update failed ({exc}) - keeping "
                f"message {state.message_id} and retrying")
        await asyncio.sleep(refresh_seconds)


class RemoteStatus:
    """Guardian's live-status message for a machine with no watcher of its own.

    His PC runs three maps but no watcher bot, because a Discord token permits
    one gateway session and his agent has none of its own. Guardian already
    holds a session and already reaches his agent over Tailscale, so it keeps a
    third embed in #cluster-status beside the two watchers' -- same layout, same
    builders, no second bot to create, host or keep logged in.
    """

    def __init__(self, channel_id, rows_fn, refresh_seconds=60, state_path=None,
                 label="remote"):
        self.channel_id = int(channel_id)
        self.rows_fn = rows_fn      # () -> {"rows": [(label, status, players)],
                                    #        "system": {...}, "host": "..."}
        self.refresh_seconds = int(refresh_seconds)
        self.state_path = state_path
        self.label = label
        self.message_id = self._load_message_id()

    def _load_message_id(self):
        if not self.state_path:
            return None
        try:
            import json
            with open(self.state_path, "r", encoding="utf-8-sig") as fh:
                return json.load(fh).get("message_id")
        except (OSError, ValueError):
            return None

    def _save_message_id(self, message_id) -> None:
        if not self.state_path:
            return
        try:
            import json
            import os
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump({"message_id": message_id}, fh)
        except OSError:
            pass

    def build_embed(self):
        data = self.rows_fn() or {}
        return live_status_embed(data.get("rows") or [], data.get("system"),
                                 data.get("host", ""), self.refresh_seconds)


class BotRunner:
    """Runs a discord.py client on its own thread and event loop.

    The agent and panel are synchronous Flask apps, so the bot cannot share
    their thread. Failures are logged and never propagate: Discord being down
    must not take a server manager with it.
    """

    def __init__(self, name: str, token: str, channel_id: str | int, logger=print):
        self.name = name
        self.token = (token or "").strip()
        self.channel_id = int(channel_id) if channel_id else None
        self.log = logger
        self.bot = None
        self.loop = None
        self._thread: threading.Thread | None = None
        self.ready = False
        self.last_error: str | None = None
        self.attempts = 0
        # channel id -> [message ids] this bot owns and re-edits
        self.published: dict = {}
        self._stopping = False
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Stop reconnecting. The thread is a daemon, so this is only needed
        when something wants the bot down without ending the process."""
        self._stopping = True
        self._stop_event.set()

    def build(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def start(self) -> bool:
        if not self.token:
            self.log(f"[discord] {self.name}: no token configured - not starting")
            return False
        if self._thread and self._thread.is_alive():
            return True

        def run():
            if not DISCORD_AVAILABLE:
                self.last_error = f"discord.py unavailable: {DISCORD_IMPORT_ERROR}"
                self.log(f"[discord] {self.name}: {self.last_error}")
                return

            # Discord answered one connect attempt with a 503 and the watcher
            # simply gave up, leaving status reporting dead until somebody
            # noticed and restarted the agent by hand. A transient failure on
            # THEIR side must not permanently silence the bot, so this retries
            # with backoff and keeps going.
            attempt = 0
            while not self._stopping:
                attempt += 1
                self.attempts = attempt
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)
                try:
                    self.bot = self.build()
                    self.loop.run_until_complete(self.bot.start(self.token))
                    # A clean return means the bot was told to close.
                    self.log(f"[discord] {self.name}: closed")
                    return
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.ready = False
                    self.log(f"[discord] {self.name} attempt {attempt} failed: "
                             f"{self.last_error}")
                    if attempt == 1:
                        self.log(traceback.format_exc(limit=3))
                finally:
                    try:
                        self.loop.close()
                    except Exception:  # noqa: BLE001
                        pass

                if self._stopping:
                    return
                # An invalid token will never succeed; retrying it forever just
                # hammers Discord and risks the token being flagged.
                if "LoginFailure" in (self.last_error or ""):
                    self.log(f"[discord] {self.name}: token rejected - not retrying")
                    return

                delay = min(RECONNECT_MAX_SECONDS, RECONNECT_BASE_SECONDS * (2 ** (attempt - 1)))
                self.log(f"[discord] {self.name}: retrying in {delay}s")
                if self._stop_event.wait(delay):
                    return

        self._thread = threading.Thread(target=run, daemon=True,
                                        name=f"discord-{self.name}")
        self._thread.start()
        self.log(f"[discord] {self.name}: connecting...")
        return True

    def status(self) -> dict:
        return {
            "name": self.name,
            "configured": bool(self.token),
            # A bot that is retrying is still "running" -- the thread is alive
            # and it will come back on its own. Reporting it as stopped is what
            # made a transient Discord outage look like something needing hands.
            "running": bool(self._thread and self._thread.is_alive()),
            "ready": self.ready,
            "channel_id": self.channel_id,
            "error": self.last_error,
            "attempts": self.attempts,
            "retrying": bool(self._thread and self._thread.is_alive()
                             and not self.ready and self.attempts > 0),
        }

    async def _sync_to_guild(self, bot) -> None:
        """Register slash commands on the guild the configured channel is in.

        Guild-scoped registration is instant; global registration can take an
        hour to appear, which makes it look broken.
        """
        try:
            channel = bot.get_channel(self.channel_id) or await bot.fetch_channel(self.channel_id)
            guild = getattr(channel, "guild", None)
            if guild is None:
                self.log(f"[discord] {self.name}: channel has no guild; syncing globally")
                await bot.tree.sync()
                return
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            self.log(f"[discord] {self.name}: {len(synced)} command(s) live in "
                     f"{guild.name}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[discord] {self.name}: command sync failed: {exc}")


class WatcherBot(BotRunner):
    """Reports on the servers on ONE machine, in one embed.

    Keeps a single live status message in the channel and EDITS it on a timer,
    rather than posting a new one each cycle -- the same approach PatchRaptor's
    `.live` used, and the reason the channel is readable instead of a wall of
    duplicates. The message id is persisted, so restarting the agent resumes
    editing the existing message instead of orphaning it.

    Originally one server per watcher. PC2 now runs two, and a watcher per
    server would have meant another bot identity and another embed saying half
    of what the first one says. So a watcher takes a list of targets and gives
    each its own field, with the host's health shown once -- which is the
    honest layout, since both servers are competing for that one machine.
    """

    @staticmethod
    def label(target: dict) -> str:
        """The server's name as it is NOW. A map switch renames a server while the
        bot runs, so a name copied at startup goes stale ("Amissa" on Scorched Earth)."""
        fn = target.get("display_fn")
        if fn:
            try:
                return fn() or target.get("display_name") or target.get("name") or "?"
            except Exception:  # noqa: BLE001 - a label must never break the embed
                pass
        return target.get("display_name") or target.get("name") or "?"

    def __init__(self, name, token, channel_id, server_name=None, display_name=None,
                 status_fn=None, players_fn=None, system_fn=None, state_path=None,
                 refresh_seconds=30, logger=print, targets=None):
        super().__init__(name, token, channel_id, logger)
        # targets: [{"name", "display_name", "status_fn", "players_fn"}]. The old
        # single-server arguments still work and become a one-item list, so the
        # Island's watcher on PC1 is unchanged.
        if targets is None:
            targets = [{"name": server_name, "display_name": display_name,
                        "status_fn": status_fn, "players_fn": players_fn}]
        self.targets = [t for t in targets if t.get("status_fn")]
        self.server_name = self.targets[0]["name"] if self.targets else server_name
        self.display_name = " & ".join(self.label(t) for t in self.targets) \
            or display_name
        self.system_fn = system_fn      # () -> {cpu, ram, disk} for host health
        self.state_path = state_path
        self.refresh_seconds = refresh_seconds
        self.message_id = self._load_message_id()

    def _load_message_id(self):
        if not self.state_path:
            return None
        try:
            import json
            with open(self.state_path, "r", encoding="utf-8-sig") as fh:
                return json.load(fh).get("message_id")
        except (OSError, ValueError):
            return None

    def _save_message_id(self, message_id) -> None:
        if not self.state_path:
            return
        try:
            import json
            import os
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as fh:
                json.dump({"message_id": message_id}, fh)
        except OSError:
            pass

    def build(self):
        intents = discord.Intents.default()  # no message content needed
        bot = discord.Client(intents=intents)
        tree = app_commands.CommandTree(bot)
        bot.tree = tree
        outer = self

        @bot.event
        async def on_ready():
            outer.ready = True
            outer.log(f"[discord] {outer.name}: connected as {bot.user}")
            await outer._sync_to_guild(bot)
            # Guarded so a reconnect does not start a second refresh loop,
            # which would double the edit rate and hit Discord's rate limit.
            if not getattr(outer, "_live_started", False):
                outer._live_started = True
                bot.loop.create_task(outer._live_loop())

        def server_field(target) -> tuple:
            """One server's row for the embed: (label, status, players)."""
            st = target["status_fn"]() or {}
            # Asked ONCE per server per refresh, and only while it is up. It is an
            # RCON round trip -- a process spawn -- and the single-server version
            # asked it twice, once for the field and again for the footer.
            players = target["players_fn"]() if st.get("running") else 0
            return outer.label(target), st, players

        def build_embed() -> "discord.Embed":
            sysinfo = (outer.system_fn() or {}) if outer.system_fn else {}
            rows = [server_field(t) for t in outer.targets]
            return live_status_embed(rows, sysinfo,
                                     refresh_seconds=outer.refresh_seconds)

        async def live_loop():
            await maintain_live_message(bot, outer.channel_id, build_embed,
                                        outer.refresh_seconds, outer,
                                        outer.log, outer.name)

        outer._live_loop = live_loop
        # Exposed so the layout can be tested without a Discord connection.
        outer._build_embed = build_embed

        @tree.command(name="status",
                      description=f"Status of {self.display_name}")
        async def status_cmd(interaction):
            await interaction.response.defer(thinking=True)
            try:
                await interaction.followup.send(embed=build_embed())
            except Exception as exc:  # noqa: BLE001
                await interaction.followup.send(f"Could not read status: {exc}")

        @tree.command(name="players",
                      description=f"Who is on {self.display_name}")
        async def players_cmd(interaction):
            await interaction.response.defer(thinking=True)
            lines = []
            for target in outer.targets:
                # Off the event loop: each count is an RCON process spawn, and two
                # servers asked back to back would stall the bot for both.
                count = await asyncio.to_thread(target["players_fn"])
                if count is None:
                    lines.append(f"**{outer.label(target)}** - player count unknown "
                                 "(the RCON query failed). Not reporting zero, because "
                                 "that would be a guess.")
                else:
                    word = "player" if count == 1 else "players"
                    lines.append(f"**{outer.label(target)}** - {count} {word} online")
            await interaction.followup.send("\n".join(lines) or "No servers on this machine.")

        return bot


# Actions that take a server away from players. These get a second click before
# anything happens -- a control panel sitting in a chat channel is one stray tap
# away from ending someone's session, and phones make stray taps easy.
DESTRUCTIVE_ACTIONS = {"stop", "restart", "kill", "update",
                       "pal_stop", "pal_restart", "game_stop", "game_restart"}

# "All" versions of these only touch maps in the right state - see
# GuardianBot.all_targets. Anything else (update, players...) is cluster-wide.
ALL_FILTERED = {"start", "stop", "restart", "save"}


class ConfirmView(ui.View if DISCORD_AVAILABLE else object):
    """Ephemeral 'are you sure' shown only to whoever pressed the button.

    Not persistent on purpose: it is created per press and dies with its
    timeout, so a stale confirm cannot be resurrected after a bot restart and
    fire an action nobody is watching for.
    """

    def __init__(self, run_coro, label: str, timeout: float = 60):
        super().__init__(timeout=timeout)
        self._run = run_coro
        self._label = label

    @ui.button(label="Yes, do it", style=discord.ButtonStyle.danger
               if DISCORD_AVAILABLE else None)
    async def confirm(self, interaction, button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"Running **{self._label}**...", view=self)
        await self._run(interaction)
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary
               if DISCORD_AVAILABLE else None)
    async def cancel(self, interaction, button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"Cancelled - **{self._label}** was not run.", view=self)
        self.stop()


class ControlButton(ui.Button if DISCORD_AVAILABLE else object):
    """One control. custom_id is stable so the panel survives a bot restart."""

    def __init__(self, guardian, action: str, target: str, label: str,
                 style, row: int, emoji=None):
        super().__init__(label=label, style=style, row=row, emoji=emoji,
                         custom_id=f"guardian:ctl:{action}:{target}")
        self.guardian = guardian
        self.action = action
        self.target = target

    async def callback(self, interaction):
        await self.guardian.handle_control(interaction, self.action,
                                           self.target, self.label)


class PlayerSelect(ui.Select if DISCORD_AVAILABLE else object):
    """Pick which logged-in player to clear. Choosing IS the confirmation: the
    button press only lists names, nothing is kicked until one is chosen."""

    def __init__(self, guardian, server: str, label: str, players: list,
                 force: bool = False):
        options = [
            discord.SelectOption(
                label=p["name"][:100], value=p["id"],
                # Only shown for the forced list, where "when were they last
                # here" is the one clue about whether a ghost is plausible.
                description=(f"last seen {str(p.get('last_seen', ''))[:16]}"
                             if force else None))
            for p in players[:25]]
        super().__init__(placeholder="Choose the stuck player to clear",
                         min_values=1, max_values=1, options=options)
        self.guardian = guardian
        self.server = server
        self.server_label = label
        # This id was NOT in the live list, so the kick must skip the "are they
        # logged in" check that would otherwise refuse the one case this is for.
        self.force = force

    async def callback(self, interaction):
        await self.guardian.finish_unstick(interaction, self.server, self.server_label,
                                           self.values[0], self.view, force=self.force)


class UnstickView(ui.View if DISCORD_AVAILABLE else object):
    """Ephemeral and short-lived, like ConfirmView: a stale list must not be
    usable to kick someone after the moment has passed."""

    def __init__(self, guardian, server: str, label: str, players: list,
                 timeout: float = 120, force: bool = False):
        super().__init__(timeout=timeout)
        self.add_item(PlayerSelect(guardian, server, label, players, force=force))


class DisconnectButton(ui.Button if DISCORD_AVAILABLE else object):
    """One online player. Pressing asks to confirm before anyone is dropped."""

    def __init__(self, guardian, server: str, label: str, player: dict, row: int):
        super().__init__(label=f"{player['name']} - {label}"[:80],
                         style=discord.ButtonStyle.danger, row=row, emoji="🔌")
        self.guardian = guardian
        self.server = server
        self.server_label = label
        self.player = player

    async def callback(self, interaction):
        await self.guardian.confirm_disconnect(interaction, self.server, self.server_label, self.player)


class OnlineView(ui.View if DISCORD_AVAILABLE else object):
    """Everyone connected right now, one button each. Ephemeral and short-lived:
    a list from five minutes ago must not be able to drop somebody who has since
    logged back in."""

    def __init__(self, guardian, rows: list, timeout: float = 120):
        super().__init__(timeout=timeout)
        if not DISCORD_AVAILABLE:
            return
        for index, (server, label, player) in enumerate(rows[:20]):
            self.add_item(DisconnectButton(guardian, server, label, player, index // 4))


class ServerControlView(ui.View if DISCORD_AVAILABLE else object):
    """The controls for ONE map, shown only to whoever picked it from the panel."""

    def __init__(self, guardian, name: str, label: str, timeout: float = 180):
        super().__init__(timeout=timeout)
        if not DISCORD_AVAILABLE:
            return
        self.add_item(ControlButton(guardian, "start", name, f"Start {label}"[:80],
                                    discord.ButtonStyle.success, 0))
        self.add_item(ControlButton(guardian, "restart", name, "Restart", discord.ButtonStyle.primary, 0))
        self.add_item(ControlButton(guardian, "stop", name, "Stop", discord.ButtonStyle.danger, 0))
        self.add_item(ControlButton(guardian, "save", name, "Save", discord.ButtonStyle.secondary, 0))
        self.add_item(ControlButton(guardian, "unstick", name, "Clear login",
                                    discord.ButtonStyle.secondary, 1, "🧹"))


class RescueSelect(ui.Select if DISCORD_AVAILABLE else object):
    """Pick whose character to put back. Choosing shows the confirm, never restores."""

    def __init__(self, guardian, characters: list):
        options = []
        for character in characters[:25]:
            good = [v for v in character.get("versions", []) if v["size"] > 0]
            if not good:
                continue
            newest = good[0]
            when = str(newest["when"])[:16].replace("T", " ")
            options.append(discord.SelectOption(
                label=(character.get("name") or character["id"][:12])[:100],
                value=character["id"],
                description=f"newest copy {when} ({newest['size'] // 1024} KB)"[:100]))
        super().__init__(placeholder="Whose character is lost?", min_values=1, max_values=1,
                         options=options or [discord.SelectOption(label="nothing saved yet",
                                                                  value="none")])
        self.guardian = guardian

    async def callback(self, interaction):
        await self.guardian.confirm_rescue(interaction, self.values[0])


class RescueView(ui.View if DISCORD_AVAILABLE else object):
    def __init__(self, guardian, characters: list, timeout: float = 180):
        super().__init__(timeout=timeout)
        if DISCORD_AVAILABLE:
            self.add_item(RescueSelect(guardian, characters))


class ServerSelect(ui.Select if DISCORD_AVAILABLE else object):
    """Which map to control. A select rather than a row of buttons per map: six
    maps x five buttons is past Discord's 25-component limit for one message."""

    def __init__(self, guardian, servers: list):
        options = [discord.SelectOption(label=(s.get("display_name") or s["name"])[:100],
                                        value=s["name"]) for s in servers[:25]]
        super().__init__(placeholder="Controls for one map...", min_values=1, max_values=1,
                         options=options, custom_id="guardian:ctl:pick", row=1)
        self.guardian = guardian

    async def callback(self, interaction):
        await self.guardian.show_server_controls(interaction, self.values[0])


class ControlPanelView(ui.View if DISCORD_AVAILABLE else object):
    """Cluster row, a map picker, and the online-players button. Persistent, so the
    buttons keep working after the panel restarts instead of going dead until
    someone reposts."""

    def __init__(self, guardian, servers: list):
        super().__init__(timeout=None)
        if not DISCORD_AVAILABLE:
            return
        green = discord.ButtonStyle.success
        blue = discord.ButtonStyle.primary
        red = discord.ButtonStyle.danger
        grey = discord.ButtonStyle.secondary

        # Row 0 - the whole cluster.
        self.add_item(ControlButton(guardian, "start", "all", "Start All", green, 0, "▶️"))
        self.add_item(ControlButton(guardian, "restart", "all", "Restart All", blue, 0, "🔄"))
        self.add_item(ControlButton(guardian, "stop", "all", "Stop All", red, 0, "⏹️"))
        self.add_item(ControlButton(guardian, "update", "all", "Update", grey, 0, "⬇️"))

        # Row 1 is the map picker; row 2 the player list.
        self.add_item(ServerSelect(guardian, servers))
        self.add_item(ControlButton(guardian, "players", "all", "Who's online", blue, 2, "👥"))
        self.add_item(ControlButton(guardian, "rescue", "all", "Rescue a character", grey, 2, "⛑"))

        self.add_item(ControlButton(guardian, "refresh", "all", "Refresh status", grey, 2, "🔃"))

        # Row 3 used to hold the game swap, because PC1 had memory for one
        # server at a time. Palworld moved to PC2 on 2026-11-09 and the two no
        # longer compete, so swapping would stop an ARK map to free memory
        # nothing needs. Palworld has its own buttons on its own post now.


class PalworldView(ui.View if DISCORD_AVAILABLE else object):
    """Start, stop, restart, save or re-read Palworld.

    These used to be the cluster panel's two "switch game" buttons, because
    Palworld and the Island shared PC1's memory and starting one meant
    stopping the other. Palworld moved to PC2 on 2026-11-09, so a swap is
    meaningless now - the Island is not in its way and never will be again.
    Pressing "Switch to Palworld" would have stopped an ARK map for no reason.
    """

    def __init__(self, guardian, state=None, ark_label="ARK Island"):
        """state: the palworld status dict, or None if it is not known.

        Given state, this shows ONE switch button naming the other game -
        whatever is running, the only thing you can usefully do is swap to the
        other, and two buttons with one of them inert just made people read
        both to work out which was live.

        state=None means "show both", which covers two cases: nothing is
        running, so there is no other game to name; and the persistent
        registration at startup, which must claim BOTH custom_ids so either
        button still routes after a restart.
        """
        super().__init__(timeout=None)
        if not DISCORD_AVAILABLE:
            return
        grey = discord.ButtonStyle.secondary
        green = discord.ButtonStyle.success
        red = discord.ButtonStyle.danger

        # Target "pal" keeps these custom_ids distinct from any copy elsewhere.
        # Two persistent views sharing a custom_id means the later registration
        # silently wins the routing for both.
        running = (state or {}).get("running")

        # state=None is the startup registration and must claim EVERY custom_id,
        # or whichever buttons were not drawn stop routing after a restart.
        show_all = state is None
        if show_all or not running:
            self.add_item(ControlButton(guardian, "pal_start", "pal",
                                        "Start", green, 0, "▶️"))
        if show_all or running:
            self.add_item(ControlButton(guardian, "pal_stop", "pal",
                                        "Stop", red, 0, "⏹️"))
            self.add_item(ControlButton(guardian, "pal_restart", "pal",
                                        "Restart", grey, 0, "🔁"))
            self.add_item(ControlButton(guardian, "pal_save", "pal",
                                        "Save now", grey, 0, "💾"))

        self.add_item(ControlButton(guardian, "pal_refresh", "pal",
                                    "Refresh", grey, 0, "🔃"))


def palworld_embed(state: dict, display_name: str = "Palworld"):
    """Live status for the Palworld channel.

    Colour carries the message for anyone skimming: green up, grey off. Off is
    NOT red - with the switch in place, stopped is a normal resting state, and
    an alarming colour would teach people to ignore it.
    """
    if state.get("error"):
        embed = discord.Embed(title=f"🐾 {display_name}", colour=COLOUR_WARN,
                              description=f"Status unavailable: {state['error']}")
        return embed

    running = bool(state.get("running"))
    embed = discord.Embed(
        title=f"🐾 {display_name}",
        colour=(COLOUR_OK if running else 0x4f545c),
        description=("🟢 **Online**" if running else "⚫ **Off** - press "
                     "**Start** to bring it up"))

    if running:
        mins = round((state.get("uptime_seconds") or 0) / 60)
        embed.add_field(name="Up for",
                        value=(f"{mins // 60}h {mins % 60}m" if mins >= 60
                               else f"{mins}m"))
        embed.add_field(name="Memory", value=f"{state.get('private_gb', 0)} GB")
        players = state.get("players")
        if players:
            embed.add_field(name=f"Playing ({len(players)})",
                            value="\n".join(players), inline=False)
        elif players == []:
            embed.add_field(name="Playing", value="nobody right now", inline=False)
        else:
            # None means RCON did not answer. Say so rather than showing zero,
            # which would read as "the server is empty".
            embed.add_field(name="Playing", value="could not ask the server",
                            inline=False)

    embed.set_footer(text="ClusterForge - updates every minute")
    embed.timestamp = discord.utils.utcnow()
    return embed


# ------------------------------------------------------------ game channels
#
# The standard for a non-ARK game with its own Discord channel (2026-11-12):
# a how-to-join post (the game adapter's discord_info) followed by ONE live
# status embed with Start / Stop / Restart / Refresh, kept current every
# minute. Adding a game is config, not code: discord.game_channels in
# config.json names the channel, and the game adapter supplies status and
# lifecycle through the same Game interface the panel's own pages use.
# Palworld predates this and keeps its own view (it has a Save button and an
# agent proxy), but its post is refreshed by the same live loop.

class GameView(ui.View if DISCORD_AVAILABLE else object):
    """Start / Stop / Restart / Refresh for one game channel.

    custom_ids carry the game id, so each game's buttons route on their own
    and all survive a restart. state=None is the startup registration and
    claims every custom_id, same rule as PalworldView.
    """

    def __init__(self, guardian, game_id: str, state=None):
        super().__init__(timeout=None)
        if not DISCORD_AVAILABLE:
            return
        grey = discord.ButtonStyle.secondary
        running = (state or {}).get("running")
        show_all = state is None
        if show_all or not running:
            self.add_item(ControlButton(guardian, "game_start", game_id, "Start",
                                        discord.ButtonStyle.success, 0, "▶️"))
        if show_all or running:
            self.add_item(ControlButton(guardian, "game_stop", game_id, "Stop",
                                        discord.ButtonStyle.danger, 0, "⏹️"))
            self.add_item(ControlButton(guardian, "game_restart", game_id,
                                        "Restart", grey, 0, "🔁"))
        self.add_item(ControlButton(guardian, "game_refresh", game_id, "Refresh",
                                    grey, 0, "🔃"))


def game_embed(state: dict, display_name: str, emoji: str = "🎮"):
    """Live status for a game channel. Same colours as the Palworld one."""
    title = f"{emoji} {display_name}"
    if state.get("error"):
        return discord.Embed(title=title, colour=COLOUR_WARN,
                             description=f"Status unavailable: {state['error']}")
    running = bool(state.get("running"))
    embed = discord.Embed(
        title=title, colour=(COLOUR_OK if running else 0x4f545c),
        description=("🟢 **Online**" if running else
                     "⚫ **Off** - an admin can press **Start**"))
    if running:
        mins = round((state.get("uptime_seconds") or 0) / 60)
        embed.add_field(name="Up for", value=(f"{mins // 60}h {mins % 60}m"
                                              if mins >= 60 else f"{mins}m"))
        embed.add_field(name="Memory", value=f"{state.get('memory_gb', 0)} GB")
        if state.get("version"):
            embed.add_field(name="Version", value=str(state["version"]))
        players, count = state.get("players"), state.get("player_count")
        if players:
            embed.add_field(name=f"Playing ({len(players)})",
                            value="\n".join(players), inline=False)
        elif count is not None:
            cap = state.get("max_players")
            embed.add_field(name="Playing", inline=False,
                            value=(f"{count} of {cap}" if cap else str(count))
                            if count else "nobody right now")
        elif state.get("transport") == "nethernet":
            # Not a fault: a NetherNet server answers no status query at all.
            embed.add_field(name="Playing", inline=False,
                            value="not shown - this server type does not report it")
        else:
            embed.add_field(name="Playing", value="could not ask the server",
                            inline=False)
    embed.set_footer(text="ClusterForge - updates every minute")
    embed.timestamp = discord.utils.utcnow()
    return embed


def _embed_signature(embed) -> str:
    """An embed's content without its timestamp, to skip no-op edits."""
    d = embed.to_dict()
    d.pop("timestamp", None)
    return repr(sorted(d.items()))


class GuardianBot(BotRunner):
    """Cluster control. Runs in the panel, which is the only thing that sees
    both machines."""

    def __init__(self, name, token, channel_id, owner_id,
                 servers_fn, action_fn, job_fn, update_fn, logger=print,
                 control_role_ids=None, control_state_path=None,
                 control_allow_admins=True, palworld_control_fn=None,
                 status_fn=None, players_fn=None, kick_fn=None,
                 characters_fn=None, rescue_fn=None, remote_status=None,
                 recent_fn=None, switch_fn=None, palworld_fn=None,
                 game_posts=None, game_status_fn=None, game_control_fn=None):
        super().__init__(name, token, channel_id, logger)
        # Game channels (see GameView): {game_id: {"display_name", "emoji"}},
        # (game_id) -> status dict, and (game_id, verb) -> {} | {"error"}.
        self.game_posts = dict(game_posts or {})
        self.game_status_fn = game_status_fn
        self.game_control_fn = game_control_fn
        self.owner_id = int(owner_id) if owner_id else None
        self.servers_fn = servers_fn    # () -> [{name, display_name}]
        self.action_fn = action_fn      # (action, server_name, opts) -> dict
        self.players_fn = players_fn    # (server_name) -> [{name, id}] | None
        self.kick_fn = kick_fn          # (server_name, account_id, force) -> dict
        # (server_name) -> [{id, name, last_seen}] seen recently, for the stuck
        # login RCON does not report.
        self.recent_fn = recent_fn
        # (target) -> dict. Swaps which game owns PC1's memory: the Island and
        # Palworld cannot both be resident on a 15.9 GB machine.
        self.switch_fn = switch_fn
        # () -> palworld status dict, for the #palworld embed.
        self.palworld_fn = palworld_fn
        self.palworld_message_id = None
        self.characters_fn = characters_fn  # () -> [{id, name, versions[...]}]
        self.rescue_fn = rescue_fn      # (account_id, version) -> dict
        self.job_fn = job_fn            # (server_name, job_id) -> dict
        self.update_fn = update_fn      # (dry_run) -> dict
        self.status_fn = status_fn      # () -> cluster status dict, for the panel embed
        # A RemoteStatus, or None. Set for the machine that has no watcher of
        # its own -- Guardian keeps its live embed instead.
        self.remote_status = remote_status
        # Owner is always allowed. Roles are the way to let the admin team press
        # these without handing anyone the panel login -- empty means owner only,
        # which is the safe default for buttons that stop servers.
        self.control_role_ids = {int(r) for r in (control_role_ids or [])}
        self.control_allow_admins = bool(control_allow_admins)
        # (action: str) -> dict. Drives Palworld wherever it actually
        # lives; the panel proxies to its host agent when it is remote.
        self.palworld_control_fn = palworld_control_fn
        self.control_state_path = control_state_path
        state = self._load_state()
        self.control_message_id = state.get("message_id")
        self.palworld_message_id = state.get("palworld_message_id")
        self.palworld_channel_id = state.get("palworld_channel_id")
        # {game_id: {"channel": id, "message": id}} for each game's live embed.
        self.game_messages = dict(state.get("game_messages") or {})

    def _load_state(self) -> dict:
        if not self.control_state_path:
            return {}
        try:
            import json
            with open(self.control_state_path, "r", encoding="utf-8-sig") as fh:
                loaded = json.load(fh)
                return loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, **changes) -> None:
        """Merge into the control-state file rather than replacing it.

        There are two owned messages now (the control panel and the Palworld
        post) and a plain rewrite would have each forget the other's id on
        every publish, orphaning whichever was written first.
        """
        if not self.control_state_path:
            return
        state = self._load_state()
        state.update(changes)
        try:
            import json
            import os
            os.makedirs(os.path.dirname(self.control_state_path), exist_ok=True)
            with open(self.control_state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
        except OSError:
            pass

    def _save_control_id(self, message_id, channel_id) -> None:
        self._save_state(message_id=message_id, channel_id=channel_id)

    def announce(self, title: str, message: str, severity: str = "info") -> bool:
        """Post a message into Guardian's channel from outside the bot's loop.

        The updater runs on the panel's own thread, so it cannot await
        anything. This schedules the send onto the bot's loop and returns
        immediately. Without it an unattended update would happen in silence --
        the operator would only infer it from the watcher embeds blinking.
        """
        if not (self.bot and self.loop and self.ready):
            return False
        colour = {"info": COLOUR_INFO, "warn": COLOUR_WARN,
                  "critical": COLOUR_BAD, "ok": COLOUR_OK}.get(severity, COLOUR_INFO)

        async def send():
            try:
                channel = self.bot.get_channel(self.channel_id) or \
                    await self.bot.fetch_channel(self.channel_id)
                embed = discord.Embed(title=title, description=message,
                                      colour=colour)
                embed.timestamp = discord.utils.utcnow()
                embed.set_footer(text="ClusterForge")
                # Only a genuine failure earns a mention.
                content = "@here" if severity == "critical" else ""
                await channel.send(content=content, embed=embed,
                                   allowed_mentions=discord.AllowedMentions(
                                       everyone=bool(content)))
            except Exception as exc:  # noqa: BLE001
                self.log(f"[discord] {self.name}: announce failed: {exc}")

        try:
            asyncio.run_coroutine_threadsafe(send(), self.loop)
            return True
        except Exception as exc:  # noqa: BLE001
            self.log(f"[discord] {self.name}: could not schedule announce: {exc}")
            return False

    def sync_channel(self, channel_id: int, chunks: list, timeout: int = 60) -> dict:
        """Make a channel hold exactly `chunks`, editing in place where possible.

        Discord will not let a bot edit a message a human wrote, so the bot owns
        its own sequence and keeps editing THAT. Editing rather than reposting
        is what stops an info channel turning into a wall of near-identical
        posts every time a rate changes.

        Returns {"ok", "message_ids", "edited", "posted", "deleted", "error"}.
        Blocking, because the caller wants to persist the ids afterwards.
        """
        if not (self.bot and self.loop and self.ready):
            return {"ok": False, "error": "bot not connected"}

        async def run(existing):
            result = {"message_ids": [], "edited": 0, "posted": 0, "deleted": 0}
            channel = self.bot.get_channel(int(channel_id)) or                 await self.bot.fetch_channel(int(channel_id))

            for index, text in enumerate(chunks):
                message = None
                if index < len(existing):
                    try:
                        message = await channel.fetch_message(int(existing[index]))
                    except discord.NotFound:
                        # Genuinely deleted by hand -- repost it.
                        message = None
                    # Anything else (network, 5xx, rate limit) is temporary, and
                    # is deliberately NOT swallowed: posting a duplicate info
                    # post and adopting it would leave the real one orphaned for
                    # a human to find and delete. Failing the sync is recoverable
                    # -- the panel reports it and the next refresh retries.
                if message is not None:
                    # Skip the edit when nothing changed: it avoids a pointless
                    # "edited" marker appearing on an unchanged post.
                    if (message.content or "") != text:
                        await message.edit(content=text)
                        result["edited"] += 1
                    result["message_ids"].append(str(message.id))
                else:
                    sent = await channel.send(
                        content=text,
                        allowed_mentions=discord.AllowedMentions.none())
                    result["posted"] += 1
                    result["message_ids"].append(str(sent.id))

            # The post got shorter -- drop the messages we no longer need.
            for stale in existing[len(chunks):]:
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

    def _authorised(self, interaction) -> bool:
        # No owner configured means nobody is authorised. Failing closed is the
        # only sane default for commands that stop servers.
        if self.owner_id and interaction.user.id == self.owner_id:
            return True
        # Anyone holding Discord's Administrator permission in this guild, by
        # the owner's choice (discord.control_allow_admins). Administrator is a
        # blanket permission, so this deliberately trusts the server's own
        # admin list rather than a separate one kept in config.json - but it
        # DOES mean granting someone Administrator now also lets them stop a
        # game server. guild_permissions exists only on a Member, so a DM still
        # gets nothing.
        if self.control_allow_admins:
            perms = getattr(interaction.user, "guild_permissions", None)
            if perms is not None and getattr(perms, "administrator", False):
                return True
        if not self.control_role_ids:
            return False
        # Roles only exist on a Member; a DM gives a plain User and gets nothing.
        roles = getattr(interaction.user, "roles", None) or []
        return any(r.id in self.control_role_ids for r in roles)

    async def _dispatch(self, action: str, targets: list, opts: dict | None = None):
        """Run one action across targets and describe what happened.

        action_fn does blocking HTTP to the agents. Run straight on the event
        loop it would stall Guardian for the length of the call -- announcements
        and info syncs would queue behind a button press -- so it goes to a
        thread.
        """
        lines = []
        for name in targets:
            try:
                result = await asyncio.to_thread(self.action_fn, action, name, opts or {})
                if result.get("error"):
                    lines.append(f":rotating_light: **{name}** - "
                                 f"{result.get('message', result['error'])}")
                else:
                    job = (result.get("job") or {}).get("id")
                    lines.append(f":white_check_mark: **{name}** - {action} started"
                                 + (f" (job `{job}`)" if job else ""))
            except Exception as exc:  # noqa: BLE001
                lines.append(f":rotating_light: **{name}** - {exc}")
        return lines

    def control_embed(self, online=None, problems=None) -> "discord.Embed":
        """The panel's own message: what is running, and what the buttons do."""
        servers = self.servers_fn() or []
        status = {}
        if self.status_fn:
            try:
                status = self.status_fn() or {}
            except Exception as exc:  # noqa: BLE001
                # Not fatal -- the panel still posts, with the maps shown as
                # unknown. But it must not fail silently, or the embed quietly
                # stops reflecting reality and looks merely stale.
                self.log(f"[discord] {self.name}: control embed could not read "
                         f"cluster status: {exc}")
                status = {}
        by_name = {s.get("name"): s for s in (status.get("servers") or [])}

        embed = discord.Embed(
            title=":video_game: Cluster Controls",
            description=("Buttons act immediately. Anything that takes a server "
                         "away from players asks to confirm first.\n"
                         "Only the owner and any configured admin roles can use these."),
            colour=COLOUR_INFO)

        lines = []
        for server in servers:
            live = by_name.get(server["name"], {})
            state = live.get("state", "unknown")
            mark = {"online": ":green_circle:", "offline": ":red_circle:"}.get(
                state, ":grey_question:")
            label = server.get("display_name", server["name"])
            extra = f" - up {live.get('uptime')}" if live.get("uptime") else ""
            lines.append(f"{mark} **{label}** - {state}{extra}")
        embed.add_field(name="Maps", value="\n".join(lines) or "none", inline=False)

        # Who is on, so a stuck login is visible without pressing anything.
        if online is not None:
            who = "\n".join(f":bust_in_silhouette: **{p['name']}** on {label}"
                            for _n, label, p in online) or "nobody connected"
            if problems:
                who += "\n" + "\n".join(f":warning: {p}" for p in problems)
            embed.add_field(name="Online now", value=who[:1024], inline=False)

        embed.set_footer(text="ClusterForge - press Refresh status to update this message")
        embed.timestamp = discord.utils.utcnow()
        return embed

    async def handle_control(self, interaction, action: str, target: str, label: str):
        """One button press."""
        who = getattr(interaction.user, "display_name", interaction.user)
        if not self._authorised(interaction):
            # Logged: a refused press is worth knowing about, because it is
            # either a permissions gap to fix or somebody trying it on.
            self.log(f"[discord] {self.name}: REFUSED {action} {target} "
                     f"for {who} ({interaction.user.id})")
            return await interaction.response.send_message(
                "You are not authorised to use the cluster controls.", ephemeral=True)
        if action not in ("refresh", "pal_refresh", "game_refresh"):
            self.log(f"[discord] {self.name}: {who} ({interaction.user.id}) "
                     f"pressed {action} on {target}")

        if action.startswith("game_"):
            return await self.handle_game(interaction, action, target)

        if action == "refresh":
            try:
                online, problems = await self.online_now() if self.players_fn else (None, None)
                await interaction.response.edit_message(
                    embed=self.control_embed(online, problems))
            except Exception as exc:  # noqa: BLE001
                await interaction.response.send_message(
                    f"Could not refresh: {exc}", ephemeral=True)
            return

        if action == "unstick":
            return await self.start_unstick(interaction, target)

        if action == "players":
            return await self.show_online(interaction)

        if action == "rescue":
            return await self.start_rescue(interaction)

        if action in ("to_palworld", "to_ark"):
            # Kept so old messages still route somewhere honest rather than
            # failing silently; the buttons themselves are gone.
            return await interaction.response.send_message(
                "That button is retired - Palworld moved to its own machine, so "
                "swapping games no longer does anything. Use the Start / Stop "
                "buttons on the Palworld post.", ephemeral=True)

        if action in ("pal_start", "pal_stop", "pal_restart", "pal_save"):
            return await self.handle_palworld(interaction, action)

        if action == "pal_refresh":
            # Same idea as "refresh", but this button sits under the Palworld
            # post and must redraw THAT embed, not the cluster one.
            try:
                state = await asyncio.to_thread(self.palworld_fn)
                await interaction.response.edit_message(
                    embed=palworld_embed(state,
                                         state.get("display_name", "Palworld")),
                    view=PalworldView(self, state, self.ark_label(state)))
            except Exception as exc:  # noqa: BLE001
                await interaction.response.send_message(
                    f"Could not refresh: {exc}", ephemeral=True)
            return

        skipped: list = []
        if target == "all":
            names, skipped = await asyncio.to_thread(self.all_targets, action)
            pretty = ", ".join(self.map_label(n) for n in names) or "no maps"
            if not names and action in ALL_FILTERED:
                why = ("every map is already running" if action == "start"
                       else "no map is running")
                return await interaction.response.send_message(
                    f"Nothing to {action} - {why}.", ephemeral=True)
        else:
            names, pretty = [target], self.map_label(target)
        left_alone = (f"\nLeft alone: {', '.join(self.map_label(n) for n in skipped)}"
                      f" ({'already running' if action == 'start' else 'not running'})"
                      if skipped else "")

        async def run(inter):
            if action == "update":
                try:
                    result = await asyncio.to_thread(self.update_fn, False)
                except Exception as exc:  # noqa: BLE001
                    return await inter.followup.send(f"Update failed: {exc}", ephemeral=True)
                ok = result.get("ok")
                await inter.followup.send(
                    embed=discord.Embed(
                        title="Update",
                        description=(str(result.get("message"))
                                     if not ok else "Update started - progress in the panel."),
                        colour=COLOUR_OK if ok else COLOUR_WARN),
                    ephemeral=True)
                return

            lines = await self._dispatch(action, names)
            if left_alone:
                lines.append(left_alone.strip())
            # Public, not ephemeral: when someone stops a server the rest of the
            # admin team should be able to see that it was deliberate and who
            # did it, rather than wondering why the map went down.
            embed = discord.Embed(
                title=f"{action.title()} - {pretty}",
                description="\n".join(lines), colour=COLOUR_INFO)
            embed.set_footer(text=f"Requested by {inter.user.display_name} - "
                                  "progress in the panel")
            await inter.followup.send(embed=embed)

        if action in DESTRUCTIVE_ACTIONS:
            warn = ("This warns players and counts down before it acts."
                    if action in ("stop", "restart") else
                    "This patches the cluster.")
            await interaction.response.send_message(
                f"**{label}** on **{pretty}**?\n{warn}{left_alone}",
                view=ConfirmView(run, f"{label} - {pretty}"), ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        await run(interaction)

    def map_label(self, name: str) -> str:
        for s in self.servers_fn() or []:
            if s.get("name") == name:
                return s.get("display_name", name)
        return name

    def all_targets(self, action: str) -> tuple[list, list]:
        """(act on, leave alone) for an "all" action (2026-11-12).

        Stop / restart / save touch only maps that are running and start only
        maps that are stopped. "Restart all" used to hit every map in config,
        and restarting a stopped map STARTS it - which brought back The Island
        that had been switched off on purpose. A map whose state is unknown
        (agent unreachable) is left alone too: guessing is how that happens."""
        names = [s["name"] for s in self.servers_fn() or []]
        if action not in ALL_FILTERED:
            return names, []
        try:
            status = (self.status_fn() if self.status_fn else {}) or {}
        except Exception as exc:  # noqa: BLE001
            self.log(f"[discord] {self.name}: could not read cluster status: {exc}")
            status = {}
        state = {s.get("name"): s.get("state") for s in status.get("servers") or []}
        want = "offline" if action == "start" else "online"
        hit = [n for n in names if state.get(n) == want]
        return hit, [n for n in names if n not in hit]

    async def online_now(self) -> tuple[list, list]:
        """(rows, problems) where each row is (server name, map label, {name, id}).

        One RCON round trip per map, all off the event loop.
        """
        rows, problems = [], []
        for server in self.servers_fn() or []:
            name = server["name"]
            label = server.get("display_name", name)
            try:
                found = await asyncio.to_thread(self.players_fn, name)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{label}: {exc}")
                continue
            if found is None:
                problems.append(f"{label}: could not ask (server down, or RCON failed)")
                continue
            rows.extend((name, label, player) for player in found)
        return rows, problems

    def _game_meta(self, game_id: str) -> tuple:
        meta = self.game_posts.get(game_id) or {}
        return meta.get("display_name", game_id.title()), meta.get("emoji", "🎮")

    async def _game_state(self, game_id: str) -> dict:
        try:
            return await asyncio.to_thread(self.game_status_fn, game_id) or {}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    async def handle_game(self, interaction, action: str, game_id: str):
        """A button on a game channel post. Same rules as Palworld's:
        stop/restart confirm first, results are posted publicly with who
        asked, and lifecycle work runs off the bot loop."""
        if game_id not in self.game_posts or not self.game_status_fn:
            return await interaction.response.send_message(
                "That game is no longer set up on the panel.", ephemeral=True)
        display, emoji = self._game_meta(game_id)

        if action == "game_refresh":
            state = await self._game_state(game_id)
            try:
                await interaction.response.edit_message(
                    embed=game_embed(state, display, emoji),
                    view=GameView(self, game_id, state))
            except Exception as exc:  # noqa: BLE001
                await interaction.response.send_message(
                    f"Could not refresh: {exc}", ephemeral=True)
            return

        verb = action[len("game_"):]
        if verb not in ("start", "stop", "restart") or not self.game_control_fn:
            return await interaction.response.send_message(
                "That control is not wired up on the panel.", ephemeral=True)

        async def run(inter):
            try:
                result = await asyncio.to_thread(self.game_control_fn, game_id, verb)
            except Exception as exc:  # noqa: BLE001
                result = {"error": str(exc)}
            if (result or {}).get("error"):
                return await inter.followup.send(
                    f"Could not {verb} {display}: "
                    f"{result.get('message') or result['error']}", ephemeral=True)
            who = inter.user.display_name
            self.log(f"[discord] {self.name}: {who} pressed {verb} on {display}")
            blurb = {"start": "Starting. The status above updates within a minute.",
                     "stop": "Stopping - it saves the world and shuts down properly.",
                     "restart": "Restarting - it saves and shuts down before coming back."}[verb]
            embed = discord.Embed(title=f"{emoji} {display} - {verb}",
                                  description=blurb, colour=COLOUR_INFO)
            embed.set_footer(text=f"Requested by {who}")
            msg = await inter.followup.send(embed=embed, wait=True)
            self.tidy_notice(msg, verb, display, emoji, who,
                             lambda: self.game_status_fn(game_id))

        if action in DESTRUCTIVE_ACTIONS:
            await interaction.response.send_message(
                f"**{verb.title()}** {display}?\nAnyone playing is disconnected; "
                "the world is saved first.",
                view=ConfirmView(run, f"{verb.title()} {display}"), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await run(interaction)

    def publish_game(self, game_id: str, channel_id: int, info_chunks: list,
                     timeout: float = 30) -> dict:
        """Fill a game channel: how-to-join post first, live embed LAST.

        Same shape and same edit-never-duplicate discipline as
        publish_palworld; a new embed is posted only when Discord says the
        old one is genuinely gone.
        """
        if not (self.bot and self.loop and self.ready):
            return {"ok": False, "error": "guardian_offline"}
        info = self.sync_channel(int(channel_id), info_chunks, timeout=timeout)
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error"), "info": info}
        display, emoji = self._game_meta(game_id)

        async def run():
            channel = self.bot.get_channel(int(channel_id)) or \
                await self.bot.fetch_channel(int(channel_id))
            state = await self._game_state(game_id)
            embed, view = game_embed(state, display, emoji), GameView(self, game_id, state)
            known = self.game_messages.get(game_id) or {}
            if known.get("message") and str(known.get("channel")) == str(channel_id):
                try:
                    await channel.get_partial_message(int(known["message"])).edit(
                        embed=embed, view=view)
                    return {"edited": True, "message_id": known["message"]}
                except discord.NotFound:
                    pass
            posted = await channel.send(embed=embed, view=view)
            self.game_messages[game_id] = {"channel": str(channel_id),
                                           "message": str(posted.id)}
            self._save_state(game_messages=self.game_messages)
            return {"edited": False, "message_id": str(posted.id)}

        try:
            out = asyncio.run_coroutine_threadsafe(run(), self.loop).result(timeout=timeout)
            out.update(ok=True, info=info)
            return out
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "info": info}

    async def _maintain_game_posts(self, refresh_seconds: int = 60):
        """Keep every game channel's embed (and Palworld's) live.

        Edits only when what the embed shows has changed, and never posts:
        a missing message is left for the next publish to replace, so a
        transient error can never start a second embed. The buttons are
        redrawn with the embed, so they follow the server's real state.
        """
        await self.bot.wait_until_ready()
        last: dict = {}
        while not self.bot.is_closed():
            await asyncio.sleep(refresh_seconds)
            jobs = []
            for gid, known in list(self.game_messages.items()):
                if gid in self.game_posts and known.get("message"):
                    jobs.append((gid, int(known["channel"]), int(known["message"])))
            if self.palworld_fn and self.palworld_message_id and self.palworld_channel_id:
                jobs.append(("palworld", int(self.palworld_channel_id),
                             int(self.palworld_message_id)))
            for gid, channel_id, message_id in jobs:
                try:
                    if gid == "palworld":
                        state = await asyncio.to_thread(self.palworld_fn) or {}
                        embed = palworld_embed(state, state.get("display_name", "Palworld"))
                        view = PalworldView(self, state, self.ark_label(state))
                    else:
                        state = await self._game_state(gid)
                        display, emoji = self._game_meta(gid)
                        embed, view = game_embed(state, display, emoji), GameView(self, gid, state)
                    sig = _embed_signature(embed) + repr(bool(state.get("running")))
                    if last.get(gid) == sig:
                        continue
                    channel = self.bot.get_channel(channel_id) or \
                        await self.bot.fetch_channel(channel_id)
                    await channel.get_partial_message(message_id).edit(embed=embed, view=view)
                    last[gid] = sig
                except discord.NotFound:
                    self.log(f"[discord] {self.name}: {gid} post is gone - "
                             "it is re-posted on the next publish")
                    last.pop(gid, None)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"[discord] {self.name}: {gid} live update failed: {exc}")

    def tidy_notice(self, message, verb: str, display: str, emoji: str, who: str,
                    state_fn, timeout: float = 600, linger: float = 60) -> None:
        """Remove a public "X - restart / Requested by" notice once it is done
        (2026-11-12, the user's request: it sat there after the server was back).

        Polls the server until the action has really finished - restart:
        running with an uptime younger than the press; start: running; stop:
        not running; save: immediately - then edits the notice to say so and
        deletes it `linger` seconds later. If it never finishes the notice is
        edited to say that and LEFT, because that is the one worth seeing."""
        if message is None:
            return
        pressed = time.time()

        def finished(st: dict) -> bool:
            if not st or st.get("error"):
                return False
            up = bool(st.get("running"))
            if verb == "restart":
                return up and (st.get("uptime_seconds") or 0) < time.time() - pressed + 5
            if verb == "start":
                return up
            if verb == "stop":
                return not up
            return True

        done_text = {"restart": "is back online", "start": "is online",
                     "stop": "is stopped", "save": "was saved"}.get(verb, "is done")

        async def follow():
            ok = False
            try:
                while time.time() - pressed < timeout:
                    await asyncio.sleep(10 if verb != "save" else 1)
                    try:
                        st = await asyncio.to_thread(state_fn) or {}
                    except Exception:  # noqa: BLE001
                        st = {}
                    if finished(st):
                        ok = True
                        break
                if ok:
                    await message.edit(embed=discord.Embed(
                        title=f"{emoji} {display} - {verb}",
                        description=f":white_check_mark: {display} {done_text}.",
                        colour=COLOUR_OK).set_footer(text=f"Requested by {who}"))
                    await asyncio.sleep(linger)
                    await message.delete()
                else:
                    await message.edit(embed=discord.Embed(
                        title=f"{emoji} {display} - {verb}",
                        description=(f":warning: Not finished after {round(timeout / 60)} "
                                     "minutes - check the panel."),
                        colour=COLOUR_WARN).set_footer(text=f"Requested by {who}"))
            except discord.NotFound:
                pass                              # someone already deleted it
            except Exception as exc:  # noqa: BLE001
                self.log(f"[discord] {self.name}: could not tidy the {display} notice: {exc}")

        asyncio.ensure_future(follow())

    async def handle_palworld(self, interaction, action: str):
        """Start / stop / restart / save Palworld on whichever machine runs it.

        Stop and restart confirm first: a control panel in a chat channel is
        one stray phone tap from ending somebody's session. The result is
        posted publicly with who asked for it, like every other control here,
        so the admin team can see a server went down on purpose.
        """
        verb = {"pal_start": "start", "pal_stop": "stop",
                "pal_restart": "restart", "pal_save": "save"}[action]

        if not self.palworld_control_fn:
            return await interaction.response.send_message(
                "Palworld control is not wired up on the panel.", ephemeral=True)

        async def run(inter):
            try:
                result = await asyncio.to_thread(self.palworld_control_fn, verb)
            except Exception as exc:  # noqa: BLE001
                return await inter.followup.send(
                    f"Could not {verb} Palworld: {exc}", ephemeral=True)
            if result.get("error"):
                return await inter.followup.send(
                    f"Could not {verb} Palworld: "
                    f"{result.get('message') or result['error']}", ephemeral=True)

            who = inter.user.display_name
            self.log(f"[discord] {self.name}: {who} pressed {verb} on Palworld")
            blurb = {
                "start": "Starting. Give it a minute, then press **Refresh**.",
                "stop": "Stopping - it saves and shuts down properly first.",
                "restart": "Restarting - it saves and shuts down before coming back.",
                "save": "Saved and snapshotted.",
            }[verb]
            embed = discord.Embed(title=f"🐾 Palworld - {verb}",
                                  description=blurb, colour=COLOUR_INFO)
            embed.set_footer(text=f"Requested by {who}")
            msg = await inter.followup.send(embed=embed, wait=True)
            self.tidy_notice(msg, verb, "Palworld", "🐾", who, self.palworld_fn)

        if action in DESTRUCTIVE_ACTIONS:
            await interaction.response.send_message(
                f"**{verb.title()}** Palworld?\nIt warns players and saves "
                "before it shuts down.",
                view=ConfirmView(run, f"{verb.title()} Palworld"), ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        await run(interaction)

    async def start_switch(self, interaction, target: str):
        """Swap which game owns PC1's memory.

        Confirms first. This stops a running server, and doing that because
        somebody's thumb found the wrong button would drop whoever is playing.
        """
        label = "Palworld" if target == "palworld" else "the ARK Island"
        other = "the ARK Island" if target == "palworld" else "Palworld"
        await interaction.response.send_message(
            f":arrows_counterclockwise: Switch to **{label}**?\n"
            f"This stops **{other}** first - they cannot both run, there is only "
            "enough memory on that PC for one.\n"
            "It takes a few minutes: the old one saves and shuts down properly "
            "before the new one starts.",
            view=ConfirmView(self, lambda inter: self._run_switch(inter, target)),
            ephemeral=True)

    async def _run_switch(self, interaction, target: str):
        result = await asyncio.to_thread(self.switch_fn, target)
        if result.get("error"):
            return await interaction.followup.send(
                f"Could not start the switch: {result.get('message', result['error'])}",
                ephemeral=True)
        label = "Palworld" if target == "palworld" else "the ARK Island"
        who = interaction.user.display_name
        self.log(f"[discord] {self.name}: {who} switched to {target}")
        await interaction.followup.send(embed=discord.Embed(
            title=f":arrows_counterclockwise: Switching to {label}",
            description=(
                "Under way now. The old server is saving and shutting down, then "
                f"{label} starts.\n\nGive it a few minutes, then press **Refresh "
                "status**. Palworld also takes a minute to appear in the in-game "
                "server list after it starts."),
            colour=COLOUR_INFO).set_footer(text=f"Requested by {who}"))

    async def show_online(self, interaction):
        """Who the servers think is connected, with a Disconnect button each.

        A crashed console leaves its login held for several minutes, and the
        player cannot rejoin until it clears - this is the manual clear.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not (self.players_fn and self.kick_fn):
            return await interaction.followup.send(
                "Player controls are not wired up on this panel.", ephemeral=True)
        rows, problems = await self.online_now()
        if not rows:
            text = "Nobody is connected to any map right now."
            if problems:
                text += "\n\n" + "\n".join(f":warning: {p}" for p in problems)
            return await interaction.followup.send(text, ephemeral=True)

        listing = "\n".join(f"• **{p['name']}** on {label}" for _n, label, p in rows)
        note = ("\nDisconnect drops that player from the map. Use it when someone has crashed "
                "and the server still shows them online - they can rejoin immediately afterwards.")
        if problems:
            note += "\n\n" + "\n".join(f":warning: {p}" for p in problems)
        await interaction.followup.send(f"**Connected right now**\n{listing}\n{note}",
                                        view=OnlineView(self, rows), ephemeral=True)

    async def confirm_disconnect(self, interaction, server: str, label: str, player: dict):
        """Second press before anyone is dropped."""
        async def run(inter):
            result = await asyncio.to_thread(self.kick_fn, server, player["id"])
            who = inter.user.display_name
            if result.get("ok"):
                self.log(f"[discord] {self.name}: {who} disconnected {player['name']} from {server}")
                await inter.followup.send(embed=discord.Embed(
                    title=f"Disconnected - {label}",
                    description=f":electric_plug: **{player['name']}** was dropped from **{label}** "
                                "and can rejoin now.",
                    colour=COLOUR_INFO).set_footer(text=f"Requested by {who}"))
            else:
                await inter.followup.send(
                    f"Could not disconnect **{player['name']}**: "
                    f"{result.get('message', result.get('error'))}", ephemeral=True)

        await interaction.response.send_message(
            f"Disconnect **{player['name']}** from **{label}**?\n"
            "Only do this if they have crashed or are stuck - it drops them immediately.",
            view=ConfirmView(run, f"Disconnect {player['name']}"), ephemeral=True)

    async def start_rescue(self, interaction):
        """List characters that have a saved copy of their transfer file."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not (self.characters_fn and self.rescue_fn):
            return await interaction.followup.send(
                "Character rescue is not wired up on this panel.", ephemeral=True)
        try:
            characters = await asyncio.to_thread(self.characters_fn)
        except Exception as exc:  # noqa: BLE001
            return await interaction.followup.send(f"Could not read the saved copies: {exc}",
                                                   ephemeral=True)
        usable = [c for c in (characters or []) if any(v["size"] > 0 for v in c.get("versions", []))]
        if not usable:
            return await interaction.followup.send(
                "No saved character copies yet. One is kept every time somebody uploads at an "
                "obelisk.", ephemeral=True)
        await interaction.followup.send(
            "**Rescue a character**\nFor when a crash during a transfer leaves someone with no "
            "character on any map. This puts a saved copy back in the transfer area; they then "
            "download it at an obelisk.\n"
            ":warning: The copy is from a moment in time - anything gained since is not in it.",
            view=RescueView(self, usable), ephemeral=True)

    async def confirm_rescue(self, interaction, account_id: str):
        if account_id == "none":
            return await interaction.response.send_message("Nothing to restore.", ephemeral=True)
        characters = await asyncio.to_thread(self.characters_fn)
        character = next((c for c in (characters or []) if c["id"] == account_id), None)
        if not character:
            return await interaction.response.send_message(
                "That character is no longer listed - try again.", ephemeral=True)
        good = [v for v in character.get("versions", []) if v["size"] > 0]
        newest = good[0]
        who = character.get("name") or account_id[:12]
        when = str(newest["when"])[:16].replace("T", " ")

        async def run(inter):
            result = await asyncio.to_thread(self.rescue_fn, account_id, newest["name"])
            if result.get("ok"):
                self.log(f"[discord] {self.name}: {inter.user.display_name} restored {who} "
                         f"from {newest['name']}")
                await inter.followup.send(embed=discord.Embed(
                    title="Character restored",
                    description=(f":ambulance: **{who}**'s character from **{when}** is waiting in "
                                 "the transfer area. Download it at any obelisk.\n"
                                 "It is an older copy, so items or tames gained since then are not "
                                 "in it."),
                    colour=COLOUR_OK).set_footer(text=f"Requested by {inter.user.display_name}"))
            else:
                await inter.followup.send(
                    f"Not restored: {result.get('message', result.get('error'))}", ephemeral=True)

        await interaction.response.send_message(
            f"Put **{who}**'s character from **{when}** "
            f"({newest['size'] // 1024} KB) back into the transfer area?\n"
            "They must be fully out of the game first, or ARK can end up with two of them.",
            view=ConfirmView(run, f"Rescue {who}"), ephemeral=True)

    async def show_server_controls(self, interaction, name: str):
        """The per-map buttons, shown to whoever picked the map from the panel."""
        label = self._server_label(name)
        if not self._authorised(interaction):
            who = getattr(interaction.user, "display_name", interaction.user)
            self.log(f"[discord] {self.name}: REFUSED controls for {name} "
                     f"for {who} ({interaction.user.id})")
            return await interaction.response.send_message(
                "You are not authorised to use the cluster controls.", ephemeral=True)
        await interaction.response.send_message(
            f"Controls for **{label}**. Stop and Restart ask to confirm.",
            view=ServerControlView(self, name, label), ephemeral=True)

    def _server_label(self, name: str) -> str:
        for server in self.servers_fn() or []:
            if server.get("name") == name:
                return server.get("display_name", name)
        return name

    async def start_unstick(self, interaction, target: str):
        """'Clear login': list who the server thinks is logged in.

        After an Xbox crashes mid-join the server keeps that login for minutes,
        and rejoining fails with 'host timeout' while every status shows no
        players. Kicking the held login lets the player straight back in.
        """
        label = self._server_label(target)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not (self.players_fn and self.kick_fn):
            return await interaction.followup.send(
                "Clear login is not wired up on this panel.", ephemeral=True)
        players = await asyncio.to_thread(self.players_fn, target)
        if players is None:
            return await interaction.followup.send(
                f"Could not ask **{label}** who is logged in (the RCON query failed). "
                "Is the server running?", ephemeral=True)
        if not players:
            # The stuck login that matters most is the one RCON will not admit
            # to. A crashed console can hold a session the server honours on
            # join but leaves out of ListPlayers, and the old version answered
            # "nobody is logged in, wait a minute" - which is the one thing that
            # does not help. Offer whoever this server saw recently instead.
            recent = await asyncio.to_thread(self.recent_fn, target) \
                if self.recent_fn else []
            if not recent:
                return await interaction.followup.send(
                    f"Nobody is logged in to **{label}** and nobody has played there "
                    "recently, so there is no login to clear.", ephemeral=True)
            return await interaction.followup.send(
                f"**{label}** reports nobody logged in - but a crashed console can "
                "hold a session the server does not list, which is what makes a "
                "rejoin time out.\n\nPick who is stuck and I will clear it anyway.",
                view=UnstickView(self, target, label, recent, force=True),
                ephemeral=True)
        await interaction.followup.send(
            f"Logged in to **{label}** right now. Pick the stuck one to clear.\n"
            "Only clear someone who is NOT actually playing - a real player gets kicked too.",
            view=UnstickView(self, target, label, players), ephemeral=True)

    async def finish_unstick(self, interaction, target: str, label: str,
                             account_id: str, view, force: bool = False):
        for item in view.children:
            item.disabled = True
        await interaction.response.edit_message(content=f"Clearing login on **{label}**...",
                                                view=view)
        result = await asyncio.to_thread(self.kick_fn, target, account_id, force)
        who = interaction.user.display_name
        if result.get("ok"):
            await interaction.edit_original_response(
                content=f"Cleared **{result.get('name')}** on **{label}**. They can join now.",
                view=view)
            # Public, like stop/restart: a kick should never look unexplained.
            embed = discord.Embed(
                title=f"Clear login - {label}",
                description=f":broom: **{result.get('name')}** was logged out so they can rejoin.",
                colour=COLOUR_INFO)
            embed.set_footer(text=f"Requested by {who}")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.edit_original_response(
                content=f"Not cleared: {result.get('message', result.get('error'))}", view=view)
        view.stop()

    def publish_controls(self, channel_id: int, timeout: float = 30) -> dict:
        """Post the control panel, or edit the one already there.

        Edits rather than reposting for the same reason the watcher does: a
        channel that accumulates dead control panels is one where somebody
        eventually presses a stale set of buttons.
        """
        if not (self.bot and self.loop and self.ready):
            return {"ok": False, "error": "guardian_offline",
                    "message": "Guardian is not connected, so nothing was posted."}

        async def run():
            channel = self.bot.get_channel(int(channel_id)) or \
                await self.bot.fetch_channel(int(channel_id))
            online, problems = await self.online_now() if self.players_fn else (None, None)
            embed = self.control_embed(online, problems)
            view = ControlPanelView(self, self.servers_fn() or [])

            if self.control_message_id:
                try:
                    await channel.get_partial_message(
                        self.control_message_id).edit(embed=embed, view=view)
                    return {"ok": True, "edited": True,
                            "message_id": self.control_message_id}
                except discord.NotFound:
                    self.control_message_id = None

            posted = await channel.send(embed=embed, view=view)
            self.control_message_id = posted.id
            self._save_control_id(posted.id, int(channel_id))
            return {"ok": True, "edited": False, "message_id": posted.id}

        try:
            future = asyncio.run_coroutine_threadsafe(run(), self.loop)
            return future.result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def list_channels(self, timeout: float = 15) -> dict:
        """Text channels Guardian can actually post in.

        So nobody has to turn on Developer Mode and copy an 18-digit id by
        hand. It reports what the bot can see rather than what exists, which
        also makes it the quickest way to spot the usual mistake: a private
        channel the bot was never added to simply is not in the list.
        """
        if not (self.bot and self.loop and self.ready):
            return {"ok": False, "error": "guardian_offline",
                    "message": "Guardian is not connected."}

        async def run():
            out = []
            for guild in self.bot.guilds:
                me = guild.me
                for channel in guild.text_channels:
                    perms = channel.permissions_for(me)
                    if not (perms.view_channel and perms.send_messages):
                        continue
                    out.append({
                        "id": str(channel.id),
                        "name": channel.name,
                        "guild": guild.name,
                        "category": channel.category.name if channel.category else "",
                        # Editing posts in place needs history; without it the
                        # bot can only ever append, so the panel warns rather
                        # than letting someone pick it and wonder why the
                        # channel fills up with duplicates.
                        "can_edit_in_place": bool(perms.read_message_history),
                        "private": not channel.permissions_for(
                            guild.default_role).view_channel,
                    })
            out.sort(key=lambda c: (c["guild"], c["category"], c["name"]))
            return out

        try:
            future = asyncio.run_coroutine_threadsafe(run(), self.loop)
            return {"ok": True, "channels": future.result(timeout=timeout)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def ark_label(self, state: dict | None = None) -> str:
        """Display name of the map Palworld swaps with, for the button text.

        "Switch to The Island" beats "Switch to ARK Island" when the server is
        actually called The Island, and it follows if the swap target ever
        changes.

        Takes the state the caller already fetched. Looking it up again here
        would mean a second blocking HTTP call - on the event loop, because
        every caller is a coroutine.
        """
        try:
            wanted = (state or {}).get("swap_with")
            for server in (self.servers_fn() or []):
                if server.get("name") == wanted:
                    return server.get("display_name") or wanted
            return wanted or "ARK Island"
        except Exception:  # noqa: BLE001
            return "ARK Island"

    def publish_palworld(self, channel_id: int, info_chunks: list,
                         timeout: float = 30) -> dict:
        """Fill the Palworld channel: the how-to-join post, then the controls.

        Two parts, because they age differently. The info post is prose that
        only changes when the password or the port does, so it goes through
        sync_channel and is edited in place. The status embed changes every
        minute and carries the buttons, so it stays LAST - a control panel
        that scrolls up above a wall of text is one nobody finds.
        """
        if not (self.bot and self.loop and self.ready):
            return {"ok": False, "error": "guardian_offline",
                    "message": "Guardian is not connected, so nothing was posted."}

        info = self.sync_channel(int(channel_id), info_chunks, timeout=timeout)
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error"), "info": info}

        async def run():
            channel = self.bot.get_channel(int(channel_id)) or \
                await self.bot.fetch_channel(int(channel_id))
            state = (await asyncio.to_thread(self.palworld_fn)
                     if self.palworld_fn else {}) or {}
            embed = palworld_embed(state, state.get("display_name", "Palworld"))
            view = PalworldView(self, state, self.ark_label(state))

            if self.palworld_message_id:
                try:
                    await channel.get_partial_message(
                        self.palworld_message_id).edit(embed=embed, view=view)
                    return {"edited": True, "message_id": self.palworld_message_id}
                except discord.NotFound:
                    self.palworld_message_id = None

            posted = await channel.send(embed=embed, view=view)
            self.palworld_message_id = posted.id
            self._save_state(palworld_message_id=posted.id,
                             palworld_channel_id=int(channel_id))
            return {"edited": False, "message_id": posted.id}

        try:
            future = asyncio.run_coroutine_threadsafe(run(), self.loop)
            out = future.result(timeout=timeout)
            out["ok"] = True
            out["info"] = info
            return out
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "info": info}

    def build(self):
        intents = discord.Intents.default()
        bot = discord.Client(intents=intents)
        tree = app_commands.CommandTree(bot)
        bot.tree = tree
        outer = self

        @bot.event
        async def on_ready():
            outer.ready = True
            outer.log(f"[discord] {outer.name}: connected as {bot.user}")
            await outer._sync_to_guild(bot)
            # Re-register the control panel's buttons. Without this the panel
            # message survives a restart but its buttons do nothing -- Discord
            # only routes a component back to a view the process has registered.
            try:
                bot.add_view(ControlPanelView(outer, outer.servers_fn() or []))
                if outer.palworld_fn:
                    bot.add_view(PalworldView(outer))
                for gid in outer.game_posts:
                    bot.add_view(GameView(outer, gid))
                outer.log(f"[discord] {outer.name}: control panel buttons registered")
                if not getattr(outer, "_games_started", False):
                    outer._games_started = True
                    bot.loop.create_task(outer._maintain_game_posts())
            except Exception as exc:  # noqa: BLE001
                outer.log(f"[discord] {outer.name}: could not register controls: {exc}")

            # Guarded the same way the watcher's is: a reconnect must not start
            # a second loop, which would double the edit rate on one message.
            try:
                if outer.remote_status and not getattr(outer, "_remote_started", False):
                    outer._remote_started = True
                    bot.loop.create_task(maintain_live_message(
                        bot, outer.remote_status.channel_id,
                        outer.remote_status.build_embed,
                        outer.remote_status.refresh_seconds,
                        outer.remote_status, outer.log,
                        f"{outer.name} ({outer.remote_status.label})"))
                    outer.log(f"[discord] {outer.name}: live status for "
                              f"{outer.remote_status.label} started")
            except Exception as exc:  # noqa: BLE001
                outer.log(f"[discord] {outer.name}: could not register controls: {exc}")

        def choices():
            opts = [app_commands.Choice(name=s.get("display_name", s["name"]),
                                        value=s["name"])
                    for s in outer.servers_fn()]
            opts.append(app_commands.Choice(name="All maps (only those in the right state)",
                                            value="all"))
            return opts

        async def deny(interaction):
            await interaction.response.send_message(
                "Only the configured owner can run control commands.",
                ephemeral=True)

        async def run_action(interaction, action: str, target: str, **opts):
            if not outer._authorised(interaction):
                return await deny(interaction)
            await interaction.response.defer(thinking=True)

            skipped = []
            if target == "all":
                targets, skipped = await asyncio.to_thread(outer.all_targets, action)
            else:
                targets = [target]
            lines = []
            for name in targets:
                try:
                    result = outer.action_fn(action, name, opts)
                    if result.get("error"):
                        lines.append(f":rotating_light: **{name}** - {result.get('message', result['error'])}")
                    else:
                        job = (result.get("job") or {}).get("id")
                        lines.append(
                            f" **{name}**  {action} started"
                            + (f" (job `{job}`)" if job else ""))
                except Exception as exc:  # noqa: BLE001
                    lines.append(f":rotating_light: **{name}** - {exc}")
            if skipped:
                lines.append("Left alone: " + ", ".join(outer.map_label(n) for n in skipped)
                             + (" (already running)" if action == "start" else " (not running)"))
            if not lines:
                lines.append("Nothing to do.")

            embed = discord.Embed(title=f"{action.title()}",
                                  description="\n".join(lines),
                                  colour=COLOUR_INFO)
            embed.set_footer(text="Progress appears in the panel  ClusterForge")
            await interaction.followup.send(embed=embed)

        @tree.command(name="start", description="Start a server")
        @app_commands.describe(server="Which map, or both")
        @app_commands.choices(server=choices())
        async def start_cmd(interaction, server: app_commands.Choice[str]):
            await run_action(interaction, "start", server.value)

        @tree.command(name="stop", description="Stop a server (warns players first)")
        @app_commands.choices(server=choices())
        async def stop_cmd(interaction, server: app_commands.Choice[str]):
            await run_action(interaction, "stop", server.value)

        @tree.command(name="restart", description="Restart a server (warns players first)")
        @app_commands.choices(server=choices())
        async def restart_cmd(interaction, server: app_commands.Choice[str]):
            await run_action(interaction, "restart", server.value)

        @tree.command(name="save", description="Save the world now")
        @app_commands.choices(server=choices())
        async def save_cmd(interaction, server: app_commands.Choice[str]):
            await run_action(interaction, "save", server.value)

        @tree.command(name="backup", description="Take a backup now")
        @app_commands.choices(server=choices())
        async def backup_cmd(interaction, server: app_commands.Choice[str]):
            await run_action(interaction, "backup", server.value)

        @tree.command(name="update",
                      description="Check for a new build, and optionally patch the cluster")
        @app_commands.describe(apply="Actually patch. Leave off to just check.")
        async def update_cmd(interaction, apply: bool = False):
            if apply and not outer._authorised(interaction):
                return await deny(interaction)
            await interaction.response.defer(thinking=True)
            try:
                result = outer.update_fn(dry_run=not apply)
            except Exception as exc:  # noqa: BLE001
                return await interaction.followup.send(f"Update check failed: {exc}")

            if not apply:
                state = result.get("state") or result
                builds = state.get("builds") or {}
                needs = state.get("needs_update") or []
                lines = [f"**{k}**  build `{v}`" for k, v in builds.items()]
                if state.get("unreachable"):
                    lines.append(f":rotating_light: unreachable: {', '.join(state['unreachable'])}")
                if state.get("unknown"):
                    lines.append(f":warning: version unknown: {', '.join(state['unknown'])}")
                lines.append("")
                lines.append(":green_circle: Everything is up to date." if not needs
                             else f":arrow_up: Update available for: {', '.join(needs)}")
                if state.get("build_mismatch"):
                    lines.append(":rotating_light: The two maps are on DIFFERENT builds - "
                                 "transfers will not work until they match.")
                embed = discord.Embed(title="Cluster build check",
                                      description="\n".join(lines),
                                      colour=COLOUR_WARN if needs else COLOUR_OK)
                embed.set_footer(text="Run /update apply:True to patch  ClusterForge")
                return await interaction.followup.send(embed=embed)

            ok = result.get("ok")
            plan = result.get("plan") or {}
            desc = (f"Updating **{', '.join(plan.get('targets', []))}**\n"
                    f"{plan.get('reason', '')}") if ok else \
                   f" {result.get('message', result.get('error', 'refused'))}"
            embed = discord.Embed(title="Cluster update",
                                  description=desc,
                                  colour=COLOUR_INFO if ok else COLOUR_BAD)
            await interaction.followup.send(embed=embed)

        return bot

