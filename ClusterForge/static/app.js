/* ClusterForge UI.
 *
 * Phase 1 is read-only: every action button renders disabled and explains why,
 * rather than being hidden. Seeing the control you'll later use, greyed out
 * with a reason, beats wondering where it went.
 */

const S = {
  config: null,
  status: null,
  server: null,      // selected server name
  tab: 'mods',
  cache: {},         // `${server}:${tab}` -> payload
  readOnly: true,
};

const TABS = [
  { id: 'commandline', label: 'Command Line' },
  { id: 'servermgmt',  label: 'Server Management' },
  { id: 'gus',         label: 'GameUserSettings.ini' },
  { id: 'game',        label: 'Game.ini' },
  { id: 'override',    label: 'Game.ini - Custom Override' },
  { id: 'mapmgmt',     label: 'Map Management' },
  // No "Find Mods" tab: real CurseForge browsing needs an API key, and an
  // empty tab that only offers an add box is worse than no tab. Adding lives
  // on Mod Management, where DSM puts it.
  { id: 'mods',        label: 'Mod Management' },
  { id: 'logs',        label: 'Logs' },
];

const $  = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path) {
  const r = await fetch(path);
  // A dead session should send you to the login page, not paint the whole UI
  // with "unauthorised" and leave you guessing.
  if (r.status === 401) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    throw new Error('session expired');
  }
  const body = await r.json().catch(() => ({ error: 'bad JSON' }));
  if (!r.ok) throw new Error(body.message || body.error || `HTTP ${r.status}`);
  return body;
}

/* The same, for the POSTs. Throws the server's own message so a caller can
   just show e.message rather than unpicking the response itself. */
async function postJSON(path, payload = {}) {
  const r = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (r.status === 401) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    throw new Error('session expired');
  }
  const body = await r.json().catch(() => ({ error: 'bad JSON' }));
  if (!r.ok) throw new Error(body.message || body.error || `HTTP ${r.status}`);
  return body;
}

/* ---------------------------------------------------------------- bootstrap */

async function boot() {
  S.config = await api('/api/config');
  S.readOnly = S.config.read_only;
  S.server = S.config.servers[0]?.name ?? null;

  $('#modeNotice').textContent = S.readOnly
    ? 'READ-ONLY — observing only, nothing is written'
    : 'READ-WRITE';
  $('#modeNotice').className = 'notice' + (S.readOnly ? ' warn' : '');

  ensureSystemPane();
  const gameList = await api('/api/games').catch(() => ({ games: [] }));
  S.games = gameList.games || [];
  // The app always opens on PCs (2026-11-12, the user's choice) - the page
  // with the fewest buttons that can take a server away. The home-screen
  // shortcuts still land on /?view=servers etc., so honour those.
  const wanted = new URLSearchParams(location.search).get('view');
  const wantedGame = VIEWS[wanted]?.games?.[0];
  S.game = wantedGame || SYSTEM_TAB;
  renderGameBar();
  renderMenuBar();
  selectView(wantedGame ? wanted : 'system');

  // guard() returns early when writing is enabled, so a button left with ONLY
  // a guard() call renders fully enabled and does nothing. Bind the handler
  // explicitly in the writable case.
  document.querySelectorAll('[data-act]').forEach(b => {
    if (S.readOnly) { guard(b, 'Enable writing in config.json first.'); return; }
    b.onclick = () => runCluster(b.dataset.act, b);
  });

  // Paint what we already know before any agent is contacted. An unreachable
  // agent costs a full timeout, and the UI must not sit blank waiting for it.
  renderTabStrip();
  renderServerTabs();
  renderTab();
  refresh();
  setInterval(refresh, 15000);
}

/* ------------------------------------------------------------- view routing */

/* What each menubar button actually shows. Every one of these used to either
 * do nothing or pop a banner announcing a phase that has since shipped. */
/* The parts every game has, in the order they make sense to a newcomer:
 * what is it doing -> the individual servers -> how it is configured ->
 * what it runs -> keeping it current -> keeping it safe -> telling people.
 *
 * A game only gets the parts it can actually do (capabilities from
 * /api/games), so nothing here offers a button that cannot work. Learning
 * one game therefore teaches you the others. */
const SYSTEM_TAB = '_system';     // the "PCs" top-bar tab: not a game

/* index.html now carries #paneSystem itself. A page served from the old
 * template (the panel caches templates until it restarts) still has Agents
 * and Host inside the header band, so move them out here - same result. */
function ensureSystemPane() {
  if (document.getElementById('paneSystem')) return;
  const pane = el('div', { id: 'paneSystem', class: 'viewpane' });
  pane.hidden = true;
  const row = el('div', { class: 'row' });
  for (const id of ['agentList', 'hostStats']) {
    const group = document.getElementById(id)?.closest('.group');
    if (group) row.append(group);
  }
  pane.append(row);
  document.getElementById('paneAddGame').before(pane);
}

const VIEWS = {
  dashboard: { header: true,  servers: false, pane: null, dash: true,
               label: 'Overview', games: ['ark'] },
  /* Cluster Health + Cluster Controls (header) only on ARK's Overview: fewer
   * big buttons per page means fewer mis-presses (2026-11-12). */
  servers:   { header: false, servers: true,  pane: null,
               label: 'Servers', games: ['ark'] },
  mods:      { header: false, servers: true,  pane: null, tab: 'mods',
               label: 'Mods', needs: 'mods' },
  updates:   { header: false, servers: false, pane: 'paneUpdates', load: viewUpdates,
               label: 'Updates', games: ['ark'] },
  backups:   { header: false, servers: false, pane: 'paneBackups', load: viewBackups,
               label: 'Backups', games: ['ark'] },
  discord:   { header: false, servers: false, pane: 'paneDiscord', load: viewDiscord,
               label: 'Discord', games: ['ark'] },
  palworld:  { header: false, servers: false, pane: 'panePalworld', load: viewPalworld,
               label: 'Overview', games: ['palworld'] },
  /* Every other set-up game (Minecraft first, 2026-11-12). Before this a game
   * with no hand-written page had no views at all, so picking it fell through
   * to "Add a game" - a list of every game, and no controls. */
  game:      { header: false, servers: false, pane: 'paneGame', load: viewGame,
               label: 'Overview', generic: true },
  /* Agents + Host: the machines, on their own top-bar tab ("PCs"). */
  system:    { header: false, servers: false, pane: 'paneSystem',
               label: 'Wardens & host', games: [SYSTEM_TAB] },
  addgame:   { header: false, servers: false, pane: 'paneAddGame', load: viewAddGame,
               label: 'Add a game', games: [] },
};

/* Which views belong to a game. Explicit `games` wins; otherwise a view is
 * offered when the game declares the capability it needs. A game nobody has
 * written views for still gets the generic ones, which is what makes adding
 * one cheap. */
function viewsForGame(gameId) {
  const game = (S.games || []).find(g => g.id === gameId);
  const caps = game?.capabilities || {};
  return Object.entries(VIEWS)
    .filter(([id, v]) => {
      if (id === 'addgame') return false;
      if (v.generic) return !!game?.configured && !HAS_OWN_PAGE.includes(gameId);
      if (v.games) return v.games.includes(gameId);
      if (v.needs) return !!caps[v.needs];
      return false;
    })
    .map(([id, v]) => ({ id, label: v.label || id }));
}

function renderGameBar() {
  const bar = $('#gameBar');
  [...bar.querySelectorAll('button')].forEach(b => b.remove());
  const spacer = bar.querySelector('.spacer');
  // PCs first, then the games that are actually set up. Games that are not
  // (7 Days to Die) are left off the bar - "+ Add a game" still lists them.
  const pcs = el('button', { 'data-game': SYSTEM_TAB, title: 'The PCs: Wardens and host health' }, 'PCs');
  pcs.classList.toggle('active', S.game === SYSTEM_TAB);
  pcs.onclick = () => selectGame(SYSTEM_TAB);
  bar.insertBefore(pcs, spacer);
  for (const g of S.games || []) {
    if (!g.configured) continue;
    const b = el('button', { 'data-game': g.id }, g.short || g.name);
    b.classList.toggle('active', g.id === S.game);
    b.onclick = () => selectGame(g.id);
    bar.insertBefore(b, spacer);
  }
  const add = el('button', { class: 'ghost', title: 'Set up another game' }, '+ Add a game');
  add.classList.toggle('active', S.view === 'addgame');
  add.onclick = () => { S.view = 'addgame'; renderGameBar(); renderMenuBar(); selectView('addgame'); };
  bar.insertBefore(add, spacer);
}

function renderMenuBar() {
  const bar = $('#menuBar');
  bar.innerHTML = '';
  if (S.view === 'addgame') return;
  for (const v of viewsForGame(S.game)) {
    const b = el('button', { 'data-view': v.id }, v.label);
    b.classList.toggle('active', v.id === S.view);
    b.onclick = () => selectView(v.id);
    bar.append(b);
  }
}

function selectGame(gameId) {
  S.game = gameId;
  try { localStorage.setItem('clusterforge.game', gameId); } catch (e) { /* private window */ }
  const first = viewsForGame(gameId)[0];
  S.view = first ? first.id : 'addgame';
  renderGameBar();
  renderMenuBar();
  selectView(S.view);
}

function selectView(id) {
  const view = VIEWS[id] || VIEWS.dashboard;
  S.view = id;

  document.querySelectorAll('#menuBar button[data-view]').forEach(b =>
    b.classList.toggle('active', b.dataset.view === id));
  document.querySelectorAll('#gameBar button').forEach(b =>
    b.classList.toggle('active',
      b.dataset.game ? (b.dataset.game === S.game && id !== 'addgame')
                     : id === 'addgame'));

  $('#headerBand').hidden = !view.header;
  // The cluster-controls band belongs to the cluster overview, not to a
  // single map's page.
  $('#headerBand').nextElementSibling.hidden = !view.header;
  $('#serverView').hidden = !view.servers;
  $('#dashView').hidden = !view.dash;

  for (const pane of document.querySelectorAll('.viewpane')) {
    pane.hidden = pane.id !== view.pane;
  }

  if (view.dash) { renderDashboard(); loadPlayers(); }

  if (view.tab) selectTab(view.tab);
  if (view.load) {
    const pane = $('#' + view.pane);
    pane.innerHTML = '<div class="spin">loading…</div>';
    view.load(pane).catch(e => {
      pane.innerHTML = '';
      pane.append(el('div', { class: 'banner err' }, `Could not load: ${e.message}`));
    });
  }
}

/** Disable an action while read-only, and say why. */
function guard(btn, reason) {
  if (!S.readOnly) return;
  btn.setAttribute('aria-disabled', 'true');
  btn.disabled = true;
  btn.title = 'Read-only mode. ' + reason;
}

function showBanner(kind, msg) {
  const box = $('#banners');
  const b = el('div', { class: `banner ${kind}` }, msg);
  box.prepend(b);
  setTimeout(() => b.remove(), 9000);
}

/* ------------------------------------------------------------------ refresh */

async function refresh() {
  try {
    S.status = await api('/api/cluster/status');
  } catch (e) {
    $('#clusterHealth').innerHTML = `<dd class="bad">panel error: ${esc(e.message)}</dd>`;
    return;
  }
  renderHeader();
  renderServerTabs();
  renderCore();
  if (S.view === 'dashboard') renderDashboard();
  loadAgents();
  loadAlerts();
}

/* Players cost an RCON round trip per map, so they are polled on their own
 * slower clock rather than riding the 15s status refresh. */
let PLAYER_TIMER = null;
async function loadPlayers() {
  try { S.players = await api('/api/cluster/players'); }
  catch { S.players = null; }
  if (S.view === 'dashboard') renderDashboard();

  clearTimeout(PLAYER_TIMER);
  PLAYER_TIMER = setTimeout(() => {
    if (S.view === 'dashboard') loadPlayers();
  }, 30000);
}

/* Active alerts sit above everything else. A monitoring panel that hides its
 * alerts below the fold is not monitoring. */
async function loadAlerts() {
  let data;
  try { data = await api('/api/cluster/alerts'); } catch { return; }
  S.alerts = data;
  if (S.view === 'dashboard') renderAlertPanel();

  let box = document.getElementById('alertBand');
  if (!box) {
    box = el('div', { id: 'alertBand' });
    $('#banners').after(box);
  }
  box.innerHTML = '';

  const active = (data.alerts || []).filter(a => a.active);
  if (!active.length) {
    box.append(el('div', { class: 'banner ok', style: 'padding:4px 10px' },
      el('strong', {}, '● '), 'No active alerts — ',
      el('span', { class: 'muted' },
        'ports in range, no duplicate processes, logs being written.')));
    return;
  }

  // Every alert can be dismissed on its own. Acknowledged = gone from here
  // (it stays in the history); the same problem happening again raises a new one.
  const ackOne = async (a, btn) => {
    btn.disabled = true;
    try {
      const r = await fetch(`/api/server/${encodeURIComponent(a.server)}/alerts/acknowledge`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ at: a.at, kind: a.kind, server: a.server }) });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
    } catch (e) { btn.disabled = false; showBanner('err', `Could not dismiss: ${e.message}`); return; }
    loadAlerts();
  };
  for (const a of active.slice(0, 6)) {
    const kind = a.severity === 'critical' ? 'err' : a.severity === 'warn' ? 'warn' : 'info';
    const when = a.at ? new Date(a.at * 1000).toLocaleTimeString() : '';
    const x = el('button', { class: 'btn grey', style: 'float:right;margin-left:8px;padding:2px 10px',
                             title: 'Acknowledge and remove this alert' }, '✕ Dismiss');
    x.onclick = () => ackOne(a, x);
    if (S.readOnly) guard(x, 'Enable writing in config.json first.');
    box.append(el('div', { class: `banner ${kind}` }, x,
      el('strong', {}, `${a.severity.toUpperCase()} · ${a.server}`),
      ' — ', a.message,
      when ? el('span', { class: 'muted' }, `  (${when})`) : ''));
  }

  if (active.length > 6) {
    box.append(el('div', { class: 'statline' }, `…and ${active.length - 6} more`));
  }

  const ack = el('button', { class: 'btn grey', style: 'margin-top:4px' },
    `Acknowledge all ${active.length} alert(s)`);
  ack.onclick = async () => {
    for (const srv of S.config.servers) {
      try {
        await fetch(`/api/server/${srv.name}/alerts/acknowledge`,
                    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      } catch { /* an unreachable agent is already reported as its own alert */ }
    }
    showBanner('ok', 'Alerts acknowledged and cleared. They stay in the alert history.');
    loadAlerts();
  };
  if (S.readOnly) guard(ack, 'Enable writing in config.json first.');
  box.append(ack);
}

function renderHeader() {
  const st = S.status;
  const online = st.servers.filter(s => s.state === 'online').length;
  const unknown = st.servers.filter(s => s.state === 'unknown').length;
  const dl = $('#clusterHealth');
  dl.innerHTML = '';
  const add = (k, v, cls) => { dl.append(el('dt', {}, k), el('dd', { class: cls || '' }, v)); };
  add('Cluster', S.config.cluster.id);
  /* A map stopped on purpose must not drag this line into a warning colour.
   * "5 / 6" in amber sends you looking for a broken server; "5 / 6 (1 stopped
   * for Palworld)" answers the question on the spot. */
  const held = st.servers.filter(s => s.state === 'maintenance');
  const off   = st.servers.filter(s => s.state === 'planned');
  const expected = st.total - held.length - off.length;
  add('Servers online',
      `${online} / ${st.total}`
        + (held.length ? ` (${held.length} stopped on purpose)` : '')
        + (off.length ? ` (${off.length} on a machine that is off)` : ''),
      online === expected ? 'good' : (online === 0 ? 'bad' : 'warn'));
  for (const h of held) {
    add(h.display_name || h.name, `stopped — ${h.maintenance}`, 'good');
  }
  if (off.length) {
    const hosts = [...new Set(off.map(s => s.host))].join(', ');
    add('Planned downtime', `${hosts} — ${off[0].planned?.reason} (${off.length} map(s))`, 'good');
  }
  if (unknown) add('Unreachable', String(unknown), 'bad');
  add('Transfer dir', S.config.cluster.cluster_dir);
  add('Mode', S.readOnly ? 'read-only' : 'read-write', S.readOnly ? 'warn' : 'good');

  const hs = $('#hostStats');
  hs.innerHTML = '';
  const addH = (k, v, cls) => { hs.append(el('dt', {}, k), el('dd', { class: cls || '' }, v)); };
  if (st.system) {
    addH('CPU', `${st.system.cpu_percent}%`);
    addH('Memory',
      `${st.system.memory_used_gb} / ${st.system.memory_total_gb} GB (${st.system.memory_percent}%)`,
      st.system.memory_percent > 90 ? 'bad' : st.system.memory_percent > 80 ? 'warn' : '');
  }
  const local = st.servers.find(s => s.disk);
  if (local?.disk) {
    addH('Disk free', `${local.disk.free_gb} GB of ${local.disk.total_gb} GB`,
      local.disk.free_gb < 30 ? 'warn' : '');
  }
}

async function loadAgents() {
  let data;
  try { data = await api('/api/agents'); } catch { return; }
  S.agents = data;
  const box = $('#agentList');
  box.innerHTML = '';

  for (const a of data.agents) {
    const ok = a.reachable;
    const dotColour = ok ? 'var(--green)' : (a.planned ? 'var(--cyan)' : 'var(--red)');
    const row = el('div', { style: 'margin-bottom:3px' },
      el('span', { style: `color:${dotColour}` }, '● '),
      el('strong', {}, a.host || a.server),
      el('span', { class: 'muted' }, ` ${a.url}`));

    if (!ok && a.planned) {
      /* Off on purpose. Shown calmly, with the reason, because the whole
       * point is to answer "is something broken?" with "no". */
      row.append(el('span', { style: 'color:var(--cyan)' },
        `  planned downtime — ${a.planned.reason}`
        + (a.planned.until ? ` (until ${a.planned.until})` : '')));
    } else if (!ok) {
      row.append(el('span', { style: 'color:var(--red)' }, `  ${a.error}`));
    } else {
      row.append(el('span', { class: 'muted' },
        a.elevated ? '  elevated' : '  NOT elevated'));
      // Which code an agent is on used to be invisible until some field
      // turned up missing. Now it is stated.
      row.append(a.stale
        ? el('span', { class: 'pill stale', title:
            `Panel is on ${data.panel_code}, this Warden is on ${a.code || 'a build too old to say'}.` },
            'OLD CODE')
        : el('span', { class: 'muted mono' }, `  ${a.code}`));
      // Agent uptime, not the server's. An agent that restarted minutes ago
      // while its map has been up for hours is a deploy, not an incident.
      if (a.started) {
        row.append(el('span', { class: 'muted' }, `  Warden up ${fmtAgo(a.started)
          .replace(' ago', '')}`));
      }
      /* A stuck restart is the one kind of "OLD CODE" that never clears by
       * itself, and pressing Update agents again will not help. Say so on
       * the row rather than making someone read a log on another machine to
       * find out. */
      if (a.restart_failed) {
        row.append(el('div', { class: 'statline err',
                               style: 'margin:2px 0 4px 14px' },
          `Update applied but the restart did not happen — ${a.restart_failed} `
          + 'Restart this Warden by hand to finish.'));
      }
    }
    box.append(row);
  }

  const stale = (data.stale || []).filter(n =>
    !data.agents.find(a => a.server === n)?.local);
  const local = data.agents.filter(a => a.local && a.stale);

  if (local.length) {
    box.append(el('div', { class: 'statline warn' },
      'This machine’s own Warden is behind the panel — restart the '
      + '"ClusterForge Warden" scheduled task here.'));
  }

  const log = el('div', { class: 'joblog hidden' });
  const btn = el('button', { class: `btn ${stale.length ? 'orange' : 'grey'}`,
                             style: 'margin-top:5px' },
    stale.length ? `Update ${stale.length} Warden(s)` : 'Update Wardens');
  if (S.readOnly) {
    guard(btn, 'Enable writing in config.json first.');
  } else {
    btn.title = 'Copy this panel’s Warden code to the other PCs and restart them.';
    btn.onclick = () => deployAgents(btn, log);
  }
  box.append(btn, log);

  /* Mark a whole machine as off on purpose. Per machine because that is what
   * gets switched off - HUBMASTER going down for building work takes three
   * maps with it, and once the PC is off there is nothing to set a flag on. */
  const plan = el('div', { style: 'margin-top:6px' });
  const planNote = el('span', { class: 'statline' }, '');
  const hosts = [...new Set((data.agents || []).map(a => a.host).filter(Boolean))];
  const pick = el('select', {});
  pick.append(el('option', { value: '' }, '— machine —'));
  for (const h of hosts) pick.append(el('option', { value: h }, h));
  const why = el('input', { type: 'text', placeholder: 'reason (e.g. building work at that house)',
                            style: 'min-width:18em' });

  const markOff = el('button', { class: 'btn grey' }, 'Mark machine off');
  markOff.onclick = async () => {
    if (!pick.value) { planNote.textContent = 'pick a machine first'; return; }
    planNote.className = 'statline'; planNote.textContent = 'saving…';
    try {
      await postJSON('/api/downtime', { host: pick.value, reason: why.value, on: true });
      planNote.textContent = `${pick.value} marked off — the panel will stop waiting on it`;
      loadAgents();
    } catch (e) { planNote.className = 'statline err'; planNote.textContent = e.message; }
  };

  const markOn = el('button', { class: 'btn grey' }, 'Back in service');
  markOn.onclick = async () => {
    if (!pick.value) { planNote.textContent = 'pick a machine first'; return; }
    planNote.className = 'statline'; planNote.textContent = 'saving…';
    try {
      await postJSON('/api/downtime', { host: pick.value, on: false });
      planNote.textContent = `${pick.value} back in service`;
      loadAgents();
    } catch (e) { planNote.className = 'statline err'; planNote.textContent = e.message; }
  };

  if (S.readOnly) [markOff, markOn].forEach(b => guard(b, 'Enable writing in config.json first.'));
  plan.append(pick, why, markOff, markOn, planNote);
  box.append(plan);
}

