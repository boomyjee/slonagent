/**
 * SlonAgent chat widget — mounted into a host page via bookmarklet.
 *
 * Bookmarklet: javascript:import('http://host:8765/{agent_id}/web_agent/run.js')
 *
 * Thin shell around the shared chat-widget.js — this file owns the floating
 * panel, shadow DOM, and local PageController. Everything chat/WebSocket
 * related lives in chat-widget.js (also used by the Chrome extension).
 *
 * Module caching protects against repeat bookmarklet clicks: a second
 * import of the same URL returns the cached module, top-level code runs once.
 */

const selfUrl = new URL(import.meta.url);
const match = selfUrl.pathname.match(/^\/([^/]+)\/web_agent\/run\.js$/);
if (!match) throw new Error('[slonagent] run.js mounted from unexpected URL: ' + selfUrl.pathname);
const AGENT_ID = match[1];
const DASH = `${selfUrl.protocol}//${selfUrl.host}/${AGENT_ID}/dashboard`;
const BASE = `${selfUrl.protocol}//${selfUrl.host}/${AGENT_ID}/web_agent`;

// Imported BEFORE chat-widget.js so we can set `stylesHost.target = shadow`
// before Chat.js module-eval runs its top-level `css` calls.
const lib = await import(`${DASH}/lib.js`);
const { render, html, stylesHost } = lib;

// Shadow DOM isolates the widget from host-page CSS. `mode: 'closed'` is
// critical — without it, dom_tree.js (used by the agent's own page controller)
// would walk into the widget's shadow root and index its own buttons, so the
// agent would "see itself" in browser_state.
// Size persists per host-page domain via localStorage. Widget is anchored
// to the viewport's bottom-right corner, so the resize handle has to be
// at its TOP-LEFT corner (the opposite side). Native CSS `resize` only
// paints a bottom-right handle, so we build a custom one below.
const SIZE_KEY = 'slonagent-widget-size';
let savedSize;
try { savedSize = JSON.parse(localStorage.getItem(SIZE_KEY) || 'null'); } catch { savedSize = null; }
const initialW = savedSize?.w ?? 380;
const initialH = savedSize?.h ?? 520;

const host = document.createElement('div');
host.id = 'slonagent-web-agent';
host.style.cssText = `
    position: fixed; right: 20px; bottom: 20px;
    width: ${initialW}px; height: ${initialH}px;
    min-width: 280px; min-height: 300px; max-width: 95vw; max-height: 95vh;
    overflow: hidden;
    z-index: 2147483647;
`;
document.body.appendChild(host);
const shadow = host.attachShadow({ mode: 'closed' });
stylesHost.target = shadow;

// @page-agent/page-controller from esm.sh — same CDN we use for preact/htm
// in lib.js. esm.sh resolves its own sibling chunks (SimulatorMask mjs,
// ai-motion, etc.) against its origin, so we don't need to mirror them
// locally. enableMask shows an animated cursor overlay so the user can
// see what the agent is doing on the page.
const { PageController } = await import('https://esm.sh/@page-agent/page-controller@1.7.1');
const page = new PageController({ enableMask: true });

// Root element inside the shadow — holds CSS vars, layout, and visual chrome.
// Shadow DOM blocks outer selectors but NOT inheritance of properties like
// text-align/color/font from the host element. `all: initial` hard-resets
// every property to its initial value, giving Chat a clean slate regardless
// of what the host page sets on <body>.
const root = document.createElement('div');
root.style.cssText = `
    all: initial;
    position: relative;
    width: 100%; height: 100%;
    --bg: #1e1e2e; --surface: #252536; --surface2: #2a2a3d; --surface3: #313147;
    --border: #333350; --text: #cdd6f4; --text-dim: #6c7086;
    --accent: #89b4fa; --green: #a6e3a1; --warn: #f9e2af; --red: #f38ba8;
    background: var(--bg); color: var(--text); border: 1px solid var(--border);
    overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    font: 14px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    display: flex; flex-direction: column;
`;
shadow.appendChild(root);

const { createWidgetApp } = await import(`${BASE}/chat-widget.js`);
const WidgetApp = await createWidgetApp({
    agentId: AGENT_ID,
    host: selfUrl.host,
    protocol: selfUrl.protocol,
    page,
    onSuperseded: () => host.remove(),
});
render(html`<${WidgetApp} />`, root);

// Custom resize handle at top-left corner (host is anchored bottom-right,
// so dragging the top-left outward grows the widget). Appended AFTER the
// preact render because preact reuses existing DOM children of the render
// target — if we appended first, preact would mutate this div into the
// chat root, inherit its inline `opacity: 0.6` + our mouseenter/leave
// listeners, and the whole chat would dim on mouseleave.
const resizer = document.createElement('div');
resizer.style.cssText = `
    position: absolute; left: 0; top: 0; width: 18px; height: 18px;
    cursor: nwse-resize; z-index: 100;
    background: linear-gradient(135deg, var(--accent) 0%, var(--accent) 45%, transparent 48%);
    opacity: 0.6;
`;
resizer.addEventListener('mouseenter', () => resizer.style.opacity = '1');
resizer.addEventListener('mouseleave', () => resizer.style.opacity = '0.6');
root.appendChild(resizer);

resizer.addEventListener('pointerdown', e => {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX, startY = e.clientY;
    const rect = host.getBoundingClientRect();
    const startW = rect.width, startH = rect.height;
    const onMove = ev => {
        const w = Math.max(280, Math.min(window.innerWidth - 40, startW + (startX - ev.clientX)));
        const h = Math.max(300, Math.min(window.innerHeight - 40, startH + (startY - ev.clientY)));
        host.style.width = w + 'px';
        host.style.height = h + 'px';
    };
    const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        try {
            localStorage.setItem(SIZE_KEY, JSON.stringify({
                w: host.offsetWidth, h: host.offsetHeight,
            }));
        } catch {}
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
});
