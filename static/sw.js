// Service worker: makes the viewer installable and keeps the app shell (page, script, styles,
// icons) available. Shell files are fetched network-first so updates land at once, with the cache
// as the fallback. Photos, thumbnails and the API are never cached here: the library changes and
// the page reloads itself when the server reports a new version.
const SHELL = 'iopix-shell-v3';  // bump when the shell files change in a way the network-first fetch would not refresh (new icons)
const JS = ['core', 'state', 'search', 'grid', 'actions', 'editing', 'download', 'pages', 'admin', 'map', 'filters', 'lightbox', 'main'].map((m) => `./js/${m}.js`);
// the map's library, served with the app (static/vendor)
const VENDOR = ['leaflet/leaflet.js', 'leaflet/leaflet.css', 'leaflet/images/marker-icon.png', 'leaflet/images/marker-icon-2x.png', 'leaflet/images/marker-shadow.png', 'leaflet/images/layers.png', 'leaflet/images/layers-2x.png',
  'leaflet.markercluster/leaflet.markercluster.js', 'leaflet.markercluster/MarkerCluster.css', 'leaflet.markercluster/MarkerCluster.Default.css'].map((f) => `./vendor/${f}`);
const FILES = ['./', './index.html', './app.js', ...JS, './app.css', './manifest.webmanifest', './icons/mark.svg', './icons/icon-192.png', './icons/icon-512.png'];
// the shell must all be there; the map's files may be refused to a visitor who still has to sign in (cached on first use then)
self.addEventListener('install', (e) => { e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES).then(() => Promise.all(VENDOR.map((f) => c.add(f).catch(() => {}))))).then(() => self.skipWaiting())); });
self.addEventListener('activate', (e) => { e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== SHELL).map((k) => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  const shell = url.pathname === '/' || /\/(index\.html|app\.js|js\/[^/]+\.js|app\.css|manifest\.webmanifest|icons\/[^/]+|vendor\/.+)$/.test(url.pathname);
  if (!shell) return;  // /api, /thumb, /preview, /orig, /face…: straight to the server
  // only a good answer replaces the cached copy: a 503 while the server restarts must not become the offline fallback
  e.respondWith(fetch(e.request).then((r) => { if (r.ok) { const copy = r.clone(); caches.open(SHELL).then((c) => c.put(e.request, copy)); } return r; })
    .catch(() => caches.match(e.request, { ignoreSearch: true })));
});