/* Stage agent code to the other PCs and restart them there.
 *
 * This was the last manual chore in the workflow: a deploy script here, then a
 * scheduled-task restart at the other machine. Forgetting the second half left
 * an agent quietly running older code, which only surfaced later as a missing
 * field or a refused endpoint. */
async function deployAgents(btn, log) {
  if (!confirm('Copy this panel’s Warden code to the other PC(s) and restart '
      + 'their Wardens?\n\nThe servers themselves keep running — only the '
      + 'Warden process restarts, for a few seconds. Any control job already in '
      + 'progress will block the restart rather than being abandoned.')) return;

  const was = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Working…';
  log.classList.remove('hidden');
  log.innerHTML = '';
  const line = (t, cls) => {
    log.append(el('div', { class: cls || '' }, t));
    log.scrollTop = log.scrollHeight;
  };
  line('Staging files and restarting remote Wardens…', 'muted');

  try {
    const r = await fetch('/api/cluster/deploy-agents', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    const body = await r.json();
    for (const s of body.steps || []) {
      line(s.message, s.level === 'error' ? 'err' : s.level === 'warn' ? 'warn' : '');
    }
    line(body.ok ? `All Wardens on ${body.code}.`
                 : `Finished with problems on: ${(body.failed || []).join(', ')}`,
         body.ok ? 'ok' : 'err');
  } catch (e) {
    line(`Failed: ${e.message}`, 'err');
  }

  btn.disabled = false;
  btn.textContent = was;
  refresh();
}

function switchServer(name) {
  if (name === S.server) return;
  S.server = name;
  renderServerTabs();
  renderCore();
  renderTab();
}

function renderServerTabs() {
  const box = $('#serverTabs');
  box.innerHTML = '';
  for (const srv of S.config.servers) {
    const live = S.status?.servers.find(s => s.name === srv.name);
    const state = live?.state ?? 'unknown';
    box.append(el('div', {
      class: 'servertab' + (srv.name === S.server ? ' active' : ''),
      onclick: () => switchServer(srv.name),
    },
      el('span', { class: `dot ${state}` }),
      srv.display_name,
      el('span', { class: 'muted', style: 'font-weight:400' }, ` ${srv.host}`)
    ));
  }
  renderCoreSwitch();
}

/* DSM had a map dropdown on the Server Management Core, which is quicker than
 * the tabs once there are more than a couple of maps. Both drive the same
 * switch, so they can never disagree. */
function renderCoreSwitch() {
  const sel = $('#coreServerSelect');
  if (!sel) return;

  const wanted = S.config.servers.map(s => s.name).join('|');
  if (sel.dataset.built !== wanted) {
    sel.innerHTML = '';
    for (const srv of S.config.servers) {
      sel.append(el('option', { value: srv.name },
        `${srv.display_name} — ${srv.map_name || 'map unknown'}`));
    }
    sel.dataset.built = wanted;
    sel.onchange = () => switchServer(sel.value);
  }
  sel.value = S.server || '';

  const live = S.status?.servers.find(s => s.name === S.server);
  const note = $('#coreSwitchNote');
  if (note) {
    const state = live?.state ?? 'unknown';
    note.textContent = live
      ? `${state}${live.pid ? ` · PID ${live.pid}` : ''} on ${live.host || '?'}`
      : '';
    note.className = 'statline ' + (state === 'online' ? 'ok' : state === 'unknown' ? 'warn' : '');
  }
}

/* ---- control actions -------------------------------------------------- */

/* The job log has to OUTLIVE a re-render. renderCore() wipes and rebuilds the
 * whole grid on the 15s refresh, so a log created inside it would vanish
 * fifteen seconds into a three-minute restart and the buttons would come back
 * enabled while the job was still running. Panes are kept here per server and
 * re-attached by renderCore instead of recreated. */
const JOBPANES = {};

function jobPane(name) {
  if (!JOBPANES[name]) {
    JOBPANES[name] = { box: el('div', { class: 'joblog hidden' }), running: null };
  }
  return JOBPANES[name];
}

/* Long operations run as jobs on the agent, so the button fires and then the
 * panel follows the job. A restart can take minutes; the UI has to show what
 * it is waiting on rather than looking frozen. */
async function runControl(action, cfg, button) {
  const name = cfg.display_name || cfg.name;

  if (action.confirm && !confirm(action.confirm.replace('{name}', name))) return;

  const pane = jobPane(cfg.name);
  const jobBox = pane.box;
  pane.running = action.label;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Working…';
  jobBox.classList.remove('hidden');
  jobBox.innerHTML = '';
  const line = (text, cls) => {
    jobBox.append(el('div', { class: cls || '' }, text));
    jobBox.scrollTop = jobBox.scrollHeight;
  };
  const done = () => { pane.running = null; refresh(); };
  line(`${action.label} on ${name}…`, 'muted');

  let body;
  try {
    const r = await fetch(`/api/server/${cfg.name}/${action.id}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    body = await r.json();
    if (!r.ok) {
      line(body.message || body.error || `HTTP ${r.status}`, 'err');
      button.disabled = false; button.textContent = original;
      done();
      return;
    }
  } catch (e) {
    line(`Request failed: ${e.message}`, 'err');
    button.disabled = false; button.textContent = original;
    done();
    return;
  }

  // /save returns immediately; the rest return a job to follow.
  const jobId = (body.job || {}).id;
  if (!jobId) {
    line(body.response ? `Done — ${body.response}` : 'Done.', 'ok');
    button.disabled = false; button.textContent = original;
    done();
    return;
  }

  await followJobs([{ server: cfg.name, id: jobId, label: '' }], line, action.label);

  button.disabled = false;
  button.textContent = original;
  done();
}

/* Follow one or more agent jobs to completion, streaming their steps into a
 * log. Cluster actions run both maps at once and are polled together, so the
 * two maps' progress interleaves instead of one being followed after the
 * other has already finished. */
async function followJobs(jobs, line, what, capMinutes = 90) {
  const seen = {}, finished = {};
  const started = Date.now();

  while (Object.keys(finished).length < jobs.length) {
    await new Promise(r => setTimeout(r, 2000));

    for (const j of jobs) {
      if (finished[j.server]) continue;
      const tag = j.label ? `${j.label} ` : '';
      let job;
      try {
        job = await api(`/api/server/${j.server}/jobs/${j.id}`);
      } catch (e) {
        line(`${tag}Lost track of the job: ${e.message}`, 'warn');
        finished[j.server] = 'lost';
        continue;
      }
      const steps = job.steps || [];
      for (const step of steps.slice(seen[j.server] || 0)) {
        const cls = step.level === 'error' ? 'err' : step.level === 'warn' ? 'warn' : '';
        line(`${tag}[${String(step.t).padStart(5)}s] ${step.message}`, cls);
      }
      seen[j.server] = steps.length;

      if (job.state && job.state !== 'running') {
        finished[j.server] = job.state;
        if (job.state === 'done') {
          line(`${tag}${what} complete after ${job.elapsed}s.`, 'ok');
        } else {
          // The failure reason is usually the last step verbatim, prefixed with
          // the exception class. Printing it twice just buries it.
          const reason = (job.error || 'unknown error').replace(/^\w*Error:\s*/, '');
          const last = steps.length ? steps[steps.length - 1].message : '';
          line(last && reason.trim() === last.trim()
            ? `${tag}${what} FAILED after ${job.elapsed}s.`
            : `${tag}FAILED: ${reason}`, 'err');
        }
      }
    }

    // A stop with a full warning ladder runs half an hour; an update adds a
    // steamcmd download on top of that.
    if (Date.now() - started > capMinutes * 60 * 1000) {
      line(`Still running after ${capMinutes} minutes — check the Warden log.`, 'warn');
      break;
    }
  }
  return finished;
}

/* --------------------------------------------------- Cluster-wide controls */

/* The five buttons in the Cluster Controls band. Start/Restart/Stop fan out to
 * each server's own Warden; the two update actions go through the panel's
 * updater, which is the only component that sees both PCs and which refuses to
 * patch one map without the other. */
const CLUSTER_ACTIONS = {
  /* Start/Restart/Stop All only touch maps in the right state (2026-11-12):
   * restart and stop hit RUNNING maps, start hits STOPPED ones. Restarting a
   * stopped map starts it, so "Restart All" used to bring back a map that was
   * off on purpose. The confirm names exactly what will happen. */
  'start-all': {
    label: 'Start All', mode: 'fanout', act: 'start', want: 'offline',
    confirm: (names) => `Start ${names}?`,
  },
  'restart-all': {
    label: 'Restart All', mode: 'fanout', act: 'restart', want: 'online',
    confirm: (names) => `Restart ${names}?\n\nPlayers on each map are warned first if any are `
           + 'online, every world is saved, and each process must fully exit before '
           + 'it starts again. Transfers are unavailable until they are all back.',
  },
  'stop-all': {
    label: 'Stop All', mode: 'fanout', act: 'stop', want: 'online',
    confirm: (names) => `Stop ${names}?\n\nPlayers are warned first if any are online, `
           + 'and every world is saved before shutdown.',
  },
  'update-all': {
    label: 'Update All', mode: 'update', body: {},
    confirm: 'Check for a new build and update the cluster?\n\nAll maps are patched '
           + 'together, or neither is — a split cluster breaks transfers. This holds '
           + 'and alerts if either Warden is unreachable or either build is unknown. '
           + 'If everything is already current, nothing happens.',
  },
  'sur-all': {
    label: 'Stop + Update + Restart All', mode: 'update', body: { force: true },
    confirm: 'FORCE a full cluster update cycle?\n\nAll maps stop, run through '
           + 'steamcmd, and start again — even if they are already on the current '
           + 'build. Use this to repair a bad install; use Update All for a normal '
           + 'patch. Expect the cluster to be down for several minutes.',
  },
};

/* Unlike the per-server pane this one lives in static HTML that no render
 * function rebuilds, so it can simply be created once and reused. */
const CLUSTERPANE = { box: null, running: null };

function clusterPane() {
  if (!CLUSTERPANE.box) {
    CLUSTERPANE.box = el('div', { class: 'joblog hidden' });
    $('#controlsNote').after(CLUSTERPANE.box);
  }
  return CLUSTERPANE;
}

function clusterButtons() {
  return [...document.querySelectorAll('[data-act]')];
}

async function runCluster(key, button) {
  const action = CLUSTER_ACTIONS[key];
  if (!action || CLUSTERPANE.running) return;
  let targets = null;
  if (action.mode === 'fanout') {
    const plan = fanoutTargets(action);
    targets = plan.hit;
    const nameOf = (s) => s.display_name || s.name;
    if (!targets.length) {
      alert(action.want === 'offline' ? 'Every map is already running - nothing to start.'
                                      : 'No map is running - nothing to ' + action.act + '.');
      return;
    }
    const names = targets.map(nameOf).join(', ');
    const skip = plan.skip.length
      ? `\n\nLeft alone (${action.want === 'offline' ? 'already running' : 'not running'}): `
        + plan.skip.map(nameOf).join(', ')
      : '';
    if (!confirm(action.confirm(names) + skip)) return;
  } else if (action.confirm && !confirm(action.confirm)) return;

  const pane = clusterPane();
  pane.running = action.label;
  const box = pane.box;
  box.classList.remove('hidden');
  box.innerHTML = '';
  const line = (text, cls) => {
    box.append(el('div', { class: cls || '' }, text));
    box.scrollTop = box.scrollHeight;
  };

  const buttons = clusterButtons();
  const labels = new Map(buttons.map(b => [b, b.textContent]));
  buttons.forEach(b => { b.disabled = true; b.style.opacity = '0.45'; });
  button.textContent = 'Working…';

  try {
    const jobs = action.mode === 'fanout'
      ? await startFanout(action, line, targets)
      : await startClusterUpdate(action, line);
    if (jobs.length) await followJobs(jobs, line, action.label);
  } catch (e) {
    line(`Request failed: ${e.message}`, 'err');
  } finally {
    pane.running = null;
    buttons.forEach(b => {
      b.disabled = false; b.style.opacity = '';
      b.textContent = labels.get(b);
    });
    refresh();
  }
}

/* Fire the same per-server action on every map. Both are fired before either
 * is followed, so the maps go down and come back together rather than one
 * waiting on the other to finish. */
/* Which maps a fan-out should touch, from the live status the page polls. A
 * map whose state is unknown (agent unreachable) is left alone, not guessed. */
function fanoutTargets(action) {
  const state = {};
  for (const s of S.status?.servers || []) state[s.name] = s.state;
  const hit = [], skip = [];
  for (const cfg of S.config.servers) (state[cfg.name] === action.want ? hit : skip).push(cfg);
  return { hit, skip };
}

async function startFanout(action, line, targets) {
  const servers = targets || S.config.servers;
  line(`${action.label}: ${servers.map(s => s.display_name || s.name).join(', ')}…`, 'muted');

  const jobs = [];
  for (const cfg of servers) {
    const name = cfg.display_name || cfg.name;
    try {
      const r = await fetch(`/api/server/${cfg.name}/${action.act}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      const body = await r.json();
      if (!r.ok) { line(`${name}: ${body.message || body.error || `HTTP ${r.status}`}`, 'err'); continue; }
      const id = (body.job || {}).id;
      if (id) jobs.push({ server: cfg.name, id, label: name });
      else line(`${name}: ${body.response || 'done'}`, 'ok');
    } catch (e) {
      line(`${name}: request failed — ${e.message}`, 'err');
    }
  }
  return jobs;
}

/* The updater decides for the whole cluster and returns one job per map. Its
 * refusals (agent unreachable, build unknown, already busy) come back as a
 * message worth reading, not an error to swallow. */
async function startClusterUpdate(action, line) {
  line(`${action.label}…`, 'muted');
  const r = await fetch('/api/cluster/update', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(action.body || {}),
  });
  const body = await r.json();

  if (body.message) line(body.message, body.ok ? '' : 'warn');
  if (!body.ok) {
    line(body.error ? `Held: ${body.error}` : 'The update did not start.', 'err');
    return [];
  }
  for (const err of body.errors || []) line(err, 'err');

  const plan = body.plan || {};
  if (plan.reason) line(`Plan: ${plan.reason}`, 'muted');

  const jobs = [];
  for (const [name, res] of Object.entries(body.results || {})) {
    const cfg = S.config.servers.find(s => s.name === name);
    const id = ((res || {}).job || {}).id;
    if (id) jobs.push({ server: name, id, label: (cfg && cfg.display_name) || name });
    else if (res && res.error) line(`${name}: ${res.error}`, 'err');
  }
  if (!jobs.length) line('Nothing to follow — no server was updated.', 'muted');
  return jobs;
}

/* ------------------------------------------------------------- the dashboard */

/* Background facts the dashboard shows but that are too expensive, or change
 * too slowly, to fetch on the 15s status poll. Fetched once when the Cluster
 * view is first opened and refreshed lazily after that. */
const DASH = { schedule: null, updates: null, at: 0 };

async function loadDashExtras() {
  if (Date.now() - DASH.at < 60000) return;
  DASH.at = Date.now();
  const [schedule, updates] = await Promise.all([
    api('/api/cluster/schedule').catch(() => null),
    api('/api/cluster/updates').catch(() => null),
  ]);
  DASH.schedule = schedule;
  DASH.updates = updates;
  if (S.view === 'dashboard') renderDashboard();
}

/** When does the next scheduled restart land? Returns null when disabled. */
function nextRestart(sched) {
  if (!sched?.enabled || !sched.restart_time) return null;
  const days = sched.restart_days || [];
  if (!days.length) return null;

  const [hh, mm] = String(sched.restart_time).split(':').map(Number);
  const now = new Date();
  // Look ahead a full week; the first matching day at or after now wins.
  for (let step = 0; step <= 7; step++) {
    const day = new Date(now);
    day.setDate(now.getDate() + step);
    day.setHours(hh, mm, 0, 0);
    if (day > now && days.includes(WEEKDAYS[(day.getDay() + 6) % 7])) return day;
  }
  return null;
}

function untilText(date) {
  const mins = Math.round((date - Date.now()) / 60000);
  if (mins < 60) return `in ${mins} min`;
  const h = Math.floor(mins / 60), m = mins % 60;
  if (h < 24) return `in ${h}h ${m}m`;
  return `in ${Math.floor(h / 24)}d ${h % 24}h`;
}

function renderDashboard() {
  loadDashExtras();
  renderGlance();
  renderMapCards();
  renderAlertPanel();
}

/* The one-line answer to "is everything fine?" */
function renderGlance() {
  const row = $('#glanceRow');
  if (!row) return;
  row.innerHTML = '';

  const servers = S.status?.servers || [];
  const online = servers.filter(s => s.state === 'online').length;
  const unknown = servers.filter(s => s.state === 'unknown').length;

  const players = S.players;
  const playerText = !players ? '—'
    : players.total_known ? String(players.total)
    : `${players.total}+ (a map did not answer)`;

  const sched = DASH.schedule?.panel_config?.schedule;
  const next = nextRestart(sched);
  const upd = DASH.updates || {};
  const behind = (upd.last_result?.needs_update || []).length;

  const tile = (label, value, cls, sub) => el('div', { class: 'glance' },
    el('div', { class: 'glabel' }, label),
    el('div', { class: `gvalue ${cls || ''}` }, value),
    sub ? el('div', { class: 'gsub' }, sub) : null);

  row.append(el('div', { class: 'group g-cyan grow glancewrap' },
    tile('Maps up', `${online} / ${servers.length}`,
      unknown ? 'warn' : online === servers.length ? 'ok' : 'bad',
      unknown ? `${unknown} Warden unreachable` : null),
    tile('Players online', playerText,
      players && players.total > 0 ? 'ok' : '', 'refreshed every 30s'),
    tile('Cluster build',
      behind ? `${behind} map behind` : upd.last_check ? 'current' : 'unchecked',
      behind ? 'bad' : upd.last_check ? 'ok' : 'warn',
      upd.auto_apply ? 'auto-update on' : 'auto-update OFF'),
    tile('Next restart', next ? untilText(next) : 'not scheduled',
      next ? '' : 'warn',
      next ? next.toLocaleString(undefined,
        { weekday: 'short', hour: '2-digit', minute: '2-digit' }) : null)
  ));
}

