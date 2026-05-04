// Component tests for Chat — the tab/router around ChatDialog. Run with:
//   node --test tests/ui/test_chat.mjs
//
// We mount Chat with a fake `app` (just a `send` spy) and check tab
// state, event routing into the right ChatDialog by thread_id, and
// the tab-level events Chat handles directly (thread_rename, processing).

import { mockResponse } from './_dom.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { render, html } = await import('../../src/transport/web/ui/lib.js');
const { Chat } = await import('../../src/transport/web/ui/components/common/Chat.js');


function mount(props = {}) {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const sent = [];
    const app = { send: msg => sent.push(msg) };
    let chat;
    render(
        html`<${Chat} ref=${c => { chat = c; }} app=${app} connected=${true} ...${props} />`,
        container,
    );
    return { chat, container, sent, app };
}

async function flush() {
    await Promise.resolve();
    await Promise.resolve();
}

const ev = (method, extra = {}) => ({ type: 'transport', method, ...extra });

// Each test starts with a clean localStorage so persisted tabs don't leak.
function resetStorage() {
    try { localStorage.clear(); } catch {}
}


test('initial mount shows a single empty tab', async () => {
    resetStorage();
    const { chat } = mount();
    await flush();
    assert.equal(chat.state.tabs.length, 1);
    assert.equal(chat.state.tabs[0], '');
    assert.equal(chat.state.activeTab, '');
});

test('fetches /api/thread_list on mount, populates threads registry', async () => {
    resetStorage();
    const calls = [];
    const list = { 'abc': { name: 'Alpha' }, 'def': { name: 'Bravo' } };
    globalThis.fetch = async (url) => { calls.push(url); return mockResponse(list); };
    try {
        const { chat } = mount();
        for (let i = 0; i < 5; i++) await flush();
        assert.ok(calls.some(u => /api\/thread_list/.test(u)));
        assert.equal(chat.state.threads['abc'], 'Alpha');
        assert.equal(chat.state.threads['def'], 'Bravo');
    } finally {
        globalThis.fetch = async () => mockResponse({ events: [] });
    }
});

test('thread_list fetch fills server-side names into threads registry', async () => {
    resetStorage();
    // Fake a previously persisted open-tab list (id-only).
    localStorage.setItem(
        Object.keys(localStorage).find(k => k.endsWith(':chat:tabs')) || 'slon::chat:tabs',
        JSON.stringify({ tabs: ['abc'], active: 'abc' }),
    );
    globalThis.fetch = async () => mockResponse({ 'abc': { name: 'Renamed' } });
    try {
        const { chat } = mount();
        for (let i = 0; i < 5; i++) await flush();
        assert.equal(chat.state.threads['abc'], 'Renamed');
        assert.deepEqual(chat.state.tabs, ['abc']);
    } finally {
        globalThis.fetch = async () => mockResponse({ events: [] });
    }
});

test('events arriving before thread_list ready are buffered, applied after', async () => {
    resetStorage();
    let resolve;
    globalThis.fetch = () => new Promise(r => { resolve = () => r(mockResponse({})); });
    try {
        const { chat } = mount();
        // Fetch is pending → state.ready is still false; events go to buffer.
        chat.handleMessage(ev('thread_rename', { uuid: 'abc', name: 'Live' }));
        assert.equal(chat.state.ready, false);
        assert.equal(chat.state.threads['abc'], undefined);
        // Resolve fetch — ready flips, componentDidUpdate drains the buffer.
        resolve();
        for (let i = 0; i < 5; i++) await flush();
        assert.equal(chat.state.ready, true);
        assert.equal(chat.state.threads['abc'], 'Live');
    } finally {
        globalThis.fetch = async () => mockResponse({ events: [] });
    }
});

test('rename arriving during thread_list fetch wins over server snapshot', async () => {
    resetStorage();
    let resolve;
    globalThis.fetch = () => new Promise(r => {
        resolve = () => r(mockResponse({ 'X': { name: 'Server' } }));
    });
    try {
        const { chat } = mount();
        // Live rename для того же uuid — приходит после серверного снимка.
        chat.handleMessage(ev('thread_rename', { uuid: 'X', name: 'Live' }));
        resolve();
        for (let i = 0; i < 5; i++) await flush();
        // Snapshot применился первым (Server), потом drain накатил Live поверх.
        assert.equal(chat.state.threads['X'], 'Live');
    } finally {
        globalThis.fetch = async () => mockResponse({ events: [] });
    }
});

test('tab tooltip shows the thread id; empty id reads as (primary)', async () => {
    resetStorage();
    const { chat, container } = mount();
    await flush();
    chat.setState({ tabs: ['', 'ab12cd34'], threads: { 'ab12cd34': 'X' }, activeTab: '' });
    await flush();
    const [main, named] = container.querySelectorAll('[data-tab-id]');
    assert.equal(main.getAttribute('title'), '(primary)');
    assert.equal(named.getAttribute('title'), 'ab12cd34');
});

