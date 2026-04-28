// Minimal service worker — empty fetch listener is enough to satisfy
// Chrome's PWA install criteria. Browser handles the request by default.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
