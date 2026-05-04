// Component tests for ChatDialog. Run with:
//   node --test tests/ui/test_chat_dialog.mjs
//
// Mount via the same render() lib the browser uses, drive the dialog
// through its public surface (handleEvent / _send), and assert on both
// component state and the rendered DOM.

import { mockResponse, fireMouse } from './_dom.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const { render, html } = await import('../../src/transport/web/ui/lib.js');
const { ChatDialog } = await import('../../src/transport/web/ui/components/common/ChatDialog.js');


// ─── helpers ───────────────────────────────────────────────────────────

function mount(props = {}) {
    const container = document.createElement('div');
    document.body.appendChild(container);
    let dialog;
    render(
        html`<${ChatDialog} ref=${c => { dialog = c; }} threadId="t1" ...${props} />`,
        container,
    );
    return { dialog, container };
}

// Preact debounces setState through Promise.resolve().then. Two await
// boundaries cover the most common case (state update → re-render).
async function flush() {
    await Promise.resolve();
    await Promise.resolve();
}

const ev = (method, extra = {}) => ({ method, ...extra });

async function fire(dialog, ...events) {
    for (const e of events) dialog.handleEvent(e);
    await flush();
}


// ─── basic ─────────────────────────────────────────────────────────────

test('mounts cleanly with empty state', async () => {
    const { dialog, container } = mount();
    await flush();
    assert.deepEqual(dialog.state.messages, []);
    assert.deepEqual(dialog.state.streams, {});
    // Renders a <textarea> for input regardless of message state.
    assert.ok(container.querySelector('textarea'));
});

test('unknown method does not change state', async () => {
    const { dialog } = mount();
    await fire(dialog, ev('whatever'));
    assert.deepEqual(dialog.state.messages, []);
});

test('reducer is pure: input state object is not mutated', () => {
    const { dialog } = mount();
    const before = { messages: [], streams: {} };
    dialog._reduce(before, ev('inject_message', { text: 'hi' }));
    assert.deepEqual(before, { messages: [], streams: {} });
});


// ─── streaming ─────────────────────────────────────────────────────────

test('send_message without stream_id appends fresh messages', async () => {
    const { dialog, container } = mount();
    await fire(dialog,
        ev('send_message', { text: 'first',  final: true }),
        ev('send_message', { text: 'second', final: true }),
    );
    assert.equal(dialog.state.messages.length, 2);
    assert.match(container.textContent, /first/);
    assert.match(container.textContent, /second/);
});

test('send_message with stream_id grows the same DOM node across chunks', async () => {
    const { dialog, container } = mount();
    await fire(dialog, ev('send_message', { text: 'Hel',         stream_id: 1, final: false }));
    await fire(dialog, ev('send_message', { text: 'Hello',       stream_id: 1, final: false }));
    await fire(dialog, ev('send_message', { text: 'Hello world', stream_id: 1, final: true  }));

    assert.equal(dialog.state.messages.length, 1);
    assert.equal(dialog.state.messages[0].text, 'Hello world');
    assert.equal(dialog.state.messages[0].final, true);
    // DOM reflects the final accumulated text, not a per-chunk concatenation.
    const assistantNodes = container.querySelectorAll('.assistant');
    assert.equal(assistantNodes.length, 1);
    assert.match(assistantNodes[0].textContent, /Hello world/);
});

test('two parallel streams render as two separate messages', async () => {
    const { dialog, container } = mount();
    await fire(dialog,
        ev('send_message', { text: 'A1',   stream_id: 1 }),
        ev('send_message', { text: 'B1',   stream_id: 2 }),
        ev('send_message', { text: 'A1A2', stream_id: 1, final: true }),
        ev('send_message', { text: 'B1B2', stream_id: 2, final: true }),
    );
    assert.equal(dialog.state.messages.length, 2);
    const texts = [...container.querySelectorAll('.assistant')].map(n => n.textContent);
    assert.deepEqual(texts, ['A1A2', 'B1B2']);
});

test('thinking and memory render as distinct bubbles', async () => {
    const { dialog, container } = mount();
    await fire(dialog,
        ev('send_thinking',    { text: 'thought-2', stream_id: 1, final: true }),
        ev('send_memory_info', { text: 'memo-2' }),
    );
    assert.equal(dialog.state.messages.length, 2);
    const thinkingNodes = [...container.querySelectorAll('.thinking')];
    const memoryNode    = thinkingNodes.find(n => n.className.includes('memory'));
    const thinkingOnly  = thinkingNodes.find(n => !n.className.includes('memory'));
    assert.match(thinkingOnly.textContent, /thought-2/);
    assert.match(memoryNode.textContent, /memo-2/);
});

