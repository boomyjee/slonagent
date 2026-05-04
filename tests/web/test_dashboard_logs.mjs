// Tests for the dashboard Logs panel — three category buttons, plus
// rendering of per-line timestamp prefix and level class.

import { flush, fireMouse, makeContainer } from './_dom.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { render, html } = await import('../../src/transport/web/ui/lib.js');
const { Logs } = await import('../../src/transport/dashboard/ui/components/Logs.js');


function mount(logs = {}) {
    const container = makeContainer();
    let inst;
    const merged = { agent: [], memory: [], transport: [], ...logs };
    render(html`<${Logs} ref=${c => { inst = c; }} logs=${merged} />`, container);
    return { logs: inst, container };
}


test('starts on the agent tab and renders agent entries', async () => {
    const { container } = mount({
        agent:    [{ level: 'INFO', text: 'agent-A' }],
        memory:   [{ level: 'INFO', text: 'memory-A' }],
        transport:[],
    });
    await flush();
    assert.match(container.textContent, /agent-A/);
    assert.doesNotMatch(container.textContent, /memory-A/);
});

test('clicking Memory switches the visible category', async () => {
    const { container } = mount({
        agent:    [{ level: 'INFO', text: 'agent-A' }],
        memory:   [{ level: 'INFO', text: 'memory-B' }],
        transport:[],
    });
    await flush();
    const buttons = [...container.querySelectorAll('div')]
        .filter(d => d.textContent === 'Memory' && !d.querySelector('div'));
    assert.equal(buttons.length, 1);
    fireMouse(buttons[0], 'click');
    await flush();
    assert.match(container.textContent, /memory-B/);
    assert.doesNotMatch(container.textContent, /agent-A/);
});

test('all three category buttons are present', async () => {
    const { container } = mount();
    await flush();
    const labels = [...container.querySelectorAll('div')]
        .map(d => d.textContent)
        .filter(t => ['Agent', 'Memory', 'Transport'].includes(t));
    assert.deepEqual([...new Set(labels)].sort(), ['Agent', 'Memory', 'Transport']);
});

test('log lines get a level class on their <div>', async () => {
    const { container } = mount({
        agent: [
            { level: 'INFO',    text: 'ok' },
            { level: 'WARNING', text: 'careful' },
            { level: 'ERROR',   text: 'boom' },
        ],
    });
    await flush();
    const lines = [...container.querySelectorAll('div')]
        .filter(d => /ok|careful|boom/.test(d.textContent) && !d.querySelector('div'));
    const classes = lines.map(d => d.className);
    assert.equal(classes.some(c => c.includes('INFO')),    true);
    assert.equal(classes.some(c => c.includes('WARNING')), true);
    assert.equal(classes.some(c => c.includes('ERROR')),   true);
});

test('lines starting with a timestamp split into <ts> + body', async () => {
    const { container } = mount({
        agent: [{ level: 'INFO', text: '2026-05-04 12:34:56 hello world' }],
    });
    await flush();
    const ts = container.querySelector('span.ts');
    assert.ok(ts);
    assert.equal(ts.textContent, '2026-05-04 12:34:56');
    // Body text is rendered separately and contains the rest.
    assert.match(container.textContent, /hello world/);
});

test('lines without a timestamp render plain', async () => {
    const { container } = mount({
        agent: [{ level: 'INFO', text: 'no prefix here' }],
    });
    await flush();
    assert.equal(container.querySelector('span.ts'), null);
    assert.match(container.textContent, /no prefix here/);
});

test('log text is rendered as text — no HTML element injection', async () => {
    const { container } = mount({
        agent: [{ level: 'INFO', text: 'tag <script>alert(1)</script>' }],
    });
    await flush();
    // No real <script> element makes it into the DOM. (The component
    // also calls esc() before letting Preact render — that's belt-and-
    // braces; the text node would be safe either way.)
    assert.equal(container.querySelector('script'), null);
});

test('empty category renders no log lines but keeps the tab bar', async () => {
    const { container } = mount();
    await flush();
    // Three tab buttons present.
    const labels = [...container.querySelectorAll('div')]
        .map(d => d.textContent)
        .filter(t => ['Agent', 'Memory', 'Transport'].includes(t));
    assert.equal(labels.length, 3);
});
