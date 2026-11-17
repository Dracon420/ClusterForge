"""
Alert delivery.

A Discord webhook rather than a bot: it needs no application, no token, no
gateway connection and no intents -- just a URL pasted into secrets.json. The
full bot (with commands) is Phase 8; this exists so port-drift alerting is
genuinely useful the moment it is built, rather than waiting for it.

If no webhook is configured, alerts still land in the panel's alert list. The
monitor never depends on delivery succeeding.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

# Only a genuine "players cannot connect" or "two processes on one save"
# problem earns an @here. Warnings that someone can look at in the morning do
# not, or the mention stops meaning anything.
MENTION_SEVERITIES = {"critical"}

COLOURS = {"info": 0x2ECC71, "warn": 0xE8A33D, "critical": 0xE04F5F}
ICONS = {"info": "✅", "warn": "⚠️", "critical": "\U0001f6a8"}

TIMEOUT = 10


def send_discord(webhook_url: str, alert, cluster: str = "") -> tuple[bool, str]:
    """Post one alert to a Discord webhook. Returns (ok, detail)."""
    if not webhook_url:
        return False, "no webhook configured"

    severity = getattr(alert, "severity", "info")
    server = getattr(alert, "server", "?")
    kind = getattr(alert, "kind", "alert")
    message = getattr(alert, "message", "")

    recovered = kind.endswith("_recovered")
    icon = "✅" if recovered else ICONS.get(severity, "ℹ️")
    colour = COLOURS["info"] if recovered else COLOURS.get(severity, COLOURS["info"])

    title = f"{icon} {server}"
    if cluster:
        title += f"  —  {cluster}"

    content = ""
    if severity in MENTION_SEVERITIES and not recovered:
        content = "@here"

    payload = {
        "content": content,
        "embeds": [{
            "title": title,
            "description": message,
            "color": colour,
            "footer": {"text": f"ArkPanel · {kind}"},
        }],
        # Only allow the @here we deliberately included; never let alert text
        # turn into a role or user ping.
        "allowed_mentions": {"parse": ["everyone"] if content else []},
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "ArkPanel"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read()[:200].decode(errors='replace')}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def make_sink(secrets: dict, config: dict, logger=print):
    """Build the notify callable the monitor uses, or None if not configured."""
    url = (secrets or {}).get("discord_webhook", "").strip()
    if not url:
        logger("[notify] no discord_webhook in secrets.json - alerts stay in the panel only")
        return None

    cluster = (config.get("cluster") or {}).get("id", "")

    def sink(alert):
        ok, detail = send_discord(url, alert, cluster)
        if not ok:
            logger(f"[notify] discord delivery failed: {detail}")

    logger("[notify] discord webhook configured")
    return sink
