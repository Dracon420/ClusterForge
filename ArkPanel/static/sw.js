/* ArkPanel service worker.
 *
 * Exists for ONE reason: Android Chrome will not offer "Install app" without a
 * service worker that has a fetch handler. HTTPS and a valid manifest only
 * earn you a plain shortcut.
 *
 * That makes the caching policy the important part, because this controls a
 * page that starts and stops game servers:
 *
 *   /api/**  is NEVER cached, and never served from cache.
 *            A stale "island: online" after the map has crashed is worse than
 *            no answer at all -- it would have someone believe the cluster is
 *            fine while it is down. Every API call goes to the network or
 *            fails honestly.
 *
 *   static   assets are cache-first. They only change when I deploy, and the
 *            version below busts them when they do.
 *
 *   the page itself is network-first, falling back to a cached shell and then
 *            to an offline notice that says plainly that it is showing nothing
 *            live.
 */

// Leave this EXACTLY 'arkpanel-v1': panel.py service_worker() replaces that text
// with a fingerprint of the static files, so every deploy reaches installed apps.
// It was once bumped to 'v2' by hand, which silently stopped the replacement and
// left phones on old JavaScript (2026-11-12).
const VERSION = 'arkpanel-v1';
const SHELL = `${VERSION}-shell`;

// Precached best-effort. A failure here must not abort the install -- these
// requests carry HTTP Basic auth and one 401 would otherwise break everything.
const PRECACHE = [
  '/static/style.css',
  '/static/theme.css',
  '/static/mobile.css',
  '/static/app.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL);
    await Promise.allSettled(PRECACHE.map(url => cache.add(new Request(url, {
      credentials: 'same-origin',
    }))));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    // Drop caches from older versions so a deploy cannot leave half the UI on
    // yesterday's JavaScript talking to today's API.
    for (const key of await caches.keys()) {
      if (!key.startsWith(VERSION)) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

const OFFLINE_PAGE = `<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ArkPanel - offline</title>
<style>
  body { background:#0d0c0a; color:#ece5d6; font:14px "Segoe UI",sans-serif;
         margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; text-align:center; padding:24px; }
  .box { max-width:340px; }
  h1 { color:#e9a83c; font-size:16px; letter-spacing:.12em;
       text-transform:uppercase; margin:0 0 10px; }
  p { color:#a3977e; line-height:1.5; margin:0 0 14px; }
  button { background:#c9711a; color:#fff; border:0; border-radius:3px;
           padding:12px 20px; font:600 12px "Segoe UI",sans-serif;
           text-transform:uppercase; letter-spacing:.06em; }
</style>
<div class="box">
  <h1>No connection</h1>
  <p>ArkPanel cannot reach the server, so it is showing nothing live.
     Check that you are on the Tailscale network.</p>
  <p><strong>The cluster itself is unaffected</strong> &mdash; the servers keep
     running whether or not this page can see them.</p>
  <button onclick="location.reload()">Try again</button>
</div>`;

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Live state is never cached. Let it go straight to the network so a failure
  // surfaces as a failure rather than as stale, confident-looking data.
  if (url.pathname.startsWith('/api/')) return;

  // Navigations: network first, cached shell second, honest notice last.
  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const fresh = await fetch(request);
        if (fresh && fresh.ok) {
          const cache = await caches.open(SHELL);
          cache.put('/', fresh.clone());
        }
        return fresh;
      } catch (err) {
        const cached = await caches.match('/');
        if (cached) return cached;
        return new Response(OFFLINE_PAGE, {
          status: 503,
          headers: { 'Content-Type': 'text/html; charset=utf-8' },
        });
      }
    })());
    return;
  }

  // Static assets: cache first, refreshed in the background.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith((async () => {
      const cached = await caches.match(request);
      if (cached) return cached;
      try {
        const fresh = await fetch(request);
        if (fresh && fresh.ok) {
          const cache = await caches.open(SHELL);
          cache.put(request, fresh.clone());
        }
        return fresh;
      } catch (err) {
        return cached || Response.error();
      }
    })());
  }
});
