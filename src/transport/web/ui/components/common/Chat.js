import { html, Component, css, keyframes, persist } from '../../lib.js';
import { Tabs } from './Tabs.js';
import { ChatDialog } from './ChatDialog.js';

const cl = {};


const ICON_CLOCK = html`
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"/>
        <polyline points="12 6 12 12 16 14"/>
    </svg>`;
const ICON_PLUS = html`
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 5v14M5 12h14"/>
    </svg>`;


// Контейнер: владеет вкладками-тредами и состоянием сообщений по каждому
// треду. Маршрутизирует входящие WS-события в соответствующий ChatDialog.
// Все треды держим в DOM, скрываем неактивные через display:none — так
// сохраняются scroll/sticky-bottom/раскрытые блоки независимо для каждого.
export class Chat extends Component {
    constructor(props) {
        super(props);
        const enabled = props.threadsEnabled !== false;
        const saved = enabled ? persist.get('chat:threads', null) : null;
        const threads = saved?.threads?.length ? saved.threads : [{ id: '', label: 'main' }];
        const active = saved?.active != null ? saved.active : threads[0].id;
        this.state = {
            threads,                                // [{id, label}]
            activeThread: active,
            messagesByThread: {},                   // tid → [...]
            processingByThread: {},                 // tid → bool
        };
        this._streams = {};        // `${tid}:${k}` → index in messagesByThread[tid]
    }

    _persist() {
        if (this.props.threadsEnabled === false) return;
        persist.set('chat:threads', { threads: this.state.threads, active: this.state.activeThread });
    }

    _ensureThread(tid) {
        if (this.state.threads.some(t => t.id === tid)) return;
        const next = [...this.state.threads, { id: tid, label: tid || 'main' }];
        const newActive = this.state.activeThread || tid;
        this.setState({ threads: next, activeThread: newActive }, () => this._persist());
    }

    _onSelectThread = (id) => {
        this.setState({ activeThread: id }, () => this._persist());
    };

    _onCloseThread = (id) => {
        const next = this.state.threads.filter(t => t.id !== id);
        const active = this.state.activeThread === id ? (next[0]?.id || '') : this.state.activeThread;
        this.setState(({ messagesByThread, processingByThread }) => {
            const m = { ...messagesByThread }; delete m[id];
            const p = { ...processingByThread }; delete p[id];
            return { threads: next, activeThread: active, messagesByThread: m, processingByThread: p };
        }, () => this._persist());
    };

    _onAddThread = () => {
        // TODO: серверный протокол создания треда. Пока — локальный пустой таб.
        const id = `new-${Date.now()}`;
        this.setState({
            threads: [...this.state.threads, { id, label: 'new' }],
            activeThread: id,
        }, () => this._persist());
    };

    _onShowHistory = () => {
        // TODO: список архивных тредов (отдельный popup/overlay).
        console.log('thread history (TODO)');
    };

    _updateThread(tid, fn) {
        this.setState(({ messagesByThread }) => {
            const cur = messagesByThread[tid] || [];
            const next = fn(cur);
            if (next === cur) return null;
            return { messagesByThread: { ...messagesByThread, [tid]: next } };
        });
    }

    _onSubmit = (tid) => (parts, preview) => {
        this.props.app.send({
            type: 'transport', method: 'process_message',
            thread_id: tid,
            content_parts: parts,
        });
        this._updateThread(tid, cur => [...cur, { kind: 'msg', role: 'user', items: preview, pending: true }]);
    };

