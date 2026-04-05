import { html, useState, useEffect, useRef } from '../lib.js';

export function Chat({ messages, onSend, onApprovalClick, rootRef }) {
    const [input, setInput] = useState('');
    const [collapsed, setCollapsed] = useState({});
    const scrollRef = useRef(null);

    useEffect(() => {
        if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }, [messages]);

    function submit() {
        const text = input.trim();
        if (!text) return;
        onSend(text);
        setInput('');
    }

    return html`
        <div class="chat" ref=${rootRef}>
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
                        const label = m.approvalKind === 'scene' ? (m.data.title || 'Scene')
                            : m.approvalKind === 'character' ? (m.data.name || 'Character')
                            : m.approvalKind === 'portrait' ? `Portrait: ${m.data.character_name || ''}`
                            : m.approvalKind;
                        return html`
                            <div
                                class=${'msg approval' + (m.resolved ? ' resolved' : '')}
                                onClick=${() => onApprovalClick(m)}
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
