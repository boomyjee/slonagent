// Tests for FileTree — virtual file-system browser with a module-level
// `tree` singleton (expanded set + children cache). We drive it via the
// exported helpers (setTreeScope, isInGitRepo, refreshTree) and a mocked
// /api/files endpoint.

import { mockResponse, flush, fireMouse, makeContainer } from './_dom.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { render, html } = await import('../../src/transport/web/ui/lib.js');
const { FileTree, setTreeScope, isInGitRepo, refreshTree } =
    await import('../../src/transport/dashboard/ui/components/FileTree.js');


// /api/files?path=<x> mock with a virtual filesystem.
function installFs(map) {
    globalThis.fetch = async (url) => {
        const m = url.match(/api\/files\?path=([^&]*)/);
        if (!m) return mockResponse({ error: 'unknown route' });
        const path = decodeURIComponent(m[1]);
        const entries = map[path];
        if (!entries) return mockResponse({ error: 'not found' });
        return mockResponse({ entries });
    };
}

const e = (name, parent, isDir = false, extra = {}) => ({
    name, path: parent === '/' ? '/' + name : `${parent}/${name}`,
    is_dir: isDir, ...extra,
});

function mount(props = {}) {
    const container = makeContainer();
    const calls = { open: [], openGit: [], openGitBlame: [], openUrl: [], changeRoot: [] };
    const defaults = {
        rootPath: '/root',
        rootName: 'root',
        onOpen:         (p, n) => calls.open.push([p, n]),
        onOpenGit:      (p, n) => calls.openGit.push([p, n]),
        onOpenGitBlame: (p, n) => calls.openGitBlame.push([p, n]),
        onOpenUrl:      u => calls.openUrl.push(u),
        onChangeRoot:   () => calls.changeRoot.push(true),
    };
    let inst;
    render(
        html`<${FileTree} ref=${c => { inst = c; }} ...${{ ...defaults, ...props }} />`,
        container,
    );
    return { tree: inst, container, calls };
}

// Each test uses a fresh scope so the module-level expanded/children
// caches don't leak in.
let _scopeCounter = 0;
async function freshScope() {
    setTreeScope(`test-${++_scopeCounter}`);
    for (let i = 0; i < 3; i++) await flush();
}


// ─── basic ─────────────────────────────────────────────────────────────

test('without rootPath renders an empty placeholder', async () => {
    await freshScope();
    const { container } = mount({ rootPath: '' });
    await flush();
    assert.match(container.textContent, /No root/);
});

test('with rootPath renders the root entry as a closed directory', async () => {
    await freshScope();
    const { container } = mount({ rootPath: '/root', rootName: 'root' });
    await flush();
    // Root chevron is collapsed (▸).
    assert.match(container.textContent, /▸/);
    assert.match(container.textContent, /root/);
});


// ─── toggle expand → fetch children ────────────────────────────────────

test('clicking the root chevron fetches children and reveals them', async () => {
    await freshScope();
    installFs({
        '/root': [e('src', '/root', true), e('README.md', '/root')],
    });
    const { container } = mount({ rootPath: '/root' });
    await flush();
    const rootNode = container.querySelector('div[draggable="true"]');
    fireMouse(rootNode, 'click');
    for (let i = 0; i < 5; i++) await flush();
    assert.match(container.textContent, /src/);
    assert.match(container.textContent, /README\.md/);
});

test('directories sort before files within a level', async () => {
    await freshScope();
    installFs({
        '/r': [e('z.txt', '/r'), e('a-folder', '/r', true), e('b.md', '/r')],
    });
    const { container } = mount({ rootPath: '/r' });
    await flush();
    const root = container.querySelector('div[draggable="true"]');
    fireMouse(root, 'click');
    for (let i = 0; i < 5; i++) await flush();
    // textContent has chevron/icon prefix — pull out just the visible name.
    const known = /(a-folder|b\.md|z\.txt)/;
    const ordered = [...container.querySelectorAll('div[draggable="true"]')]
        .map(n => n.textContent.match(known)?.[1])
        .filter(Boolean);
    assert.deepEqual(ordered, ['a-folder', 'b.md', 'z.txt']);
});


// ─── file open / selection ─────────────────────────────────────────────

test('clicking a file calls onOpen with (path, name)', async () => {
    await freshScope();
    installFs({ '/r': [e('app.py', '/r')] });
    const { container, calls } = mount({ rootPath: '/r' });
    await flush();
    fireMouse(container.querySelector('div[draggable="true"]'), 'click');
    for (let i = 0; i < 5; i++) await flush();
    // The file <div> has draggable=true too — find by its text.
    const fileNode = [...container.querySelectorAll('div[draggable="true"]')]
        .find(n => n.textContent.includes('app.py'));
    fireMouse(fileNode, 'click', { button: 0 });
    await flush();
    assert.deepEqual(calls.open, [['/r/app.py', 'app.py']]);
});

test('Ctrl-click toggles a file into the selection without opening it', async () => {
    await freshScope();
    installFs({ '/r': [e('a.txt', '/r'), e('b.txt', '/r')] });
    const { container, tree, calls } = mount({ rootPath: '/r' });
    await flush();
    fireMouse(container.querySelector('div[draggable="true"]'), 'click');
    for (let i = 0; i < 5; i++) await flush();
    const aNode = [...container.querySelectorAll('div[draggable="true"]')]
        .find(n => n.textContent.includes('a.txt'));
    fireMouse(aNode, 'click', { ctrlKey: true });
    await flush();
    assert.equal(tree.state.selected.has('/r/a.txt'), true);
    assert.deepEqual(calls.open, []);  // not opened on ctrl-click
});