/* One card per map. This is what the Cluster view was missing: you had to go
 * to the Servers page to learn anything at all about a map. */
function renderMapCards() {
  const box = $('#mapCards');
  if (!box) return;
  box.innerHTML = '';

  const sched = DASH.schedule?.agents || {};

  for (const cfg of S.config.servers) {
    const srv = (S.status?.servers || []).find(s => s.name === cfg.name) || {};
    const state = srv.state || 'unknown';
    /* Down on purpose is not a fault, so it does not get the fault colour.
     * A card that reads "offline" in red for a server deliberately stopped so
     * Palworld can have the memory sends you hunting for a problem that is
     * not there. */
    const colour = state === 'online' ? 'green'
                 : state === 'unknown' ? 'orange'
                 : (state === 'maintenance' || state === 'planned') ? 'cyan' : 'red';

    const card = el('div', { class: `group g-${colour} mapstatus` });
    card.append(el('h3', {},
      el('span', { class: `dot ${state}` }),
      cfg.display_name,
      el('span', { class: 'muted', style: 'font-weight:400;margin-left:auto' },
        `${cfg.map_name || ''} · ${cfg.host}`)));

    if (state === 'planned') {
      card.append(el('div', { class: 'banner' },
        `${cfg.host} is switched off on purpose — ${srv.planned?.reason}`
        + (srv.planned?.until ? `, until ${srv.planned.until}` : '')
        + '. Nothing is wrong with this map; it comes back when that machine does.'));
      box.append(card);
      continue;
    }
    if (state === 'unknown') {
      card.append(el('div', { class: 'banner err' },
        `Agent unreachable: ${srv.agent_error || 'no answer'}. This map is `
        + 'unmonitored until it is back; the rest of the cluster is unaffected.'));
      box.append(card);
      continue;
    }

    // Players on this map, by name.
    const pdata = S.players?.servers?.[cfg.name];
    let playerLine;
    if (!pdata) playerLine = el('span', { class: 'muted' }, 'checking…');
    else if (pdata.error) playerLine = el('span', { class: 'warn' }, pdata.error);
    else if (pdata.count === null) playerLine = el('span', { class: 'warn' }, 'RCON did not answer');
    else if (!pdata.count) playerLine = el('span', { class: 'muted' }, 'nobody online');
    else if (!pdata.names?.length) {
      // An agent on older code reports a count but no names. Show what it did
      // give us rather than throwing on the missing field.
      playerLine = el('span', {}, `${pdata.count} online`,
        el('span', { class: 'muted' }, ' — this Warden is on older code and does '
          + 'not report names; restart it to see them'));
    } else {
      playerLine = el('span', {}, ...pdata.names.map(n =>
        el('span', { class: 'playerchip' }, n)));
    }

    const health = srv.port_health || {};
    const agentSched = sched[cfg.name] || {};
    const lastBackup = (agentSched.servers || [])
      .find(s => s.server === cfg.name)?.last_backup;

    card.append(el('dl', { class: 'kv' },
      el('dt', {}, 'State'),
      el('dd', { class: state === 'online' ? 'ok' : state === 'maintenance' ? '' : 'bad' },
        state === 'maintenance'
          ? `stopped on purpose — ${srv.maintenance}`
          : state + (srv.pid ? ` · PID ${srv.pid}` : '')),
      el('dt', {}, 'Uptime'), el('dd', {}, srv.uptime || '—'),
      el('dt', {}, 'Memory'), el('dd', {}, srv.memory_gb ? `${srv.memory_gb} GB` : '—'),
      el('dt', {}, 'CPU'), el('dd', {}, srv.cpu_percent != null ? `${srv.cpu_percent}%` : '—'),
      el('dt', {}, 'Port'),
      el('dd', { class: health.state === 'ok' ? 'ok' : 'warn' },
        health.bound ? `${health.bound}${health.state === 'ok' ? '' : ' — ' + health.message}`
                     : (health.message || '—')),
      el('dt', {}, 'Disk free'),
      el('dd', { class: (srv.disk?.free_gb ?? 999) < 25 ? 'bad' : '' },
        srv.disk ? `${srv.disk.free_gb} GB` : '—'),
      el('dt', {}, 'Last backup'),
      el('dd', { class: lastBackup ? '' : 'warn' },
        lastBackup ? fmtAgo(lastBackup) : 'none recorded')));

    card.append(el('div', { class: 'playerrow' },
      el('span', { class: 'plabel' }, 'Players'), playerLine));

    if ((srv.duplicate_pids || []).length) {
      card.append(el('div', { class: 'banner err' },
        `TWO processes are running this map (PIDs ${srv.duplicate_pids.join(', ')}). `
        + 'They are writing to the same save files. Stop one.'));
    }

    // Straight to that map's controls, which is what you want next.
    const go = el('button', { class: 'btn grey' }, 'Open this map');
    go.onclick = () => { switchServer(cfg.name); selectView('servers'); };
    card.append(go);

    box.append(card);
  }
}

/** Remember a collapsed/expanded choice across reloads. */
function remember(key, value) {
  try { localStorage.setItem('clusterforge.' + key, value ? '1' : '0'); } catch { /* private mode */ }
}
function recall(key, fallback) {
  try {
    const v = localStorage.getItem('clusterforge.' + key);
    return v === null ? fallback : v === '1';
  } catch { return fallback; }
}

function renderAlertPanel() {
  const box = $('#alertPanel');
  if (!box || !S.alerts) return;

  const list = S.alerts.alerts || [];
  const active = list.filter(a => a.active);
  const worst = active.some(a => a.severity === 'critical') ? 'critical'
    : active.some(a => a.severity === 'warn') ? 'warn' : null;

  // Collapsed by default: history is something you go looking for, not
  // something that should sit open taking up the page. A real active alert
  // opens it on its own -- and still shows in the band above regardless.
  if (box.dataset.init !== '1') {
    box.dataset.init = '1';
    box.open = recall('alertHistoryOpen', false);
    box.addEventListener('toggle', () => remember('alertHistoryOpen', box.open));
  }
  if (worst && !box.dataset.forced) {
    box.dataset.forced = '1';
    box.open = true;
  } else if (!worst) {
    delete box.dataset.forced;
  }

  box.innerHTML = '';
  box.append(el('summary', { class: 'grouphead' },
    '▲ Alert history',
    el('span', { class: 'count' },
      list.length ? `${list.length} recorded` + (active.length ? ` · ${active.length} active` : '')
                  : 'nothing recorded')));

  if (!list.length) {
    box.append(el('div', { class: 'muted' },
      'Nothing has been raised. The monitor checks ports, unexpected stops, '
      + 'duplicate processes, a silent log and low disk every 60s — and only '
      + 'alerts on a real change of state, never on a single odd reading.'));
    return;
  }

  const t = el('table', {}, el('thead', {}, el('tr', {},
    el('th', {}, 'When'), el('th', {}, 'Map'), el('th', {}, 'Kind'),
    el('th', {}, 'State'), el('th', {}, 'What'))));
  const tb = el('tbody');
  for (const a of list.slice(0, 20)) {
    // "expired" is an informational event that aged out, which is not the same
    // thing as a condition that cleared.
    const state = a.active ? a.severity
      : a.acknowledged && a.condition_active ? `${a.severity} · acknowledged`
      : a.expired ? `${a.severity} · passed`
      : `${a.severity} · resolved`;
    tb.append(el('tr', {},
      el('td', { class: 'nowrap' }, a.at ? fmtAgo(a.at) : '—'),
      el('td', {}, a.server),
      el('td', { class: 'mono' }, a.kind),
      el('td', {
        class: !a.active ? 'muted'
          : a.severity === 'critical' ? 'bad' : a.severity === 'warn' ? 'warn' : '',
      }, state),
      el('td', { class: a.active ? '' : 'muted' }, a.message)));
  }
  t.append(tb);
  box.append(t);
}

/* ------------------------------------------------------ cluster-level views */

const fmtTime = (epoch) => epoch ? new Date(epoch * 1000).toLocaleString() : '—';
const fmtAgo = (epoch) => {
  if (!epoch) return 'never';
  const s = Math.max(0, Math.round(Date.now() / 1000 - epoch));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${(s / 86400).toFixed(1)}d ago`;
};

function kvGroup(title, colour, pairs) {
  const kv = el('dl', { class: 'kv' });
  for (const [k, v, cls] of pairs) {
    if (v === undefined) continue;
    kv.append(el('dt', {}, k), el('dd', { class: cls || '' }, v ?? '—'));
  }
  return el('div', { class: `group g-${colour}` }, el('h3', {}, title), kv);
}

/* ---- Any other game (generic) ---------------------------------------------- */

/* Games with hand-written pages; every other configured game gets viewGame. */
const HAS_OWN_PAGE = ['ark', 'palworld'];

/* One page per game, showing that game only: live status, its own controls,
 * its settings and its Discord post. Built from the game adapter API, so a
 * new game needs no page of its own. */
async function viewGame(pane) {
  const gameId = S.game;
  const game = (S.games || []).find(g => g.id === gameId);
  pane.innerHTML = '';
  if (!game || !game.configured) {
    pane.append(el('div', { class: 'banner' }, `${game?.name || gameId} is not set up yet.`));
    return;
  }
  const uptime = (s) => {
    if (!s && s !== 0) return '—';
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  };

  for (const srv of game.servers) {
    const base = `/api/games/${gameId}/servers/${encodeURIComponent(srv.name)}`;
    const label = srv.display_name || game.name;
    let st;
    try { st = await api(`${base}/status`); }
    catch (e) {
      pane.append(el('div', { class: 'banner err' }, `${label}: could not read status - ${e.message}`));
      continue;
    }
    const up = st.running === true;
    const players = Array.isArray(st.players)
      ? `${st.players.length}${st.players.length ? ' - ' + st.players.join(', ') : ''}`
      : (typeof st.players === 'number' ? String(st.players) : 'not reported by this game');
    pane.append(kvGroup(`◆ ${label}`, up ? 'green' : 'red', [
      ['Status', up ? 'online' : 'stopped', up ? 'ok' : 'bad'],
      ['Host', srv.host || undefined],
      ['Uptime', up ? uptime(st.uptime_seconds) : undefined],
      ['Memory', up && st.memory_gb != null ? `${st.memory_gb} GB` : undefined],
      ['Players', up ? players : undefined],
      ['Port', st.port ? String(st.port) : undefined, 'mono'],
      ['Installed', st.installed ? 'yes' : 'NO', st.installed ? 'ok' : 'bad'],
      ['Note', st.detail || undefined],
    ]));

    const ctl = el('div', { class: 'group g-green' }, el('h3', {}, `▶ ${label} controls`));
    const log = el('div', { class: 'joblog hidden' });
    const line = (t, cls = '') => { log.classList.remove('hidden'); log.append(el('div', { class: cls }, t)); };
    const act = async (action, verb, confirmText) => {
      if (confirmText && !confirm(confirmText)) return;
      log.innerHTML = '';
      line(`${verb} ${label}…`, 'muted');
      try {
        await postJSON(`${base}/${action}`, {});
        line('Requested. The status above refreshes in about 20 seconds.', 'ok');
        setTimeout(() => { if (S.view === 'game' && S.game === gameId) selectView('game'); }, 20000);
      } catch (e) { line(e.message, 'err'); }
    };
    const bStart = el('button', { class: 'btn green' }, '▶ Start');
    bStart.onclick = () => act('start', 'Starting');
    const bRestart = el('button', { class: 'btn blue' }, '🔄 Restart');
    bRestart.onclick = () => act('restart', 'Restarting',
      `Restart ${label}?\n\nIt saves and shuts down cleanly, then starts again. `
      + 'Anyone playing is disconnected for about a minute.');
    const bStop = el('button', { class: 'btn red' }, '⏹ Stop');
    bStop.onclick = () => act('stop', 'Stopping',
      `Stop ${label}?\n\nIt saves and shuts down. It stays off until someone starts it.`);
    if (up) guard(bStart, `${label} is already running.`);
    else { guard(bRestart, `${label} is not running.`); guard(bStop, `${label} is not running.`); }
    if (up) bStart.disabled = true; else { bRestart.disabled = true; bStop.disabled = true; }
    if (S.readOnly) [bStart, bRestart, bStop].forEach(b => guard(b, 'Enable writing in config.json first.'));
    ctl.append(bStart, bRestart, bStop, log);
    pane.append(ctl);

    pane.append(gameSettingsSection(gameId, srv.name));
  }

  /* Its Discord channel (discord.game_channels). Guardian refreshes the live
   * post by itself every minute; this is for after editing the join text. */
  const dis = el('div', { class: 'group g-purple' }, el('h3', {}, '◆ Discord channel'));
  const disLog = el('div', { class: 'joblog hidden' });
  dis.append(el('p', { class: 'muted' },
    `Guardian keeps a how-to-join post and a live status post with buttons in this `
    + `game's channel, if it has one in discord.game_channels.`));
  const pub = el('button', { class: 'primary' }, 'Publish to Discord');
  pub.onclick = async () => {
    disLog.innerHTML = ''; disLog.classList.remove('hidden');
    try {
      await postJSON(`/api/discord/games/${gameId}/publish`, {});
      disLog.append(el('div', { class: 'ok' }, 'Published.'));
    } catch (e) { disLog.append(el('div', { class: 'err' }, e.message)); }
  };
  if (S.readOnly) guard(pub, 'Enable writing in config.json first.');
  dis.append(pub, disLog);
  pane.append(dis);
}

/* ---- Palworld ------------------------------------------------------------- */

/* PC1 holds one game server at a time: the Island commits ~11 GB of 15.9, and
 * Palworld wants 8 or more. So this page is as much about the SWAP as about
 * Palworld - the two are mutually exclusive, and the page says so rather than
 * offering a Start button that would quietly fail. */
async function viewPalworld(pane) {
  const [pal, cluster] = await Promise.all([
    api('/api/palworld'),
    api('/api/cluster/status').catch(() => null),
  ]);
  pane.innerHTML = '';

  if (!pal.enabled) {
    pane.append(el('div', { class: 'banner' },
      'Palworld is not set up on this cluster. Add a "palworld" block to config.json.'));
    return;
  }
  if (pal.error) {
    pane.append(el('div', { class: 'banner err' }, pal.error));
  }

  const rows = Array.isArray(cluster) ? cluster : (cluster?.servers || []);
  const ark = rows.find(r => r.name === (pal.swap_with || 'island'));
  const arkUp = ark?.running === true;
  const palUp = pal.running === true;

  const uptime = (s) => {
    if (!s) return '—';
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h}h ${m}m` : `${m}m`;
  };

  pane.append(kvGroup('◆ Palworld server', palUp ? 'green' : 'red', [
    ['Status', palUp ? 'online' : 'stopped', palUp ? 'ok' : 'bad'],
    ['Server name', pal.server_name || '—'],
    ['Uptime', palUp ? uptime(pal.uptime_seconds) : undefined],
    ['Memory', palUp ? `${pal.private_gb} GB` : undefined],
    ['Players', pal.players === null ? 'unknown'
      : `${(pal.players || []).length}${(pal.players || []).length ? ' — ' + pal.players.join(', ') : ''}`],
    ['Port', `UDP ${pal.port}`, 'mono'],
    ['Installed', pal.installed ? 'yes' : 'NO', pal.installed ? 'ok' : 'bad'],
  ]));

  /* Controls for Palworld ALONE. The switch was the only control there was,
   * and it always moved both games - so after Palworld crashed there was no
   * way to bring it back without also stopping the ARK map. */
  const ctl = el('div', { class: 'group g-green' }, el('h3', {}, '▶ Palworld controls'));
  const ctlLog = el('div', { class: 'joblog hidden' });
  const ctlLine = (t, cls = '') => {
    ctlLog.classList.remove('hidden');
    ctlLog.append(el('div', { class: cls }, t));
  };

  const act = async (action, label, confirmText) => {
    if (confirmText && !confirm(confirmText)) return;
    ctlLog.innerHTML = '';
    ctlLine(`${label}…`, 'muted');
    try {
      await postJSON(`/api/palworld/control/${action}`, {});
      ctlLine('Requested. Watch the status above — give it up to a minute.', 'ok');
      // Re-render once it has had time to take effect.
      setTimeout(() => { if (S.view === 'palworld') selectView('palworld'); }, 20000);
    } catch (e) { ctlLine(e.message, 'err'); }
  };

  const bStart = el('button', { class: 'btn green' }, '▶ Start');
  bStart.onclick = () => act('start', 'Starting Palworld');
  const bStop = el('button', { class: 'btn red' }, '⏹ Stop');
  bStop.onclick = () => act('stop', 'Stopping Palworld',
    'Stop the Palworld server?\n\nIt saves first, then shuts down.');
  const bRestart = el('button', { class: 'btn blue' }, '🔄 Restart');
  bRestart.onclick = () => act('restart', 'Restarting Palworld',
    'Restart the Palworld server?\n\nIt saves, shuts down and comes straight '
    + 'back. About a minute.');

  if (palUp) guard(bStart, 'Palworld is already running.');
  else { guard(bStop, 'Palworld is not running.'); guard(bRestart, 'Palworld is not running.'); }
  // Only when Palworld shares PC1 with the ARK map. On its own machine (remote,
  // since 2026-11-09) the ARK map has nothing to do with it.
  if (arkUp && !pal.remote) guard(bStart, 'The ARK map is running — use Switch to Palworld instead.');
  if (S.readOnly) [bStart, bStop, bRestart].forEach(b =>
    guard(b, 'Enable writing in config.json first.'));

  ctl.append(bStart, bRestart, bStop, ctlLog);
  pane.append(ctl);

  /* The search term comes from the backend so the page and the Discord post
   * cannot end up telling people to search for different things. */
  const searchFor = pal.search_name || pal.server_name || '';
  pane.append(kvGroup('◆ How to join', 'cyan', [
    ['Xbox / PS5', searchFor
      ? `Community Servers tab in game — search "${searchFor}". They cannot join by IP.`
      : 'Community Servers tab in game. They cannot join by IP.'],
    ['Steam / PC', 'Community Servers, or direct connect to this PC on UDP ' + pal.port],
    ['Password', pal.password || '—'],
    ['Crossplay', 'Steam, Xbox, PS5, Mac'],
  ]));

  /* The swap. Only one side can be up, so the page offers the move that makes
   * sense right now rather than four buttons of which two would fail. */
  const swap = el('div', { class: 'group g-orange' },
    el('h3', {}, '◆ Which game is running'));
  const state = el('dl', { class: 'kv' },
    el('dt', {}, 'Palworld'), el('dd', { class: palUp ? 'ok' : '' },
      palUp ? 'online' : 'stopped'),
    el('dt', {}, ark?.display_name || 'ARK Island'),
    el('dd', { class: arkUp ? 'ok' : '' }, arkUp ? 'online' : 'stopped'));
  swap.append(state);

  const log = el('div', { class: 'joblog hidden' });
  const line = (text, cls = '') => {
    log.classList.remove('hidden');
    log.append(el('div', { class: cls }, text));
    log.scrollTop = log.scrollHeight;
  };

  const doSwitch = async (target, label) => {
    if (!confirm(`Switch to ${label}?\n\nThe other server is stopped first — `
               + `they cannot both run. This takes a few minutes.`)) return;
    line(`Switching to ${label}…`, 'muted');
    try {
      const r = await fetch('/api/palworld/switch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to: target }),
      });
      const body = await r.json();
      if (!r.ok) throw new Error(body.message || body.error);
      pollSwitch(line);
    } catch (e) {
      line(`Could not start the switch: ${e.message}`, 'err');
    }
  };

  /* ONE button that names the other game, because the swap only ever has one
   * sensible direction: whatever is running, you want the other. Two buttons
   * with one permanently greyed out made you read both to work out which was
   * live.
   *
   * The exception is nothing running at all. There is no "other" game to
   * toggle to then, and picking one for you would either hide the Island or
   * hide Palworld - so that case, and only that case, still offers both. */
  const arkName = ark?.display_name || 'ARK Island';
  const buttons = [];

  if (palUp || arkUp) {
    const toPalworld = palUp === false;   // ARK is up, so offer Palworld
    const btn = el('button', { class: 'primary' },
      toPalworld ? '→ Switch to Palworld' : `→ Switch to ${arkName}`);
    btn.onclick = () => (toPalworld
      ? doSwitch('palworld', 'Palworld')
      : doSwitch('ark', `the ${arkName}`));
    btn.title = toPalworld
      ? `Stops ${arkName} and starts Palworld. They cannot both run.`
      : `Stops Palworld and starts ${arkName}. They cannot both run.`;
    buttons.push(btn);
  } else {
    const startPal = el('button', { class: 'primary' }, '▶ Start Palworld');
    startPal.onclick = () => doSwitch('palworld', 'Palworld');
    const startArk = el('button', {}, `▶ Start ${arkName}`);
    startArk.onclick = () => doSwitch('ark', `the ${arkName}`);
    buttons.push(startPal, startArk);
    swap.append(el('div', { class: 'statline muted' },
      'Neither is running, so pick one.'));
  }

  // A switch already under way must not be able to start a second one.
  if (pal.switch?.running) {
    buttons.forEach(b => {
      b.disabled = true;
      b.title = `A switch to ${pal.switch.target} is already under way.`;
    });
  }
  if (S.readOnly) {
    buttons.forEach(b => guard(b, 'Enable writing in config.json first.'));
  }

  swap.append(...buttons, log);
  // The swap only exists while Palworld and the ARK map share PC1. Palworld
  // has its own machine now, so this page shows Palworld and nothing else.
  if (!pal.remote) pane.append(swap);

  if (!pal.remote && pal.switch?.running) {
    line(`A switch to ${pal.switch.target} is already under way…`, 'muted');
    for (const s of pal.switch.steps || []) line(`${s.at}  ${s.message}`);
    pollSwitch(line);
  } else if (!pal.remote && pal.switch?.error) {
    pane.append(el('div', { class: 'banner err' },
      `Last switch failed: ${pal.switch.error}`));
  }

  /* Settings, rendered generically from the adapter API. Every game gets
   * this screen - the panel asks the game what its settings are and how to
   * describe them, rather than knowing anything about this one. */
  pane.append(gameSettingsSection('palworld', 'palworld'));

  /* ---- the Discord half ---------------------------------------------- */
  const dis = el('div', { class: 'group g-purple' },
    el('h3', {}, '◆ Discord channel'));
  const disLog = el('div', { class: 'joblog hidden' });
  const disLine = (text, cls = '') => {
    disLog.classList.remove('hidden');
    disLog.append(el('div', { class: cls }, text));
  };

  dis.append(el('p', { class: 'muted' },
    'Guardian keeps the how-to-join post and the live status post with its '
    + 'buttons here, editing those two messages in place rather than adding new ones.'));

  /* A picker rather than "paste the channel id", which would mean turning on
   * Developer Mode first. The list is what Guardian can actually post in, so
   * a private channel it was never added to is visibly absent rather than
   * failing later with "Unknown Channel". */
  const pick = el('select', {});
  pick.append(el('option', { value: '' }, '— choose a channel —'));
  const pickNote = el('span', { class: 'statline' }, 'loading channels…');
  dis.append(el('div', { class: 'coreswitch' },
    el('label', {}, 'Channel'), pick, pickNote));

  api('/api/discord/channels').then(r => {
    pickNote.textContent = '';
    for (const c of r.channels || []) {
      const bits = [c.private ? 'private' : ''].filter(Boolean);
      if (!c.can_edit_in_place) bits.push('no history — cannot edit in place');
      pick.append(el('option', { value: c.id },
        `#${c.name}${bits.length ? '  (' + bits.join(', ') + ')' : ''}`));
    }
    pick.value = pal.discord_channel || '';
    if (!pal.discord_channel) pickNote.textContent = 'not set yet';
  }).catch(e => {
    pickNote.textContent = `could not list channels: ${e.message}`;
    pickNote.className = 'statline err';
  });

  pick.onchange = async () => {
    pickNote.className = 'statline';
    pickNote.textContent = 'saving…';
    try {
      await postJSON('/api/discord/palworld/channel', { channel: pick.value });
      pal.discord_channel = pick.value;
      pickNote.textContent = pick.value ? 'saved' : 'cleared';
    } catch (e) {
      pickNote.textContent = e.message;
      pickNote.className = 'statline err';
    }
  };

  const preview = el('button', {}, 'Preview');
  preview.onclick = async () => {
    disLog.innerHTML = '';
    try {
      const r = await postJSON('/api/discord/palworld/publish', { dry_run: true });
      disLine(`${r.messages} message(s), ${r.chars} characters:`, 'muted');
      for (const chunk of r.preview || []) disLine(chunk);
    } catch (e) { disLine(e.message, 'err'); }
  };

  const publish = el('button', { class: 'primary' }, 'Publish to Discord');
  publish.onclick = async () => {
    disLog.innerHTML = '';
    disLine('Publishing…', 'muted');
    try {
      const r = await postJSON('/api/discord/palworld/publish', {});
      const info = r.info || {};
      disLine(`Info post: ${info.posted || 0} new, ${info.edited || 0} edited, `
            + `${info.deleted || 0} removed.`, 'ok');
      disLine(r.edited ? 'Buttons updated in place.' : 'Buttons posted.', 'ok');
    } catch (e) { disLine(e.message, 'err'); }
  };

  if (S.readOnly) {
    guard(publish, 'Enable writing in config.json first.');
    guard(pick, 'Enable writing in config.json first.');
  }
  dis.append(preview, publish, disLog);
  pane.append(dis);
}

