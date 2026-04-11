/**
 * Extension sidepanel iframe entry. Mirrors run.js but without the floating
 * panel / shadow DOM / local PageController — chat-widget.js gets a
 * postMessage shim instead, RPCing up through the sidepanel → background →
 * content script → real PageController in the active tab's main world.
 *
 * The iframe is served from our tunnel origin (not chrome-extension://),
 * so remote ES module imports work; the sidepanel itself couldn't import
 * them due to MV3 CSP, which is why we need the iframe hop at all.
 */

const selfUrl = new URL(import.meta.url);
const match = selfUrl.pathname.match(/^\/([^/]+)\/web_agent\/ext\.js$/);
if (!match) throw new Error('[slonagent] ext.js mounted from unexpected URL: ' + selfUrl.pathname);
const AGENT_ID = match[1];
const BASE = `${selfUrl.protocol}//${selfUrl.host}/${AGENT_ID}/web_agent`;
const DASH = `${selfUrl.protocol}//${selfUrl.host}/${AGENT_ID}/dashboard`;

const lib = await import(`${DASH}/lib.js`);
const { render, html } = lib;

// RemotePageController shim — Proxy + postMessage RPC to the active tab's
// real PageController (via sidepanel → background → content script).
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

// `dispose` is a local-only lifecycle hook — nothing to tear down over
// RPC. The `then` filter prevents `page` from looking like a thenable.
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
