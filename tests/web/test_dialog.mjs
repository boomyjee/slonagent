// Tests for Dialog — imperative singleton modal that mounts itself at
// import time and exposes Dialog.open(content) / Dialog.close().

import { flush, fireMouse } from './_dom.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { html } = await import('../../src/transport/web/ui/lib.js');
const { Dialog } = await import('../../src/transport/web/ui/components/common/Dialog.js');


test('initial state: nothing rendered', () => {
    // The dialog mount lives at the bottom of <body>. Before .open() is
    // called, content is null and the host returns null → no backdrop.
    const backdrops = document.body.querySelectorAll('div[class*="bau"]');
    // We can't know precise classes — look for any modal backdrop in body.
    // The host node itself is empty until open() is called.
    let hasModal = false;
    for (const el of backdrops) if (el.textContent.includes('hello')) hasModal = true;
    assert.equal(hasModal, false);
});

test('Dialog.open renders the content inside a modal', async () => {
    Dialog.open(html`<div class="my-content">hello world</div>`);
    await flush();
    const node = document.querySelector('.my-content');
    assert.ok(node);
    assert.equal(node.textContent, 'hello world');
    Dialog.close();
    await flush();
});

test('Dialog.close removes the modal from the DOM', async () => {
    Dialog.open(html`<div class="closeable">visible</div>`);
    await flush();
    assert.ok(document.querySelector('.closeable'));

    Dialog.close();
    await flush();
    assert.equal(document.querySelector('.closeable'), null);
});

test('open replaces previous content (singleton)', async () => {
    Dialog.open(html`<div class="first">first</div>`);
    await flush();
    Dialog.open(html`<div class="second">second</div>`);
    await flush();
    assert.equal(document.querySelector('.first'), null);
    assert.ok(document.querySelector('.second'));
    Dialog.close();
    await flush();
});

test('mousedown on backdrop + mouseup on backdrop closes the dialog', async () => {
    Dialog.open(html`<div class="content">x</div>`);
    await flush();
    const content = document.querySelector('.content');
    // Walk up two levels: content → modal → backdrop.
    const backdrop = content.parentElement.parentElement;

    fireMouse(backdrop, 'mousedown', { button: 0 });
    fireMouse(backdrop, 'mouseup', { button: 0 });
    await flush();
    assert.equal(document.querySelector('.content'), null);
});

test('mousedown inside modal does not close (drag-out guard)', async () => {
    Dialog.open(html`<div class="content">stay</div>`);
    await flush();
    const content = document.querySelector('.content');
    const modal = content.parentElement;
    const backdrop = modal.parentElement;

    // Stop propagation kicks in inside modal — backdrop's _downTarget
    // never gets set, so the mouseup on backdrop will not match.
    fireMouse(modal, 'mousedown', { button: 0 });
    fireMouse(backdrop, 'mouseup', { button: 0 });
    await flush();
    // Still mounted because mousedown was inside the modal.
    assert.ok(document.querySelector('.content'));
    Dialog.close();
    await flush();
});
