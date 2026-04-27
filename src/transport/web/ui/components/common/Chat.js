import { html, Component, css, keyframes } from '../../lib.js';

const cl = {};

// Minimal markdown → HTML, mirrors src/transport/telegram.py:_markdown_to_html.
// Code spans/blocks are stashed first so their contents don't get re-formatted.
function mdToHtml(text) {
    if (!text) return '';
    const blocks = [];
    text = text.replace(/```[\w]*\n?([\s\S]*?)```/g, (_, c) => `\x00CB${blocks.push(c) - 1}\x00`);
    const inlines = [];
    text = text.replace(/`([^`]+)`/g, (_, c) => `\x00IC${inlines.push(c) - 1}\x00`);
    text = text.replace(/^>\s*(.*)$/gm, '$1');
    text = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    text = text.replace(/^(#{1,6})\s+(.+)$/gm, (_, h, t) => `<h${h.length}>${t}</h${h.length}>`);
    text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    text = text.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
    text = text.replace(/__(.+?)__/g, '<b>$1</b>');
    text = text.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<i>$1</i>');
    text = text.replace(/(?<![a-zA-Z0-9])_([^_\n]+)_(?![a-zA-Z0-9])/g, '<i>$1</i>');
    text = text.replace(/~~(.+?)~~/g, '<s>$1</s>');
    text = text.replace(/^[-*]\s+/gm, '• ');
    // Bare URLs (not already inside an <a> tag)
    text = text.replace(/(?<!href="|">)(https?:\/\/[^\s<)]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    // /commands at the start of a message
    text = text.replace(/^(\/\w+)/gm, '<code class="slash-cmd">$1</code>');
    const esc = c => c.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    inlines.forEach((c, i) => { text = text.replace(`\x00IC${i}\x00`, `<code>${esc(c)}</code>`); });
    blocks.forEach((c, i) => { text = text.replace(`\x00CB${i}\x00`, `<pre><code>${esc(c)}</code></pre>`); });
    return text;
}

export class Chat extends Component {
    constructor(props) {
        super(props);
        this.state = { messages: [], input: '', expanded: {}, processing: false };
        this._streams = {};
        // Sticky-bottom flag. Starts true so initial buffer replay (where
        // scrollTop=0 but scrollHeight is already huge) still snaps down.
        // Flipped off when the user scrolls up, back on when they scroll
        // to within 120px of the bottom.
        this._stick = true;
    }

    _onScroll = () => {
        const el = this._scroll;
        if (!el) return;
        this._stick = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    };

    handleMessage(ev) {
        const m = ev.method;
        if (m === 'send_message') {
            this.setState(({ messages }) => {
                if (ev.stream_id != null && this._streams[ev.stream_id] != null) {
                    const idx = this._streams[ev.stream_id];
                    const next = [...messages];
                    next[idx] = { ...next[idx], text: ev.text, final: ev.final };
                    return { messages: next };
                }
                const next = [...messages, { kind: 'msg', role: 'assistant', text: ev.text, stream_id: ev.stream_id, final: ev.final }];
                if (ev.stream_id != null) this._streams[ev.stream_id] = next.length - 1;
                return { messages: next };
            });
        } else if (m === 'send_thinking') {
            this.setState(({ messages }) => {
                if (ev.stream_id != null && this._streams['t_' + ev.stream_id] != null) {
                    const idx = this._streams['t_' + ev.stream_id];
                    const next = [...messages];
                    next[idx] = { ...next[idx], text: ev.text, final: ev.final };
                    return { messages: next };
                }
                const next = [...messages, { kind: 'thinking', text: ev.text, stream_id: ev.stream_id, final: ev.final }];
                if (ev.stream_id != null) this._streams['t_' + ev.stream_id] = next.length - 1;
                return { messages: next };
            });
        } else if (m === 'on_tool_call') {
            this.setState(({ messages }) => ({
                messages: [...messages, { kind: 'tool', name: ev.name, args: ev.args, result: null }]
            }));
        } else if (m === 'on_tool_result') {
            this.setState(({ messages }) => {
                for (let i = messages.length - 1; i >= 0; i--) {
                    if (messages[i].kind === 'tool' && messages[i].name === ev.name && messages[i].result == null) {
                        const next = [...messages];
                        next[i] = { ...next[i], result: ev.result };
                        return { messages: next };
                    }
                }
                return {};
            });
        } else if (m === 'send_processing') {
            this.setState({ processing: !!ev.active });
        } else if (m === 'send_system_prompt') {
            const preview = ev.text.split('\n')[0].slice(0, 80);
            this.setState(({ messages }) => ({
                messages: [...messages, { kind: 'tool', name: preview, args: null, result: ev.text }]
            }));
        } else if (m === 'inject_message') {
            this.setState(({ messages }) => ({
                messages: [...messages, { kind: 'msg', role: 'inject', text: ev.text }]
            }));
        } else if (m === 'process_message') {
            const text = (ev.content_parts || []).filter(p => p.type === 'text').map(p => p.text).join('\n');
            if (text) {
                this.setState(({ messages }) => ({
                    messages: [...messages, { kind: 'msg', role: 'user', text }]
                }));
            }
        }
    }

    componentDidMount() {
        // Snap down once the initial buffer replay has rendered.
        if (this._scroll) this._scroll.scrollTop = this._scroll.scrollHeight;
    }

    componentDidUpdate() {
        // Honor the sticky flag — proximity is recomputed only on user
        // scroll, not on every render, so in-flight updates that grow
        // scrollHeight past the threshold don't disable autoscroll.
        if (this._stick && this._scroll)
            this._scroll.scrollTop = this._scroll.scrollHeight;
    }

    _submit() {
        const text = this.state.input.trim();
        if (!text) return;
        this.props.app.send({
            type: 'transport', method: 'process_message',
            content_parts: [{ type: 'text', text }],
        });
        // Don't add to local state — the server echoes process_message back
        // through the event buffer, which renders via handleMessage. This
        // way the message survives page reloads (buffer replay).
        this.setState({ input: '' });
    }

    _formatArgs(args) {
        if (!args) return '';
        return Object.entries(args).map(([k, v]) => `${k}: ${v}`).join('\n');
    }

    _formatResult(result) {
        if (result == null) return null;
        if (typeof result === 'object') {
            return Object.entries(result).map(([k, v]) => `[${k}]\n${v}`).join('\n');
        }
        return String(result);
    }

    render({ connected, className }, { messages, input, expanded, processing }) {
        return html`
            <div class="${cl.chat} ${className || ''}">
                <div class=${cl.header}>
                    <span>Chat</span>
                    ${processing && html`<span class=${cl.spinner}></span>`}
                </div>
                <div class=${cl.messages} ref=${el => this._scroll = el} onScroll=${this._onScroll}>
                    ${messages.map((m, i) => {
                        if (m.kind === 'msg') return html`
                            <div class="${cl.msg} ${m.role}"
                                 dangerouslySetInnerHTML=${{__html: mdToHtml(m.text)}}></div>
                        `;
                        if (m.kind === 'thinking') {
                            const isCollapsed = !(expanded[i] ?? false) && m.final;
                            return html`
                                <div
                                    class="${cl.msg} thinking${isCollapsed ? ' collapsed' : ''}"
                                    onClick=${() => this.setState(({ expanded: e }) => ({ expanded: { ...e, [i]: isCollapsed } }))}
                                    dangerouslySetInnerHTML=${{__html: mdToHtml((m.text || '').trimEnd())}}></div>
                            `;
                        }
                        if (m.kind === 'tool') {
                            const open = expanded[i];
                            const argsText = this._formatArgs(m.args);
                            const resultText = this._formatResult(m.result);
                            return html`
                                <div class=${cl.tool}>
                                    <div class="hdr" onClick=${() => this.setState(({ expanded: e }) => ({ expanded: { ...e, [i]: !open } }))}>
                                        <span class="arr">${open ? '\u25BC' : '\u25B6'}</span>
                                        <span>\u2699 ${m.name}</span>
                                    </div>
                                    ${open && argsText && html`<div class="body">${argsText}</div>`}
                                    ${open && resultText && html`<div class="result">${resultText}</div>`}
                                </div>
                            `;
                        }
                    })}
                </div>
                <div class=${cl.input}>
                    <textarea
                        value=${input}
                        onInput=${e => this.setState({ input: e.target.value })}
                        onKeyDown=${e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._submit(); } }}
                        placeholder="Write a message..."
                        disabled=${!connected}
                    ></textarea>
                    <button onClick=${() => this._submit()} disabled=${!connected}>\u25B6</button>
                </div>
            </div>
        `;
    }
}

// --- styles ---

cl.chat = css`
  flex: 1 1 0; min-width: 200px; min-height: 0; display: flex; flex-direction: column;
  border-left: 1px solid var(--border); background: var(--surface);
  &, & * { box-sizing: border-box; }