test('send_memory_info entries are marked final so they can collapse', async () => {
    const { dialog, container } = mount();
    await fire(dialog, ev('send_memory_info', { text: 'observation' }));
    // The reducer must stamp final=true even though the event omits it —
    // render uses m.final to gate the .collapsed CSS class.
    assert.equal(dialog.state.messages[0].final, true);
    const node = container.querySelector('.memory');
    assert.ok(node);
    assert.ok(node.className.includes('collapsed'),
        `expected memory bubble to be collapsed, got class=${node.className}`);
});

test('clicking a collapsed memory bubble expands it (and back)', async () => {
    const { dialog, container } = mount();
    await fire(dialog, ev('send_memory_info', { text: 'observation' }));
    const node = container.querySelector('.memory');
    assert.ok(node.className.includes('collapsed'));
    fireMouse(node, 'click');
    await flush();
    const after = container.querySelector('.memory');
    assert.ok(!after.className.includes('collapsed'));
    fireMouse(after, 'click');
    await flush();
    const reCollapsed = container.querySelector('.memory');
    assert.ok(reCollapsed.className.includes('collapsed'));
});

test('streams unaffected by interleaved non-stream events', async () => {
    // If the stream index is recomputed by raw array length each tick,
    // a tool_call between chunks would shift the stream's slot.
    const { dialog } = mount();
    await fire(dialog,
        ev('send_message', { text: 'a',  stream_id: 1 }),
        ev('on_tool_call', { name: 'foo', args: {} }),
        ev('send_message', { text: 'ab', stream_id: 1, final: true }),
    );
    assert.equal(dialog.state.messages.length, 2);
    const stream = dialog.state.messages.find(m => m.kind === 'msg');
    assert.equal(stream.text, 'ab');
    assert.equal(stream.final, true);
});


// ─── tool call/result ──────────────────────────────────────────────────

test('on_tool_result attaches to the latest unresolved tool call by name', async () => {
    const { dialog } = mount();
    await fire(dialog,
        ev('on_tool_call',   { name: 'read_file', args: { path: '/a' } }),
        ev('on_tool_call',   { name: 'read_file', args: { path: '/b' } }),
        ev('on_tool_result', { name: 'read_file', result: 'contents-of-b' }),
    );
    assert.equal(dialog.state.messages[0].result, null);
    assert.equal(dialog.state.messages[1].result, 'contents-of-b');
});

test('on_tool_result with nothing pending is a no-op', async () => {
    const { dialog } = mount();
    await fire(dialog, ev('on_tool_call', { name: 'foo', args: {} }));
    await fire(dialog, ev('on_tool_result', { name: 'foo', result: 'ok' }));
    const before = dialog.state.messages;
    await fire(dialog, ev('on_tool_result', { name: 'foo', result: 'late' }));
    assert.equal(dialog.state.messages, before);
});


// ─── system prompt → tool-style preview ────────────────────────────────

test('send_system_prompt becomes a tool entry with first-line preview', async () => {
    const { dialog } = mount();
    await fire(dialog, ev('send_system_prompt', {
        text: 'You are a helpful assistant.\n\nMore details follow.',
    }));
    const m = dialog.state.messages[0];
    assert.equal(m.kind, 'tool');
    assert.equal(m.name, 'You are a helpful assistant.');
    assert.equal(m.args, null);
    assert.match(m.result, /^You are a helpful/);
});

test('send_system_prompt truncates preview to 80 chars', async () => {
    const { dialog } = mount();
    await fire(dialog, ev('send_system_prompt', { text: 'x'.repeat(200) }));
    assert.equal(dialog.state.messages[0].name.length, 80);
});


// ─── inject / media / suggestions ──────────────────────────────────────

test('inject_message renders as a styled message', async () => {
    const { dialog, container } = mount();
    await fire(dialog, ev('inject_message', { text: 'pinged you' }));
    assert.equal(dialog.state.messages[0].role, 'inject');
    assert.match(container.textContent, /pinged you/);
});

test('send_images renders an album with one <img> per item', async () => {
    const items = [{ url: 'a.jpg', name: 'a' }, { url: 'b.jpg', name: 'b' }];
    const { dialog, container } = mount();
    await fire(dialog, ev('send_images', { items }));
    const imgs = container.querySelectorAll('img');
    assert.equal(imgs.length, 2);
    assert.equal(imgs[0].getAttribute('src'), 'a.jpg');
});

