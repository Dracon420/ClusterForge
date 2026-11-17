# ClusterForge

**A self-hosted control panel for a multi-PC game-server cluster:** ARK: Survival Ascended, Palworld, Minecraft Bedrock and 7 Days to Die, all from one web page. It also works as an installable app on a phone.

ClusterForge replaced *ASA Dedicated Server Manager* (DSM) for a small cluster spread across three Windows PCs. That cluster has five ARK maps sharing one cluster folder, a Palworld server and a Minecraft Bedrock world that Xbox consoles can join.

It's built around one idea: every PC runs a small agent called **Warden** (`agent.py`) that controls the servers on that machine, and **the panel only aggregates them.** You can open the panel from any PC or from a phone and see the same view of the whole cluster.

![Wardens and host](docs/screenshots/01-pcs-agents-host.png)

> The screenshots come from a **read-only** copy of a live install, which is why every page says *READ-ONLY*. IP addresses, keys, player IDs and gamertags are blurred.

---

## Contents

- [What it does](#what-it-does)
- [Screens](#screens)
- [How it is put together](#how-it-is-put-together)
- [Install](#install)
- [Configuration](#configuration)
- [Security model](#security-model)
- [Project layout](#project-layout)
- [Tests](#tests)
- [Hard-won lessons](#hard-won-lessons)

---

## What it does

### ARK: Survival Ascended

- **Start, stop, restart, kill and save** each map. Long operations run as background jobs, with an in-game countdown that warns players on a configurable ladder (30, 15, 10, 5, 4, 3, 2 and 1 minutes by default) and saves the world before stopping. The countdown is skipped when nobody is online.
- **Process supervision.** It detects duplicate server processes and the *silent port drift* that happens when ARK can't bind its port: ARK quietly moves to the next port without telling anyone. It also tracks which log file is actually live.
- **Settings editor** for `GameUserSettings.ini` and `Game.ini`. The editor is driven by a typed schema built from the ARK wiki. The ini writer **preserves every line**, including the repeated keys that ARK relies on (a normal ini parser would quietly drop them).
- **Launch line editor.** It parses and regenerates the server command line token by token and never drops a flag it doesn't recognise.
- **Mod management.** Mods are read from the `-mods=` launch argument, the installed mod folders and the server's own logs. It can reorder, add and remove mods with no CurseForge API key.
- **Map rotation from a catalog.** One server install can switch between maps. Everything that makes a map different, such as its mod and its save folder name, lives in `data/maps/catalog.json`.
- **Cluster-coordinated auto-update.** It watches Steam for new builds, waits a *settle delay* (Steam sometimes pulls a bad build) and then patches **one map at a time**. It stops if any map fails, so one bad patch never takes the whole cluster down.
- **Backups:**
  - versioned save snapshots every few hours, kept locally and copied to an external drive;
  - an install mirror refreshed on each scheduled restart;
  - a **cluster transfer watcher** that keeps a copy of every character or tame file the moment it changes. A crash in the middle of a transfer can no longer erase a character, and the panel can put a saved copy back.
- **Health monitor.** Port drift, duplicate processes, stale logs and unreachable Wardens raise alerts. It only detects problems and never restarts anything itself.

### Palworld

- Start, stop, save and snapshot the server, with a watchdog and a nightly restart.
- A **settings editor** that explains all 122 `PalWorldSettings.ini` keys in plain words.
- Applies settings safely. Palworld rewrites its settings file from memory when it shuts down, so the Warden always does **stop → write → start**.

### Minecraft Bedrock

- Control the server, take world snapshots and edit `server.properties` and **gamerules** from the panel.
- Keeps the server **on the same version as its clients**, which update themselves.
- **Xbox join support.** A tiny DNS forwarder plus BedrockConnect let consoles reach a self-hosted server, even though the Xbox has no "add server" button.

### 7 Days to Die

- Config, control and status over telnet, with a schema written from the server's real `serverconfig.xml`.

### Discord

- **Guardian bot:**
  - a button control panel (start, stop, restart, status) that only chosen roles can press, with confirmation steps for anything disruptive;
  - live status embeds and *how to join* posts for every game channel.
- **Watcher bots:** per-map status and player lists.
- **Server info posts kept accurate.** Stack sizes, rates and mod lists are rendered from the live config, so the posts never go out of date.
- **Bouncer:** one onboarding channel with the rules and role buttons together.

### Other

- **An installable app (PWA)** for phones, plus a desktop launcher (`ClusterForge.py`) that opens the panel in a native WebView2 window.
- **Wardens update themselves.** Each Warden fetches its own code from the panel and reports a code fingerprint. A machine quietly running old code shows up straight away.
- **Planned downtime and maintenance mode.** Mark a PC as off on purpose so it doesn't raise alerts.

---

## Screens

| | |
|---|---|
| **ARK overview**: every map, its host, players and health at a glance<br>![](docs/screenshots/02-ark-overview.png) | **Servers**: per-map controls, install details, status and log health<br>![](docs/screenshots/03-ark-servers.png) |
| **Mods**: the cluster's mod table and the differences between maps<br>![](docs/screenshots/04-ark-mods.png) | **Backups**: snapshot policy, cluster transfer watcher, rescuing a character<br>![](docs/screenshots/06-ark-backups.png) |
| **Discord**: per-map watcher and Guardian channel wiring<br>![](docs/screenshots/07-ark-discord.png) | **Command line**: parsed launch arguments and the full regenerated line<br>![](docs/screenshots/08-ark-map-command-line.png) |
| **Server management**: restart schedule, warning ladder, network and startup options<br>![](docs/screenshots/09-ark-map-server-management.png) | **GameUserSettings.ini**: schema-driven editor, grouped by category<br>![](docs/screenshots/10-ark-map-gameusersettingsini.png) |
| **Game.ini**<br>![](docs/screenshots/11-ark-map-gameini.png) | **Game.ini custom override**: raw lines that stay exactly as written<br>![](docs/screenshots/12-ark-map-gameini-custom-override.png) |
| **Map management**: switch the rotating slot to another map from the catalog<br>![](docs/screenshots/13-ark-map-map-management.png) | **Mod management**: load order, add, remove, open on CurseForge<br>![](docs/screenshots/14-ark-map-mod-management.png) |
| **Logs**: the live server log, located automatically<br>![](docs/screenshots/15-ark-map-logs.png) | **Palworld**: status, controls, how to join, settings<br>![](docs/screenshots/16-palworld-overview.png) |
| **Palworld preview**<br>![](docs/screenshots/17-palworld-preview.png) | **Minecraft Bedrock**: status, controls, world settings, gamerules<br>![](docs/screenshots/18-minecraft-overview.png) |
| **Add a game**: the game adapters available to set up<br>![](docs/screenshots/19-add-a-game.png) | **On a phone**: the same panel as an installed app<br><img src="docs/screenshots/20-mobile-ark.png" width="300"> |

---

## How it is put together

```
            phone / any PC browser            Discord
                     │                           ▲
                     ▼                           │
        ┌────────────────────────────┐   Guardian, Watchers,
        │  panel.py  (Flask, :8090)  │── Bouncer, info posts
        │  UI · auth · updater ·     │
        │  aggregation only          │
        └──────┬──────────┬──────────┘
          HTTP + shared token (X-ClusterForge-Token)
     ┌─────────┘          │          └─────────┐
     ▼                    ▼                    ▼
┌──────────┐        ┌──────────┐         ┌──────────┐
│ agent.py │        │ agent.py │         │ agent.py │   one per PC, :8767
│  PC1     │        │  PC2     │         │  PC3     │   (over LAN or Tailscale)
└────┬─────┘        └────┬─────┘         └────┬─────┘
 ARK, Minecraft     ARK, Palworld         3 × ARK maps
 RCON · files · processes · backups · scheduler (all local)
```

- **Why Wardens?** A server can't be *started* over an SMB share, because a process has to be launched on its own machine. Each Warden does everything locally: RCON, files, processes, backups and its own schedule. Nothing reaches across the network except the panel's HTTP calls.
- **The panel holds no server-control logic.** It renders the UI, combines what the Wardens report, runs the one thing that must see every machine (the cluster-wide updater) and runs the Discord bots.
- **Game adapters.** Each game is a module in `core/` (`palworld.py`, `minecraft.py`, `sdtd.py`, plus ARK across several modules), each with a matching `*_schema.py` that describes its settings in plain words.
- **RCON is implemented directly** in `core/rcon.py`, so there is no dependency on third-party RCON software.

---

## Install

Requirements: Windows 10 or 11 and Python 3.12. The servers themselves are installed with SteamCMD as usual.

```bash
pip install -r requirements.txt
```

1. Copy `config.example.json` to `config.json` and `secrets.example.json` to `secrets.json`, then fill them in (see below).
2. On **each PC**, run the Warden:
   ```bash
   python agent.py
   ```
   `scripts/install-task.ps1` registers it as an at-boot scheduled task. Run it as a task, not from a terminal: a game server launched from a window dies when that window closes.
3. On the main PC, run the panel:
   ```bash
   python panel.py
   ```
   Open `http://localhost:8090`. `scripts/install-shortcuts.ps1` adds a desktop shortcut for the `ClusterForge.py` app window.
4. To open it from other devices, run `scripts/allow-panel-firewall.ps1`. For access away from home, Tailscale is the easiest route (`scripts/setup-remote-access.ps1`).

Start with `"read_only": true`, which is the default. The panel then only observes, and every write action answers *403*. Switch it to `false` once you've checked it reads your servers correctly.

---

## Configuration

`config.json` describes the cluster. **`config.example.json`** is a full, real-world example with every identifying value replaced, and every section carries a `_comment` explaining it. The main sections:

| Section | What it holds |
|---|---|
| `cluster` | cluster id and the shared `ClusterData` folder |
| `servers[]` | one entry per ARK map: host, `agent_url`, install path, ports, map key |
| `panel` / `agent` | listen addresses and ports; `behind_proxy` for TLS in front |
| `backup` / `schedule` | snapshot interval and retention; restart days and the warning ladder |
| `updates` | `enabled`, `auto_apply`, `settle_minutes`, `stagger` |
| `monitor` / `autostart` | health check interval; restart servers after an unattended reboot |
| `discord` | channel and role ids for Guardian, live status and game channels |
| `palworld` / `minecraft` / `sdtd` / `bedrockconnect` | one block per extra game |

`secrets.json` holds every credential and is git-ignored. The key names are in `secrets.example.json`:
- `agent_token`: shared between the panel and every Warden
- `rcon_password`
- `panel_user` and `panel_password`: the web login
- the Discord bot tokens
- `panel_secret`: signs session cookies
- `desktop_key`: lets the desktop window sign in without typing the password

---

## Security model

- **Wardens** only accept requests that carry the shared `agent_token`, and they refuse writes while the cluster is in read-only mode.
- **The panel** requires a login (a session cookie, or HTTP Basic auth for scripts). If no login is configured, it **only accepts connections from the PC it runs on**, so an unconfigured install is never open to the network. If a reverse proxy sits in front of it, `behind_proxy` removes that exemption, because behind a proxy every request appears to come from the same PC.
- **Discord buttons** are limited to chosen role ids. The owner is always allowed, and an empty list means owner only.
- **Never port-forward** RCON or the Warden port. Use Tailscale for remote access.

---

## Project layout

```
panel.py              web UI + aggregation, updater, Discord bots
agent.py              per-PC server control (HTTP API on :8767)
ClusterForge.py           desktop launcher (native WebView2 window)
core/
  control.py          start/stop/restart jobs, warning ladder
  procwatch.py        process + port supervision
  launch.py           command-line parser/regenerator
  inifile.py          line-preserving ini reader/writer
  schema.py           ARK settings schema (from the wiki CSVs)
  mods.py, maps.py    mod table, map catalog and rotation
  steam.py, updater.py  build detection, cluster-coordinated patching
  backup.py, clusterbackup.py, filebackup.py   saves, transfer folder, PC files
  monitor.py, notify.py  health checks and alerts
  rcon.py             Source RCON client
  discord_bot.py, bouncer.py, serverinfo.py    Discord
  palworld*.py, minecraft*.py, sdtd*.py, bedrockdns.py   other games
  agentupdate.py, codestamp.py   Wardens fetch and fingerprint their own code
templates/, static/   the single-page UI, PWA manifest and service worker
data/maps/catalog.json  the ARK map catalog
scripts/              install tasks, deploy, restore, firewall, tests
```

The background image (`static/bg/background.jpg`) and the map pictures (`data/maps/images/`) aren't included, because they're game artwork. Drop in your own and the UI picks them up.

---

## Tests

`scripts/test_*.py` are plain Python scripts that need no framework. They cover:
- the ini writer, launch-line round trips and map switching;
- the maintenance guards, auth, autostart and Warden self-update;
- the backups (file, cluster and Minecraft), the RCON client and the game adapters.

```bash
python scripts/test_inifile.py
```

---

## Hard-won lessons

Everything here came from a real incident:

- **ARK's `Game.ini` repeats keys on purpose.** For example, one file held 42 `ConfigOverrideItemMaxQuantity` lines. `configparser` keeps only one of them. Hence `core/inifile.py`.
- **Config files come in mixed encodings** (UTF-8, UTF-8 with BOM, UTF-16). Read them carefully and write them back in the same encoding.
- **ARK silently moves to the next port** when the configured one is taken. Trust the port the server *actually* bound, not the configured one.
- **Palworld overwrites its settings when it shuts down**, so a setting written while the server is running is lost at the next stop.
- **Never kill a Palworld server or launch it from a terminal**: killing it has corrupted a world. It runs as a scheduled task that owns the process.
- **A Warden silently running old code** was the most common failure in this project, so each Warden fetches its own code from the panel and reports a fingerprint.
- **Never run a backup copy before a server start.** A copy to a network share that has gone away can hang and can't be killed, and the server never starts.
