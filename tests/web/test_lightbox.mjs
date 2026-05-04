// Tests for Lightbox — imperative singleton image viewer.
// Lightbox.open(imgEl) reads data-lightbox group, finds peers in DOM,
// and shows a modal with prev/next navigation.

import { flush, fireKey } from './_dom.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { Lightbox } = await import('../../src/transport/web/ui/components/common/Lightbox.js');


// Plant a few <img data-lightbox="<group>"> nodes into <body> and return
// them. We use a unique group per test so peers from one test don't leak
// into another's `_items()` query.
function plantGroup(n, group) {
    const imgs = [];
    for (let i = 0; i < n; i++) {
        const img = document.createElement('img');
        img.dataset.lightbox = group;
        img.src = `pic${i}.jpg`;
        document.body.appendChild(img);
        imgs.push(img);
    }
    return imgs;
}

function clearGroup(group) {
    document.querySelectorAll(`img[data-lightbox="${group}"]`).forEach(el => el.remove());
}


test('opening on an image shows the modal with that image visible', async () => {
    const imgs = plantGroup(3, 'g1');
    try {
        Lightbox.open(imgs[1]);
        await flush();
        // Find the rendered <img> inside the lightbox modal (not a peer in body).
        // The modal has a counter "2 / 3" which uniquely identifies us.
        assert.ok([...document.querySelectorAll('div')].some(d => /^2 \/ 3$/.test(d.textContent)));
    } finally {
        Lightbox.close();
        await flush();
        clearGroup('g1');
    }
});

test('opening on a non-lightbox img is a no-op', async () => {
    const stray = document.createElement('img');
    stray.src = 'x.jpg';
    document.body.appendChild(stray);
    try {
        Lightbox.open(stray);
        await flush();
        // No counter rendered → modal is not open.
        const opened = [...document.querySelectorAll('div')].some(d => /^\d+ \/ \d+$/.test(d.textContent));
        assert.equal(opened, false);
    } finally {
        stray.remove();
    }
});

test('ArrowRight advances the index, ArrowLeft moves it back', async () => {
    const imgs = plantGroup(3, 'g2');
    try {
        Lightbox.open(imgs[0]);  // index 0
        await flush();
        assert.ok([...document.querySelectorAll('div')].some(d => /^1 \/ 3$/.test(d.textContent)));

        fireKey('ArrowRight');
        await flush();
        assert.ok([...document.querySelectorAll('div')].some(d => /^2 \/ 3$/.test(d.textContent)));

        fireKey('ArrowRight');
        await flush();
        assert.ok([...document.querySelectorAll('div')].some(d => /^3 \/ 3$/.test(d.textContent)));

        // Past the end clamps; counter stays at 3 / 3.
        fireKey('ArrowRight');
        await flush();
        assert.ok([...document.querySelectorAll('div')].some(d => /^3 \/ 3$/.test(d.textContent)));

        fireKey('ArrowLeft');
        await flush();
        assert.ok([...document.querySelectorAll('div')].some(d => /^2 \/ 3$/.test(d.textContent)));
    } finally {
        Lightbox.close();
        await flush();
        clearGroup('g2');
    }
});

test('Escape closes the modal', async () => {
    const imgs = plantGroup(2, 'g3');
    try {
        Lightbox.open(imgs[0]);
        await flush();
        assert.ok([...document.querySelectorAll('div')].some(d => /^1 \/ 2$/.test(d.textContent)));
        fireKey('Escape');
        await flush();
        const stillOpen = [...document.querySelectorAll('div')].some(d => /^\d+ \/ \d+$/.test(d.textContent));
        assert.equal(stillOpen, false);
    } finally {
        clearGroup('g3');
    }
});

test('Lightbox.close hides the modal even without keyboard', async () => {
    const imgs = plantGroup(1, 'g4');
    try {
        Lightbox.open(imgs[0]);
        await flush();
        Lightbox.close();
        await flush();
        const stillOpen = [...document.querySelectorAll('div')].some(d => /^\d+ \/ \d+$/.test(d.textContent));
        assert.equal(stillOpen, false);
    } finally {
        clearGroup('g4');
    }
});

test('opening on a wrapper element finds the inner img by data-lightbox', async () => {
    const wrap = document.createElement('div');
    const img = document.createElement('img');
    img.dataset.lightbox = 'g5';
    img.src = 'inner.jpg';
    wrap.appendChild(img);
    document.body.appendChild(wrap);
    try {
        Lightbox.open(wrap);
        await flush();
        assert.ok([...document.querySelectorAll('div')].some(d => /^1 \/ 1$/.test(d.textContent)));
    } finally {
        Lightbox.close();
        await flush();
        wrap.remove();
    }
});
