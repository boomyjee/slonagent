/**
 * Slonagent extension service worker.
 *
 * Two jobs:
 *  1) Open the side panel when the toolbar action is clicked.
 *  2) Proxy PAGE_CONTROL RPCs from the sidepanel to the active tab's content
 *     script, forwarding the reply back.
 *
 * "Active tab" = the last-focused tab in the same window as the sidepanel,
 * resolved at the moment each call arrives. This way switching tabs in the
 * same window naturally redirects actions to whatever the user is looking
 * at without any extra state on our side.
 */

chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((e) => console.error('[slonagent] setPanelBehavior failed:', e));

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg?.channel !== 'SLON_PAGE_CTRL' || msg.type !== 'call') return;

    (async () => {
        let tabId;
        try {
            // Sidepanel lives in a window — find the active tab of that
            // same window. Falling back to lastFocusedWindow covers the
            // case where the message came from a background context.
            const windowId = sender.tab?.windowId
                ?? (await chrome.windows.getLastFocused({ populate: false })).id;
            const [tab] = await chrome.tabs.query({ active: true, windowId });
            if (!tab?.id) { sendResponse({ error: 'no active tab' }); return; }
            tabId = tab.id;
        } catch (err) {
            sendResponse({ error: err?.message || String(err) });
            return;
        }

        try {
            const reply = await chrome.tabs.sendMessage(tabId, {
                channel: 'SLON_PAGE_CTRL', type: 'call',
                method: msg.method, args: msg.args,
            });
            sendResponse(reply ?? { error: 'no reply from content script' });
        } catch (err) {
            // "Could not establish connection. Receiving end does not exist"
            // — the content script is gone. Almost always because the
            // previous action caused navigation and the old CS got torn
            // down mid-call. Mirror WebAgentTransport._on_ws_connect's
            // policy: treat as navigation success for action methods, hard
            // error for getBrowserState (PageSkill will swallow and retry).
            const dead = /receiving end does not exist|could not establish connection/i.test(err?.message || '');
            if (dead && msg.method !== 'getBrowserState') {
                sendResponse({ result: '🔄 Действие вызвало переход на новую страницу' });
            } else {
                sendResponse({ error: err?.message || String(err) });
            }
        }
    })();

    return true; // keep the message channel open for the async sendResponse
});
