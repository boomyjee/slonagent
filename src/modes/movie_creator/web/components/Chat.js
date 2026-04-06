import { html, Component } from '../lib.js';
import { app } from '../app.js';
import { ApproveDialog } from './ApproveDialog.js';

export class Chat extends Component {
    constructor(props) {
        super(props);
        this.state = { messages: [], input: '', collapsed: {} };
        this._streams = {};
    }

    handleMessage(msg) {
        if (msg.type === 'message') {
            this.setState(({ messages }) => {
                if (msg.stream_id != null && this._streams[msg.stream_id] != null) {
                    const idx = this._streams[msg.stream_id];
                    const next = [...messages];
                    next[idx] = { ...next[idx], text: msg.text, final: msg.final };
                    return { messages: next };
                }
                const next = [...messages, { kind: 'msg', role: msg.role, text: msg.text, stream_id: msg.stream_id, final: msg.final }];
                if (msg.stream_id != null) this._streams[msg.stream_id] = next.length - 1;
                return { messages: next };
            });
        } else if (msg.type === 'tool_call') {
            this.setState(({ messages }) => ({ messages: [...messages, { kind: 'tool', name: msg.name }] }));
        } else if (msg.type === 'processing') {
            this.setState(({ messages }) => ({ messages: [...messages, { kind: 'processing' }] }));
        } else if (msg.type === 'processing_done') {
            this.setState(({ messages }) => ({ messages: messages.filter(m => m.kind !== 'processing') }));
        } else if (msg.type === 'approval_request') {
            const m = { kind: 'approval', approvalKind: msg.kind, data: msg.data, resolved: false };
            this.setState(({ messages }) => ({ messages: [...messages, m] }));
            ApproveDialog.open(m);
        }
    }

    componentDidUpdate() {
        if (this._scroll) this._scroll.scrollTop = this._scroll.scrollHeight;
    }

    submit() {
        const text = this.state.input.trim();
        if (!text) return;
        app.send({ type: 'chat', text });
        this.setState({ input: '' });
    }

    render() {
        const { messages, input, collapsed } = this.state;

        return html`
            <div class="chat">
                <div class="chat-header">AI Assistant</div>
                <div class="chat-messages" ref=${el => this._scroll = el}>
                    ${messages.map((m, i) => {
                        if (m.kind === 'msg') {
                            const isThinking = m.role === 'thinking';
                            const isCollapsed = isThinking && (collapsed[i] ?? m.final);
                            return html`
                                <div
                                    class=${'msg ' + m.role + (isCollapsed ? ' collapsed' : '')}
                                    onClick=${isThinking ? () => this.setState(({ collapsed: c }) => ({ collapsed: { ...c, [i]: !isCollapsed } })) : null}
                                >${m.text}</div>
                            `;
                        }
                        if (m.kind === 'tool') return html`<div class="msg tool_call">\u2699 ${m.name}</div>`;
                        if (m.kind === 'processing') return html`<div class="processing">AI is thinking...</div>`;
                        if (m.kind === 'approval') {
                            return html`
                                <div
                                    class=${'msg approval' + (m.resolved ? ' resolved' : '')}
                                    onClick=${() => { if (!m.resolved) ApproveDialog.open(m); }}
                                >\u270f ${m.approvalKind}</div>
                            `;
                        }
                        return null;
                    })}
                </div>
                <div class="chat-input">
                    <textarea
                        value=${input}
                        onInput=${e => this.setState({ input: e.target.value })}
                        onKeyDown=${e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.submit(); } }}
                        placeholder="Ask AI..."
                    ></textarea>
                    <button onClick=${() => this.submit()}>\u25B6</button>
                </div>
            </div>
        `;
    }
}
