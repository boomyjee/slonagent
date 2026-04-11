/**
 * Isolated-world content script — the side with `chrome.runtime` access.
 *
 * Translates chrome.runtime messages (from the background worker) into
 * window.postMessage'd RPCs to the main-world content script where the
 * real PageController lives, and routes replies back.
 *
 * We track whether the main world has signalled `ready` so background
 * calls that arrive before the esm.sh import finishes get a clean error
 * instead of hanging forever.
 */

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

    // Safety net — if the main world never replies (e.g. PageController
    // method hangs indefinitely), time out so the agent sees an error
    // instead of a stuck step.
    setTimeout(() => {
        if (pending.has(request_id)) {
            pending.delete(request_id);
            sendResponse({ error: 'PageController call timed out' });
        }
    }, 30000);

    return true; // async response
});
