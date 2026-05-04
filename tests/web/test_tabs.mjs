// Tests for Tabs — generic tab bar with select/close, context menu and
// drag-to-reorder. We mount via lib.js's render (same Preact instance as
// the browser uses) and drive interactions with synthesized DOM events.

import { mockResponse, flush, fireMouse, makeContainer } from './_dom.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { render, html } = await import('../../src/transport/web/ui/lib.js');
const { Tabs } = await import('../../src/transport/web/ui/components/common/Tabs.js');


function mount(props = {}) {
    const container = makeContainer();
    const calls = { select: [], close: [], reorder: [] };
    const defaults = {
        active: '',
        onSelect: id => calls.select.push(id),
        onClose: id => calls.close.push(id),
        onReorder: (id, at) => calls.reorder.push([id, at]),
    };
    let inst;
    render(
        html`<${Tabs} ref=${c => { inst = c; }} ...${{ ...defaults, ...props }} />`,
        container,
    );
    return { tabs: inst, container, calls };
}

const tab = (id, extra = {}) => ({ id, label: id, ...extra });


// ─── basic ─────────────────────────────────────────────────────────────

test('renders one DOM node per tab with its label', async () => {
    const { container } = mount({ tabs: [tab('a'), tab('b'), tab('c')] });
    await flush();
    const els = container.querySelectorAll('[data-tab-id]');
    assert.equal(els.length, 3);
    assert.equal(els[0].dataset.tabId, 'a');
    assert.match(els[0].textContent, /a/);
});

test('active tab gets the .active class', async () => {
    const { container } = mount({ tabs: [tab('a'), tab('b')], active: 'b' });
    await flush();
    const [a, b] = container.querySelectorAll('[data-tab-id]');
    assert.equal(a.className.includes('active'), false);
    assert.equal(b.className.includes('active'), true);
});

test('tooltip prop is rendered as the title attribute', async () => {
    const { container } = mount({ tabs: [
        { id: 'a', label: 'A', tooltip: 'long description' },
        { id: 'b', label: 'B' },
    ]});
    await flush();
    const [a, b] = container.querySelectorAll('[data-tab-id]');
    assert.equal(a.getAttribute('title'), 'long description');
    assert.equal(b.getAttribute('title'), '');
});

test('clicking a tab calls onSelect with its id', async () => {
    const { container, calls } = mount({ tabs: [tab('a'), tab('b')] });
    await flush();
    const b = container.querySelector('[data-tab-id="b"]');
    fireMouse(b, 'click');
    assert.deepEqual(calls.select, ['b']);
});

test('clicking the × calls onClose and does not also fire onSelect', async () => {
    const { container, calls } = mount({ tabs: [tab('a'), tab('b')] });
    await flush();
    const close = container.querySelector('[data-tab-id="b"] .close');
    fireMouse(close, 'click');
    assert.deepEqual(calls.close, ['b']);
    assert.deepEqual(calls.select, []);
});

test('closable=false hides the × button', async () => {
    const { container } = mount({ tabs: [
        { id: 'logs', label: 'Logs', closable: false },
        tab('a'),
    ]});
    await flush();
    const closes = container.querySelectorAll('[data-tab-id="logs"] .close');
    assert.equal(closes.length, 0);
    // Closable tab still shows it.
    assert.equal(container.querySelectorAll('[data-tab-id="a"] .close').length, 1);
});


// ─── dirty / disk / status dot ─────────────────────────────────────────

test('dirty tabs get the .dirty class', async () => {
    const { container } = mount({ tabs: [tab('a', { dirty: true })] });
    await flush();
    const el = container.querySelector('[data-tab-id="a"]');
    assert.equal(el.className.includes('dirty'), true);
});

test('diskChanged wins over dirty (file changed underneath edits)', async () => {
    const { container } = mount({ tabs: [tab('a', { dirty: true, diskChanged: true })] });
    await flush();
    const el = container.querySelector('[data-tab-id="a"]');
    assert.equal(el.className.includes('disk'), true);
    assert.equal(el.className.includes('dirty'), false);
});


// ─── context menu ──────────────────────────────────────────────────────

test('right-click opens a menu with items from contextItems(tab)', async () => {
    let captured;
    const { container, tabs } = mount({
        tabs: [tab('a'), tab('b')],
        contextItems: t => {
            captured = t.id;
            return [
                { label: 'Close', action: () => {} },
                { label: 'Close Others', action: () => {} },
            ];
        },
    });
    await flush();
    const a = container.querySelector('[data-tab-id="a"]');
    fireMouse(a, 'contextmenu', { clientX: 50, clientY: 60 });
    await flush();
    assert.equal(captured, 'a');
    assert.ok(tabs.state.menu);
    assert.equal(tabs.state.menu.items.length, 2);
    assert.equal(tabs.state.menu.x, 50);
    assert.equal(tabs.state.menu.y, 60);
});