test('send_voice renders an audio player', async () => {
    const { dialog, container } = mount();
    await fire(dialog, ev('send_voice', { item: { url: 'voice.wav' } }));
    const audio = container.querySelector('audio');
    assert.ok(audio);
    assert.equal(audio.getAttribute('src'), 'voice.wav');
});

test('send_files renders a download chip', async () => {
    const { dialog, container } = mount();
    await fire(dialog, ev('send_files', {
        items: [{ url: 'doc.pdf', name: 'doc.pdf', size: 100 }],
    }));
    const link = container.querySelector('a[download]');
    assert.ok(link);
    assert.equal(link.getAttribute('href'), 'doc.pdf');
});

test('send_suggestions renders a button per option that calls onSend on click', async () => {
    const sent = [];
    const { dialog, container } = mount({ onSend: parts => sent.push(parts) });
    await fire(dialog, ev('send_suggestions', { text: 'pick one', options: ['a', 'b'] }));
    const buttons = [...container.querySelectorAll('button')]
        .filter(b => b.textContent === 'a' || b.textContent === 'b');
    assert.equal(buttons.length, 2);
    buttons[0].click();
    await flush();
    assert.deepEqual(sent[0], [{ type: 'text', text: 'a' }]);
});


// ─── _send + process_message echo ──────────────────────────────────────

test('_send forwards parts via onSend and adds a pending placeholder', async () => {
    const sent = [];
    const { dialog, container } = mount({ onSend: parts => sent.push(parts) });
    dialog._send([{ type: 'text', text: 'hi' }], [{ kind: 'text', text: 'hi' }]);
    await flush();
    assert.deepEqual(sent, [[{ type: 'text', text: 'hi' }]]);
    assert.equal(dialog.state.messages[0].pending, true);
    // The "pending" CSS class is what dims the bubble in the UI.
    const userBubble = container.querySelector('.user');
    assert.ok(userBubble && userBubble.className.includes('pending'));
});

test('process_message echo replaces the pending placeholder', async () => {
    const { dialog, container } = mount({ onSend: () => {} });
    dialog._send([{ type: 'text', text: 'hi' }], [{ kind: 'text', text: 'hi' }]);
    await flush();
    await fire(dialog, ev('process_message', {
        content_parts: [{ type: 'text', text: 'hi' }],
    }));
    assert.equal(dialog.state.messages.length, 1);
    assert.equal(dialog.state.messages[0].pending, undefined);
    const userBubble = container.querySelector('.user');
    assert.ok(userBubble && !userBubble.className.includes('pending'));
});

test('process_message decodes image_url parts into <img>', async () => {
    const { dialog, container } = mount({ onSend: () => {} });
    dialog._send([], [{ kind: 'image', url: 'data:img' }]);
    await flush();
    await fire(dialog, ev('process_message', {
        content_parts: [{ type: 'image_url', image_url: { url: 'http://srv/img.jpg' } }],
    }));
    const userImg = container.querySelector('.user img');
    assert.ok(userImg);
    assert.equal(userImg.getAttribute('src'), 'http://srv/img.jpg');
});

test('process_message without prior pending appends a fresh user message', async () => {
    const { dialog } = mount();
    await fire(dialog, ev('process_message', {
        content_parts: [{ type: 'text', text: 'echo' }],
    }));
    assert.equal(dialog.state.messages.length, 1);
    assert.equal(dialog.state.messages[0].pending, undefined);
});

test('process_message with empty parts is a no-op', async () => {
    const { dialog } = mount({ onSend: () => {} });
    dialog._send([], [{ kind: 'text', text: 'hi' }]);
    await flush();
    const before = dialog.state.messages;
    await fire(dialog, ev('process_message', { content_parts: [] }));
    assert.equal(dialog.state.messages, before);
});


// ─── history bootstrap on mount ────────────────────────────────────────

test('events arriving before history ready are buffered and applied after', async () => {
    let resolve;
    globalThis.fetch = () => new Promise(r => { resolve = () => r(mockResponse({ events: [] })); });
    try {
        const { dialog } = mount();
        dialog.handleEvent(ev('inject_message', { text: 'live', id: 50 }));
        assert.equal(dialog.state.ready, false);
        assert.equal(dialog.state.messages.length, 0);   // буфер, не state
        resolve();
        for (let i = 0; i < 5; i++) await flush();
        assert.equal(dialog.state.ready, true);
        assert.equal(dialog.state.messages.length, 1);
        assert.equal(dialog.state.messages[0].text, 'live');
    } finally {
        globalThis.fetch = async () => mockResponse({ events: [] });
    }
});

