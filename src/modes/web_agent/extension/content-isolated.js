/**
 * Isolated-world CS — has chrome.runtime, talks to the main world via
 * window.postMessage on SLON_PAGE_CTRL_MAIN. Bridges background RPCs to
 * the real PageController (which lives in content-main.js).
 *
 * Tracks main-world `ready`/`init_failed` so calls arriving before the
 * page-controller import finishes return a clean error instead of hanging.
 */

// Hand the vendored page-controller URL to the main-world CS. Dataset
// writes from isolated world are visible in main world synchronously, and
// content-isolated.js is declared first in manifest so it runs before
// content-main.js — no postMessage race needed.
document.documentElement.dataset.slonPcUrl = chrome.runtime.getURL('vendor/page-controller.mjs');

let mainReady = false;
let initError = null;
const pending = new Map();

window.addEventListener('message', (e) => {
    if (e.source !== window) return;
    const msg = e.data;
    if (msg?.channel !== 'SLON_PAGE_CTRL_MAIN') return;

    if (msg.type === 'ready') { mainReady = true; initError = null; return; }
    if (msg.type === 'init_failed') { mainReady = false; initError = msg.error; return; }

    if (msg.type === 'result') {
        const cb = pending.get(msg.request_id);
        if (!cb) return;
        pending.delete(msg.request_id);
        cb({ result: msg.result, error: msg.error });
    }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg?.channel !== 'SLON_PAGE_CTRL' || msg.type !== 'call') return;

    if (initError) {
        sendResponse({ error: `PageController init failed: ${initError}` });
        return;
    }
    if (!mainReady) {
        sendResponse({ error: 'PageController not ready yet on this page — try again in a moment' });
        return;
    }

    const request_id = (crypto.randomUUID?.() || String(Math.random()).slice(2));
    pending.set(request_id, sendResponse);
    window.postMessage({
        channel: 'SLON_PAGE_CTRL_MAIN', type: 'call',
        method: msg.method, args: msg.args, request_id,
    }, '*');

    // Safety net: if main world never replies, surface an error to the
    // agent instead of hanging the step forever.
    setTimeout(() => {
        if (pending.has(request_id)) {
            pending.delete(request_id);
            sendResponse({ error: 'PageController call timed out' });
        }
    }, 30000);

    return true; // async response
});
