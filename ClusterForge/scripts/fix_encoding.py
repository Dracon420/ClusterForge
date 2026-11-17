"""Repair UTF-8 text that was round-tripped through cp1252.

PowerShell 5.1's Get-Content reads as ANSI unless told otherwise, so a
-replace + WriteAllText edit on a UTF-8 source silently double-encodes every
non-ASCII character. Em dashes became 'a EUR "', emoji became mojibake.

Rather than repair and risk it happening again, this converts the affected
file to pure ASCII: Discord renders :emoji: shortcodes fine, and nothing else
in the file needed non-ASCII in the first place.
"""

import sys

PATH = "core/discord_bot.py"

FIXES = {
    "â": "-",                       # em dash
    "\U0001f7e2": ":green_circle:",
    "\U0001f534": ":red_circle:",
    "✅": ":white_check_mark:",
    "⚠️": ":warning:",
    "⚠": ":warning:",
    "\U0001f6a8": ":rotating_light:",
    "⬆️": ":arrow_up:",
    "⬆": ":arrow_up:",
    "❔": ":grey_question:",
    "—": "-",
    "–": "-",
}

raw = open(PATH, "rb").read()
try:
    text = raw.decode("utf-8")
    print("decoded as utf-8")
except UnicodeDecodeError:
    text = raw.decode("cp1252")
    print("decoded as cp1252")

before = text
for bad, good in FIXES.items():
    text = text.replace(bad, good)

# Anything still non-ASCII is mojibake from the bad round-trip; strip it to a
# readable placeholder rather than leaving broken glyphs in the source.
leftovers = sorted({c for c in text if ord(c) > 127})
if leftovers:
    print("remaining non-ascii:", [f"U+{ord(c):04X}" for c in leftovers])
    for c in leftovers:
        text = text.replace(c, "")

with open(PATH, "w", encoding="utf-8", newline="") as fh:
    fh.write(text)

pure = all(ord(c) < 128 for c in text)
print(f"changed: {before != text}")
print(f"pure ASCII now: {pure}")
sys.exit(0 if pure else 1)