/* The switch takes minutes, so the page follows it rather than freezing. */
async function pollSwitch(line) {
  let seen = 0;
  for (let i = 0; i < 80; i++) {
    await new Promise(r => setTimeout(r, 4000));
    let data;
    try { data = await api('/api/palworld'); } catch { continue; }
    const sw = data.switch || {};
    for (const s of (sw.steps || []).slice(seen)) line(`${s.at}  ${s.message}`);
    seen = (sw.steps || []).length;
    if (!sw.running) {
      line(sw.error ? `Failed: ${sw.error}` : 'Done.', sw.error ? 'err' : 'ok');
      if (S.view === 'palworld') selectView('palworld');
      return;
    }
  }
}

/* ---- Updates -------------------------------------------------------------- */

async function viewUpdates(pane) {
  const [state, versions] = await Promise.all([
    api('/api/cluster/updates'),
    api('/api/cluster/status').catch(() => null),
  ]);
  pane.innerHTML = '';

  if (state.error) {
    pane.append(el('div', { class: 'banner err' }, state.error));
    return;
  }

  const last = state.last_result || {};
  const perServer = last.servers || {};

  pane.append(el('div', { class: 'cols2' },
    kvGroup('⟳ Version watch', 'green', [
      ['Polling', state.running ? `every ${state.poll_seconds}s` : 'NOT RUNNING',
        state.running ? 'ok' : 'bad'],
      ['Steam branch', state.branch],
      ['Auto-apply', state.auto_apply ? 'ON — patches applied automatically'
                                      : 'off — detection only', state.auto_apply ? 'ok' : 'warn'],
      ['Last check', `${fmtAgo(state.last_check)} (${fmtTime(state.last_check)})`],
      // The two safety knobs. Shown because they were working invisibly, and a
      // safety setting you cannot see is one you cannot trust.
      ['Settle delay', state.settle_minutes
        ? `${state.settle_minutes} min after a build appears`
        : 'OFF — patches the moment a build is spotted',
        state.settle_minutes ? 'ok' : 'warn'],
      ['Stagger', state.stagger
        ? 'ON — one map at a time, stops if one fails'
        : 'off — both maps patched together',
        state.stagger ? 'ok' : 'warn'],
    ]),
    kvGroup('▤ Cluster build', 'cyan', [
      ['Needs update', (last.needs_update || []).length
        ? (last.needs_update || []).join(', ') : 'nothing — cluster is current',
        (last.needs_update || []).length ? 'warn' : 'ok'],
      ['Unreachable', (last.unreachable || []).length
        ? (last.unreachable || []).join(', ') : 'none',
        (last.unreachable || []).length ? 'bad' : 'ok'],
      ['Build unknown', (last.unknown || []).length
        ? (last.unknown || []).join(', ') : 'none',
        (last.unknown || []).length ? 'warn' : 'ok'],
    ])));

  // Per-map builds, which is the thing you actually look at when wondering
  // whether the cluster has split.
  const rows = el('table', { class: 'grid' },
    el('thead', {}, el('tr', {},
      el('th', {}, 'Map'), el('th', {}, 'Installed build'),
      el('th', {}, 'Available'), el('th', {}, 'State'))));
  const tb = el('tbody');
  for (const cfg of S.config.servers) {
    const v = perServer[cfg.name] || {};
    const behind = v.installed && v.available && String(v.installed) !== String(v.available);
    tb.append(el('tr', {},
      el('td', {}, cfg.display_name),
      el('td', { class: 'mono' }, v.installed ?? '—'),
      el('td', { class: 'mono' }, v.available ?? '—'),
      el('td', { class: behind ? 'bad' : (v.installed ? 'ok' : 'warn') },
        v.error ? v.error : behind ? 'BEHIND' : v.installed ? 'current' : 'unknown')));
  }
  rows.append(tb);
  pane.append(el('div', { class: 'group g-orange' },
    el('h3', {}, '▤ Build per map'), rows));

  // Actions
  const log = el('div', { class: 'joblog hidden' });
  const line = (t, c) => { log.classList.remove('hidden'); log.append(el('div', { class: c || '' }, t)); };
  const check = el('button', { class: 'btn blue' }, 'Check for updates now');
  check.onclick = async () => {
    check.disabled = true; check.textContent = 'Checking…';
    try {
      const r = await fetch('/api/cluster/updates/check', { method: 'POST' });
      const b = await r.json();
      line(`Checked: ${(b.needs_update || []).length ? 'update available for '
        + b.needs_update.join(', ') : 'everything is current'}`,
        (b.needs_update || []).length ? 'warn' : 'ok');
      selectView('updates');
    } catch (e) { line(`Check failed: ${e.message}`, 'err'); }
    check.disabled = false; check.textContent = 'Check for updates now';
  };
  const actions = el('div', { class: 'group g-red' }, el('h3', {}, '▶ Update actions'), check);
  for (const key of ['update-all', 'sur-all']) {
    const a = CLUSTER_ACTIONS[key];
    const b = el('button', { class: `btn ${key === 'sur-all' ? 'purple' : 'orange'}` }, a.label);
    if (S.readOnly) guard(b, 'Enable writing in config.json first.');
    else b.onclick = () => runCluster(key, b);
    actions.append(b);
  }
  actions.append(log);
  pane.append(actions);

  // History
  if ((state.history || []).length) {
    const hist = el('div', { class: 'logview' });
    for (const h of [...state.history].reverse()) {
      const names = Object.keys(h.results || {}).join(', ');
      hist.append(el('div', { class: (h.errors || []).length ? 'err' : '' },
        `${fmtTime(h.at)}  ${(h.plan || {}).reason || ''}  →  ${names || 'no targets'}`
        + ((h.errors || []).length ? `  ERRORS: ${h.errors.join('; ')}` : '')));
    }
    pane.append(el('div', { class: 'group g-purple' },
      el('h3', {}, '▤ Update history'), hist));
  } else {
    pane.append(el('div', { class: 'banner info' },
      'No update has run yet. Detection, cluster coordination and every refusal path are '
      + 'tested, but a live steamcmd patch has never happened here — the first real one is '
      + 'the true test. On failure the sequence deliberately leaves that map STOPPED rather '
      + 'than starting on a half-applied install, and a pre-update backup is always taken.'));
  }
}

/* ---- Backups -------------------------------------------------------------- */

async function viewBackups(pane) {
  const results = await Promise.all(S.config.servers.map(async cfg => {
    try { return { cfg, data: await api(`/api/server/${cfg.name}/backups`) }; }
    catch (e) { return { cfg, error: e.message }; }
  }));
  pane.innerHTML = '';

  const b = S.config.backup || {};
  pane.append(kvGroup('▤ Backup policy', 'green', [
    ['Versioned saves', b.enabled === false ? 'OFF'
      : `every ${b.save_interval_hours ?? 4}h`, b.enabled === false ? 'bad' : 'ok'],
    ['Keep locally', b.local_keep],
    ['Keep on external', b.external_keep],
    ['External root', b.external_root, 'mono'],
    ['Install mirror', b.mirror_on_scheduled_restart === false ? 'off'
      : 'refreshed on each scheduled restart, while the server is stopped'],
  ]));

  pane.append(await clusterTransferBackupGroup());
  pane.append(await characterRescueGroup());

  for (const { cfg, data, error } of results) {
    const box = el('div', { class: 'group g-cyan' },
      el('h3', {}, `▤ ${cfg.display_name}`));
    if (error) {
      box.append(el('div', { class: 'banner err' }, `Warden unreachable: ${error}`));
      pane.append(box);
      continue;
    }

    for (const [label, tier] of [['Local', data.local], ['External', data.external]]) {
      const list = tier.backups || [];
      box.append(el('div', { class: 'statline' },
        `${label}: ${list.length} snapshot(s), keep ${tier.keep ?? '—'} — ${tier.root || '—'}`));
      if (tier.error) box.append(el('div', { class: 'banner warn' }, tier.error));
      if (!list.length) continue;

      const t = el('table', { class: 'grid' },
        el('thead', {}, el('tr', {},
          el('th', {}, 'Snapshot'), el('th', {}, 'Taken'),
          el('th', {}, 'Files'), el('th', {}, 'Size'))));
      const tb = el('tbody');
      for (const snap of list.slice(0, 12)) {
        tb.append(el('tr', {},
          el('td', { class: 'mono' }, snap.stamp),
          el('td', {}, fmtAgo(snap.created)),
          el('td', {}, String(snap.files)),
          el('td', {}, `${snap.gb} GB`)));
      }
      t.append(tb);
      box.append(t);
      if (list.length > 12) {
        box.append(el('div', { class: 'muted' }, `…and ${list.length - 12} older.`));
      }
    }

    // Backup and mirror are safe to run on demand. Restore is deliberately NOT
    // here: it overwrites live saves, requires the server stopped and a typed
    // confirmation, and belongs behind more than one click.
    const log = el('div', { class: 'joblog hidden' });
    const line = (t, c) => { log.classList.remove('hidden'); log.append(el('div', { class: c || '' }, t)); };
    for (const [act, label, cls] of [['backup', 'Back up now', 'green'],
                                     ['mirror', 'Refresh install mirror', 'blue']]) {
      const btn = el('button', { class: `btn ${cls}` }, label);
      if (S.readOnly) guard(btn, 'Enable writing in config.json first.');
      else btn.onclick = async () => {
        btn.disabled = true;
        const was = btn.textContent; btn.textContent = 'Working…';
        try {
          const r = await fetch(`/api/server/${cfg.name}/${act}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
          const body = await r.json();
          if (!r.ok) line(body.message || body.error || `HTTP ${r.status}`, 'err');
          else if (body.job?.id) {
            await followJobs([{ server: cfg.name, id: body.job.id, label: cfg.display_name }],
              line, label);
          }
        } catch (e) { line(`Failed: ${e.message}`, 'err'); }
        btn.disabled = false; btn.textContent = was;
      };
      box.append(btn);
    }
    box.append(log);
    box.append(restoreControls(cfg, data, line));
    pane.append(box);
  }
}

/* Put a saved copy of one character back into the transfer area.
 *
 * For the case that actually happened: a transfer crashed mid-upload and the
 * survivor existed nowhere -- not on the map it left, not on the one it was
 * going to. Every upload is versioned, so the fix is to put one back and let
 * the player download it on any map.
 *
 * Deliberately blunt about the cost: the copy is from a moment in time, so
 * anything gained since is gone. Refused while that player is connected, which
 * is how you get two of somebody. */
async function characterRescueGroup() {
  const box = el('div', { class: 'group g-orange' },
    el('h3', {}, '⛑ Rescue a character'));
  let data;
  try { data = await api('/api/cluster/characters'); }
  catch (e) {
    box.append(el('div', { class: 'banner err' }, `Could not read the saved copies: ${e.message}`));
    return box;
  }
  const characters = (data.characters || []).filter(c => (c.versions || []).length);
  if (!characters.length) {
    box.append(el('div', { class: 'statline' },
      'No saved character copies yet. One is kept every time somebody uploads at an obelisk.'));
    return box;
  }

  box.append(el('div', { class: 'statline' },
    'Use this when a crash during a transfer leaves someone with no character anywhere. '
    + 'It puts a saved copy back in the transfer area; they then download it at any obelisk.'));

  for (const character of characters) {
    const good = (character.versions || []).filter(v => v.size > 0);
    const row = el('div', { class: 'maprow' });
    const who = character.name || `account ${character.id.slice(0, 12)}…`;
    row.append(el('div', {},
      el('b', {}, who),
      el('div', { class: 'statline' },
        character.last_seen_on ? `last seen on ${character.last_seen_on}` : 'never seen online yet',
        ` · ${good.length} saved cop${good.length === 1 ? 'y' : 'ies'}`,
        character.online_on ? ` · ONLINE on ${character.online_on}` : '')));

    if (!good.length) {
      row.append(el('div', { class: 'statline' }, 'nothing to restore (only empty copies)'));
      box.append(row);
      continue;
    }

    const pick = el('select', { class: 'sel' });
    for (const v of good) {
      pick.append(el('option', { value: v.name },
        `${new Date(v.when).toLocaleString()} — ${(v.size / 1024 / 1024).toFixed(1)} MB`));
    }
    const note = el('div', { class: 'statline' }, '');
    const btn = el('button', { class: 'btn amber' }, 'Restore to transfer area');
    btn.onclick = async () => {
      const chosen = good.find(v => v.name === pick.value);
      const when = new Date(chosen.when);
      if (!confirm(
        `Put ${who}'s character back into the transfer area?\n\n`
        + `Copy from: ${when.toLocaleString()} (${(chosen.size / 1024 / 1024).toFixed(1)} MB)\n\n`
        + 'This is an OLD copy. Anything gained since then — items, tames carried, levels — '
        + 'is not in it.\n\nThey must be fully out of the game first, then download at any '
        + 'obelisk. Continue?')) return;
      btn.disabled = true;
      note.textContent = 'Restoring…';
      try {
        const r = await fetch(`/api/cluster/characters/${character.id}/restore`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ version: pick.value }),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok || !body.ok) throw new Error(body.message || body.error || `HTTP ${r.status}`);
        note.textContent = `Done — the copy from ${when.toLocaleString()} is waiting in the `
          + 'transfer area. Download it at any obelisk.';
        showBanner('ok', `${who} restored from ${when.toLocaleString()}`);
      } catch (e) {
        note.textContent = '';
        showBanner('err', `Restore failed: ${e.message}`);
      } finally {
        btn.disabled = false;
      }
    };
    if (S.readOnly) guard(btn, 'Enable writing in config.json first.');
    row.append(el('div', { class: 'checkline' }, pick, btn));
    box.append(row, note);
  }
  return box;
}

/* The cluster transfer folder: survivors, tames and items between maps.
 *
 * Shown first on the Backups page because it is the one thing with no other
 * copy while a transfer is in flight. If the watcher is not running, that is
 * shown in red rather than as an empty list -- a missing transfer backup must
 * never look like an idle one. */
