/**
 * Main-world CS — runs with page JS privileges to drive page-agent's
 * PageController from inside the site's own context. One controller per page
 * load, exposed to content-isolated.js via SLON_PAGE_CTRL_MAIN postMessages.
 *
 * page-controller + its lazy chunks are vendored into extension/vendor/ and
 * loaded via chrome-extension:// URL (passed in by content-isolated.js via
 * `documentElement.dataset.slonPcUrl`). We used to import from esm.sh, but
 * page CSP / service workers / import maps on third-party sites broke the
 * chain's relative dynamic imports unpredictably; chrome-extension URLs are
 * exempt from all of that.
 */

(async () => {
    let page;
    try {
        const pcUrl = document.documentElement.dataset.slonPcUrl;
        if (!pcUrl) throw new Error('slonPcUrl not set by content-isolated.js');
        const { PageController } = await import(pcUrl);
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