test('plain click after a selection clears it', async () => {
    await freshScope();
    installFs({ '/r': [e('a.txt', '/r')] });
    const { container, tree } = mount({ rootPath: '/r' });
    await flush();
    fireMouse(container.querySelector('div[draggable="true"]'), 'click');
    for (let i = 0; i < 5; i++) await flush();
    const aNode = [...container.querySelectorAll('div[draggable="true"]')]
        .find(n => n.textContent.includes('a.txt'));
    fireMouse(aNode, 'click', { ctrlKey: true });
    await flush();
    assert.equal(tree.state.selected.size, 1);
    fireMouse(aNode, 'click');
    await flush();
    assert.equal(tree.state.selected.size, 0);
});


// ─── isInGitRepo helper ────────────────────────────────────────────────

test('isInGitRepo: true if any ancestor cache shows a .git child', async () => {
    await freshScope();
    installFs({
        '/repo': [e('.git', '/repo', true), e('src', '/repo', true)],
        '/repo/src': [e('a.py', '/repo/src')],
    });
    const { container } = mount({ rootPath: '/repo' });
    await flush();
    fireMouse(container.querySelector('div[draggable="true"]'), 'click');
    for (let i = 0; i < 5; i++) await flush();
    // After expanding /repo, .git is in the listing — descendants are in repo.
    assert.equal(isInGitRepo('/repo/src/a.py'), true);
});

test('isInGitRepo: false when no .git anywhere up the tree', async () => {
    await freshScope();
    installFs({ '/no-git': [e('a.py', '/no-git')] });
    const { container } = mount({ rootPath: '/no-git' });
    await flush();
    fireMouse(container.querySelector('div[draggable="true"]'), 'click');
    for (let i = 0; i < 5; i++) await flush();
    assert.equal(isInGitRepo('/no-git/a.py'), false);
});


// ─── refreshTree ───────────────────────────────────────────────────────

test('refreshTree refetches only the parent dirs of changed paths that are open', async () => {
    await freshScope();
    let nFetches = 0;
    globalThis.fetch = async (url) => {
        nFetches++;
        const m = url.match(/api\/files\?path=([^&]*)/);
        const path = decodeURIComponent(m[1]);
        if (path === '/r') return mockResponse({ entries: [e('a.txt', '/r')] });
        return mockResponse({ entries: [] });
    };
    const { container } = mount({ rootPath: '/r' });
    await flush();
    fireMouse(container.querySelector('div[draggable="true"]'), 'click');
    for (let i = 0; i < 5; i++) await flush();
    const before = nFetches;
    await refreshTree(['/r/a.txt', '/closed/x.py']);
    for (let i = 0; i < 5; i++) await flush();
    // Only /r is open → exactly one new fetch fires.
    assert.equal(nFetches - before, 1);
});


// ─── context menu ──────────────────────────────────────────────────────

test('right-click on a file opens a menu with Git blame / Download / Rename', async () => {
    await freshScope();
    installFs({ '/r': [e('a.py', '/r')] });
    const { container, tree } = mount({ rootPath: '/r' });
    await flush();
    fireMouse(container.querySelector('div[draggable="true"]'), 'click');
    for (let i = 0; i < 5; i++) await flush();
    const fileNode = [...container.querySelectorAll('div[draggable="true"]')]
        .find(n => n.textContent.includes('a.py'));
    fireMouse(fileNode, 'contextmenu', { clientX: 30, clientY: 40 });
    await flush();
    const labels = tree.state.menu.items.map(i => i.label);
    assert.ok(labels.includes('Rename'));
    assert.ok(labels.includes('Delete'));
    assert.ok(labels.includes('Git blame'));
    assert.ok(labels.includes('Download'));
});

test('right-click on the root shows New file/folder + Change Root…', async () => {
    await freshScope();
    installFs({ '/r': [] });
    const { container, tree } = mount({ rootPath: '/r' });
    await flush();
    const rootNode = container.querySelector('div[draggable="true"]');
    fireMouse(rootNode, 'contextmenu', { clientX: 1, clientY: 1 });
    await flush();
    const labels = tree.state.menu.items.map(i => i.label);
    assert.ok(labels.includes('New file'));
    assert.ok(labels.includes('New folder'));
    assert.ok(labels.includes('Change Root…'));
    // Root has no Rename / Delete.
    assert.ok(!labels.includes('Rename'));
    assert.ok(!labels.includes('Delete'));
});

test('right-click without onChangeRoot does not include the Change Root… item', async () => {
    await freshScope();
    installFs({ '/r': [] });
    const { container, tree } = mount({ rootPath: '/r', onChangeRoot: undefined });
    await flush();
    fireMouse(container.querySelector('div[draggable="true"]'), 'contextmenu', { clientX: 1, clientY: 1 });
    await flush();
    const labels = tree.state.menu.items.map(i => i.label);
    assert.ok(!labels.includes('Change Root…'));
});
