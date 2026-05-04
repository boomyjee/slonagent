// Tests for Resizer — a 2px column-resize handle. Drags adjacent sibling
// (previous on side="left", next on side="right") to a new width and
// persists the result under `resizer.<persistKey>` in localStorage.

import { flush, fireMouse, makeContainer } from './_dom.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { render, html } = await import('../../src/transport/web/ui/lib.js');
const { Resizer } = await import('../../src/transport/web/ui/components/common/Resizer.js');


// Mount [pane][resizer][pane] so both sides have a sibling. Stub each
// pane's getBoundingClientRect since jsdom returns zeros.
function mountWithPanes({ side = 'right', persistKey, leftWidth = 200, rightWidth = 200 } = {}) {
    const container = makeContainer();
    container.style.display = 'flex';

    render(
        html`
            <div class="pane left">L</div>
            <${Resizer} side=${side} persistKey=${persistKey} />
            <div class="pane right">R</div>
        `,
        container,
    );

    const left  = container.querySelector('.left');
    const right = container.querySelector('.right');
    left.getBoundingClientRect  = () => ({ left: 0,   right: leftWidth,            width: leftWidth,  top: 0, bottom: 100, height: 100 });
    right.getBoundingClientRect = () => ({ left: leftWidth + 2, right: leftWidth + 2 + rightWidth, width: rightWidth, top: 0, bottom: 100, height: 100 });

    const resizer = container.querySelector('div[class*="bau"]');
    return { container, left, right, resizer };
}

function clearStorage() {
    try { localStorage.clear(); } catch {}
}


test('side="right" drag widens the previous sibling (right side shrinks)', async () => {
    clearStorage();
    const { right, resizer } = mountWithPanes({ side: 'right', rightWidth: 200 });
    await flush();

    fireMouse(resizer, 'mousedown', { button: 0, clientX: 200 });
    fireMouse(window.document, 'mousemove', { clientX: 150 }); // dx = -50, side=right → +50
    assert.equal(right.style.width, '250px');
    fireMouse(window.document, 'mouseup', {});
});

test('side="left" drag widens the next sibling (left side shrinks)', async () => {
    clearStorage();
    const { left, resizer } = mountWithPanes({ side: 'left', leftWidth: 200 });
    await flush();

    fireMouse(resizer, 'mousedown', { button: 0, clientX: 200 });
    fireMouse(window.document, 'mousemove', { clientX: 250 }); // dx = +50, side=left → +50
    assert.equal(left.style.width, '250px');
    fireMouse(window.document, 'mouseup', {});
});

test('width is clamped to a 150px minimum', async () => {
    clearStorage();
    const { right, resizer } = mountWithPanes({ side: 'right', rightWidth: 200 });
    await flush();

    fireMouse(resizer, 'mousedown', { button: 0, clientX: 200 });
    // Drag way past — would compute -1000px width, clamp kicks in.
    fireMouse(window.document, 'mousemove', { clientX: 1400 });
    assert.equal(right.style.width, '150px');
    fireMouse(window.document, 'mouseup', {});
});

test('mouseup persists the final width under resizer.<key>', async () => {
    clearStorage();
    const { resizer } = mountWithPanes({ side: 'right', rightWidth: 200, persistKey: 'chat' });
    await flush();

    fireMouse(resizer, 'mousedown', { button: 0, clientX: 200 });
    fireMouse(window.document, 'mousemove', { clientX: 100 }); // → 300
    fireMouse(window.document, 'mouseup', {});

    // The persist module scopes by location.pathname under "slon:<path>".
    const stored = Object.entries(localStorage).find(([k]) => k.endsWith(':resizer.chat'));
    assert.ok(stored, 'expected a resizer.chat entry in localStorage');
    assert.equal(JSON.parse(stored[1]), 300);
});

test('without persistKey, mouseup does not write to localStorage', async () => {
    clearStorage();
    const { resizer } = mountWithPanes({ side: 'right', rightWidth: 200 });
    await flush();

    fireMouse(resizer, 'mousedown', { button: 0, clientX: 200 });
    fireMouse(window.document, 'mousemove', { clientX: 100 });
    fireMouse(window.document, 'mouseup', {});

    const anyResizerKey = Object.keys(localStorage).some(k => k.includes(':resizer.'));
    assert.equal(anyResizerKey, false);
});

test('mounting with a persisted width applies it to the target sibling', async () => {
    clearStorage();
    // persist.set uses 'slon:<pathname>:<key>'. pathname is '/' by default.
    localStorage.setItem('slon::resizer.editor', JSON.stringify(420));
    const { right } = mountWithPanes({ side: 'right', persistKey: 'editor' });
    await flush();
    assert.equal(right.style.width, '420px');
});

test('mousemove without prior mousedown does nothing', async () => {
    clearStorage();
    const { right } = mountWithPanes({ side: 'right', rightWidth: 200 });
    await flush();
    // No mousedown — handler is not registered. width stays empty.
    fireMouse(window.document, 'mousemove', { clientX: 100 });
    assert.equal(right.style.width, '');
});
