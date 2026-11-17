"""
Line-preserving INI reader/writer for ARK config files.

Why this exists instead of configparser:

ARK's Game.ini legitimately repeats keys. The Island's file currently holds 42
ConfigOverrideItemMaxQuantity lines and 25 HarvestResourceItemAmountClassMultipliers
lines. These are NOT duplicates -- each line configures a different item or
resource for stack-size and harvest tuning. Any dict-based ini library keeps the
last one and silently discards the other 41 and 24, destroying the server's
economy config with no error.

So this module models a file as an ordered list of lines, each keeping its exact
original text, separator spacing and line terminator. Reading and writing back
with no edits must produce a byte-identical file -- see selftest_roundtrip().

Nothing here reorders, merges, deduplicates or "cleans" anything.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

# Encodings tried in order when there is no BOM. cp1252 never raises, so it acts
# as the terminal fallback and keeps byte values intact for round-tripping.
_FALLBACK_ENCODINGS = ("utf-8", "cp1252")

_BOMS = (
    ("utf-8-sig", b"\xef\xbb\xbf"),
    ("utf-16", b"\xff\xfe"),
    ("utf-16", b"\xfe\xff"),
)

KIND_SECTION = "section"
KIND_KEYVALUE = "keyvalue"
KIND_COMMENT = "comment"
KIND_BLANK = "blank"


@dataclass
class IniLine:
    """One physical line. `raw` excludes the terminator, `eol` is the exact
    terminator that followed it ('' for a final line with no trailing newline)."""

    kind: str
    raw: str
    eol: str
    key: str | None = None
    value: str | None = None
    section: str | None = None  # section this line belongs to ('' before any header)

    def render(self) -> str:
        return self.raw + self.eol

    def set_value(self, new_value: str) -> None:
        """Rewrite this line's value, preserving the original spacing around '='."""
        if self.kind != KIND_KEYVALUE:
            raise ValueError(f"cannot set a value on a {self.kind} line")
        head, sep, _ = self.raw.partition("=")
        if not sep:  # defensive; a keyvalue line always contains '='
            raise ValueError(f"malformed key/value line: {self.raw!r}")
        self.raw = f"{head}={new_value}"
        self.value = new_value


def _split_keep_eol(text: str) -> list[tuple[str, str]]:
    """Split into (content, terminator) pairs, splitting ONLY on \\r\\n, \\n, \\r.

    str.splitlines() also breaks on \\x0b, \\x0c, \\x1c-\\x1e, \\u2028 and \\u2029,
    which can appear inside ARK values (mod-authored strings, in particular) and
    would corrupt the file on write. This scanner does not.
    """
    out: list[tuple[str, str]] = []
    start = i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                out.append((text[start:i], "\r\n"))
                i += 2
            else:
                out.append((text[start:i], "\r"))
                i += 1
            start = i
        elif c == "\n":
            out.append((text[start:i], "\n"))
            i += 1
            start = i
        else:
            i += 1
    if start < n:
        out.append((text[start:], ""))
    return out


def _decode(data: bytes) -> tuple[str, str]:
    """Return (text, encoding_name), honouring a BOM when present."""
    for name, bom in _BOMS:
        if data.startswith(bom):
            return data.decode(name), name
    for name in _FALLBACK_ENCODINGS:
        try:
            return data.decode(name), name
        except UnicodeDecodeError:
            continue
    # Unreachable in practice: cp1252 decodes any byte string.
    return data.decode("cp1252", errors="replace"), "cp1252"


