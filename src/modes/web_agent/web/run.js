/**
 * SlonAgent bookmarklet entry — `javascript:import('.../web_agent/run.js')`.
 *
 * Owns the floating panel, shadow DOM, and local PageController; delegates
 * chat/WebSocket to the shared chat-widget.js (also used by the extension).
 * Repeat bookmarklet clicks are harmless — ES module caching makes the
 * second import a no-op.
 */

const selfUrl = new URL(import.meta.url);
const match = selfUrl.pathname.match(/^\/([^/]+)\/web_agent\/run\.js$/);
if (!match) throw new Error('[slonagent] run.js mounted from unexpected URL: ' + selfUrl.pathname);
const AGENT_ID = match[1];
const DASH = `${selfUrl.protocol}//${selfUrl.host}/${AGENT_ID}/dashboard`;
const BASE = `${selfUrl.protocol}//${selfUrl.host}/${AGENT_ID}/web_agent`;

// Imported before chat-widget.js so we can set `stylesHost.target = shadow`
// before Chat.js module-eval runs its top-level `css` calls.
const lib = await import(`${DASH}/lib.js`);
const { render, html, stylesHost } = lib;

// `mode: 'closed'` is critical — without it, dom_tree.js (used by the
// agent's own PageController) walks into the widget's shadow root and
// indexes its own buttons, so the agent "sees itself" in browser_state.
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

// esm.sh resolves its own sibling chunks (SimulatorMask, ai-motion, ...)
// against its origin, so we don't mirror them locally.
const { PageController } = await import('https://esm.sh/@page-agent/page-controller@1.7.1');
const page = new PageController({ enableMask: true });

// Shadow DOM blocks outer selectors but NOT inheritance of properties like
// color/font from the host element — `all: initial` is a hard reset so the
// widget looks the same no matter what the host page sets on <body>.
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

const [{ Chat }, { createWidgetApp }] = await Promise.all([
    import(`${DASH}/components/Chat.js`),
    import(`${BASE}/chat-widget.js`),
]);
const wsUrl = `${selfUrl.protocol === 'https:' ? 'wss' : 'ws'}://${selfUrl.host}/${AGENT_ID}/web_agent/ws`;
const WidgetApp = createWidgetApp({
    lib, Chat, wsUrl, page,
    onSuperseded: () => host.remove(),
});
render(html`<${WidgetApp} />`, root);

// Custom resize handle at top-left (host is anchored bottom-right, native
// CSS `resize` only draws a bottom-right handle). Must be appended AFTER
// the preact render — preact reuses existing children of the render target,
// so inserting first would turn this resizer div into the chat root and
// leak its opacity/listeners to the whole chat.
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
