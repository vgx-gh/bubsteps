// BubSteps service worker - deliberately minimal.
//
// This app is entirely login-gated, per-household dynamic content (entries, journal,
// weight, settings) - there is no safe way to cache and replay those pages offline
// without risking one household's data being served from a shared browser cache to
// someone else on the same device later. So this worker does NOT do full offline
// support. Its only job is:
//   1. Exist and be installable, which is what actually triggers the browser's
//      "Add to Home Screen" / PWA install prompt.
//   2. Cache the static, non-personal app shell (CSS, icons) so those load instantly
//      on repeat visits instead of a network round-trip every time.
// Every other request (every page, every form POST) just passes straight through to
// the network, exactly as if this worker didn't exist.

const CACHE_NAME = "bubsteps-shell-v2";
const SHELL_ASSETS = [
  "/static/style.css",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // Clean up any older cache version from a previous deploy.
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const isShellAsset = SHELL_ASSETS.includes(url.pathname);

  if (!isShellAsset) {
    // Not part of the static shell (i.e. it's a real page or form submission) -
    // let it go straight to the network untouched.
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((response) => {
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        return response;
      });
    })
  );
});
