/**
 * Service worker: opens the side panel on action click and proxies
 * PAGE_CONTROL RPCs from the sidepanel to the active tab's content script.
 *
 * "Active tab" is resolved at each call, so switching tabs in the same
 * window naturally redirects actions to whatever the user is looking at.
 */

chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((e) => console.error('[slonagent] setPanelBehavior failed:', e));

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg?.channel !== 'SLON_PAGE_CTRL' || msg.type !== 'call') return;

    (async () => {
        let tabId;
        try {
            // sender.tab is absent for messages from the sidepanel — fall
            // back to the last-focused window to find the right active tab.
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
            // "Receiving end does not exist" = content script is gone,
            // almost always because the previous action navigated mid-call.
            // Mirror WebAgentTransport._on_ws_connect: navigation success
            // for actions, hard error for getBrowserState (PageSkill retries).
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
