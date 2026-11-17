"""
Prove the Discord control buttons cannot be pressed by the wrong person.

These buttons stop game servers from a chat channel, so "who is allowed" is the
part worth testing rather than assuming. Checks the real _authorised against
fake interactions: the owner, an admin with a permitted role, a member with
some other role, a stranger, and a DM user with no roles at all.

Run:  python scripts\\test_control_auth.py
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.discord_bot import DESTRUCTIVE_ACTIONS, GuardianBot   # noqa: E402

OWNER = 000000000000000000
ADMIN_ROLE = 000000000000000000
OTHER_ROLE = 000000000000000000

FAILED = []


class FakeRole:
    def __init__(self, rid):
        self.id = rid


class FakeUser:
    """A guild Member has .roles; a DM User does not."""

    def __init__(self, uid, roles=None, has_roles_attr=True):
        self.id = uid
        if has_roles_attr:
            self.roles = [FakeRole(r) for r in (roles or [])]


class FakeInteraction:
    def __init__(self, user):
        self.user = user


def guardian(control_role_ids):
    return GuardianBot(
        name="test", token="x", channel_id=1, owner_id=OWNER,
        servers_fn=lambda: [], action_fn=lambda *a: {}, job_fn=lambda *a: {},
        update_fn=lambda *a: {}, control_role_ids=control_role_ids)


def check(label, got, want):
    ok = got is want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: allowed={got} (expected {want})")
    if not ok:
        FAILED.append(label)


print("Owner-only (control_role_ids empty) -- the shipped default:")
g = guardian([])
check("owner is allowed", g._authorised(FakeInteraction(FakeUser(OWNER))), True)
check("member with a role is denied",
      g._authorised(FakeInteraction(FakeUser(999, [ADMIN_ROLE]))), False)
check("stranger is denied", g._authorised(FakeInteraction(FakeUser(999))), False)

print("\nWith an admin role permitted:")
g = guardian([ADMIN_ROLE])
check("owner still allowed", g._authorised(FakeInteraction(FakeUser(OWNER))), True)
check("admin role allowed",
      g._authorised(FakeInteraction(FakeUser(999, [ADMIN_ROLE]))), True)
check("admin role among others allowed",
      g._authorised(FakeInteraction(FakeUser(999, [OTHER_ROLE, ADMIN_ROLE]))), True)
check("different role denied",
      g._authorised(FakeInteraction(FakeUser(999, [OTHER_ROLE]))), False)
check("no roles denied", g._authorised(FakeInteraction(FakeUser(999, []))), False)
check("DM user without .roles denied",
      g._authorised(FakeInteraction(FakeUser(999, has_roles_attr=False))), False)

print("\nNo owner configured (fails closed):")
g = GuardianBot(name="t", token="x", channel_id=1, owner_id=None,
                servers_fn=lambda: [], action_fn=lambda *a: {},
                job_fn=lambda *a: {}, update_fn=lambda *a: {})
check("nobody is allowed", g._authorised(FakeInteraction(FakeUser(OWNER))), False)

print("\nActions requiring confirmation:")
for action in ("stop", "restart", "kill", "update"):
    check(f"{action} confirms", action in DESTRUCTIVE_ACTIONS, True)
for action in ("start", "save", "backup", "refresh"):
    check(f"{action} does not confirm", action in DESTRUCTIVE_ACTIONS, False)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed")
