"""A version marker that exists to be changed.

Nothing reads STAMP for any decision. Its only job is to be part of the agent
code fingerprint, so a smoke test can make the panel publish "new code"
without editing anything that does real work.

That matters because the alternative is testing the update path by shipping a
genuine change, which is how it was tested tonight: not repeatable, and a
failed test leaves a live agent running a half-applied edit. Bumping a string
nobody reads cannot break an agent even if every other part of the update
fails.

scripts/smoke_selfupdate.py bumps this and puts it back afterwards.
"""

from __future__ import annotations

# Bumped by the smoke test, and by hand when you want to force every agent to
# pull and restart. Any value will do; the fingerprint only cares that it
# changed.
STAMP = "2026-09-20.2"