`;
cl.header = css`
  padding: 10px 16px; border-bottom: 1px solid var(--border); font-size: 12px;
  color: var(--text-dim); text-transform: uppercase;
  display: flex; align-items: center; gap: 10px;
`;
cl.messages = css`flex: 1; min-height: 0; overflow-y: auto; padding: 12px;`;
cl.msg = css`
  margin-bottom: 10px; font-size: 13px; line-height: 1.5; padding: 8px 12px;
  border-radius: 8px; max-width: 90%; white-space: pre-wrap; word-break: break-word;
  &.user, &.inject { background: var(--accent); color: #1e1e2e; margin-left: auto; }
  &.assistant { background: var(--surface2); }
  &.thinking { background: var(--surface2); font-size: 12px; color: var(--text-dim); font-style: italic; }
  &.thinking.collapsed {
    /* line-height becomes the visual height of the box; line 2 starts
       AT the bottom edge so overflow has nothing to clip mid-glyph. */
    line-height: 34px; height: 34px;
    padding-top: 0; padding-bottom: 0;
    overflow: hidden; cursor: pointer; opacity: 0.5;
  }
  &.thinking.collapsed:hover { opacity: 0.7; }
  & h1, & h2, & h3, & h4, & h5, & h6 { font-size: 14px; font-weight: 600; margin: 4px 0; }
  & a { color: inherit; text-decoration: underline; }
  & code { background: rgba(0,0,0,0.25); padding: 1px 4px; border-radius: 3px;
           font-family: monospace; font-size: 12px; }
  & code.slash-cmd { color: var(--accent); font-weight: 600; }
  & pre { background: rgba(0,0,0,0.25); padding: 8px 10px; border-radius: 4px;
          margin: 4px 0; overflow-x: auto; white-space: pre; }
  & pre code { background: transparent; padding: 0; }
`;
cl.tool = css`
  margin-bottom: 5px; border: 1px solid var(--border); border-radius: 3px; font-size: 12px;
  border-left: 3px solid var(--accent);
  & .hdr { padding: 5px 10px; background: var(--surface); cursor: pointer; color: var(--text-dim);
            display: flex; align-items: center; gap: 6px; user-select: none; }
  & .hdr:hover { background: var(--surface2); color: var(--text); }
  & .arr { font-size: 9px; }
  & .body { padding: 8px 10px; white-space: pre-wrap; color: var(--accent);
            border-top: 1px solid var(--border); max-height: 400px; overflow-y: auto; word-break: break-word; }
  & .result { padding: 8px 10px; white-space: pre-wrap; color: var(--text);
              border-top: 1px solid var(--border); max-height: 400px; overflow-y: auto; word-break: break-word;
              background: var(--bg); }
`;
const spin = keyframes`to { transform: rotate(360deg); }`;
cl.spinner = css`
  display: inline-block; width: 12px; height: 12px;
  border: 2px solid var(--surface3); border-top-color: var(--accent);
  border-radius: 50%; animation: ${spin} 0.8s linear infinite;
`;
cl.input = css`
  display: flex; border-top: 1px solid var(--border);
  & textarea { flex: 1; background: var(--bg); color: var(--text); border: none; padding: 12px;
               font-size: 13px; resize: none; height: 56px; font-family: inherit; outline: none; }
  & button { background: var(--accent); color: #1e1e2e; border: none; padding: 0 16px; cursor: pointer; font-size: 14px; }
  & button:hover { opacity: 0.85; }
  & button:disabled { background: var(--border); cursor: default; opacity: 1; }
`;