test('handleMessage routes per-thread events to the matching ChatDialog', async () => {
    resetStorage();
    const { chat } = mount();
    await flush();
    chat.handleMessage(ev('send_message', {
        thread_id: '', text: 'hello', stream_id: 1, final: true,
    }));
    await flush();
    const dialog = chat._dialogs[''];
    assert.ok(dialog);
    assert.equal(dialog.state.messages.length, 1);
    assert.equal(dialog.state.messages[0].text, 'hello');
});

test('events for a thread without an open tab are dropped (history covers reopen)', async () => {
    resetStorage();
    const { chat } = mount();
    await flush();
    // No tab exists for thread "ghost" — Chat must not throw and must
    // not implicitly create a tab for it.
    chat.handleMessage(ev('inject_message', { thread_id: 'ghost', text: 'hi' }));
    await flush();
    assert.equal(chat.state.tabs.length, 1);
    assert.ok(!chat._dialogs['ghost']);
});

test('send_processing flips the per-thread spinner flag', async () => {
    resetStorage();
    const { chat, container } = mount();
    await flush();
    chat.handleMessage(ev('send_processing', { thread_id: '', active: true }));
    await flush();
    assert.equal(chat.state.processingByThread[''], true);

    chat.handleMessage(ev('send_processing', { thread_id: '', active: false }));
    await flush();
    assert.equal(chat.state.processingByThread[''], false);
});

test('thread_rename updates the threads registry (tabs untouched)', async () => {
    resetStorage();
    const { chat } = mount();
    await flush();
    chat.setState({ tabs: ['abc'], activeTab: 'abc' });
    await flush();
    chat.handleMessage(ev('thread_rename', { uuid: 'abc', name: 'My Thread' }));
    await flush();
    assert.deepEqual(chat.state.tabs, ['abc']);  // tabs хранят только id
    assert.equal(chat.state.threads['abc'], 'My Thread');
});

test('opening a new tab creates a stable id and persists only ids', async () => {
    resetStorage();
    const { chat } = mount();
    await flush();
    chat._onAddTab();
    await flush();
    assert.equal(chat.state.tabs.length, 2);
    const newId = chat.state.tabs[1];
    assert.ok(newId && newId.length > 0);
    assert.equal(chat.state.activeTab, newId);
    const persisted = JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k => k.endsWith(':chat:tabs'))));
    assert.deepEqual(persisted.tabs, ['', newId]);
    assert.equal(persisted.active, newId);
});

test('closing a tab removes its dialog and any processing flag', async () => {
    resetStorage();
    const { chat } = mount();
    await flush();
    chat._onAddTab();
    await flush();
    const tid = chat.state.tabs[1];
    chat.handleMessage(ev('send_processing', { thread_id: tid, active: true }));
    await flush();
    assert.equal(chat.state.processingByThread[tid], true);

    chat._onCloseTab(tid);
    await flush();
    assert.equal(chat.state.tabs.includes(tid), false);
    assert.equal(chat.state.processingByThread[tid], undefined);
    assert.equal(chat._dialogs[tid], undefined);
});

test('onSend forwards parts to app.send with the right thread_id', async () => {
    resetStorage();
    const { chat, sent } = mount();
    await flush();
    const send = chat._onSend('');
    send([{ type: 'text', text: 'hi' }]);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].method, 'process_message');
    assert.equal(sent[0].thread_id, '');
    assert.deepEqual(sent[0].content_parts, [{ type: 'text', text: 'hi' }]);
});

test('two threads keep messages independent', async () => {
    resetStorage();
    const { chat } = mount();
    await flush();
    chat._onAddTab();
    await flush();
    const t2 = chat.state.tabs[1];

    chat.handleMessage(ev('send_message', { thread_id: '', text: 'A', stream_id: 1, final: true }));
    chat.handleMessage(ev('send_message', { thread_id: t2, text: 'B', stream_id: 2, final: true }));
    await flush();

    assert.equal(chat._dialogs[''].state.messages[0].text, 'A');
    assert.equal(chat._dialogs[t2].state.messages[0].text, 'B');
});

test('threadsEnabled=false hides tab-management UI', async () => {
    resetStorage();
    const { container } = mount({ threadsEnabled: false });
    await flush();
    // No "+" / history buttons rendered when threading is off.
    const headerBtns = [...container.querySelectorAll('button')].filter(b =>
        b.getAttribute('title') === 'Новый тред' ||
        b.getAttribute('title') === 'История тредов');
    assert.equal(headerBtns.length, 0);
});
