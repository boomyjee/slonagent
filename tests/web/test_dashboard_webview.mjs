// Tests for the dashboard WebView — wraps an <iframe> with reload-via-
// about:blank and a same-origin title-sync onLoad handler.

import { flush, makeContainer } from './_dom.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { render, html } = await import('../../src/transport/web/ui/lib.js');
const { WebView } = await import('../../src/transport/dashboard/ui/components/WebView.js');


function mount(props = {}) {
    const container = makeContainer();
    let inst;
    render(html`<${WebView} ref=${c => { inst = c; }} ...${props} />`, container);
    return { view: inst, container };
}


test('renders an iframe with the given src and a sandbox attribute', async () => {
    const { container } = mount({ url: 'http://example.test/' });
    await flush();
    const iframe = container.querySelector('iframe');
    assert.ok(iframe);
    assert.equal(iframe.getAttribute('src'), 'http://example.test/');
    const sandbox = iframe.getAttribute('sandbox') || '';
    assert.match(sandbox, /allow-scripts/);
    assert.match(sandbox, /allow-same-origin/);
});

test('refresh() bounces src through about:blank then back', async () => {
    const { view, container } = mount({ url: 'http://a.test/' });
    await flush();
    const iframe = container.querySelector('iframe');
    iframe.src = 'http://a.test/';
    view.refresh();
    assert.equal(iframe.src, 'about:blank');

    // The bounce-back is queued via requestAnimationFrame.
    await new Promise(r => requestAnimationFrame(r));
    assert.equal(iframe.src, 'http://a.test/');
});

test('refresh() before mount is a safe no-op (no _iframe ref yet)', () => {
    const { view } = mount({ url: 'http://a.test/' });
    view._iframe = null;
    // Should not throw.
    view.refresh();
});

test('onLoad calls onTitle with the iframe document title (same-origin)', async () => {
    const titles = [];
    const { container } = mount({ url: 'about:blank', onTitle: t => titles.push(t) });
    await flush();
    const iframe = container.querySelector('iframe');
    // Force a contentDocument with a known title — JSDOM gives us one
    // for about:blank already, but we plant the title to verify the path.
    Object.defineProperty(iframe, 'contentDocument', {
        configurable: true,
        get: () => ({ title: 'Example Page' }),
    });
    iframe.dispatchEvent(new window.Event('load'));
    await flush();
    assert.deepEqual(titles, ['Example Page']);
});

test('onLoad swallows SecurityError when contentDocument is cross-origin', async () => {
    const titles = [];
    const { container } = mount({ url: 'http://x.test/', onTitle: t => titles.push(t) });
    await flush();
    const iframe = container.querySelector('iframe');
    Object.defineProperty(iframe, 'contentDocument', {
        configurable: true,
        get: () => { throw new Error('SecurityError'); },
    });
    // Should not throw.
    iframe.dispatchEvent(new window.Event('load'));
    await flush();
    assert.deepEqual(titles, []);
});

test('onLoad with no onTitle prop is fine', async () => {
    const { container } = mount({ url: 'about:blank' });
    await flush();
    const iframe = container.querySelector('iframe');
    Object.defineProperty(iframe, 'contentDocument', {
        configurable: true,
        get: () => ({ title: 'whatever' }),
    });
    iframe.dispatchEvent(new window.Event('load'));
});
