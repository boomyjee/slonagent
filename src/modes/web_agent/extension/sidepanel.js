/**
 * Sidepanel bootstrap: prompts for the slonagent URL, then directly imports
 * the vendored chat UI (lib.js + Chat.js + chat-widget.js from ./web/) and
 * renders it. No iframe, no remote fetches — the UI is visible immediately
 * after opening the panel.
 *
 * PageController RPCs go straight from the widget-owned `page` Proxy to
 * `chrome.runtime.sendMessage` → background → content-isolated → content-main.
 * That's one fewer hop than the old iframe flow (which needed postMessage
 * into the iframe, then up to sidepanel, then sendMessage).
 */

const STORAGE_KEY = 'slonagent_base_url';

const root = document.getElementById('root');
const config = document.getElementById('config');
const urlInput = document.getElementById('url');
const saveBtn = document.getElementById('save');
const resetBtn = document.getElementById('reset-btn');

function showConfig(prefill = '') {
    urlInput.value = prefill;
    root.classList.remove('visible');
    root.innerHTML = '';
    resetBtn.style.display = 'none';
    config.classList.add('visible');
    urlInput.focus();
}

// PageController proxy: every method access returns a function that RPCs
// through chrome.runtime to the active tab's content-main PageController.
// `then` is excluded so `page` never looks like a thenable (would break
// `await page` and similar awaiting-truthy checks).
const page = new Proxy({}, {
    get(_, method) {
        if (typeof method !== 'string' || method === 'then') return undefined;
        return async (...args) => {
            const reply = await chrome.runtime.sendMessage({
                channel: 'SLON_PAGE_CTRL', type: 'call', method, args,
            });
            if (reply?.error) throw new Error(reply.error);
            return reply?.result;
        };
    },
});

function buildWsUrl(baseUrl) {
    // baseUrl: e.g. http://localhost:8765/main/web_agent/
    // wsUrl:         ws://localhost:8765/main/web_agent/ws
    const u = new URL(baseUrl);
    u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
    u.pathname = u.pathname.replace(/\/+$/, '') + '/ws';
    return u.toString();
}

async function startWidget(baseUrl) {
    config.classList.remove('visible');
    resetBtn.style.display = 'block';
    root.classList.add('visible');

    const [lib, { Chat }, { createWidgetApp }] = await Promise.all([
        import('./web/lib.js'),
        import('./web/components/Chat.js'),
        import('./web/chat-widget.js'),
    ]);
    const { render, html } = lib;

    const WidgetApp = createWidgetApp({
        lib, Chat,
        wsUrl: buildWsUrl(baseUrl),
        page,
    });
    render(html`<${WidgetApp} />`, root);
}

chrome.storage.local.get([STORAGE_KEY], ({ [STORAGE_KEY]: saved }) => {
    if (saved) startWidget(saved).catch(err => {
        console.error('[slonagent] startWidget failed:', err);
        showConfig(saved);
    });
    else showConfig();
});

saveBtn.addEventListener('click', () => {
    const url = urlInput.value.trim();
    if (!url) return;
    chrome.storage.local.set({ [STORAGE_KEY]: url }, () => startWidget(url));
});

urlInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') saveBtn.click();
});

resetBtn.addEventListener('click', () => {
    chrome.storage.local.get([STORAGE_KEY], ({ [STORAGE_KEY]: saved }) => {
        showConfig(saved || '');
    });
});