async function clusterTransferBackupGroup() {
  const box = el('div', { class: 'group g-orange' },
    el('h3', {}, '⇄ Cluster transfer data'));
  let data;
  try { data = await api('/api/cluster/transfer-backups'); }
  catch (e) {
    box.append(el('div', { class: 'banner err' }, `Could not read its status: ${e.message}`));
    return box;
  }
  const when = iso => iso ? new Date(iso).toLocaleString() : '—';

  if (!data.running) {
    box.append(el('div', { class: 'banner err' },
      `NOT being backed up — ${data.reason || 'the watcher is not running'}. `
      + 'A survivor caught in a crashed transfer would have no other copy.'));
    return box;
  }

  box.append(kvGroup('Watcher', 'orange', [
    ['Status', `copying every change, checked every ${data.poll_seconds}s`, 'ok'],
    ['Watching', data.cluster_dir, 'mono'],
    ['Kept', `last ${data.keep_versions} versions per player, up to ${data.keep_days} days — `
      + 'the newest non-empty copy is never pruned'],
    ['Last copy', data.last_snapshot
      ? `${fmtAgo(data.last_snapshot.at)} (${data.last_snapshot.size} bytes)` : 'none yet'],
    ['Mirror', data.mirror_root || 'none', 'mono'],
  ]));
  if (data.last_error) {
    box.append(el('div', { class: 'banner warn' }, `Last problem: ${data.last_error}`));
  }

  const files = data.files || [];
  if (files.length) {
    const t = el('table', { class: 'grid' },
      el('thead', {}, el('tr', {},
        el('th', {}, 'Player file'), el('th', {}, 'Versions'),
        el('th', {}, 'Latest good copy'), el('th', {}, 'Right now'))));
    const tb = el('tbody');
    for (const f of files) {
      const id = f.file.split('/').pop();
      // Emptied since the last good copy: ARK does this after a download, so it
      // is usually normal -- but it is also what a crashed download looks like.
      const emptied = f.newest_size === 0 && f.latest_non_empty_size > 0;
      tb.append(el('tr', {},
        el('td', { class: 'mono' }, id.slice(0, 12) + '…'),
        el('td', {}, String(f.versions)),
        el('td', {}, f.latest_non_empty
          ? `${when(f.latest_non_empty)} · ${f.latest_non_empty_size} bytes` : 'never had uploads'),
        el('td', { class: emptied ? 'warn' : '' },
          f.newest_size === 0 ? 'empty (nothing uploaded)' : `${f.newest_size} bytes`)));
    }
    t.append(tb);
    box.append(t);
  }
  box.append(el('div', { class: 'statline' },
    'If a transfer crashes and a character goes missing, keep that player OFFLINE and ask for '
    + 'their latest good copy to be restored into the cluster folder. Restoring while they are '
    + 'mid-transfer could duplicate or overwrite them.'));
  return box;
}

/* Pick a restore point and roll the world back to it.
 *
 * Two gates, not one. The dropdown plus a plain confirm would make an
 * irreversible world rollback a two-tap action on a phone, so the second gate
 * is TYPING the word -- it cannot be hit by accident, and typing it makes you
 * read what you are about to do.
 *
 * Defaults to the world only. The snapshot also holds Config, but rolling that
 * back would revert every rate and setting tuned since, which is almost never
 * what someone recovering a lost build wants -- so it is an explicit tick.
 *
 * It does NOT start the server afterwards, on purpose: look at the world before
 * letting anyone in, because the moment players connect, the restore is much
 * harder to walk back.
 */
function restoreControls(cfg, data, line) {
  const points = [];
  for (const [tier, label] of [['local', 'Local'], ['external', 'External']]) {
    for (const snap of (data[tier]?.backups || [])) {
      points.push({ ...snap, source: tier, tierLabel: label });
    }
  }
  points.sort((a, b) => b.created - a.created);

  const wrap = el('div', { class: 'group g-red', style: 'margin-top:6px' },
    el('h3', {}, '⟲ Restore a point in time'));

  if (!points.length) {
    wrap.append(el('div', { class: 'statline warn' }, 'No snapshots to restore from.'));
    return wrap;
  }

  const select = el('select', { style: 'width:100%;margin-bottom:5px' });
  for (const p of points) {
    select.append(el('option', { value: `${p.source}|${p.stamp}` },
      `${fmtTime(p.created)} — ${fmtAgo(p.created)} · ${p.gb} GB · ${p.tierLabel}`));
  }

  const alsoConfig = el('input', { type: 'checkbox', id: `cfgroll-${cfg.name}` });
  const configLine = el('label', { for: `cfgroll-${cfg.name}`, class: 'checkline' },
    alsoConfig, el('span', {}, ' Also roll back Game.ini / GameUserSettings.ini'));

  const btn = el('button', { class: 'btn red wide' }, `Restore ${cfg.display_name}`);

  if (S.readOnly) {
    guard(btn, 'Enable writing in config.json first.');
  } else {
    btn.onclick = () => runRestore(cfg, points, select, alsoConfig, btn, line);
  }

  wrap.append(
    el('div', { class: 'statline' },
      'Rolls the whole map back to the chosen moment. Everything done since — tames, '
      + 'levels, structures, items, transfers — is lost, for every player, not just the '
      + 'thing you are recovering.'),
    select, configLine, btn,
    el('div', { class: 'statline' },
      'The server must be stopped; a safety snapshot of the current state is taken first, '
      + 'so the restore itself can be undone. The server is left stopped afterwards so you '
      + 'can check the world before anyone joins.'));
  return wrap;
}

async function runRestore(cfg, points, select, alsoConfig, btn, line) {
  const [source, stamp] = select.value.split('|');
  const point = points.find(p => p.stamp === stamp && p.source === source);
  if (!point) return;

  const parts = alsoConfig.checked ? ['SavedArks', 'Config'] : ['SavedArks'];
  const live = (S.status?.servers || []).find(s => s.name === cfg.name);

  if (!confirm(
    `Roll ${cfg.display_name} back to ${fmtTime(point.created)}?\n\n`
    + `Snapshot: ${point.stamp} (${point.tierLabel}, ${point.gb} GB)\n`
    + `Restoring: ${parts.join(' + ')}\n\n`
    + `EVERYTHING on this map since ${fmtAgo(point.created)} is lost — tames, levels, `
    + `structures, items and transfers, for every player.\n\n`
    + (live?.state === 'online'
      ? 'The server is running and will be STOPPED first.\n\n' : '')
    + 'A safety snapshot is taken first, so this can be undone.')) return;

  const typed = prompt(
    `Type RESTORE to confirm rolling ${cfg.display_name} back to ${point.stamp}.`);
  if (typed !== 'RESTORE') {
    showBanner('info', 'Restore cancelled — nothing was changed.');
    return;
  }

  const was = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Restoring…';
  const done = (msg, cls) => {
    if (msg) line(msg, cls);
    btn.disabled = false;
    btn.textContent = was;
    refresh();
  };

  try {
    if (live?.state === 'online') {
      line(`Stopping ${cfg.display_name} before restoring…`, 'muted');
      const r = await fetch(`/api/server/${cfg.name}/stop`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: 'Server going down for a save restore in {minutes} minutes '
                 + '- please log out safely',
        }),
      });
      const b = await r.json();
      if (!r.ok) return done(b.message || b.error || `HTTP ${r.status}`, 'err');
      if (b.job?.id) {
        const out = await followJobs(
          [{ server: cfg.name, id: b.job.id, label: '' }], line, 'Stop');
        if (out[cfg.name] !== 'done') {
          return done('Stop did not finish cleanly — NOTHING was restored.', 'err');
        }
      }
    }

    line(`Restoring ${parts.join(' + ')} from ${stamp}…`, 'muted');
    const r = await fetch(`/api/server/${cfg.name}/restore`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stamp, source, parts, confirm: 'RESTORE' }),
    });
    const b = await r.json();
    if (!r.ok) return done(b.message || b.error || `HTTP ${r.status}`, 'err');

    if (b.job?.id) {
      const out = await followJobs(
        [{ server: cfg.name, id: b.job.id, label: '' }], line, 'Restore');
      if (out[cfg.name] !== 'done') {
        return done('Restore FAILED — the server was left stopped and nothing was started.',
                    'err');
      }
    }
    done(`Restored from ${stamp}. ${cfg.display_name} is still STOPPED — start it and `
       + 'check the world before letting anyone in. If it is wrong, restore the safety '
       + 'snapshot listed above.', 'ok');
  } catch (e) {
    done(`Restore failed: ${e.message}`, 'err');
  }
}

/* ---- Discord -------------------------------------------------------------- */

async function viewDiscord(pane) {
  const data = await api('/api/discord');
  pane.innerHTML = '';

  const bot = (title, colour, st, role) => {
    if (st?.error) {
      return el('div', { class: `group g-${colour}` }, el('h3', {}, title),
        el('div', { class: 'banner err' }, st.error));
    }
    const ready = st?.ready ?? st?.running;
    return kvGroup(title, colour, [
      ['Role', role],
      ['Configured', st?.configured ? 'yes' : 'NO TOKEN', st?.configured ? 'ok' : 'bad'],
      ['Connected', ready ? 'yes' : 'no', ready ? 'ok' : 'bad'],
      ['Bot user', st?.user],
      ['Channel', st?.channel != null ? String(st.channel) : undefined, 'mono'],
    ]);
  };

  pane.append(bot('◆ Guardian', 'red', data.guardian,
    'Control for BOTH maps — start, stop, restart, save, backup, update. '
    + 'Lives in the private control channel; commands are locked to the owner id.'));

  const cols = el('div', { class: 'cols2' });
  for (const cfg of S.config.servers) {
    cols.append(bot(`◆ ${cfg.display_name} watcher`, 'cyan', data[cfg.name],
      'Live status embed for this map only, edited every 30s, plus /status and /players.'));
  }
  pane.append(cols);

  const log = el('div', { class: 'joblog hidden' });
  const test = el('button', { class: 'btn blue' }, 'Send a test alert');
  test.onclick = async () => {
    test.disabled = true;
    log.classList.remove('hidden');
    try {
      const r = await fetch('/api/discord/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const b = await r.json();
      log.append(el('div', { class: r.ok ? 'ok' : 'err' },
        b.message || b.error || (r.ok ? 'Test alert sent.' : `HTTP ${r.status}`)));
    } catch (e) { log.append(el('div', { class: 'err' }, e.message)); }
    test.disabled = false;
  };
  pane.append(el('div', { class: 'group g-orange' },
    el('h3', {}, '▶ Check delivery'), test, log,
    el('div', { class: 'muted', style: 'margin-top:4px' },
      'A Discord token allows only one live gateway session, so PatchRaptor’s watcher '
      + 'tasks must stay disabled on both PCs — two processes using one token knock '
      + 'each other offline.')));
}

/* --------------------------------------------------- Server Management Core */

function renderCore() {
  const srv = S.status?.servers.find(s => s.name === S.server);
  const cfg = S.config.servers.find(s => s.name === S.server);
  const grid = $('#coreGrid');
  grid.innerHTML = '';
  if (!srv || !cfg) { grid.append(el('div', { class: 'spin' }, 'no server selected')); return; }

  if (srv.state === 'unknown') {
    grid.append(el('div', { class: 'banner err', style: 'grid-column:1/-1' },
      `Agent unreachable on ${cfg.host}: ${srv.agent_error}. `
      + `This server shows as unknown and its controls are disabled; the rest of the cluster is unaffected.`));
  }

  // Installation. Prefer the path the AGENT reports: that is the server's real
  // location on its own machine. The panel's own config holds PC1's view of it,
  // which for Svartalfheim is the Z: share — showing that would suggest the
  // server runs off a network drive, which it does not.
  const realPath = srv.install_dir || cfg.install_dir;
  const viaShare = cfg.install_dir && realPath !== cfg.install_dir ? cfg.install_dir : null;

  grid.append(el('div', { class: 'group g-red' },
    el('h3', {}, '⚙ Installation'),
    el('div', { class: 'kv' },
      el('dt', {}, 'Path'), el('dd', { class: 'mono' }, realPath),
      ...(viaShare ? [el('dt', {}, 'From here'), el('dd', { class: 'mono muted' }, viaShare)] : []),
      el('dt', {}, 'Map'), el('dd', {}, cfg.map_name),
      el('dt', {}, 'Host'), el('dd', {}, cfg.host))
  ));

  // Server Controls
  const controls = el('div', { class: 'group g-orange' }, el('h3', {}, '▶ Server Controls'));
  const pane = jobPane(cfg.name);

  const ACTIONS = [
    { id: 'start',   label: 'Start server',   cls: 'green',
      confirm: null,
      need: false },
    { id: 'restart', label: 'Restart server', cls: 'blue',
      confirm: 'Restart {name}?\n\nPlayers are warned first if any are online, the '
             + 'world is saved, and the process must fully exit before it starts again.',
      need: true },
    { id: 'stop',    label: 'Stop server',    cls: 'orange',
      confirm: 'Stop {name}?\n\nPlayers are warned first if any are online, and the '
             + 'world is saved before shutdown.',
      need: true },
    { id: 'kill',    label: 'Kill server',    cls: 'red',
      confirm: 'FORCE KILL {name}?\n\nNo warning, no save. Anything since the last '
             + 'save is lost. Only use this if the server is genuinely hung — '
             + 'Stop is almost always the right choice.',
      need: true },
    { id: 'save',    label: 'Save the game',  cls: 'purple',
      confirm: null, need: false },
  ];

  for (const action of ACTIONS) {
    const b = el('button', { class: `btn ${action.cls}` }, action.label);
    if (S.readOnly) {
      guard(b, 'Enable writing in config.json first.');
    } else if (srv.state === 'unknown') {
      b.disabled = true;
      b.style.opacity = '0.45';
      b.title = 'The Warden for this server is unreachable.';
    } else if (pane.running) {
      // Only one control job runs per server at a time - the agent enforces
      // this and would return 409, but the buttons should not invite the click.
      b.disabled = true;
      b.style.opacity = '0.45';
      b.title = `${pane.running} is already running on this server.`;
    } else {
      b.onclick = () => runControl(action, cfg, b);
    }
    controls.append(b);
  }
  controls.append(pane.box);   // re-attached, never rebuilt: see jobPane()
  grid.append(controls);

  // Server Status
  const status = el('div', { class: 'group g-green' }, el('h3', {}, '■ Server Status'));
  const kv = el('dl', { class: 'kv' });
  const add = (k, v, cls) => kv.append(el('dt', {}, k), el('dd', { class: cls || '' }, v));
  if (srv.state === 'unknown') {
    add('State', 'unknown — Warden unreachable', 'bad');
  } else if (srv.running) {
    add('State', 'ONLINE', 'good');
    add('PID', srv.pid);
    add('Uptime', srv.uptime);
    add('Memory', `${srv.memory_gb} GB`);
    const ph = srv.port_health || {};
    add('Game port', ph.bound ?? '—',
        ph.state === 'ok' ? 'good' : ph.state === 'drifted' ? 'warn' : 'bad');
    add('UDP bound', (srv.udp_ports || []).join(', '));
    if (ph.state !== 'ok') add('Port note', ph.message, 'warn');
    if (!srv.cmdline_readable) {
      add('Cmdline', 'unreadable — Warden needs elevation', 'warn');
    }
  } else {
    add('State', 'OFFLINE', 'bad');
  }
  if (srv.duplicate_pids?.length) {
    add('DUPLICATES', `${srv.duplicate_pids.join(', ')} — two processes on the same saves`, 'bad');
  }
  status.append(kv);
  grid.append(status);

  // Logs / health
  if (srv.logs) {
    const lg = el('div', { class: 'group g-cyan' }, el('h3', {}, '≡ Log Health'));
    const k2 = el('dl', { class: 'kv' });
    k2.append(el('dt', {}, 'Active log'), el('dd', { class: 'mono' }, srv.logs.active_name || '—'));
    if (srv.logs.active_age_seconds != null) {
      k2.append(el('dt', {}, 'Last written'),
        el('dd', { class: srv.logs.active_age_seconds > 600 ? 'warn' : 'good' },
          `${srv.logs.active_age_seconds}s ago`));
    }
    k2.append(el('dt', {}, 'Candidates'), el('dd', {}, srv.logs.candidates));
    lg.append(k2);
    if (srv.logs.stale?.length) {
      lg.append(el('div', { class: 'banner warn', style: 'margin-top:6px' },
        `${srv.logs.stale.length} stale numbered log(s): `
        + srv.logs.stale.map(s => `${s.name} (${s.age_hours}h)`).join(', ')
        + '. Detection picks the newest by write time, so these are inert — but clearing them removes the trap.'));
    }
    grid.append(lg);
  }
}

/* --------------------------------------------------------------- inner tabs */

function renderTabStrip() {
  const strip = $('#tabStrip');
  strip.innerHTML = '';
  for (const t of TABS) {
    strip.append(el('button', {
      class: t.id === S.tab ? 'active' : '',
      onclick: () => selectTab(t.id),
    }, t.label));
  }
}

function selectTab(id) {
  S.tab = id;
  renderTabStrip();
  renderTab();
}

async function renderTab() {
  const body = $('#tabBody');
  body.innerHTML = '<div class="spin">loading&hellip;</div>';
  if (!S.server) { body.innerHTML = '<div class="spin">no server</div>'; return; }
  try {
    switch (S.tab) {
      case 'mods':        return await tabMods(body);
      case 'gus':         return await tabSettings(body, 'GameUserSettings.ini');
      case 'game':        return await tabSettings(body, 'Game.ini');
      case 'override':    return await tabRaw(body, 'Game.ini');
      case 'commandline': return await tabCommandLine(body);
      case 'servermgmt':  return await tabServerMgmt(body);
      case 'mapmgmt':     return await tabMapMgmt(body);
      case 'logs':        return await tabLogs(body);
    }
  } catch (e) {
    body.innerHTML = '';
    body.append(el('div', { class: 'banner err' }, `Could not load: ${esc(e.message)}`));
  }
}

/* ---- Mod Management ---- */

/* Working copy of the mod load order while it is being edited. Order matters:
 * leftmost wins on conflicts, so this is a list, never a set. */
let modState = null;

async function tabMods(body) {
  const data = await api(`/api/server/${S.server}/mods`);
  body.innerHTML = '';

  const meta = new Map(data.mods.map(m => [m.mod_id, m]));
  const original = data.mods.filter(m => m.in_load_order).map(m => m.mod_id);
  modState = { list: [...original], original, selected: null, meta };

  const table = el('div', { class: 'tablewrap' });
  const counts = el('dl', { class: 'kv' });
  const status = el('div', { class: 'statline' });

  const saveBtn = el('button', { class: 'btn green wide' }, 'Save mod list');
  const revertBtn = el('button', { class: 'btn grey wide' }, 'Revert changes');

  /* Scope. Saving THIS map writes the list you see. Saving both maps sends the
   * CHANGE rather than the list, so each map keeps its own exclusives -- the
   * Island's Additional Island Dinos and Svartalfheim's four are not meant to
   * be levelled into one shared list, and sending a whole list would do exactly
   * that. Reordering has no delta form, so it stays per-map. */
  const bothBox = el('input', { type: 'checkbox', id: 'modsBoth' });
  const bothLabel = el('label', { for: 'modsBoth', class: 'checkline' }, bothBox,
    el('span', {}, ' Apply to both maps'));
  const bothNote = el('div', { class: 'statline' }, '');
  const otherMaps = (S.config.servers || [])
    .filter(s => s.name !== S.server).map(s => s.display_name || s.name);
  bothBox.onchange = () => { refreshChrome(); };

  function changed() {
    return modState.list.join(',') !== modState.original.join(',');
  }

  function refreshChrome() {
    const added = modState.list.filter(m => !modState.original.includes(m));
    const removed = modState.original.filter(m => !modState.list.includes(m));
    const reordered = !added.length && !removed.length && changed();

    counts.innerHTML = '';
    const add = (k, v, cls) => counts.append(el('dt', {}, k), el('dd', { class: cls || '' }, v));
    add('In load order', modState.list.length);
    add('Installed', data.installed);
    add('Drift', data.drifted, data.drifted ? 'bad' : 'good');
    if (added.length) add('Adding', added.length, 'good');
    if (removed.length) add('Removing', removed.length, 'bad');
    if (reordered) add('Reordered', 'yes', 'warn');

    const added2 = modState.list.filter(m => !modState.original.includes(m));
    const removed2 = modState.original.filter(m => !modState.list.includes(m));
    const reorderOnly = !added2.length && !removed2.length && changed();

    if (bothBox.checked) {
      saveBtn.textContent = 'Save to both maps';
      bothNote.textContent = reorderOnly
        ? `Reordering cannot apply to both — load order is per map. Untick to reorder ${S.server}.`
        : `Adds and removals go to ${S.server} and ${otherMaps.join(', ') || 'the other map'}. `
          + 'Each map keeps its own exclusive mods.';
      bothNote.className = 'statline ' + (reorderOnly ? 'warn' : '');
    } else {
      saveBtn.textContent = 'Save mod list';
      bothNote.textContent = `Writes ${S.server} only.`
        + (otherMaps.length ? ` ${otherMaps.join(', ')} is left alone.` : '');
      bothNote.className = 'statline';
    }

    saveBtn.disabled = !changed() || S.readOnly
                       || (bothBox.checked && reorderOnly);
    saveBtn.style.opacity = saveBtn.disabled ? '0.45' : '1';
    revertBtn.disabled = !changed();
    revertBtn.style.opacity = revertBtn.disabled ? '0.45' : '1';
    status.textContent = changed()
      ? 'Unsaved — takes effect on next server restart'
      : '';
    status.style.color = changed() ? 'var(--yellow)' : '';
  }

  function renderTable() {
    const rows = modState.list.map((id, index) => {
      const m = meta.get(id) || { mod_id: id, name: `Mod${id}`, installed: false };
      const isNew = !modState.original.includes(id);
      const tr = el('tr', {
        class: (m.drift ? 'drift ' : '') + (modState.selected === id ? 'selected ' : '') + (isNew ? 'newmod' : ''),
        onclick: () => { modState.selected = id; renderTable(); },
      },
        el('td', {}, el('input', { type: 'checkbox', checked: m.enabled !== false, disabled: true })),
        el('td', {}, el('input', { type: 'checkbox', checked: !!m.passive, disabled: true })),
        el('td', {}, el('input', { type: 'checkbox', checked: !!m.dev, disabled: true })),
        el('td', { class: 'num' }, index + 1),
        el('td', {}, m.logo ? el('img', { class: 'modlogo', src: m.logo, loading: 'lazy', alt: '' }) : ''),
        el('td', {}, el('strong', {}, m.name),
          isNew ? el('span', { class: 'tag', style: 'background:#1d4f2b;color:#8ce99a;margin-left:5px' }, 'new') : '',
          m.drift ? el('div', { style: 'color:var(--orange);font-size:10px' }, m.drift) : ''),
        el('td', { class: 'num mono' }, id),
        el('td', {}, m.author || '—'),
        el('td', { class: 'nowrap muted' }, (m.last_updated || '—').replace('-', ' ')),
        el('td', {}, m.url ? el('a', { href: m.url, target: '_blank', rel: 'noopener' }, 'CurseForge') : '—'));
      return tr;
    });

    // Mods installed on disk but absent from the load order — shown so the
    // gap is visible rather than silently ignored.
    for (const m of data.mods) {
      if (modState.list.includes(m.mod_id)) continue;
      rows.push(el('tr', { class: 'drift' },
        el('td', { colspan: 4, class: 'muted' }, 'not loaded'),
        el('td', {}, m.logo ? el('img', { class: 'modlogo', src: m.logo, alt: '' }) : ''),
        el('td', {}, m.name, el('div', { style: 'color:var(--orange);font-size:10px' }, m.drift || 'not in launch args')),
        el('td', { class: 'num mono' }, m.mod_id),
        el('td', {}, m.author || '—'), el('td', {}, '—'), el('td', {}, '—')));
    }

    table.innerHTML = '';
    table.append(el('table', {},
      el('thead', {}, el('tr', {},
        ...['Enable', 'Passive', 'Dev', '#', '', 'Mod Name', 'Mod ID', 'Author', 'Last Updated', 'Link']
          .map(h => el('th', {}, h)))),
      el('tbody', {}, ...rows)));
    refreshChrome();
  }

  function move(delta) {
    const i = modState.list.indexOf(modState.selected);
    if (i < 0) { showBanner('warn', 'Select a mod row first.'); return; }
    const j = delta === 'top' ? 0 : i + delta;
    if (j < 0 || j >= modState.list.length) return;
    modState.list.splice(i, 1);
    modState.list.splice(j, 0, modState.selected);
    renderTable();
  }

  function removeSelected() {
    const id = modState.selected;
    if (!id) { showBanner('warn', 'Select a mod row first.'); return; }
    const m = meta.get(id);
    if (!confirm(
      `Remove ${m ? m.name : id} (${id}) from ${S.server}?\n\n`
      + 'Anything players have stored inside this mod is permanently lost, and the '
      + 'server must restart for the change to apply.\n\nThis only stages the change — '
      + 'you still have to press Save.')) return;
    modState.list = modState.list.filter(x => x !== id);
    modState.selected = null;
    renderTable();
  }

  saveBtn.onclick = () => (bothBox.checked ? saveModsToBoth() : saveModList(data));
  revertBtn.onclick = () => { modState.list = [...modState.original]; modState.selected = null; renderTable(); };
  if (S.readOnly) guard(saveBtn, 'Enable writing in config.json first.');

  const side = el('div', {},
    el('div', { class: 'group g-green' }, el('h3', {}, 'Actions'),
      (() => { const b = el('button', { class: 'btn green wide' }, 'Refresh List'); b.onclick = () => renderTab(); return b; })(),
      (() => {
        const b = el('button', { class: 'btn teal wide' }, 'Copy Mod List');
        b.onclick = () => {
          navigator.clipboard.writeText(modState.list.join(','))
            .then(() => showBanner('ok', `Copied ${modState.list.length} mod IDs.`))
            .catch(() => showBanner('err', 'Clipboard blocked by the browser.'));
        };
        return b;
      })(),
      (() => {
        const b = el('button', { class: 'btn blue wide' }, 'Open Mods Folder');
        b.onclick = () => showBanner('info',
          `Mods live under ${S.server === 'island' ? 'D:\\Servers\\TheIsland' : 'Z:\\Svartalfhiem'}`
          + '\\ShooterGame\\Binaries\\Win64\\ShooterGame\\Mods — the server downloads them itself on boot.');
        return b;
      })(),
      bothLabel, bothNote, saveBtn, revertBtn, status),
    el('div', { class: 'group g-red', style: 'margin-top:5px' }, el('h3', {}, 'Reorder'),
      (() => { const b = el('button', { class: 'btn grey wide' }, 'Move to top'); b.onclick = () => move('top'); return b; })(),
      (() => { const b = el('button', { class: 'btn grey wide' }, 'Move up'); b.onclick = () => move(-1); return b; })(),
      (() => { const b = el('button', { class: 'btn grey wide' }, 'Move down'); b.onclick = () => move(1); return b; })(),
      (() => { const b = el('button', { class: 'btn red wide' }, 'Remove mod'); b.onclick = removeSelected; return b; })(),
      el('div', { class: 'statline' }, 'Click a row to select it. Leftmost mod wins on conflicts.')),
    el('div', { class: 'group g-cyan', style: 'margin-top:5px' }, el('h3', {}, 'Counts'), counts)
  );

  renderTable();

  const note = el('div', { class: 'banner info' },
    'Load order is significant — leftmost wins on conflicts. Names, authors and dates come '
    + "from the game's own library.json, so no CurseForge API key is needed. The server installs "
    + 'mods itself on boot from the -mods= list; ClusterForge only maintains that list.');

  // Browsing CurseForge properly needs an API key. Until then, send the search
  // straight to CurseForge in a new tab — genuinely useful, and honest about
  // what it is, rather than an empty tab pretending to be a browser.
  const searchBox = el('input', {
    type: 'text', placeholder: 'Search CurseForge for ASA mods…',
    style: 'font-family:"Segoe UI",sans-serif',
  });
  const doSearch = () => {
    const q = searchBox.value.trim();
    if (!q) return;
    window.open(
      'https://www.curseforge.com/ark-survival-ascended/search?page=1&search=' + encodeURIComponent(q),
      '_blank', 'noopener');
  };
  searchBox.onkeydown = (e) => { if (e.key === 'Enter') doSearch(); };

  const finder = el('div', { class: 'group g-cyan', style: 'margin-top:6px' },
    el('h3', {}, '⌕ Find Mods'),
    el('div', { class: 'searchbar' },
      searchBox,
      el('button', { class: 'btn teal', onclick: doSearch }, 'Search on CurseForge')),
    el('div', { class: 'statline' },
      'Opens CurseForge in a new tab. In-panel browsing needs a free API key from '
      + 'console.curseforge.com — add one and this becomes a real browser. '
      + 'Copy the mod ID from the page and paste it below.'));

  body.append(note,
    el('div', { class: 'sidebyside' }, side, table),
    el('div', { class: 'cols2' }, finder, addManuallyBlock(renderTable)));
}

async function saveModList(data) {
  const added = modState.list.filter(m => !modState.original.includes(m));
  const removed = modState.original.filter(m => !modState.list.includes(m));

  let msg = `Write the mod list for ${S.server}?\n\n`;
  msg += `Load order: ${modState.original.length} → ${modState.list.length} mods\n`;
  if (added.length) {
    msg += `\nAdding:\n` + added.map(id =>
      `  + ${(modState.meta.get(id) || {}).name || id} (${id})`).join('\n');
  }
  if (removed.length) {
    msg += `\n\nREMOVING — anything players stored in these is permanently lost:\n`
      + removed.map(id => `  - ${(modState.meta.get(id) || {}).name || id} (${id})`).join('\n');
  }
  if (!added.length && !removed.length) msg += '\nOrder changed only.';
  msg += '\n\nStartServer.bat is rewritten. Takes effect on the next server restart.';
  if (!confirm(msg)) return;

  try {
    const r = await fetch(`/api/server/${S.server}/mods`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mods: modState.list, confirm_removals: removed.length > 0 }),
    });
    const j = await r.json();
    if (!r.ok) {
      showBanner('err', `Refused: ${j.message || j.error}`);
      return;
    }
    showBanner('ok',
      `Mod list written (${j.after.length} mods). `
      + (j.added.length ? `Added ${j.added.length}. ` : '')
      + (j.removed.length ? `Removed ${j.removed.length}. ` : '')
      + (j.reordered ? 'Reordered. ' : '')
      + `Backup: ${j.written.backup || 'none'}. Applies on next restart.`);
    renderTab();
  } catch (e) {
    showBanner('err', `Save failed: ${e.message}`);
  }
}