test('clicking a menu item runs its action and closes the menu', async () => {
    let ran = false;
    const { container, tabs } = mount({
        tabs: [tab('a')],
        contextItems: () => [{ label: 'Run', action: () => { ran = true; } }],
    });
    await flush();
    fireMouse(container.querySelector('[data-tab-id="a"]'), 'contextmenu', { clientX: 1, clientY: 1 });
    await flush();
    // Find the menu item by its text.
    // Reverse for deepest-first — the menu host also satisfies the text
    // match, but we want the leaf item div, not its parent.
    const item = [...container.querySelectorAll('div')].reverse().find(d => d.textContent.trim() === 'Run');
    fireMouse(item, 'click');
    await flush();
    assert.equal(ran, true);
    assert.equal(tabs.state.menu, null);
});

test('disabled menu items do not fire and do not close the menu', async () => {
    let ran = false;
    const { container, tabs } = mount({
        tabs: [tab('a')],
        contextItems: () => [{ label: 'Nope', disabled: true, action: () => { ran = true; } }],
    });
    await flush();
    fireMouse(container.querySelector('[data-tab-id="a"]'), 'contextmenu', { clientX: 1, clientY: 1 });
    await flush();
    const item = [...container.querySelectorAll('div')].reverse().find(d => d.textContent.trim() === 'Nope');
    fireMouse(item, 'click');
    await flush();
    assert.equal(ran, false);
    assert.ok(tabs.state.menu);  // menu stays open
});

test('an empty contextItems array does not open a menu', async () => {
    const { container, tabs } = mount({
        tabs: [tab('a')],
        contextItems: () => [],
    });
    await flush();
    fireMouse(container.querySelector('[data-tab-id="a"]'), 'contextmenu', { clientX: 1, clientY: 1 });
    await flush();
    assert.equal(tabs.state.menu, null);
});


// ─── drag reorder ──────────────────────────────────────────────────────

test('drag past the half of a target slot reorders via onReorder(id, idx)', async () => {
    const { container, tabs, calls } = mount({ tabs: [tab('a'), tab('b'), tab('c')] });
    await flush();

    // jsdom's getBoundingClientRect is all-zeros — stub it to give each tab
    // a 100px slot so the insert-at math has something real to chew on.
    const els = [...container.querySelectorAll('[data-tab-id]')];
    els.forEach((el, i) => {
        el.getBoundingClientRect = () => ({
            left: i * 100, right: (i + 1) * 100, width: 100,
            top: 0, bottom: 35, height: 35, x: i * 100, y: 0,
        });
    });

    // Press on "a", drag past the midpoint of "c" (x>250 → insertAt=3).
    fireMouse(els[0], 'mousedown', { button: 0, clientX: 50 });
    fireMouse(window.document, 'mousemove', { clientX: 270 });
    await flush();
    assert.equal(tabs.state.dragInsertAt, 3);

    fireMouse(window.document, 'mouseup', {});
    assert.deepEqual(calls.reorder, [['a', 3]]);
});

test('drag that lands on the source itself is treated as no-op', async () => {
    const { container, calls } = mount({ tabs: [tab('a'), tab('b')] });
    await flush();
    const els = [...container.querySelectorAll('[data-tab-id]')];
    els.forEach((el, i) => {
        el.getBoundingClientRect = () => ({
            left: i * 100, right: (i + 1) * 100, width: 100,
            top: 0, bottom: 35, height: 35, x: i * 100, y: 0,
        });
    });

    // Press on "a" and drop without moving past "b"'s midpoint.
    fireMouse(els[0], 'mousedown', { button: 0, clientX: 30 });
    fireMouse(window.document, 'mousemove', { clientX: 40 });
    await flush();
    fireMouse(window.document, 'mouseup', {});
    assert.deepEqual(calls.reorder, []);
});

test('mousedown with button !== 0 does not start a drag', async () => {
    const { container, calls } = mount({ tabs: [tab('a'), tab('b')] });
    await flush();
    const a = container.querySelector('[data-tab-id="a"]');
    fireMouse(a, 'mousedown', { button: 2, clientX: 10 });
    fireMouse(window.document, 'mousemove', { clientX: 200 });
    fireMouse(window.document, 'mouseup', {});
    assert.deepEqual(calls.reorder, []);
});

test('non-closable tabs do not start a drag', async () => {
    const { container, calls } = mount({
        tabs: [{ id: 'logs', label: 'Logs', closable: false }, tab('a')],
    });
    await flush();
    const logs = container.querySelector('[data-tab-id="logs"]');
    fireMouse(logs, 'mousedown', { button: 0, clientX: 10 });
    fireMouse(window.document, 'mousemove', { clientX: 200 });
    fireMouse(window.document, 'mouseup', {});
    assert.deepEqual(calls.reorder, []);
});