    handleMessage(ev) {
        const tid = ev.thread_id || '';
        if (this.props.threadsEnabled !== false) this._ensureThread(tid);
        const m = ev.method;
        const sid = ev.stream_id;
        const sk = sid != null ? `${tid}:${sid}` : null;
        if (m === 'send_message') {
            this._updateThread(tid, cur => {
                if (sk && this._streams[sk] != null) {
                    const idx = this._streams[sk];
                    const next = [...cur];
                    next[idx] = { ...next[idx], text: ev.text, final: ev.final };
                    return next;
                }
                const next = [...cur, { kind: 'msg', role: 'assistant', text: ev.text, stream_id: sid, final: ev.final }];
                if (sk) this._streams[sk] = next.length - 1;
                return next;
            });
        } else if (m === 'send_thinking' || m === 'send_memory_info') {
            const kind = m === 'send_memory_info' ? 'memory' : 'thinking';
            const k2 = sk && (kind === 'memory' ? `m_${sk}` : `t_${sk}`);
            this._updateThread(tid, cur => {
                if (k2 && this._streams[k2] != null) {
                    const idx = this._streams[k2];
                    const next = [...cur];
                    next[idx] = { ...next[idx], text: ev.text, final: ev.final };
                    return next;
                }
                const next = [...cur, { kind, text: ev.text, stream_id: sid, final: ev.final }];
                if (k2) this._streams[k2] = next.length - 1;
                return next;
            });
        } else if (m === 'on_tool_call') {
            this._updateThread(tid, cur => [...cur, { kind: 'tool', name: ev.name, args: ev.args, result: null }]);
        } else if (m === 'on_tool_result') {
            this._updateThread(tid, cur => {
                for (let i = cur.length - 1; i >= 0; i--) {
                    if (cur[i].kind === 'tool' && cur[i].name === ev.name && cur[i].result == null) {
                        const next = [...cur];
                        next[i] = { ...next[i], result: ev.result };
                        return next;
                    }
                }
                return cur;
            });
        } else if (m === 'send_processing') {
            this.setState(({ processingByThread }) => ({
                processingByThread: { ...processingByThread, [tid]: !!ev.active },
            }));
        } else if (m === 'send_system_prompt') {
            const preview = ev.text.split('\n')[0].slice(0, 80);
            this._updateThread(tid, cur => [...cur, { kind: 'tool', name: preview, args: null, result: ev.text }]);
        } else if (m === 'inject_message') {
            this._updateThread(tid, cur => [...cur, { kind: 'msg', role: 'inject', text: ev.text }]);
        } else if (m === 'send_images') {
            this._updateThread(tid, cur => [...cur, { kind: 'media', media: 'images', items: ev.items || [] }]);
        } else if (m === 'send_files') {
            this._updateThread(tid, cur => [...cur, { kind: 'media', media: 'files', items: ev.items || [] }]);
        } else if (m === 'send_voice') {
            this._updateThread(tid, cur => [...cur, { kind: 'media', media: 'voice', items: [ev.item] }]);
        } else if (m === 'send_suggestions') {
            this._updateThread(tid, cur => [...cur, { kind: 'suggestions', text: ev.text || '', options: ev.options || [] }]);
        } else if (m === 'process_message') {
            const items = [];
            for (const p of (ev.content_parts || [])) {
                if (p.type === 'text') items.push({ kind: 'text', text: p.text });
                else if (p.type === 'image_url') items.push({ kind: 'image', url: (p.image_url || {}).url });
            }
            if (!items.length) return;
            this._updateThread(tid, cur => {
                const pi = cur.findLastIndex(x => x.pending);
                if (pi !== -1) {
                    const next = [...cur];
                    next[pi] = { kind: 'msg', role: 'user', items };
                    return next;
                }
                return [...cur, { kind: 'msg', role: 'user', items }];
            });
        }
    }

    render({ connected, className, threadsEnabled = true }, { threads, activeThread, messagesByThread, processingByThread }) {
        const tabs = threads.map(t => ({
            id: t.id,
            label: html`<span>${t.label}</span>${processingByThread[t.id] ? html`<span class=${cl.tabSpinner}></span>` : null}`,
            closable: threadsEnabled,
        }));
        return html`
            <div class="${cl.chat} ${className || ''}">
                <div class=${cl.tabBar}>
                    <${Tabs} tabs=${tabs} active=${activeThread}
                             onSelect=${this._onSelectThread}
                             onClose=${this._onCloseThread} />
                    ${threadsEnabled && html`
                        <button class=${cl.headerBtn} title="Thread history" onClick=${this._onShowHistory}>${ICON_CLOCK}</button>
                        <button class=${cl.headerBtn} title="New thread" onClick=${this._onAddThread}>${ICON_PLUS}</button>`}
                </div>
                ${threads.map(t => html`
                    <${ChatDialog}
                        key=${t.id}
                        connected=${connected}
                        className=${t.id === activeThread ? '' : cl.hidden}
                        messages=${messagesByThread[t.id] || []}
                        threadId=${t.id}
                        onSubmit=${this._onSubmit(t.id)} />`)}
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
cl.hidden = css`display: none !important;`;
cl.tabBar = css`
  display: flex; align-items: stretch;
  border-bottom: 1px solid var(--border);
  /* Tabs занимают всё свободное место слева, кнопки прижаты вправо. */
  & > :first-child { flex: 1 1 0; min-width: 0; border-bottom: none; }
`;
cl.headerBtn = css`
  flex: 0 0 auto;
  background: transparent; color: var(--text-dim); border: none;
  padding: 0 10px; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  &:hover { background: var(--surface2); color: var(--text); }
  & svg { display: block; }
`;
const spin = keyframes`to { transform: rotate(360deg); }`;
cl.tabSpinner = css`
  display: inline-block; width: 8px; height: 8px;
  margin-left: 6px; vertical-align: middle;
  border: 1.5px solid var(--surface3); border-top-color: var(--accent);
  border-radius: 50%; animation: ${spin} 0.8s linear infinite;
`;
