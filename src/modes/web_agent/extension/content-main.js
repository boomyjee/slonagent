/**
 * Main-world CS — runs with page JS privileges (subject to page CSP) so it
 * can import @page-agent/page-controller from esm.sh, which the extension's
 * own MV3 CSP (script-src 'self') would forbid in isolated/background land.
 *
 * One PageController per page load, exposed to content-isolated.js via
 * SLON_PAGE_CTRL_MAIN postMessages. If the page CSP blocks the esm.sh
 * import, the controller never comes up and every RPC errors — same
 * failure mode as the bookmarklet on a locked-down page.
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
