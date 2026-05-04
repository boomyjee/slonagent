// JSDOM bootstrap — must be imported before any module that touches `window`
// or `document` at load time (Lightbox mounts a Preact tree at import).
//
// We expose the JSDOM window/document on globalThis so the inlined Preact
// inside src/transport/web/ui/lib.js can find them. We share Preact's
// instance with the components by reusing lib.js's render — there is no
// second Preact running, no dual-instance trap.
//
// We also install a resolution hook (_loader.mjs) so dashboard components
// can find shared files (lib.js, Tabs.js, ...) under web/ui/. Browser does
// this via WebTransportServer's _ui_dirs fallback chain; Node needs help.

import { register } from 'node:module';
register('./_loader.mjs', import.meta.url);

import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>', {
    url: 'http://localhost/',
    pretendToBeVisual: true,
});
const w = dom.window;

// Some properties on globalThis are read-only in modern Node (navigator);
// assign with defineProperty so we win regardless of descriptor.
const expose = (k, v) => Object.defineProperty(globalThis, k, {
    value: v, writable: true, configurable: true,
});
expose('window', w);
expose('document', w.document);
expose('HTMLElement', w.HTMLElement);
expose('Element', w.Element);
expose('Node', w.Node);
expose('navigator', w.navigator);
expose('location', w.location);
expose('localStorage', w.localStorage);
expose('requestAnimationFrame', w.requestAnimationFrame);
expose('cancelAnimationFrame', w.cancelAnimationFrame);

// Default fetch returns "no history" — tests can override globalThis.fetch
// when they need to simulate the /api/history endpoint. We return a duck-
// typed Response since JSDOM does not expose the Response constructor.
export const mockResponse = (body, status = 200) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
});
globalThis.fetch = async () => mockResponse({ events: [] });

// Preact debounces setState through Promise.resolve().then. Two await
// boundaries cover state-update → re-render in our tests.
export async function flush() {
    await Promise.resolve();
    await Promise.resolve();
}

// Synthesize a MouseEvent and dispatch it on `target`. Pass clientX/clientY/
// button via `init` so drag handlers see realistic coordinates.
export function fireMouse(target, type, init = {}) {
    const e = new w.MouseEvent(type, { bubbles: true, cancelable: true, ...init });
    target.dispatchEvent(e);
    return e;
}

export function fireKey(key, init = {}) {
    const e = new w.KeyboardEvent('keydown', { bubbles: true, key, ...init });
    w.document.dispatchEvent(e);
    return e;
}

export function makeContainer() {
    const c = w.document.createElement('div');
    w.document.body.appendChild(c);
    return c;
}

export { dom };
