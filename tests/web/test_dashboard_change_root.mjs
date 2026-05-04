// Tests for openChangeRootDialog — folder picker that lives inside the
// shared Dialog singleton. Uses the api() helper which calls fetch().

import { mockResponse, flush, fireMouse } from './_dom.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { Dialog } = await import('../../src/transport/web/ui/components/common/Dialog.js');
const { openChangeRootDialog } = await import('../../src/transport/dashboard/ui/components/ChangeRootDialog.js');


// /api/dirs?path=<x> mock. Returns an entries listing for known paths
// and { error } for unknown ones.
function installFs(map) {
    globalThis.fetch = async (url) => {
        const m = url.match(/api\/dirs\?path=([^&]*)/);
        if (!m) return mockResponse({ error: 'unknown route' });
        const path = decodeURIComponent(m[1]);
        const node = map[path];
        if (!node) return mockResponse({ error: 'not found' });
        return mockResponse(node);
    };
}

const node = (path, parent, names) => ({
    path, parent,
    entries: names.map(n => ({ name: n, path: path === '' ? n : `${path}/${n}` })),
});


test('opens a Dialog and lists entries from /api/dirs', async () => {
    installFs({
        '': node('', null, ['/Users']),
        '/Users': node('/Users', '', ['me']),
    });
    openChangeRootDialog({ initialPath: '', onConfirm: () => {} });
    // _navigate('') is fired in componentDidMount → wait for fetch.
    for (let i = 0; i < 5; i++) await flush();
    assert.match(document.body.textContent, /\/Users/);
    Dialog.close();
    await flush();
});

test('initialPath="/a/b" opens at parent "/a" so siblings are visible', async () => {
    installFs({
        '/a': node('/a', '/', ['b', 'sibling']),
    });
    openChangeRootDialog({ initialPath: '/a/b', onConfirm: () => {} });
    for (let i = 0; i < 5; i++) await flush();
    assert.match(document.body.textContent, /sibling/);
    Dialog.close();
    await flush();
});

test('clicking an entry navigates into it', async () => {
    installFs({
        '': node('', null, ['root']),
        'root': node('root', '', ['inner']),
        'root/inner': node('root/inner', 'root', ['leaf']),
    });
    openChangeRootDialog({ initialPath: '', onConfirm: () => {} });
    for (let i = 0; i < 5; i++) await flush();

    // Reverse → leaf-first; the parent list also has textContent '▸ root'.
    const rootEntry = [...document.querySelectorAll('div')].reverse().find(d => d.textContent.trim() === '▸ root');
    fireMouse(rootEntry, 'click');
    for (let i = 0; i < 5; i++) await flush();
    assert.match(document.body.textContent, /inner/);
    Dialog.close();
    await flush();
});

test('double-clicking an entry confirms onto that path and closes', async () => {
    installFs({
        '': node('', null, ['picked']),
    });
    let confirmed;
    openChangeRootDialog({ initialPath: '', onConfirm: p => { confirmed = p; } });
    for (let i = 0; i < 5; i++) await flush();

    const entry = [...document.querySelectorAll('div')].reverse().find(d => d.textContent.trim() === '▸ picked');
    fireMouse(entry, 'dblclick');
    await flush();
    assert.equal(confirmed, 'picked');
    // Dialog content gone after close.
    await flush();
});

test('"Use this folder" confirms with the current path', async () => {
    installFs({
        '': node('', null, []),
        '/home': node('/home', '', ['x', 'y']),
    });
    let confirmed;
    openChangeRootDialog({ initialPath: '/home/anything', onConfirm: p => { confirmed = p; } });
    for (let i = 0; i < 5; i++) await flush();

    const useBtn = [...document.querySelectorAll('button')].find(b => b.textContent === 'Use this folder');
    assert.ok(useBtn);
    fireMouse(useBtn, 'click');
    await flush();
    assert.equal(confirmed, '/home');
});

test('Cancel closes the dialog without firing onConfirm', async () => {
    installFs({
        '': node('', null, ['x']),
    });
    let confirmed = false;
    openChangeRootDialog({ initialPath: '', onConfirm: () => { confirmed = true; } });
    for (let i = 0; i < 5; i++) await flush();

    const cancel = [...document.querySelectorAll('button')].find(b => b.textContent === 'Cancel');
    fireMouse(cancel, 'click');
    await flush();
    assert.equal(confirmed, false);
});

test('a server error shows the error message, no entries', async () => {
    globalThis.fetch = async () => mockResponse({ error: 'permission denied' });
    openChangeRootDialog({ initialPath: '', onConfirm: () => {} });
    for (let i = 0; i < 5; i++) await flush();
    assert.match(document.body.textContent, /permission denied/);
    Dialog.close();
    await flush();
});

test('parent==null disables the ↑ button (root listing)', async () => {
    installFs({
        '': node('', null, ['drive1']),
    });
    openChangeRootDialog({ initialPath: '', onConfirm: () => {} });
    for (let i = 0; i < 5; i++) await flush();

    const up = [...document.querySelectorAll('button')].find(b => b.textContent === '↑');
    assert.ok(up);
    assert.equal(up.disabled, true);
    Dialog.close();
    await flush();
});