/* Apply the mod CHANGE to every map at once.
 *
 * Sends the delta, not the list. Each map has exclusives -- the Island's
 * Additional Island Dinos, Svartalfheim's four -- and pushing one whole list to
 * both would silently flatten them into a single shared set. Sending "+ these,
 * - those" adds the QOL mod everywhere while leaving each map's own mods alone.
 *
 * Dry-runs first so the confirm shows what each map ACTUALLY ends up with,
 * which is not the same number for both.
 */
async function saveModsToBoth() {
  const add = modState.list.filter(m => !modState.original.includes(m));
  const remove = modState.original.filter(m => !modState.list.includes(m));
  if (!add.length && !remove.length) {
    showBanner('info', 'Load order is per map — untick "Apply to both maps" to reorder.');
    return;
  }

  const label = id => `${(modState.meta.get(id) || {}).name || id} (${id})`;

  let plan;
  try {
    const r = await fetch('/api/cluster/mods', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ add, remove, dry_run: true }),
    });
    plan = await r.json();
    if (!r.ok) {
      showBanner('err', `Refused: ${plan.message || plan.error}`);
      return;
    }
  } catch (e) {
    showBanner('err', `Could not plan the change: ${e.message}`);
    return;
  }

  let msg = 'Apply this mod change to BOTH maps?\n\n';
  if (add.length) msg += 'Adding everywhere:\n' + add.map(i => `  + ${label(i)}`).join('\n') + '\n';
  if (remove.length) {
    msg += '\nREMOVING everywhere — anything players stored in these is permanently lost:\n'
         + remove.map(i => `  - ${label(i)}`).join('\n') + '\n';
  }
  msg += '\nResulting mod count per map:\n';
  for (const name of Object.keys(plan.after || {})) {
    msg += `  ${name}: ${(plan.before[name] || []).length} → ${(plan.after[name] || []).length}\n`;
  }
  msg += '\nBoth start scripts are rewritten. Takes effect on each map’s next restart.';
  if (!confirm(msg)) return;

  try {
    const r = await fetch('/api/cluster/mods', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ add, remove, confirm_removals: remove.length > 0 }),
    });
    const j = await r.json();
    if (!r.ok && !j.results) {
      showBanner('err', `Refused: ${j.message || j.error}`);
      return;
    }
    const lines = Object.entries(j.results || {}).map(([name, out]) =>
      out.skipped ? `${name}: no change needed`
        : out.error ? `${name}: ${out.message || out.error}`
        : `${name}: ${(out.after || []).length} mods`);
    showBanner(j.ok ? 'ok' : 'warn',
      (j.ok ? 'Applied to both maps. ' : 'Applied with problems. ')
      + lines.join(' · ')
      + (j.failures?.length ? ` FAILED: ${j.failures.join('; ')}` : '')
      + ' Applies on next restart.');
    renderTab();
  } catch (e) {
    showBanner('err', `Save failed: ${e.message}`);
  }
}

/* ---- Settings editors ---- */

/* Pending edits for the settings tab, keyed section|key|occurrence so one of
 * the 42 repeated tuning lines can be edited without touching its siblings. */
let dirty = new Map();
let currentEtag = null;
let currentFile = null;

function dirtyKey(s) { return `${s.section}|${s.key}|${s.occurrence}`; }

function markDirty(s, value, originalValue) {
  const k = dirtyKey(s);
  if (String(value) === String(originalValue)) dirty.delete(k);
  else dirty.set(k, { op: 'set', section: s.section, key: s.key, occurrence: s.occurrence, value: String(value) });
  updateDirtyUI();
}

function updateDirtyUI() {
  const statusEl = document.getElementById('dirtyStatus');
  if (!statusEl) return;
  const n = dirty.size;
  statusEl.textContent = n ? `${n} unsaved change${n === 1 ? '' : 's'}` : '';
  statusEl.style.color = n ? 'var(--yellow)' : 'var(--text-dim)';
  const save = document.getElementById('saveSettings');
  if (save && !S.readOnly) {
    save.disabled = n === 0;
    save.style.opacity = n === 0 ? '0.45' : '1';
  }
}

async function tabSettings(body, filename) {
  const data = await api(`/api/server/${S.server}/settings/${filename}`);
  body.innerHTML = '';
  dirty = new Map();
  currentEtag = data.etag;
  currentFile = filename;

  const search = el('input', { type: 'text', placeholder: 'Filter settings…' });
  search.oninput = () => {
    const q = search.value.toLowerCase();
    body.querySelectorAll('.setting').forEach(row => {
      row.classList.toggle('hidden', q && !row.dataset.k.includes(q));
    });
    body.querySelectorAll('.cat').forEach(cat => {
      const any = [...cat.querySelectorAll('.setting')].some(r => !r.classList.contains('hidden'));
      cat.classList.toggle('hidden', !any);
      if (q && any) cat.open = true;
    });
  };

  const status = el('span', { class: 'muted nowrap', id: 'dirtyStatus' });

  const saveBtn = el('button', { class: 'btn green', id: 'saveSettings' }, 'Save');
  if (S.readOnly) {
    guard(saveBtn, 'Editing is enabled once read_only is turned off in config.json.');
  } else {
    saveBtn.disabled = true;
    saveBtn.style.opacity = '0.45';
    saveBtn.onclick = () => saveSettings(filename, saveBtn, status);
  }

  const reloadBtn = el('button', { class: 'btn blue' }, 'Reload');
  reloadBtn.onclick = () => {
    if (dirty.size && !confirm(`Discard ${dirty.size} unsaved change(s)?`)) return;
    renderTab();
  };

  const notepadBtn = el('button', { class: 'btn orange' }, 'Open in Notepad');
  notepadBtn.onclick = async () => {
    try {
      const r = await fetch(`/api/server/${S.server}/open-notepad`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: filename }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.message || j.error);
      showBanner('ok', `Opened ${j.path} in Notepad. Reload here after saving there.`);
    } catch (e) { showBanner('err', `Could not open Notepad: ${e.message}`); }
  };

  body.append(el('div', { class: 'searchbar' },
    search,
    el('span', { class: 'muted nowrap' },
      `${data.counts.known} documented · ${data.counts.unknown} mod/unlisted · `
      + `${data.counts.repeated} repeated · ${data.encoding}`),
    status, saveBtn, reloadBtn, notepadBtn));

  body.append(el('div', { class: 'banner info' },
    'This file is kept read-only on purpose — that is what stops ARK overwriting your settings '
    + 'when the server shuts down. ClusterForge clears the flag only for the instant of the write '
    + 'and puts it straight back. Changes apply on the next server restart.'));

  if (data.counts.repeated) {
    body.append(el('div', { class: 'banner warn' },
      `${data.counts.repeated} lines use a repeated key. These are per-item and per-resource `
      + 'tuning entries — each line configures a different thing and every one is shown '
      + 'separately. They are never merged or reordered.'));
  }

  // Wiki-documented settings, in their categories.
  if (data.categories.length) {
    body.append(el('div', { class: 'subhead' }, 'Documented server settings'));
    for (const cat of data.categories) {
      const det = el('details', { class: 'cat' });
      det.append(el('summary', {}, cat.name,
        el('span', { class: 'count' }, `${cat.settings.length}`)));
      for (const s of cat.settings) det.append(settingRow(s));
      body.append(det);
    }
  }

  // Everything else, grouped by the ini section that owns it. On this cluster
  // those sections are per-mod, so each mod gets its own expander with its real
  // name and icon rather than being buried in one undifferentiated pile.
  const sections = data.sections || [];
  const modSecs = sections.filter(s => s.kind !== 'engine');
  const engSecs = sections.filter(s => s.kind === 'engine');

  if (modSecs.length) {
    const orphans = modSecs.filter(s => s.orphan).length;
    body.append(el('div', { class: 'subhead', style: 'color:var(--purple)' },
      `Mod settings — ${modSecs.length} mods`,
      orphans ? el('span', { class: 'muted' }, `  (${orphans} left over from removed mods)`) : ''));
    for (const sec of modSecs) body.append(sectionGroup(sec));
  }

  if (engSecs.length) {
    body.append(el('div', { class: 'subhead', style: 'color:var(--text-dim)' },
      'Engine & client settings'));
    for (const sec of engSecs) body.append(sectionGroup(sec));
  }
}

function sectionGroup(sec) {
  const det = el('details', { class: 'cat sec-' + sec.kind + (sec.orphan ? ' orphan' : '') });
  const head = el('summary', {});

  if (sec.logo) {
    head.append(el('img', { class: 'modlogo', src: sec.logo, loading: 'lazy', alt: '' }));
  }
  head.append(el('span', { class: 'seclabel' }, sec.label));
  if (sec.section && sec.section.toLowerCase() !== sec.label.toLowerCase()) {
    head.append(el('span', { class: 'secraw' }, `[${sec.section}]`));
  }
  if (sec.orphan) {
    head.append(el('span', { class: 'tag orphanTag' }, 'mod not loaded'));
  }
  head.append(el('span', { class: 'count' }, `${sec.settings.length}`));
  det.append(head);

  if (sec.orphan) {
    det.append(el('div', { class: 'banner warn', style: 'margin:6px' },
      sec.kind === 'mod'
        ? 'This mod is installed but not in the launch args, so these settings do nothing until it is loaded again.'
        : 'No mod in the current load order claims this section — almost certainly leftover config '
          + 'from a mod that was removed. Harmless, and kept exactly as-is.'));
  }
  // Inside a section group the [Section] suffix on every row is just noise.
  for (const s of sec.settings) det.append(settingRow(s, true));
  return det;
}

