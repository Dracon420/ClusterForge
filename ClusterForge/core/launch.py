"""
Parse and regenerate ARK server launch command lines.

Same philosophy as core/inifile.py: keep every token's original text and
spacing, edit surgically, and never drop something just because we didn't
recognise it. An unrecognised flag that silently disappears is how a server
loses -preventHibernation (which Der Dino Finder needs) or -ServerPlatform=ALL
(which Xbox players need) without anyone noticing until they can't join.

The two servers deliberately quote their map segment differently:

  Island:  "TheIsland_WP"?SessionName="[ToDs]Dracon-The-Island" -preventHibernation ...
  Svart:   "Svartalfheim_WP?listen?ServerPVE=True?...?RCONPort=27020" -Port=8000 ...

Both must round-trip untouched, so the map segment keeps its raw form and is
only parsed for display.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Flags that carry a value we understand and surface in the UI. Everything else
# still round-trips, it just shows up as a generic flag.
KNOWN_VALUE_FLAGS = {
    "port", "queryport", "rconport", "winlivemaxplayers", "serverplatform",
    "clusterid", "clusterdiroverride", "mods", "culture", "maxplayers",
    "structurememopts", "automanagedmods",
}

MOD_FLAG_NAMES = {"mods", "gamemodids"}

# SessionName lives inside the map segment as ?SessionName="[ToDs]Dracon-X",
# not as a flag, so changing it is string surgery on that one token rather
# than a Flag edit. The value may or may not be quoted; both round-trip.
SESSION_NAME_RE = re.compile(r'(\?SessionName=)("[^"]*"|[^?\s]*)', re.IGNORECASE)


@dataclass
class Token:
    """One command-line token plus the whitespace that followed it."""

    raw: str
    space: str = " "

    def render(self) -> str:
        return self.raw + self.space


@dataclass
class Flag:
    token: Token
    name: str              # without the leading dash, original casing
    value: str | None      # None for bare switches like -usestore
    quoted: bool = False   # whether the value was wrapped in double quotes

    @property
    def key(self) -> str:
        return self.name.lower()

    def set_value(self, new_value: str) -> None:
        if self.value is None:
            raise ValueError(f"-{self.name} is a switch and takes no value")
        dash = self.token.raw[0]
        quote = '"' if self.quoted else ""
        self.token.raw = f"{dash}{self.name}={quote}{new_value}{quote}"
        self.value = new_value


def _tokenize(cmdline: str) -> list[Token]:
    """Split on whitespace that sits outside double quotes, keeping the exact
    separator so rendering is faithful."""
    tokens: list[Token] = []
    buf: list[str] = []
    in_quotes = False
    i, n = 0, len(cmdline)

    leading = ""
    while i < n and cmdline[i].isspace():
        leading += cmdline[i]
        i += 1

    while i < n:
        c = cmdline[i]
        if c == '"':
            in_quotes = not in_quotes
            buf.append(c)
            i += 1
        elif c.isspace() and not in_quotes:
            space = ""
            while i < n and cmdline[i].isspace():
                space += cmdline[i]
                i += 1
            tokens.append(Token("".join(buf), space))
            buf = []
        else:
            buf.append(c)
            i += 1
    if buf:
        tokens.append(Token("".join(buf), ""))
    if tokens and leading:
        tokens[0].raw = leading + tokens[0].raw
    return tokens


class LaunchConfig:
    """A parsed launch command line that can be rendered back verbatim."""

    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.map_token: Token | None = None
        self.flags: list[Flag] = []
        self._classify()

    @classmethod
    def parse(cls, cmdline: str) -> "LaunchConfig":
        return cls(_tokenize(cmdline.rstrip("\r\n")))

    def _classify(self) -> None:
        for token in self.tokens:
            raw = token.raw
            if raw.startswith("-") and len(raw) > 1:
                body = raw[1:]
                if "=" in body:
                    name, _, value = body.partition("=")
                    quoted = value.startswith('"') and value.endswith('"') and len(value) >= 2
                    self.flags.append(
                        Flag(token, name, value[1:-1] if quoted else value, quoted)
                    )
                else:
                    self.flags.append(Flag(token, body, None))
            elif self.map_token is None:
                self.map_token = token

    # ---------- rendering ----------

    def render(self) -> str:
        return "".join(t.render() for t in self.tokens).rstrip()

    # ---------- flag access ----------

    def get(self, name: str) -> Flag | None:
        key = name.lower()
        for flag in self.flags:
            if flag.key == key:
                return flag
        return None

    def get_value(self, name: str, default: str | None = None) -> str | None:
        flag = self.get(name)
        return flag.value if flag and flag.value is not None else default

    def resolve(self, name: str, default: str | None = None) -> str | None:
        """Look up a setting as a flag first, then in the map segment's query
        string.

        The two servers express the same settings differently: Island passes
        -RCONPort=27015 as a flag, Svartalfheim carries ?RCONPort=27020 inside
        its quoted map segment. Both are valid and both must resolve.
        """
        value = self.get_value(name)
        if value is not None:
            return value
        key = name.lower()
        for qk, qv in self.query_params():
            if qk.lower() == key:
                return qv
        return default

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def switches(self) -> list[str]:
        """Bare on/off flags, in order, e.g. usestore, preventHibernation."""
        return [f.name for f in self.flags if f.value is None]

    def remove_flag(self, name: str) -> bool:
        flag = self.get(name)
        if not flag:
            return False
        # Hand this token's trailing whitespace to its predecessor so spacing
        # stays sane and the line doesn't collapse.
        idx = self.tokens.index(flag.token)
        if idx > 0 and not flag.token.space:
            self.tokens[idx - 1].space = ""
        self.tokens.remove(flag.token)
        self.flags.remove(flag)
        return True

    def add_switch(self, name: str) -> None:
        if self.has(name):
            return
        if self.tokens and not self.tokens[-1].space:
            self.tokens[-1].space = " "
        token = Token(f"-{name}", "")
        self.tokens.append(token)
        self.flags.append(Flag(token, name, None))

    # ---------- map segment ----------

    @property
    def map_segment(self) -> str:
        return self.map_token.raw if self.map_token else ""

    @property
    def map_name(self) -> str:
        seg = self.map_segment.replace('"', "")
        return seg.split("?", 1)[0]

    def query_params(self) -> list[tuple[str, str]]:
        """The ?Key=Value pairs in the map segment, for display."""
        seg = self.map_segment.replace('"', "")
        parts = seg.split("?")[1:]
        out: list[tuple[str, str]] = []
        for part in parts:
            if not part:
                continue
            key, sep, value = part.partition("=")
            out.append((key, value if sep else ""))
        return out

    def set_map(self, map_name: str) -> str:
        """Swap the map, leaving the segment's quoting and every ?Key=Value
        setting exactly as it was. Returns the previous map name.

        Only the map name itself moves. Everything the segment carries --
        ?listen, ?ServerPVE, ?RCONPort on Svartalfheim -- belongs to the
        SERVER, not to the map, and losing one of them on a map switch would
        be a silent behaviour change nobody would connect to the rotation.
        """
        if self.map_token is None:
            raise ValueError("this launch line has no map segment to change")
        old = self.map_name
        if not old:
            raise ValueError("could not identify the current map name")

        raw = self.map_token.raw
        # The map name is the first thing in the segment, so the first match is
        # the right one even when SessionName echoes part of it.
        index = raw.find(old)
        if index < 0:
            raise ValueError(f"map name {old!r} not found in {raw!r}")
        self.map_token.raw = raw[:index] + map_name + raw[index + len(old):]
        return old

    def set_session_name(self, name: str) -> str | None:
        """Set the advertised server name. Returns the previous one, or None if
        the segment did not carry a SessionName at all."""
        if self.map_token is None:
            raise ValueError("this launch line has no map segment to change")

        raw = self.map_token.raw
        previous: str | None = None

        def swap(match):
            nonlocal previous
            previous = match.group(2).strip('"')
            quote = '"' if match.group(2).startswith('"') else ""
            return f"{match.group(1)}{quote}{name}{quote}"

        # A function replacement, not a string: a session name is user text and
        # a backslash in a re.sub template would be read as a group reference.
        new_raw, count = SESSION_NAME_RE.subn(swap, raw, count=1)
        if count:
            self.map_token.raw = new_raw
            return previous

        # Nothing to replace, so add one, matching the segment's own quoting.
        if raw.startswith('"') and raw.endswith('"') and raw.count('"') == 2:
            self.map_token.raw = f'{raw[:-1]}?SessionName={name}"'
        else:
            self.map_token.raw = f'{raw}?SessionName="{name}"'
        return None

    # ---------- mods ----------

    def _mod_flag(self) -> Flag | None:
        for flag in self.flags:
            if flag.key in MOD_FLAG_NAMES:
                return flag
        return None

    @property
    def mods(self) -> list[int]:
        """Mod IDs in load order. Order is significant -- leftmost wins."""
        flag = self._mod_flag()
        if not flag or not flag.value:
            return []
        return [int(m) for m in re.findall(r"\d+", flag.value)]

    def set_mods(self, mod_ids: list[int]) -> None:
        flag = self._mod_flag()
        joined = ",".join(str(m) for m in mod_ids)
        if flag:
            flag.set_value(joined)
        else:
            if self.tokens and not self.tokens[-1].space:
                self.tokens[-1].space = " "
            token = Token(f"-mods={joined}", "")
            self.tokens.append(token)
            self.flags.append(Flag(token, "mods", joined))

    # ---------- summary ----------

    def summary(self) -> dict:
        return {
            "map_name": self.map_name,
            "map_segment": self.map_segment,
            "query": self.query_params(),
            "port": self.resolve("port"),
            "rcon_port": self.resolve("RCONPort"),
            "max_players": self.resolve("WinLiveMaxPlayers"),
            "platform": self.resolve("ServerPlatform"),
            "cluster_id": self.resolve("clusterid"),
            "cluster_dir": self.resolve("ClusterDirOverride"),
            "culture": self.resolve("culture"),
            "session_name": self.resolve("SessionName"),
            "mods": self.mods,
            "mod_count": len(self.mods),
            "switches": self.switches(),
            "all_flags": [
                {"name": f.name, "value": f.value, "quoted": f.quoted}
                for f in self.flags
            ],
        }


def build_start_bat(exe_path: str, cmdline: str, header: str = "") -> str:
    """Emit a StartServer.bat that launches `exe_path` with `cmdline`.

    ClusterForge owns this file once it takes over, so it is generated from the
    launch model rather than hand-maintained -- which is what let it drift out
    of sync with reality in the first place.
    """
    lines = ["@echo off"]
    if header:
        lines.extend(f"REM {ln}" for ln in header.splitlines())
    lines.append(f'start "" "{exe_path}" {cmdline}')
    return "\r\n".join(lines) + "\r\n"


def extract_from_bat(text: str) -> tuple[str, str] | None:
    """Pull (exe_path, cmdline) out of an existing StartServer.bat."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("start "):
            continue
        rest = stripped[6:].lstrip()
        if rest.startswith('""'):
            rest = rest[2:].lstrip()
        elif rest.startswith('"'):  # a window title we don't care about
            end = rest.find('"', 1)
            if end != -1:
                rest = rest[end + 1:].lstrip()
        if not rest.startswith('"'):
            continue
        end = rest.find('"', 1)
        if end == -1:
            continue
        return rest[1:end], rest[end + 1:].strip()
    return None
