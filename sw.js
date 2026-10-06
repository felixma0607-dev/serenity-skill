// Minimal service worker — required by some browsers (mainly Android/Chrome)
// for "Add to Home Screen" installability. Does no offline caching on purpose:
// this dashboard should always show fresh data, never a stale cached copy.
self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => self.clients.claim());
self.addEventListener("fetch", (e) => {
  // pass-through, no caching
});