class IniFile:
    """An ARK ini file held as an ordered list of lines."""

    def __init__(self, path: str, lines: list[IniLine], encoding: str):
        self.path = path
        self.lines = lines
        self.encoding = encoding

    # ---------- loading / saving ----------

    @classmethod
    def load(cls, path: str) -> "IniFile":
        with open(path, "rb") as fh:
            data = fh.read()
        text, encoding = _decode(data)

        lines: list[IniLine] = []
        current_section = ""
        for content, eol in _split_keep_eol(text):
            stripped = content.strip()
            if not stripped:
                lines.append(IniLine(KIND_BLANK, content, eol, section=current_section))
            elif stripped[0] in ";#":
                lines.append(IniLine(KIND_COMMENT, content, eol, section=current_section))
            elif stripped.startswith("[") and stripped.endswith("]"):
                current_section = stripped[1:-1]
                lines.append(
                    IniLine(KIND_SECTION, content, eol, section=current_section)
                )
            elif "=" in content:
                key, _, value = content.partition("=")
                lines.append(
                    IniLine(
                        KIND_KEYVALUE,
                        content,
                        eol,
                        key=key.strip(),
                        value=value,
                        section=current_section,
                    )
                )
            else:
                # Not a comment, header or assignment. Keep it verbatim rather
                # than guessing -- dropping unknown lines is how configs get eaten.
                lines.append(IniLine(KIND_COMMENT, content, eol, section=current_section))
        return cls(path, lines, encoding)

    def render(self) -> str:
        return "".join(line.render() for line in self.lines)

    def to_bytes(self) -> bytes:
        return self.render().encode(self.encoding)

    def save(
        self,
        path: str | None = None,
        backup_dir: str | None = None,
    ) -> tuple[str | None, bool]:
        """Write the file back, preserving its read-only flag.

        Returns (backup_path, was_readonly).

        The read-only flag matters enormously here. Both servers keep Game.ini
        and GameUserSettings.ini read-only on purpose: ARK rewrites
        GameUserSettings.ini when it shuts down, discarding manual edits, and
        the read-only bit is what prevents that. DSM exposes this as a "Remove
        read-only" button.

        So the flag is cleared only for the instant of the write and then
        restored. Leaving it cleared would hand the next server shutdown
        permission to silently erase the operator's settings -- a far worse
        outcome than a failed save.
        """
        target = path or self.path
        exists = os.path.exists(target)
        was_readonly = exists and not os.access(target, os.W_OK)

        backup_path = None
        if backup_dir and exists:
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = os.path.join(
                backup_dir, f"{os.path.basename(target)}.{stamp}.bak"
            )
            shutil.copy2(target, backup_path)
            # copy2 carries the read-only bit across; a read-only backup is a
            # nuisance to restore from later.
            try:
                os.chmod(backup_path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass

        if was_readonly:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)

        try:
            tmp = target + ".arkpanel.tmp"
            with open(tmp, "wb") as fh:
                fh.write(self.to_bytes())
            os.replace(tmp, target)  # atomic on Windows for same-volume paths
        finally:
            if was_readonly:
                # Restore even if the write failed, so an error never leaves the
                # config exposed to being overwritten by the server.
                try:
                    os.chmod(target, stat.S_IREAD)
                except OSError:
                    pass

        return backup_path, was_readonly

    # ---------- reading ----------

    def sections(self) -> list[str]:
        """Section names in file order, including '' if keys precede any header."""
        seen: list[str] = []
        for line in self.lines:
            if line.section is not None and line.section not in seen:
                seen.append(line.section)
        return seen

    def entries(self, section: str | None = None) -> Iterator[IniLine]:
        for line in self.lines:
            if line.kind != KIND_KEYVALUE:
                continue
            if section is not None and line.section != section:
                continue
            yield line

    def get_all(self, section: str, key: str) -> list[IniLine]:
        """Every line for this key, in file order. The 42 stack-override lines
        come back as 42 entries, which is the whole point."""
        lk = key.lower()
        return [
            line
            for line in self.entries(section)
            if line.key and line.key.lower() == lk
        ]

    def get_one(self, section: str, key: str) -> IniLine | None:
        found = self.get_all(section, key)
        return found[0] if found else None

    def get_occurrence(self, section: str, key: str, occurrence: int) -> IniLine | None:
        """The Nth line for this key (1-based), in file order.

        This is how a single one of the 42 ConfigOverrideItemMaxQuantity lines
        gets edited without disturbing the other 41. Addressing by key alone
        would be ambiguous, which is exactly the ambiguity that makes
        dict-based parsers destroy these files.
        """
        found = self.get_all(section, key)
        if occurrence < 1 or occurrence > len(found):
            return None
        return found[occurrence - 1]

    def sha256(self) -> str:
        import hashlib
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def key_counts(self, section: str | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for line in self.entries(section):
            if line.key:
                counts[line.key] = counts.get(line.key, 0) + 1
        return counts

    # ---------- editing ----------

    def set_single(self, section: str, key: str, value: str) -> None:
        """Set a key that appears at most once. Refuses to touch repeated keys,
        because collapsing those is exactly the failure this module prevents."""
        existing = self.get_all(section, key)
        if len(existing) > 1:
            raise ValueError(
                f"{key} appears {len(existing)} times in [{section}]; "
                "edit the specific line instead of collapsing them"
            )
        if existing:
            existing[0].set_value(value)
        else:
            self._append_to_section(section, key, value)

    def _append_to_section(self, section: str, key: str, value: str) -> None:
        """Insert after the last line of the section, creating it if needed."""
        last_idx = None
        for idx, line in enumerate(self.lines):
            if line.section == section:
                last_idx = idx
        eol = self._dominant_eol()
        if last_idx is None:
            if self.lines and self.lines[-1].eol == "":
                self.lines[-1].eol = eol
            self.lines.append(IniLine(KIND_SECTION, f"[{section}]", eol, section=section))
            last_idx = len(self.lines) - 1
        # A file whose last line has no newline would otherwise get the new key
        # glued onto it. That happened for real: adding boss settings produced
        # "bAutoUnlockAllEngrams=TrueDinoClassDamageMultipliers=(...)", which
        # silently turned off automatic engram unlocking on two servers.
        if self.lines[last_idx].eol == "":
            self.lines[last_idx].eol = eol
        self.lines.insert(
            last_idx + 1,
            IniLine(
                KIND_KEYVALUE, f"{key}={value}", eol,
                key=key, value=value, section=section,
            ),
        )

    def remove_line(self, line: IniLine) -> None:
        self.lines.remove(line)

    def _dominant_eol(self) -> str:
        counts: dict[str, int] = {}
        for line in self.lines:
            if line.eol:
                counts[line.eol] = counts.get(line.eol, 0) + 1
        if not counts:
            return "\r\n"  # Windows server, sane default
        return max(counts, key=counts.get)


# ---------- applying edits ----------

class ChangeError(Exception):
    """A requested edit could not be applied exactly as specified."""


def apply_changes(ini: IniFile, changes: list[dict]) -> list[dict]:
    """Apply a list of edits, or raise without touching anything.

    Each change addresses one specific line by (section, key, occurrence), so a
    single one of the 42 stack-override lines can be edited in place. Every
    change is resolved to a line BEFORE any mutation happens: a request that
    references a line that no longer exists fails the whole batch rather than
    applying half of it.

    Supported ops:
        set    -- replace the value on that line
        delete -- remove that line entirely
        add    -- append a new key to a section
    """
    resolved: list[tuple[str, IniLine | None, dict]] = []

    for change in changes:
        op = (change.get("op") or "set").lower()
        section = change.get("section", "")
        key = change.get("key")
        if not key:
            raise ChangeError("change is missing 'key'")

        if op == "add":
            resolved.append(("add", None, change))
            continue

        occurrence = int(change.get("occurrence", 1))
        line = ini.get_occurrence(section, key, occurrence)
        if line is None:
            total = len(ini.get_all(section, key))
            raise ChangeError(
                f"{key} occurrence {occurrence} not found in [{section}] "
                f"(file has {total}). The file changed since it was loaded."
            )
        if op not in ("set", "delete"):
            raise ChangeError(f"unsupported op '{op}' for {key}")
        resolved.append((op, line, change))

    applied: list[dict] = []
    # Deletions last, so earlier occurrence indexes stay valid while setting.
    for op, line, change in sorted(resolved, key=lambda r: r[0] == "delete"):
        if op == "set":
            before = line.value
            line.set_value(str(change.get("value", "")))
            applied.append({"op": "set", "key": line.key, "section": line.section,
                            "from": before, "to": line.value})
        elif op == "delete":
            ini.remove_line(line)
            applied.append({"op": "delete", "key": line.key,
                            "section": line.section, "from": line.value})
        else:  # add
            ini._append_to_section(
                change.get("section", ""), change["key"], str(change.get("value", "")))
            applied.append({"op": "add", "key": change["key"],
                            "section": change.get("section", ""),
                            "to": change.get("value", "")})
    return applied


# ---------- the gate ----------

def selftest_roundtrip(path: str) -> tuple[bool, str]:
    """Load and re-render `path`, comparing against the original bytes.

    This is the gate the plan puts in front of all ini editing: if this does not
    pass on the real Game.ini and GameUserSettings.ini, no write path ships.
    """
    with open(path, "rb") as fh:
        original = fh.read()
    ini = IniFile.load(path)
    produced = ini.to_bytes()
    if produced == original:
        return True, f"OK  byte-identical  ({len(ini.lines)} lines, {ini.encoding})"

    for i, (a, b) in enumerate(zip(original, produced)):
        if a != b:
            lo = max(0, i - 40)
            return False, (
                f"FAIL diverges at byte {i}\n"
                f"  original: {original[lo:i+40]!r}\n"
                f"  produced: {produced[lo:i+40]!r}"
            )
    return False, (
        f"FAIL length differs: original {len(original)} vs produced {len(produced)}"
    )
