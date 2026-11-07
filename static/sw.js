// HackerGit service worker: keeps GitHub avatars in the browser for about a week (GitHub only allows 5 minutes),
// so repeat visits show icons instantly without any network request. Everything else goes to the network as usual.
const VERSION = 1;
const WEEK = Math.floor(Date.now() / (7 * 86400e3));
const CACHE = `avatars-v${VERSION}-w${WEEK}`;   // a new cache each week; older ones are deleted
const MAX_ITEMS = 3000;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) if (name.startsWith("avatars-") && name !== CACHE) await caches.delete(name);
    await self.clients.claim();
  })());
});

const isAvatar = (url) =>
  url.hostname === "avatars.githubusercontent.com" ||
  (url.hostname === "github.com" && /^\/[^/]+\.png$/.test(url.pathname));

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (!isAvatar(url)) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(req.url);
    if (hit) return hit;
    let res;
    try {
      // avatars.githubusercontent.com allows CORS, so the stored copy is a normal (non-opaque) response
      res = await fetch(req.url, { mode: url.hostname === "github.com" ? "no-cors" : "cors", credentials: "omit" });
    } catch (e) {
      return fetch(req);
    }
    if (res.ok || res.type === "opaque") {
      event.waitUntil((async () => {
        await cache.put(req.url, res.clone());
        const keys = await cache.keys();
        for (let i = 0; i < keys.length - MAX_ITEMS; i++) await cache.delete(keys[i]);
      })());
    }
    return res;
  })());
});
