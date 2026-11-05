// Service worker minimal : rend l'application installable et garde l'interface
// en cache pour qu'elle s'ouvre vite. Les appels à l'API passent toujours par le réseau.
const CACHE = "playlist-transfer-v1";
const SHELL = ["/", "/static/style.css", "/static/app.js", "/static/manifest.webmanifest", "/static/icons/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/auth/")) return;
  // Réseau d'abord (pour avoir la dernière version), cache si hors ligne.
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        if (res.ok && url.search === "") {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(event.request, { ignoreSearch: true }))
  );
});
