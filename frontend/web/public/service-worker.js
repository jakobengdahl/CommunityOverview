const CACHE_NAME = 'community-graph-app-shell-v1';
const APP_SHELL_ASSETS = [
  '/',
  '/manifest.webmanifest',
  '/icon-192.png',
  '/icon-512.png',
  '/icon-maskable-192.png',
  '/icon-maskable-512.png',
];
const BYPASS_PATH_PREFIXES = [
  '/api/',
  '/sessions',
  '/session',
  '/auth',
  '/oauth',
  '/login',
  '/logout',
  '/account',
  '/admin',
  '/billing',
  '/checkout',
  '/subscriptions',
  '/subscription',
  '/iap',
  '/purchase',
  '/payments',
];
const BYPASS_SEARCH_PARAMS = ['session', 'collect', 'akc'];

function shouldBypass(requestUrl) {
  if (requestUrl.origin !== self.location.origin) {
    return true;
  }

  if (BYPASS_PATH_PREFIXES.some((prefix) => requestUrl.pathname.startsWith(prefix))) {
    return true;
  }

  return BYPASS_SEARCH_PARAMS.some((param) => requestUrl.searchParams.has(param));
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(names.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') {
    return;
  }

  const requestUrl = new URL(event.request.url);
  if (shouldBypass(requestUrl)) {
    return;
  }

  if (event.request.mode !== 'navigate') {
    event.respondWith(
      caches.match(event.request).then((response) => response || fetch(event.request))
    );
    return;
  }

  event.respondWith(fetch(event.request).catch(() => caches.match('/')));
});
