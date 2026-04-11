/**
 * Main-world content script — runs with page JS privileges (subject to page
 * CSP) so it can dynamically import @page-agent/page-controller from esm.sh
 * without tripping the extension's own MV3 CSP (which forbids remote imports).
 *
 * Creates one PageController per page load and exposes it to the
 * isolated-world content script via window.postMessage on the
 * SLON_PAGE_CTRL_MAIN channel. The isolated script has chrome.runtime and
 * handles the RPC plumbing with the background worker — we just do the
 * actual DOM work here.
 *
 * If the page's CSP blocks the esm.sh import (strict script-src), the
 * controller simply never comes up and every RPC returns an error — same
 * failure mode as the bookmarklet.
 */

(async () => {
    let page;
    try {
        const { PageController } = await import('https://esm.sh/@page-agent/page-controller@1.7.1');
        page = new PageController({ enableMask: true });
    } catch (err) {
        console.warn('[slonagent] PageController init failed:', err);
        window.postMessage({ channel: 'SLON_PAGE_CTRL_MAIN', type: 'init_failed', error: err?.message || String(err) }, '*');
        return;
    }

    window.postMessage({ channel: 'SLON_PAGE_CTRL_MAIN', type: 'ready' }, '*');

    window.addEventListener('message', async (e) => {
        if (e.source !== window) return;
        const msg = e.data;
        if (msg?.channel !== 'SLON_PAGE_CTRL_MAIN' || msg.type !== 'call') return;

        let result, error;
        try {
            const fn = page[msg.method];
            if (typeof fn !== 'function') throw new Error(`unknown action: ${msg.method}`);
            result = await fn.apply(page, msg.args || []);
        } catch (err) {
            error = err?.message || String(err);
        }
        window.postMessage({
            channel: 'SLON_PAGE_CTRL_MAIN', type: 'result',
            request_id: msg.request_id, result, error,
        }, '*');
    });
})();