function settingRow(s, hideSection = false) {
  const label = el('div', { class: 'name' }, s.key);
  if (s.repeated) label.append(el('span', { class: 'tag repeat' }, `${s.occurrence}/${s.occurrences}`));
  if (s.section && !hideSection) label.append(el('small', {}, `[${s.section}]`));

  const locked = S.readOnly;
  const original = s.value;
  let control;

  if (s.type === 'boolean') {
    const isTrue = String(s.value).trim().toLowerCase() === 'true';
    control = el('input', { type: 'checkbox', disabled: locked, checked: isTrue });
    control.onchange = () => {
      // Match the casing already in the file rather than forcing True/False.
      const asWritten = /^[a-z]+$/.test(String(original).trim());
      const v = control.checked ? 'True' : 'False';
      markDirty(s, asWritten ? v.toLowerCase() : v, original);
      row.classList.toggle('edited', dirty.has(dirtyKey(s)));
    };
  } else {
    control = el('input', { type: 'text', value: s.value, disabled: locked });
    control.oninput = () => {
      markDirty(s, control.value, original);
      row.classList.toggle('edited', dirty.has(dirtyKey(s)));
      if (slider && slider.type === 'range') {
        const n = parseFloat(control.value);
        if (!Number.isNaN(n)) slider.value = String(n);
      }
    };
  }

  // A slider alongside the number, as DSM does, when the value is numeric.
  let slider = el('span', {});
  const num = parseFloat(s.value);
  if ((s.type === 'float' || s.type === 'integer') && !Number.isNaN(num)) {
    const max = Math.max(num * 2, num + 10, 10);
    slider = el('input', {
      type: 'range', min: 0, max: String(max),
      step: s.type === 'integer' ? '1' : '0.01',
      value: String(num), disabled: locked,
    });
    slider.oninput = () => {
      control.value = slider.value;
      markDirty(s, slider.value, original);
      row.classList.toggle('edited', dirty.has(dirtyKey(s)));
    };
  }

  const info = el('span', {
    class: 'muted nowrap',
    title: s.description || '',
  }, s.default ? `default ${s.default}` : (s.description ? 'ℹ' : ''));

  const row = el('div', { class: 'setting' }, label, control, slider, info);
  row.dataset.k = (s.key + ' ' + (s.section || '')).toLowerCase();
  return row;
}

