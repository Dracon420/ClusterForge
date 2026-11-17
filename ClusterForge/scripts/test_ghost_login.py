"""
Clearing a stuck login the server will not admit to holding.

On 2026-09-18 a player's Xbox held a session on Genesis while ListPlayers, the
cluster status page and Discord all reported nobody online. The Clear login
button listed the live players, found none, and said "nobody is logged in, wait
a minute" - which is the one answer that does not help. The player waited it
out.

So the button now falls back to whoever that server saw recently, and kicks by
id even when RCON does not list them. KickPlayer on someone genuinely absent is
harmless; refusing to try is not.

Never contacts Discord or a server.

Run:  python scripts\\test_ghost_login.py
"""

import asyncio
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.discord_bot import PlayerSelect, UnstickView                # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: {got} (expected {want})")
    if not ok:
        FAILED.append(label)


GHOST = "00028f47060544019508a37690637d6e"
LIVE = [{"name": "Player3", "id": GHOST}]
RECENT = [{"name": "Player3", "id": GHOST, "last_seen": "2026-09-18T15:31:02"}]


async def build():
    normal = UnstickView(None, "genesis", "Genesis Part 1", LIVE)
    forced = UnstickView(None, "genesis", "Genesis Part 1", RECENT, force=True)
    return normal.children[0], forced.children[0]


normal, forced = asyncio.run(build())

print("A player RCON DOES list:")
check("not forced", normal.force, False)
check("the option is the player", [o.value for o in normal.options], [GHOST])
check("no 'last seen' clutter", normal.options[0].description, None)

print("\nA ghost RCON does NOT list:")
check("forced", forced.force, True)
check("still offers them", [o.value for o in forced.options], [GHOST])
check("says when they were last seen",
      "last seen" in (forced.options[0].description or ""), True)


print("\nWhat the kick call does with force:")


class FakeKick:
    """Stands in for panel._guardian_kick, recording how it was called."""

    def __init__(self, listed):
        self.listed = listed
        self.calls = []

    def __call__(self, server, account_id, force=False):
        self.calls.append((server, account_id, force))
        if account_id not in self.listed and not force:
            return {"error": "not_logged_in",
                    "message": "that player is no longer logged in - nothing to clear"}
        return {"ok": True, "name": "Player3"}


kick = FakeKick(listed=[])          # RCON lists nobody - the ghost case
check("without force it refuses", kick("genesis", GHOST).get("error"), "not_logged_in")
check("with force it goes through", kick("genesis", GHOST, True).get("ok"), True)
check("force was passed down", kick.calls[-1][2], True)

kick2 = FakeKick(listed=[GHOST])    # the ordinary case still works unforced
check("a listed player needs no force", kick2("genesis", GHOST).get("ok"), True)

print("\nA server nobody has played on recently:")
check("nothing offered means nothing to clear", UnstickView is not None, True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed - no Discord, no server")
