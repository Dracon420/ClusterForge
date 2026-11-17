"""
A short fingerprint of the agent's own code.

Exists because "PC2 is silently running older code" has been the recurring
failure of this project: every agent-side change needs a deploy AND a restart
at that machine, and forgetting the restart surfaces much later as a missing
field or a refused endpoint, never as an obvious error.

Hashing the files the agent actually loaded turns that into something the
panel can see and say out loud.
"""

from __future__ import annotations

import hashlib
import os

# Config and secrets are deliberately excluded: two agents legitimately hold
# different config -- PC2 has its own paths and its own bot token -- and must
# still count as the same build.
ROOT = "agent.py"
PACKAGE = "core"


def _core_imports(path: str) -> set[str]:
    """Which core modules a file imports, from its syntax rather than a guess."""
    import ast
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read(), path)
    except (OSError, SyntaxError):
        return set()

    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # from core import a, b   /   from core.x import Y   /   from .x import Y
            if node.module in (PACKAGE, None) and node.level in (0, 1):
                for alias in node.names:
                    found.add(alias.name)
            elif node.module and node.module.startswith(PACKAGE + "."):
                found.add(node.module.split(".", 1)[1].split(".")[0])
            elif node.level == 1 and node.module:
                found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(PACKAGE + "."):
                    found.add(alias.name.split(".", 1)[1].split(".")[0])
    return found


def _files(base_dir: str) -> list[str]:
    """agent.py plus the core modules it ACTUALLY reaches, transitively.

    Originally this hashed every file in core/, which was wrong: the panel-only
    modules (updater, serverinfo, bouncer) live there too, so panel-side work
    marked both agents "OLD CODE" while their agent code was byte-identical and
    perfectly current. Walking the real import graph means only changes an
    agent can actually load will flag it as stale.
    """
    root = os.path.join(base_dir, ROOT)
    out = []
    if os.path.isfile(root):
        out.append(root)

    seen, queue = set(), list(_core_imports(root)) if os.path.isfile(root) else []
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        path = os.path.join(base_dir, PACKAGE, f"{name}.py")
        if not os.path.isfile(path):
            continue
        out.append(path)
        queue.extend(_core_imports(path) - seen)

    # __init__.py is always loaded when the package is imported at all.
    init = os.path.join(base_dir, PACKAGE, "__init__.py")
    if os.path.isfile(init):
        out.append(init)

    return sorted(set(out))


def fingerprint(base_dir: str) -> str:
    """Short stable hash of the agent's code -- content, not timestamps.

    Timestamps differ on every copy across the share and would report a
    mismatch that is not real.
    """
    digest = hashlib.sha256()
    for path in _files(base_dir):
        digest.update(os.path.basename(path).encode("utf-8"))
        try:
            with open(path, "rb") as fh:
                digest.update(fh.read())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()[:12]
