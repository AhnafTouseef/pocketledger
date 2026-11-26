/* PocketLedger service worker.
 *
 * Caches ONLY the static app shell (CSS/JS/icons) and a generic offline page. It never caches
 * pages or API responses, so balances and transactions are never shown stale or from another
 * session. Offline recording is handled in the page (IndexedDB queue), not here.
 */
const VERSION = 'pl-v3';
const SHELL = `${VERSION}-shell`;
const PRECACHE = [
  '/offline',
  '/static/css/app.css',
  '/static/js/app.js',
  '/static/js/busy.js',
  '/static/js/store.js',
  '/static/js/txform.js',
  '/static/vendor/lucide.min.js',
  '/static/icons/icon-192.png',
  '/static/icons/favicon-32.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL).then((cache) =>
      Promise.allSettled(PRECACHE.map((url) => cache.add(new Request(url, { cache: 'reload' }))))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Page navigations: always go to the network; if that fails, show the offline page.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match('/offline', { ignoreSearch: true }).then((r) => r || new Response('Offline', { status: 503 })))
    );
    return;
  }

  // Static assets: serve from cache, refresh in the background.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.open(SHELL).then((cache) =>
        cache.match(req, { ignoreSearch: true }).then((cached) => {
          const network = fetch(req).then((res) => {
            if (res && res.ok) cache.put(req, res.clone());
            return res;
          }).catch(() => cached);
          return cached || network;
        })
      )
    );
  }
  // Everything else (API, forms, data pages) is never cached.
});
