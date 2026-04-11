/**
 * Chrome extension sidepanel iframe entry point.
 *
 * Mirrors run.js but without the floating panel / shadow DOM / local
 * PageController. Instead of a real PageController we hand chat-widget.js a
 * postMessage shim that RPCs up to the sidepanel parent window → background
 * → content script → the real PageController living in the active tab's
 * main world.
 *
 * The iframe is loaded from our web server origin (via the tunnel), so ES
 * module imports to the dashboard path work the same as in the bookmarklet.
 * The extension sidepanel itself is a `chrome-extension://` page that can't
 * import remote modules due to MV3 CSP — that's why we need the iframe hop.
 */

const selfUrl = new URL(import.meta.url);
const match = selfUrl.pathname.match(/^\/([^/]+)\/web_agent\/ext\.js$/);
if (!match) throw new Error('[slonagent] ext.js mounted from unexpected URL: ' + selfUrl.pathname);
const AGENT_ID = match[1];
const BASE = `${selfUrl.protocol}//${selfUrl.host}/${AGENT_ID}/web_agent`;
const DASH = `${selfUrl.protocol}//${selfUrl.host}/${AGENT_ID}/dashboard`;

const lib = await import(`${DASH}/lib.js`);
const { render, html } = lib;

// RemotePageController shim. Each method-invocation turns into a postMessage
// to parent (sidepanel) with a request_id; sidepanel relays through the
// extension's background service worker to the active tab's content script,
// then back. The Proxy makes `page.clickElement(...)` just work without
// enumerating methods — mirrors the real PageController's surface.
const _pending = new Map();
window.addEventListener('message', (e) => {
    const msg = e.data;
    if (msg?.channel !== 'SLON_WEB_AGENT' || msg.type !== 'action_result') return;
    const p = _pending.get(msg.request_id);
    if (!p) return;
    _pending.delete(msg.request_id);
    if (msg.error) p.reject(new Error(msg.error));
    else p.resolve(msg.result);
});

function rpc(method, args) {
    return new Promise((resolve, reject) => {
        const request_id = (crypto.randomUUID?.() || String(Math.random()).slice(2));
        _pending.set(request_id, { resolve, reject });
        window.parent.postMessage({
            channel: 'SLON_WEB_AGENT', type: 'action',
            method, args, request_id,
        }, '*');
    });
}

// `dispose` is a local lifecycle hook on the real PageController — there's
// nothing to tear down on the RPC side, so short-circuit it. Everything
// else (click, type, getBrowserState, showMask, hideMask, ...) goes over
// the bridge to the content script's PageController in the active tab.
// Symbol keys and `then` are filtered so the Proxy doesn't accidentally
// turn `page` itself into a thenable.
const page = new Proxy({}, {
    get(_, method) {
        if (typeof method !== 'string') return undefined;
        if (method === 'dispose') return () => {};
        if (method === 'then') return undefined;
        return (...args) => rpc(method, args);
    },
});

const { createWidgetApp } = await import(`${BASE}/chat-widget.js`);
const WidgetApp = await createWidgetApp({
    agentId: AGENT_ID,
    host: selfUrl.host,
    protocol: selfUrl.protocol,
    page,
});

render(html`<${WidgetApp} />`, document.getElementById('root'));