async function saveSettings(filename, btn, statusEl) {
  if (!dirty.size) return;
  const changes = [...dirty.values()];

  const summary = changes.slice(0, 6)
    .map(c => `  ${c.key}${c.occurrence > 1 ? ` (#${c.occurrence})` : ''} → ${c.value}`)
    .join('\n');
  const more = changes.length > 6 ? `\n  …and ${changes.length - 6} more` : '';
  if (!confirm(
    `Write ${changes.length} change(s) to ${filename}?\n\n${summary}${more}\n\n`
    + 'A timestamped backup is taken first, and the file\'s read-only flag is restored '
    + 'afterwards. Changes take effect on the next server restart.')) return;

  const label = btn.textContent;
  btn.textContent = 'Saving…';
  btn.disabled = true;

  try {
    const r = await fetch(`/api/server/${S.server}/settings/${filename}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ etag: currentEtag, changes }),
    });
    const j = await r.json();
    if (!r.ok) {
      if (r.status === 409) {
        showBanner('err', `${j.message} — press Reload to pick up the current file.`);
      } else {
        showBanner('err', `Save refused: ${j.message || j.error}`);
      }
      btn.textContent = label;
      btn.disabled = false;
      return;
    }
    showBanner('ok',
      `Wrote ${j.count} change(s) to ${filename}. Backup: ${j.backup || 'none'}. `
      + (j.readonly_restored ? 'Read-only protection restored. ' : '')
      + 'Takes effect on next restart.');
    dirty = new Map();
    renderTab();
  } catch (e) {
    showBanner('err', `Save failed: ${e.message}`);
    btn.textContent = label;
    btn.disabled = false;
  }
}

async function tabRaw(body, filename) {
  const data = await api(`/api/server/${S.server}/settings/${filename}/raw`);
  body.innerHTML = '';
  body.append(
    el('div', { class: 'searchbar' },
      el('span', { class: 'muted mono' }, data.path),
      el('span', { class: 'muted' }, `(${data.encoding})`),
      (() => { const b = el('button', { class: 'btn green' }, 'Save'); guard(b, 'Editing arrives in Phase 2.'); return b; })()),
    el('div', { class: 'banner info' },
      'Raw view. The parser preserves every line, comment, blank and repeated key byte-for-byte '
      + '— verified against your real configs before any write path was built.'),
    el('textarea', { rows: 30, disabled: true, class: 'mono' }, data.text)
  );
}

/* ---- Command Line ---- */

async function tabCommandLine(body) {
  const data = await api(`/api/server/${S.server}/launch`);
  body.innerHTML = '';
  const L = data.launch || {};

  body.append(el('div', { class: 'banner ' + (data.conflicts?.length ? 'warn' : 'ok') },
    data.conflicts?.length
      ? `${data.conflicts.length} disagreement(s) between sources — listed below.`
      : `Sources agree. Authoritative: ${data.authoritative_source}.`));

  const kv = el('dl', { class: 'kv' });
  const add = (k, v) => kv.append(el('dt', {}, k), el('dd', { class: 'mono' }, v ?? '—'));
  add('Map', L.map_name); add('Session', L.session_name);
  add('Game port', L.port); add('RCON port', L.rcon_port);
  add('Max players', L.max_players); add('Platform', L.platform);
  add('Cluster', L.cluster_id); add('Cluster dir', L.cluster_dir);
  add('Mods', `${L.mod_count} in load order`);

  body.append(el('div', { class: 'cols2' },
    el('div', { class: 'group g-cyan' }, el('h3', {}, 'Resolved Launch Settings'), kv),
    el('div', { class: 'group g-purple' }, el('h3', {}, 'Switches'),
      el('div', {}, (L.switches || []).map(s => el('span', { class: 'tag', style: 'margin:2px;display:inline-block;padding:2px 6px;background:#3a3a44;border-radius:3px' }, '-' + s))),
      el('div', { class: 'statline' },
        'Xbox crossplay needs -ServerPlatform=ALL with -WinLiveMaxPlayers. '
        + 'Der Dino Finder needs -preventHibernation.'))
  ));

  body.append(el('div', { class: 'group g-orange', style: 'margin-top:5px' },
    el('h3', {}, 'Full command line'),
    el('textarea', { rows: 5, disabled: true, class: 'mono' }, data.cmdline || '')));

  // Taking ownership of StartServer.bat. Safe while a server is running: the
  // file is only read at launch, so nothing changes until the next restart.
  const owned = !!data.owned;
  const adopt = el('button', { class: 'btn purple' },
    owned ? 'Regenerate StartServer.bat' : 'Adopt StartServer.bat');
  adopt.onclick = async () => {
    if (!confirm(
      'Regenerate StartServer.bat from the current launch arguments?\n\n'
      + 'The original is archived as StartServer.bat.pre-clusterforge the first time, '
      + 'and a timestamped backup is taken every time.\n\n'
      + 'Nothing about the running server changes — this file is only read at launch. '
      + 'While DSM is still doing the launching, it is not used at all.')) return;
    try {
      const r = await fetch(`/api/server/${S.server}/launch`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      const j = await r.json();
      if (!r.ok) { showBanner('err', `Refused: ${j.message || j.error}`); return; }
      showBanner('ok',
        `StartServer.bat written from ${j.basis}.`
        + (j.written.archived_original ? ' Original archived as .pre-clusterforge.' : '')
        + ` Backup: ${j.written.backup || 'none'}.`);
      renderTab();
    } catch (e) { showBanner('err', `Failed: ${e.message}`); }
  };
  if (S.readOnly) guard(adopt, 'Enable writing in config.json first.');

  body.append(el('div', { class: 'group g-purple', style: 'margin-top:5px' },
    el('h3', {}, 'Start script ownership'),
    el('div', { class: 'statline', style: 'margin-bottom:6px' },
      owned
        ? 'ClusterForge owns StartServer.bat — it is regenerated whenever mods or launch options change, '
          + 'so it can no longer drift out of date the way a hand-maintained script does.'
        : 'DSM builds its own command line and never reads StartServer.bat, which is exactly why that '
          + 'file went stale unnoticed. Adopting it makes ClusterForge keep it accurate, ready for the '
          + 'Phase 4 cutover when ClusterForge starts launching the servers.'),
    adopt));

  const src = el('div', { class: 'tablewrap', style: 'margin-top:5px' },
    el('table', {},
      el('thead', {}, el('tr', {}, ...['Source', 'Available', 'Mods', 'Port', 'RCON', 'Detail'].map(h => el('th', {}, h)))),
      el('tbody', {}, ...Object.values(data.sources || {}).map(s => el('tr', {},
        el('td', {}, s.source),
        el('td', { style: `color:${s.available ? 'var(--green)' : 'var(--text-faint)'}` }, s.available ? 'yes' : 'no'),
        el('td', { class: 'num' }, s.mod_count),
        el('td', { class: 'num' }, s.port ?? '—'),
        el('td', { class: 'num' }, s.rcon_port ?? '—'),
        el('td', { class: 'muted' }, s.detail))))));
  body.append(el('div', { class: 'subhead' }, 'Sources compared'), src);

  if (data.conflicts?.length) {
    const rows = data.conflicts.map(c => el('tr', {},
      el('td', {}, el('strong', {}, c.field)),
      el('td', { class: 'mono' }, Object.entries(c.values).map(([k, v]) => `${k}: ${v}`).join('   '))));
    body.append(el('div', { class: 'subhead' }, 'Disagreements'),
      el('div', { class: 'tablewrap' }, el('table', {}, el('tbody', {}, ...rows))));
  }
}

/* ---- Server Management (operational settings imported from DSM) ---- */

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday',
                  'Friday', 'Saturday', 'Sunday'];

async function tabServerMgmt(body) {
  const [data, sched] = await Promise.all([
    api(`/api/server/${S.server}/launch`),
    api('/api/cluster/schedule'),
  ]);
  const op = data.operational || {};
  const cur = sched.panel_config?.schedule || {};
  body.innerHTML = '';

  /* ---- editable restart schedule ---- */

  const form = el('div', { class: 'group g-orange' },
    el('h3', {}, '⏱ Scheduled restarts'),
    el('div', { class: 'muted', style: 'margin-bottom:6px' },
      'Applied to BOTH maps at once. Two maps restarting at different times means '
      + 'transfers land on a map that is down.'));

  const enabled = el('input', { type: 'checkbox' });
  enabled.checked = !!cur.enabled;
  form.append(el('label', { class: 'check' }, enabled, ' Restart on a schedule'));

  const dayBoxes = {};
  const days = el('div', { class: 'daypick' });
  for (const d of WEEKDAYS) {
    const cb = el('input', { type: 'checkbox' });
    cb.checked = (cur.restart_days || []).includes(d);
    dayBoxes[d] = cb;
    days.append(el('label', { class: 'check' }, cb, ' ' + d.slice(0, 3)));
  }
  form.append(el('div', { class: 'field' }, el('label', {}, 'Days'), days));

  const time = el('input', { type: 'time', value: cur.restart_time || '23:00' });
  form.append(el('div', { class: 'field' }, el('label', {}, 'Time'), time));

  const skip = el('input', {
    type: 'number', min: '0', step: '1', style: 'width:70px',
    value: String(cur.skip_if_restarted_within_hours ?? 8),
  });
  form.append(el('div', { class: 'field' }, el('label', {}, 'Skip if restarted within'),
    skip, el('span', { class: 'muted' }, 'hours — so a patch at 6pm does not cause a '
      + 'second disruption at 11pm')));

  const warn = el('input', {
    type: 'text', style: 'width:260px',
    value: (cur.warn_minutes || []).join(', '),
  });
  form.append(el('div', { class: 'field' }, el('label', {}, 'Warning ladder'),
    warn, el('span', { class: 'muted' }, 'minutes before shutdown — skipped entirely '
      + 'when nobody is online')));

  const msg = el('input', {
    type: 'text', style: 'width:420px',
    value: cur.warn_message || 'Server restarting in {minutes} minutes',
  });
  form.append(el('div', { class: 'field' }, el('label', {}, 'Warning message'), msg,
    el('span', { class: 'muted' }, '{minutes} is substituted')));

  const wipe = el('input', { type: 'checkbox' });
  wipe.checked = !!cur.wipe_wild_dinos;
  form.append(el('label', { class: 'check' }, wipe,
    ' Wipe wild dinos when the server comes back up'));
  form.append(el('div', { class: 'muted', style: 'margin:2px 0 8px 22px' },
    'Runs DestroyWildDinos once the map is joinable — respawns everything untamed at '
    + 'fresh levels. Tamed dinos, structures and players are untouched. It runs only on '
    + 'the SCHEDULED restart, never on a reboot recovery: a power cut should not quietly '
    + 'wipe the map.'));

  const status = el('span', { class: 'statline' });
  const save = el('button', { class: 'btn green' }, 'Save schedule');
  if (S.readOnly) guard(save, 'Enable writing in config.json first.');
  else save.onclick = async () => {
    save.disabled = true; status.textContent = 'Saving…'; status.className = 'statline';
    const payload = {
      enabled: enabled.checked,
      restart_days: WEEKDAYS.filter(d => dayBoxes[d].checked),
      restart_time: time.value,
      skip_if_restarted_within_hours: Number(skip.value),
      warn_minutes: warn.value.split(/[,\s]+/).filter(Boolean).map(Number),
      warn_message: msg.value,
      wipe_wild_dinos: wipe.checked,
    };
    try {
      const r = await fetch('/api/cluster/schedule', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const b = await r.json();
      if (!r.ok || !b.ok) {
        const detail = Object.entries(b.results || {})
          .filter(([, v]) => v.error)
          .map(([n, v]) => `${n}: ${(v.messages || [v.message || v.error]).join('; ')}`)
          .join(' · ');
        status.textContent = (b.phase === 'validate' ? 'Nothing was saved — ' : '')
          + (detail || b.error || `HTTP ${r.status}`);
        status.className = 'statline bad';
      } else {
        status.textContent = 'Saved on every Warden.';
        status.className = 'statline ok';
      }
    } catch (e) {
      status.textContent = `Failed: ${e.message}`;
      status.className = 'statline bad';
    }
    save.disabled = false;
  };
  form.append(el('div', { class: 'field' }, save, status));
  body.append(form);

  /* ---- what each agent's scheduler currently believes ---- */

  const agents = el('div', { class: 'group g-cyan' }, el('h3', {}, '● Scheduler per Warden'));
  for (const cfg of S.config.servers) {
    const a = sched.agents?.[cfg.name] || {};
    if (a.error) {
      agents.append(el('div', { class: 'banner err' }, `${cfg.display_name}: ${a.error}`));
      continue;
    }
    const own = a.schedule || {};
    const matches = own.restart_time === cur.restart_time
      && (own.restart_days || []).join() === (cur.restart_days || []).join()
      && !!own.enabled === !!cur.enabled;
    agents.append(el('div', { class: 'statline ' + (matches ? 'ok' : 'warn') },
      `${cfg.display_name}: ${own.enabled ? 'on' : 'off'} · `
      + `${(own.restart_days || []).map(d => d.slice(0, 3)).join(' ') || 'no days'} `
      + `at ${own.restart_time || '—'} · wild wipe ${own.wipe_wild_dinos ? 'ON' : 'off'}`
      + (matches ? '' : '  ← differs from the panel')));
  }
  body.append(agents);

  /* ---- reference: what DSM had, and this machine's launch settings ---- */

  const mk = (title, colour, pairs) => kvGroup(title, colour, pairs);
  body.append(el('div', { class: 'cols2' },
    mk('Network', 'cyan', [
      ['Server port', op.server_port],
      ['RCON port', op.rcon_port],
      ['Max players', op.max_players],
      ['Cluster ID', op.cluster_id],
      ['Cluster path', op.cluster_path],
    ]),
    mk('Startup (imported from DSM, reference only)', 'purple', [
      ['DSM auto-start', String(op.auto_start)],
      ['DSM auto-update', String(op.auto_update)],
      ['Startup timeout', `${op.startup_timeout_min} min`],
      ['Zombie detection', String(op.zombie_detection)],
      ['CPU priority', op.cpu_priority],
    ])));

  body.append(el('div', { class: 'banner warn', style: 'margin-top:5px' },
    'Warning messages sent over RCON cannot contain ( ) $ & | < > \\ — PatchRaptor’s RCON '
    + 'allowlist rejects them as injection characters, and there is no Broadcast command, only '
    + 'ServerChat. ClusterForge strips them before sending and tells you when it did.'));
}

/* Rotate one server onto a different map.
 *
 * Three steps behind one button, because that is how a rotation actually goes:
 * warn and stop, repoint, start again. The two slow halves run on the existing
 * job plumbing so the shutdown countdown and the boot stream into the log live,
 * rather than disappearing inside one long request that looks like a hang.
 *
 * It stops at the first failure and says so. A half-done switch -- stopped but
 * not repointed, or repointed but not started -- is recoverable; one that
 * ploughs on after a failed stop is not.
 */
async function switchMap(map, button) {
  const cfg = (S.config.servers || []).find(s => s.name === S.server);
  if (!cfg) return;
  const from = cfg.display_name || cfg.name;

  if (!confirm(
    `Switch ${from} to ${map.name}?\n\n`
    + (map.paid
      ? 'THIS IS A PAID MOD. Every player has to buy it on their own CurseForge '
        + 'account before they can join — anyone who has not will be locked out '
        + 'until they do.\n\n'
      : '')
    + `• Server name becomes ${map.session_name}\n`
    + `• World saves into ${map.save_folder}\n`
    + `• Rates, mods, ports and the password do not change\n\n`
    + `The current world is left untouched and comes back whenever you switch back.\n`
    + `Anyone still online gets the usual shutdown countdown first.`)) return;

  const pane = jobPane(cfg.name);
  const box = pane.box;
  pane.running = 'Map switch';
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Switching…';
  box.classList.remove('hidden');
  box.innerHTML = '';
  const line = (text, cls) => {
    box.append(el('div', { class: cls || '' }, text));
    box.scrollTop = box.scrollHeight;
  };
  const finish = (text, cls) => {
    if (text) line(text, cls);
    pane.running = null;
    button.disabled = false;
    button.textContent = original;
    refresh();
  };

  try {
    // 1. Stop, with the normal warning ladder so nobody is dropped mid-fight.
    const live = (S.status?.servers || []).find(s => s.name === cfg.name);
    if (live && live.state === 'online') {
      line(`Stopping ${from}…`, 'muted');
      const r = await fetch(`/api/server/${cfg.name}/stop`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: `Server changing map to ${map.name} in {minutes} minutes `
                 + '- please log out safely',
        }),
      });
      const b = await r.json();
      if (!r.ok) return finish(b.message || b.error || `HTTP ${r.status}`, 'err');
      const stopJob = (b.job || {}).id;
      if (stopJob) {
        const out = await followJobs(
          [{ server: cfg.name, id: stopJob, label: '' }], line, 'Stop');
        if (out[cfg.name] !== 'done') {
          return finish('Stop did not finish cleanly — the map was NOT changed.', 'err');
        }
      }
    } else {
      line(`${from} is already stopped.`, 'muted');
    }

    // 2. Repoint. Fast: one start-script rewrite and one ini line.
    line(`Pointing ${cfg.name} at ${map.map_name}…`, 'muted');
    const r2 = await fetch(`/api/server/${cfg.name}/switch-map`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ map: map.key }),
    });
    const b2 = await r2.json();
    if (!r2.ok) return finish(b2.message || b2.error || `HTTP ${r2.status}`, 'err');

    line(`Map:  ${b2.before?.map_name} → ${b2.after?.map_name}`, 'ok');
    line(`Name: ${b2.after?.session_name}`, 'ok');
    if ((b2.mods_added || []).length) line(`Mods added: ${b2.mods_added.join(', ')}`, 'ok');
    if ((b2.mods_removed || []).length) line(`Mods removed: ${b2.mods_removed.join(', ')}`, 'ok');
    line(b2.save_exists
      ? `Existing world found in ${b2.after?.save_dir} — resuming it.`
      : `No world in ${b2.after?.save_dir} yet — this will start a fresh one.`,
      b2.save_exists ? 'ok' : 'warn');
    // A map's save folder is not always its map name. If the expected one is
    // missing, show what IS there, so a wrong catalog guess is caught now
    // rather than by a backup quietly archiving an empty directory.
    if (!b2.save_exists && (b2.save_folders_present || []).length) {
      line(`SavedArks currently holds: ${b2.save_folders_present.join(', ')}`, 'muted');
      line('If this map writes into a folder named differently from its map name, set '
         + '"save_folder" for it in data/maps/catalog.json so backups follow it.', 'muted');
    }
    if (b2.session_name_ini && !b2.session_name_ini.ok) {
      line(`GameUserSettings.ini not updated: ${b2.session_name_ini.message}`, 'warn');
    }
    if (b2.discord) {
      line(b2.discord.ok
        ? 'Discord info channels updated.'
        : `Discord not updated: ${(b2.discord.errors || []).join('; ')
            || b2.discord.message || b2.discord.error || 'unknown'}`,
        b2.discord.ok ? 'ok' : 'warn');
    }

    // 3. Start. A first boot on a new map downloads that map's mods, so this
    //    runs longer than a normal start and must not be read as a hang.
    line('Starting — a first boot on a new map downloads its mods and can take '
       + 'several minutes.', 'muted');
    const r3 = await fetch(`/api/server/${cfg.name}/start`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    const b3 = await r3.json();
    if (!r3.ok) return finish(b3.message || b3.error || `HTTP ${r3.status}`, 'err');
    const startJob = (b3.job || {}).id;
    if (startJob) {
      const out = await followJobs(
        [{ server: cfg.name, id: startJob, label: '' }], line, 'Start');
      if (out[cfg.name] !== 'done') {
        return finish(`${map.name} did not come up. The map IS switched, so fix the `
                    + 'start or switch back from this page.', 'err');
      }
    }
    finish(`${from} is now running ${map.name}.`, 'ok');
  } catch (e) {
    finish(`Request failed: ${e.message}`, 'err');
  }
}

async function tabMapMgmt(body) {
  const [data, catalog] = await Promise.all([
    api(`/api/server/${S.server}/launch`),
    api('/api/maps').catch(e => ({ error: e.message, maps: [] })),
  ]);
  const op = data.operational || {};
  const activeMap = data.launch?.map_name || '';
  body.innerHTML = '';

  const thisServer = (S.config.servers || []).find(s => s.name === S.server) || {};
  const thisLabel = thisServer.display_name || S.server;

  body.append(kvGroup(`▤ ${thisLabel} (${S.server}) — current map`, 'green', [
    ['Map (launch)', activeMap, 'mono'],
    ['Save folder', op.map_folder, 'mono'],
    ['Save file', op.map_file, 'mono'],
  ]));

  if (catalog.error) {
    body.append(el('div', { class: 'banner err' }, `Map catalog: ${catalog.error}`));
  }

  const grid = el('div', { class: 'mapgrid' });
  for (const m of catalog.maps || []) {
    const running = (m.servers || []).length > 0;
    const isActive = m.map_name === activeMap;

    const art = m.image
      ? el('img', { class: 'mapart', src: m.image, alt: m.name, loading: 'lazy' })
      : el('div', { class: 'mapart placeholder' },
          el('span', {}, m.name),
          el('small', {}, `drop ${m.key}.jpg in data/maps/images`));

    // Running this map on ANOTHER server would put two of them in one cluster
    // answering to the same session name, so that is refused rather than warned about.
    const elsewhere = running && !isActive
      ? m.servers.map(s => s.display_name || s.name).join(', ')
      : '';

    let action = null;
    if (isActive) {
      action = el('div', { class: 'statline ok' }, 'This is the current map');
    } else if (m.available === false) {
      // Catalogued but not runnable yet. Shown rather than hidden so it is
      // obviously pending instead of looking forgotten.
      action = el('div', { class: 'statline warn' }, 'Not available yet');
    } else if (elsewhere) {
      action = el('div', { class: 'statline warn' }, `Already running on ${elsewhere}`);
    } else {
      const btn = el('button', { class: 'btn blue wide' }, `Switch to ${m.name}`);
      btn.onclick = () => switchMap(m, btn);
      action = btn;
    }

    const card = el('div', {
      class: 'mapcard' + (isActive ? ' active' : '')
           + (m.available === false ? ' pending' : ''),
    }, art,
      el('div', { class: 'maptitle' }, m.name,
        m.kind === 'mod' ? el('span', { class: 'pill mod' }, 'MOD') : null,
        m.kind === 'official-mod' ? el('span', { class: 'pill mod' }, 'OFFICIAL MOD') : null,
        m.paid ? el('span', { class: 'pill paid' }, 'PAID') : null,
        isActive ? el('span', { class: 'pill on' }, 'RUNNING HERE') : null),
      el('div', { class: 'mono muted' }, m.map_name),
      elsewhere ? el('div', { class: 'statline ok' }, 'on ' + elsewhere) : null,
      m.session_name ? el('div', { class: 'muted' }, `Shows as ${m.session_name}`) : null,
      m.mod_id ? el('div', { class: 'muted' }, `Needs mod ${m.mod_id} in -mods=`) : null,
      m.unavailable_reason
        ? el('div', { class: 'muted maprnote' }, m.unavailable_reason)
        : (m.notes ? el('div', { class: 'muted maprnote' }, m.notes) : null),
      el('div', { class: 'maprow' }, action));
    grid.append(card);
  }

  body.append(el('div', { class: 'group g-cyan' },
    el('h3', {}, '▤ Maps'),
    el('div', { class: 'muted', style: 'margin-bottom:6px' },
      `These buttons act on ${thisLabel} — the server selected above. `
      + 'Switching stops it, repoints it and starts it again. '
      + 'Rates, mods, ports and the join password stay exactly as they are — the maps are '
      + 'meant to feel the same. Nothing is copied and nothing is deleted: ARK keeps every '
      + 'map’s world in its own save folder, so the map you leave is waiting untouched '
      + 'whenever the rotation comes back to it.'),
    grid));

  body.append(el('div', { class: 'banner info' },
    'Players keep whatever they upload to the cluster, but a character left standing on the '
    + 'old map stays there — they arrive on the new map at level 1 unless they upload first. '
    + 'Worth announcing a few days ahead of a rotation.'));

  if ((catalog.maps || []).some(m => !m.image)) {
    body.append(el('div', { class: 'banner info' },
      'Map pictures are read from local files, not fetched from the internet — the panel '
      + 'has to work when the connection does not, and the artwork is Wildcard’s. '
      + `Drop images named after each map’s key into ${catalog.image_dir || 'data\\maps\\images'} `
      + '(jpg, png or webp) and they appear here.'));
  }

  body.append(el('div', { class: 'banner info' },
    'Svartalfheim’s save folder is named without the _WP suffix even though the file inside '
    + 'is Svartalfheim_WP.ark — that is the engine’s real convention for this map, not a typo to fix.'));
}

/* Parse the Add Manually box.
 *
 * Accepts DSM's own format (Name,ModID,URL;Name,ModID,URL), bare mod IDs, and
 * pasted CurseForge URLs — whichever the user happens to have to hand.
 *
 * A CurseForge URL alone is NOT enough: the trailing number in
 * .../mods/automated-ark/download/8449295 is the FILE id, not the project id
 * (928597), and only the project id belongs in -mods=. So a URL yields a name
 * from its slug and is flagged as still needing an ID, rather than silently
 * adding the wrong number.
 */
function parseModInput(text) {
  const entries = [];
  for (const chunk of text.split(/[;\n]+/)) {
    const raw = chunk.trim();
    if (!raw) continue;

    const parts = raw.split(',').map(p => p.trim());
    let name = '', id = null, url = '';

    for (const part of parts) {
      if (/^\d{4,}$/.test(part)) id = parseInt(part, 10);
      else if (/^https?:\/\//i.test(part)) url = part;
      else if (part) name = name || part;
    }

    if (!name && url) {
      const m = url.match(/\/mods\/([a-z0-9][a-z0-9-]*)/i);
      if (m) name = m[1].split('-').map(w => w[0].toUpperCase() + w.slice(1)).join(' ');
    }

    entries.push({
      raw, name: name || (id ? `Mod${id}` : '(unnamed)'), id, url,
      problem: id ? null
        : (url ? 'URL has no project ID — the number in a CurseForge link is the file ID, not the mod ID'
               : 'no mod ID found'),
    });
  }
  return entries;
}

/* onAdded redraws the mod TABLE. It must not be renderTab.
 *
 * This block used to finish by calling renderTab(), which rebuilds the whole
 * tab -- and tabMods starts by refetching the saved list and doing
 * `modState = {list: [...original], ...}`. So the mod that had just been
 * staged was thrown away a millisecond after being added, and the rebuild
 * bounced the page back to the top. It looked like the button did nothing
 * except scroll. */
function addManuallyBlock(onAdded) {
  // "Already present" means already in the LOAD ORDER, checked live against
  // modState.list. It used to be a snapshot Set of every mod the agent knew
  // about, which was wrong twice over: it went stale the moment something was
  // staged, and it counted mods that are installed on disk but deliberately
  // NOT in -mods= (the rows the table flags as "not loaded"), so one of those
  // could never be added back -- the button just said it was already there.
  const inLoadOrder = (id) => modState.list.includes(id);
  const box = el('textarea', {
    rows: 4, class: 'mono',
    placeholder: 'ModName,ModID,URL;ModName,ModID,URL\n…or just paste mod IDs, one per line',
  });
  const preview = el('div', { class: 'statline' });
  const btn = el('button', { class: 'btn red wide' }, 'Add mod manually');
  btn.onclick = () => {
    const entries = parseModInput(box.value);
    const usable = entries.filter(e => e.id && !inLoadOrder(e.id));
    if (!usable.length) {
      showBanner('warn', 'Nothing to add — every entry is already in the load order '
                       + 'or has no mod ID.');
      return;
    }
    for (const e of usable) {
      modState.list.push(e.id);
      if (!modState.meta.has(e.id)) {
        modState.meta.set(e.id, {
          mod_id: e.id, name: e.name, author: '', url: e.url,
          installed: false, enabled: true,
          drift: 'not installed yet — the server downloads it on next start',
        });
      }
    }
    box.value = '';
    preview.innerHTML = '';
    showBanner('ok',
      `Staged ${usable.length} mod(s) at the end of the load order. `
      + 'Press Save mod list to write it, then restart the server to apply.');
    if (onAdded) onAdded();
  };
  if (S.readOnly) guard(btn, 'Enable writing in config.json first.');

  box.oninput = () => {
    const entries = parseModInput(box.value);
    preview.innerHTML = '';
    if (!entries.length) { preview.textContent = ''; return; }
    for (const e of entries) {
      const dupe = e.id && inLoadOrder(e.id);
      preview.append(el('div', {
        style: `color:${e.problem ? 'var(--red)' : dupe ? 'var(--yellow)' : 'var(--green)'}`,
      },
        e.problem ? `✕ ${e.raw} — ${e.problem}`
          : dupe ? `! ${e.name} (${e.id}) — already in the load order`
          : `✓ ${e.name} (${e.id})${e.url ? ' + URL' : ''}`));
    }
  };

  return el('div', { class: 'group g-red', style: 'margin-top:6px' },
    el('h3', {}, '+ Add Manually'),
    el('div', { class: 'statline', style: 'margin-bottom:5px' },
      'Format: ModName,ModID,URL;ModName,ModID,URL — or paste bare mod IDs. '
      + 'The server downloads and installs mods itself on next boot from the -mods= list, '
      + 'so no separate install step is needed.'),
    box, preview, btn);
}

async function tabLogs(body) {
  const data = await api(`/api/server/${S.server}/logs?lines=400`);
  body.innerHTML = '';
  const s = data.summary || {};
  body.append(el('div', { class: 'searchbar' },
    el('span', { class: 'muted mono' }, s.active || 'no active log'),
    el('span', { class: 'muted' }, s.active_age_seconds != null ? `written ${s.active_age_seconds}s ago` : ''),
    (() => { const b = el('button', { class: 'btn blue' }, 'Refresh'); b.onclick = () => renderTab(); return b; })()));

  if (s.stale?.length) {
    body.append(el('div', { class: 'banner warn' },
      `Stale numbered logs present: ${s.stale.map(x => x.name).join(', ')}. `
      + 'ClusterForge picks the most recently written file, so these are inert here — but '
      + 'selecting by filename instead of write time is what once pointed log tailing at a dead file.'));
  }

  const view = el('div', { class: 'logview' });
  for (const line of data.lines) {
    let cls = '';
    if (/error|failed|exception|fatal/i.test(line)) cls = 'err';
    else if (/warn/i.test(line)) cls = 'warn';
    else if (/advertising for join|completed startup/i.test(line)) cls = 'ok';
    view.append(el('div', { class: cls }, line));
  }
  body.append(view);
  view.scrollTop = view.scrollHeight;
}

boot().catch(e => {
  document.body.prepend(el('div', { class: 'banner err' }, `Startup failed: ${e.message}`));
});


/* ---- Add a game ----------------------------------------------------------
 * The point of the adapter layer, made visible: every registered game shows
 * up here whether or not it is set up, with what it needs spelled out. */
async function viewAddGame(pane) {
  const data = await api('/api/games');
  pane.innerHTML = '';
  pane.append(el('div', { class: 'group g-cyan' },
    el('h3', {}, '◆ Games'),
    el('p', { class: 'muted' },
      'Every game ClusterForge knows how to run. A game that is not set up yet '
      + 'shows what it needs — the server software is a free download in '
      + 'every case; owning the game is only needed to play, not to host.')));

  for (const g of data.games || []) {
    const caps = g.capabilities || {};
    const can = Object.entries(caps).filter(([k, v]) => v && k !== 'settings_need_stop')
                      .map(([k]) => k.replace('_', ' '));
    const card = el('div', { class: `group g-${g.configured ? 'green' : 'orange'}` },
      el('h3', {},
        el('span', { class: `dot ${g.configured ? 'online' : 'offline'}` }),
        g.name,
        el('span', { class: 'muted', style: 'font-weight:400;margin-left:auto' },
          g.configured ? `${g.servers.length} server(s)` : 'not set up')));

    const rows = [
      ['Can do', can.join(', ') || 'start and stop only'],
      ['Server software', g.steam_app_id
        ? `free from SteamCMD (app ${g.steam_app_id})`
        : 'downloaded from the publisher'],
    ];
    if (g.configured) {
      rows.push(['Servers', g.servers.map(s => s.display_name || s.name).join(', ')]);
    }
    card.append(el('dl', { class: 'kv' },
      ...rows.flatMap(([k, v]) => [el('dt', {}, k), el('dd', {}, v)])));

    if (g.configured) {
      const open = el('button', { class: 'btn green' }, `Open ${g.short}`);
      open.onclick = () => selectGame(g.id);
      card.append(open);
    } else {
      card.append(el('p', { class: 'muted' },
        `To add ${g.short}: install the dedicated server, then add a "${g.id}" `
        + 'block to config.json with its install_dir and ports. Ask Claude to '
        + 'do it and it will walk through the whole thing.'));
    }
    pane.append(card);
  }
}


/* ---- generic game screens ------------------------------------------------
 * Driven entirely by /api/games/<id>/..., so a game added to core/games gets
 * these for free. This is the other half of the adapter work: the back end
 * stopped knowing what ARK and Palworld are, and now the front end does too.
 */

function gameSettingsSection(gameId, serverName) {
  const box = el('div', { class: 'group g-cyan' },
    el('h3', {}, '⚙ Server settings'));
  const body = el('div', {}, el('span', { class: 'muted' }, 'loading settings…'));
  box.append(body);

  api(`/api/games/${gameId}/servers/${serverName}/settings`).then(data => {
    body.innerHTML = '';
    const dirty = new Map();
    const note = el('span', { class: 'statline' },
      (data.groups || []).reduce((n, g) => n + g.fields.length, 0) + ' settings');

    if (data.needs_stop) {
      box.insertBefore(el('p', { class: 'muted' },
        'Saving restarts this server. It has to be stopped to take new '
        + 'settings — it rewrites its own config file on shutdown, so a '
        + 'change saved while it runs would be undone. ClusterForge does the '
        + 'stop, the write and the start as one step.'), body);
    } else {
      box.insertBefore(el('p', { class: 'muted' },
        'Changes are written safely — the file is backed up first and '
        + 'settings you did not touch are preserved. Most need a restart to '
        + 'take effect.'), body);
    }

    const apply = async (payload, describe) => {
      note.className = 'statline';
      note.textContent = 'applying…';
      try {
        const r = await postJSON(`/api/games/${gameId}/servers/${serverName}/settings`, payload);
        note.className = 'statline ok';
        note.textContent = r.message || `${r.count} setting(s) changed`;
        dirty.clear(); save.disabled = true;
        setTimeout(() => { if (S.view) selectView(S.view); }, r.cycled ? 60000 : 1500);
      } catch (e) { note.className = 'statline err'; note.textContent = e.message; }
    };

    for (const [key, p] of Object.entries(data.presets || {})) {
      const b = el('button', { class: 'btn purple' }, `Apply: ${p.label}`);
      b.title = p.why;
      b.onclick = () => {
        const lines = Object.entries(p.values).map(([k, v]) => `  ${k} -> ${v}`).join('\n');
        if (confirm(`${p.label}

${p.why}

This changes:
${lines}

Apply?`)) {
          apply({ preset: key });
        }
      };
      if (S.readOnly) guard(b, 'Enable writing in config.json first.');
      body.append(b);
    }
    body.append(note);

    for (const g of data.groups || []) {
      const det = el('details', { class: 'group', style: 'margin-top:5px' });
      det.append(el('summary', { class: 'grouphead' }, g.group,
        el('span', { class: 'count' }, `${g.fields.length}`)));
      const dl = el('dl', { class: 'kv' });
      for (const f of g.fields) {
        const dt = el('dt', {}, f.label);
        if (f.help) dt.title = f.help;
        let input;
        if (f.kind === 'bool') {
          input = el('select', {});
          for (const v of ['True', 'False']) {
            const o = el('option', { value: v }, v === 'True' ? 'on' : 'off');
            if (v.toLowerCase() === String(f.value).toLowerCase()) o.selected = true;
            input.append(o);
          }
        } else if (String(f.kind).startsWith('choice:')) {
          input = el('select', {});
          for (const v of f.kind.slice(7).split('|')) {
            const o = el('option', { value: v }, v);
            if (v === String(f.value)) o.selected = true;
            input.append(o);
          }
        } else {
          input = el('input', { type: 'text', value: f.value, style: 'min-width:9em' });
        }
        if (f.locked || S.readOnly) {
          input.disabled = true;
          input.title = f.locked_why || 'Read-only mode.';
        } else {
          input.onchange = () => { dirty.set(f.key, input.value); save.disabled = false; };
        }
        const dd = el('dd', {}, input);
        if (f.help) dd.append(el('div', { class: 'muted', style: 'font-size:.9em' }, f.help));
        if (f.locked) dd.append(el('div', { class: 'warn', style: 'font-size:.9em' }, f.locked_why));
        dl.append(dt, dd);
      }
      det.append(dl);
      body.append(det);
    }

    var save = el('button', { class: 'btn green', style: 'margin-top:5px' }, 'Save changes');
    save.disabled = true;
    save.onclick = () => {
      const lines = [...dirty].map(([k, v]) => `  ${k} -> ${v}`).join('\n');
      if (confirm(`Apply ${dirty.size} change(s)?

${lines}`)) {
        apply({ changes: Object.fromEntries(dirty) });
      }
    };
    if (S.readOnly) guard(save, 'Enable writing in config.json first.');
    body.append(save);
  }).catch(e => {
    body.innerHTML = '';
    body.append(el('div', { class: 'banner err' }, `Could not load settings: ${e.message}`));
  });

  return box;
}
