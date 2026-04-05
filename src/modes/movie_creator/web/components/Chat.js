import { html, useState, useEffect, useRef, send } from '../lib.js';
import { app } from '../app.js';
import { Dialog } from '../common/Dialog.js';
import { ApprovalView } from './ApprovalView.js';

function markResolved(idx) {
    if (idx == null) return;
    app.setState(({ messages }) => ({
        messages: messages.map((m, i) => i === idx ? { ...m, resolved: true } : m),
    }));
}

function openApproval(m) {
    if (m.resolved) return;
    const idx = m.idx;
    Dialog.open(html`<${ApprovalView}
        kind=${m.approvalKind}
        data=${m.data.fields || m.data}
        onApprove=${data => { send({ type: 'approval_response', action: 'approve', data }); markResolved(idx); }}
        onReject=${() => { send({ type: 'approval_response', action: 'reject', reason: prompt('Reason (optional):') || '' }); markResolved(idx); }}
    />`);
}

export function Chat() {
    const { messages } = app.state;
    const [input, setInput] = useState('');
    const [collapsed, setCollapsed] = useState({});
    const scrollRef = useRef(null);

    useEffect(() => {
        if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }, [messages]);

    function submit() {
        const text = input.trim();
        if (!text) return;
        send({ type: 'chat', text });
        setInput('');
    }

    return html`
        <div class="chat">
            <div class="chat-header">AI Assistant</div>
            <div class="chat-messages" ref=${scrollRef}>
                ${messages.map((m, i) => {
                    if (m.kind === 'msg') {
                        const isThinking = m.role === 'thinking';
                        const isCollapsed = isThinking && (collapsed[i] ?? m.final);
                        return html`
                            <div
                                class=${'msg ' + m.role + (isCollapsed ? ' collapsed' : '')}
                                onClick=${isThinking ? () => setCollapsed(c => ({ ...c, [i]: !isCollapsed })) : null}
                            >${m.text}</div>
                        `;
                    }
                    if (m.kind === 'tool') return html`<div class="msg tool_call">\u2699 ${m.name}</div>`;
                    if (m.kind === 'processing') return html`<div class="processing">AI is thinking...</div>`;
                    if (m.kind === 'approval') {
                        const fields = m.data.fields || m.data;
                        const label = m.approvalKind === 'scene' ? (fields.title || 'Scene')
                            : m.approvalKind === 'character' ? (fields.name || 'Character')
                            : m.approvalKind === 'shot' ? ((fields.description || '').slice(0, 40) || 'Shot')
                            : m.approvalKind === 'portrait' ? `Portrait: ${m.data.character_name || ''}`
                            : m.approvalKind === 'shots_bulk' ? `Storyboard (${(m.data.text || '').split('---').filter(s => s.trim()).length} shots)`
                            : m.approvalKind;
                        return html`
                            <div
                                class=${'msg approval' + (m.resolved ? ' resolved' : '')}
                                onClick=${() => openApproval(m)}
                            >\u270f Approve: ${label}</div>
                        `;
                    }
                    return null;
                })}
            </div>
            <div class="chat-input">
                <textarea
                    value=${input}
                    onInput=${e => setInput(e.target.value)}
                    onKeyDown=${e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); } }}
                    placeholder="Ask AI..."
                ></textarea>
                <button onClick=${submit}>\u25B6</button>
            </div>
        </div>
    `;
}