test('events with id <= lastSeenId after history are deduplicated', async () => {
    globalThis.fetch = async () => mockResponse({ events: [
        { method: 'inject_message', text: 'hist', id: 100 },
    ]});
    try {
        const { dialog } = mount();
        for (let i = 0; i < 5; i++) await flush();
        assert.equal(dialog._lastSeenId, 100);

        // Тот же id — игнор.
        dialog.handleEvent(ev('inject_message', { text: 'dup', id: 100 }));
        await flush();
        assert.equal(dialog.state.messages.length, 1);

        // Старый id (был в реплее, но история его обогнала) — игнор.
        dialog.handleEvent(ev('inject_message', { text: 'old', id: 50 }));
        await flush();
        assert.equal(dialog.state.messages.length, 1);

        // Новее последнего — применяется.
        dialog.handleEvent(ev('inject_message', { text: 'new', id: 101 }));
        await flush();
        assert.equal(dialog.state.messages.length, 2);
        assert.equal(dialog.state.messages[1].text, 'new');
    } finally {
        globalThis.fetch = async () => mockResponse({ events: [] });
    }
});

test('on F5 race: stale replay older than history boundary is filtered', async () => {
    let resolve;
    globalThis.fetch = () => new Promise(r => {
        resolve = () => r(mockResponse({ events: [
            { method: 'inject_message', text: 'hist', id: 100 },
        ]}));
    });
    try {
        const { dialog } = mount();
        // Реплей шлёт перекрывающие историю события — old и fresh:
        dialog.handleEvent(ev('inject_message', { text: 'stale', id: 99 }));
        dialog.handleEvent(ev('inject_message', { text: 'fresh', id: 101 }));
        resolve();
        for (let i = 0; i < 5; i++) await flush();
        // history → lastSeenId=100. drain: stale(99) отфильтрован, fresh(101) ок.
        assert.equal(dialog.state.messages.length, 2);
        assert.equal(dialog.state.messages[0].text, 'hist');
        assert.equal(dialog.state.messages[1].text, 'fresh');
    } finally {
        globalThis.fetch = async () => mockResponse({ events: [] });
    }
});

test('componentDidMount fetches /api/history and replays events into state', async () => {
    const calls = [];
    const events = [
        { method: 'inject_message', text: 'historical 1' },
        { method: 'send_message',   text: 'historical reply', final: true },
    ];
    globalThis.fetch = async (url) => {
        calls.push(url);
        return mockResponse({ events });
    };
    try {
        const { dialog, container } = mount({ threadId: 'abc' });
        // _fetchHistory is async — wait for the promise chain to resolve.
        for (let i = 0; i < 10; i++) await flush();
        assert.equal(calls.length, 1);
        assert.match(calls[0], /api\/history\?thread_id=abc/);
        assert.equal(dialog.state.messages.length, 2);
        assert.match(container.textContent, /historical 1/);
        assert.match(container.textContent, /historical reply/);
    } finally {
        globalThis.fetch = async () => mockResponse({ events: [] });
    }
});


// ─── realistic scenario ────────────────────────────────────────────────

test('full conversation: user → thinking → tool → assistant stream', async () => {
    const { dialog, container } = mount({ onSend: () => {} });
    dialog._send(
        [{ type: 'text', text: 'List files' }],
        [{ kind: 'text', text: 'List files' }],
    );
    await flush();
    await fire(dialog,
        ev('process_message',  { content_parts: [{ type: 'text', text: 'List files' }] }),
        ev('send_thinking',    { text: 'thinking...', stream_id: 10, final: true }),
        ev('on_tool_call',     { name: 'ls', args: { path: '/' } }),
        ev('on_tool_result',   { name: 'ls', result: ['a', 'b'] }),
        ev('send_message',     { text: 'Found ',        stream_id: 11, final: false }),
        ev('send_message',     { text: 'Found 2 files.', stream_id: 11, final: true  }),
    );

    assert.equal(dialog.state.messages.length, 4);
    assert.equal(dialog.state.messages[0].role, 'user');
    assert.equal(dialog.state.messages[0].pending, undefined);
    assert.equal(dialog.state.messages[1].kind, 'thinking');
    assert.equal(dialog.state.messages[2].kind, 'tool');
    assert.equal(dialog.state.messages[3].text, 'Found 2 files.');
    assert.match(container.textContent, /List files/);
    assert.match(container.textContent, /Found 2 files/);
});
