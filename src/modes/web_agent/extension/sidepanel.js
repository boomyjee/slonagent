/**
 * Slonagent sidepanel — loads the chat UI from the user-configured
 * slonagent server in an iframe, and relays PAGE_CONTROL messages
 * between the iframe and the extension's background worker.
 *
 * Why an iframe: MV3 extension pages forbid loading remote scripts
 * (script-src 'self'), so we can't import chat-widget.js directly here.
 * An iframe pointed at our own server side-steps that — the iframe runs
 * under the tunnel origin with its own (permissive) CSP and can import
 * everything it needs.
 */

const STORAGE_KEY = 'slonagent_base_url';

const frame = document.getElementById('frame');
const config = document.getElementById('config');
const urlInput = document.getElementById('url');
const saveBtn = document.getElementById('save');
const resetBtn = document.getElementById('reset-btn');

function showConfig(prefill = '') {
    urlInput.value = prefill;
    frame.style.display = 'none';
    frame.src = 'about:blank';
    resetBtn.style.display = 'none';
    config.classList.add('visible');
    urlInput.focus();
}

function showFrame(baseUrl) {
    // Normalize: strip trailing slash, append /ext.html if not already pointing at a file.
    let url = baseUrl.replace(/\/+$/, '');
    if (!/\.html?$/.test(url)) url += '/ext.html';
    config.classList.remove('visible');
    frame.style.display = 'block';
    frame.src = url;
    resetBtn.style.display = 'block';
}

chrome.storage.local.get([STORAGE_KEY], ({ [STORAGE_KEY]: saved }) => {
    if (saved) showFrame(saved);
    else showConfig();
});

saveBtn.addEventListener('click', () => {
    const url = urlInput.value.trim();
    if (!url) return;
    chrome.storage.local.set({ [STORAGE_KEY]: url }, () => showFrame(url));
});

urlInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') saveBtn.click();
});

resetBtn.addEventListener('click', () => {
    chrome.storage.local.get([STORAGE_KEY], ({ [STORAGE_KEY]: saved }) => {
        showConfig(saved || '');
    });
});

// --- iframe ↔ background relay ---
//
// iframe (ext.js) posts {channel:'SLON_WEB_AGENT', type:'action', method, args, request_id}
// We forward to background, await its reply, and postMessage the result
// back to the iframe. Background handles talking to the content script.
window.addEventListener('message', async (e) => {
    if (e.source !== frame.contentWindow) return;
    const msg = e.data;
    if (msg?.channel !== 'SLON_WEB_AGENT' || msg.type !== 'action') return;

    let reply;
    try {
        reply = await chrome.runtime.sendMessage({
            channel: 'SLON_PAGE_CTRL', type: 'call',
            method: msg.method, args: msg.args,
        });
    } catch (err) {
        reply = { error: err?.message || String(err) };
    }

    frame.contentWindow.postMessage({
        channel: 'SLON_WEB_AGENT', type: 'action_result',
        request_id: msg.request_id,
        result: reply?.result,
        error: reply?.error,
    }, '*');
});
