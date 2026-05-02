import { html, Component, css, keyframes, persist } from '../../lib.js';
import { Tabs } from './Tabs.js';
import { ChatDialog } from './ChatDialog.js';
import { Dialog } from './Dialog.js';

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
        const saved = enabled ? persist.get('chat:tabs', null) : null;
        const tabs = saved?.tabs?.length ? saved.tabs : [{ id: '', label: '' }];
        const active = saved?.active != null ? saved.active : tabs[0].id;
        this.state = {
            tabs,                                   // [{id, label}] — открытые табы
            activeTab: active,
            messagesByThread: {},                   // tid → [...]
            processingByThread: {},                 // tid → bool
            threads: {},                            // uuid → label, всё что прислал сервер
        };
        this._streams = {};        // `${tid}:${k}` → index in messagesByThread[tid]
    }

    _persist() {
        if (this.props.threadsEnabled === false) return;
        persist.set('chat:tabs', { tabs: this.state.tabs, active: this.state.activeTab });
    }

    _onSelectTab = (id) => {
        this.setState({ activeTab: id }, () => this._persist());
    };

    _onCloseTab = (id) => {
        const next = this.state.tabs.filter(t => t.id !== id);
        const active = this.state.activeTab === id ? (next[0]?.id || '') : this.state.activeTab;
        this.setState(({ messagesByThread, processingByThread }) => {
            const m = { ...messagesByThread }; delete m[id];
            const p = { ...processingByThread }; delete p[id];
            return { tabs: next, activeTab: active, messagesByThread: m, processingByThread: p };
        }, () => this._persist());
    };

    _onAddTab = () => {
        // Тред регистрируется на сервере лениво — при первом сообщении (через make_agent
        // → Agent.start) или явном rename. Закрытие пустого таба не оставит хвостов.
        const id = (crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`).replace(/-/g, '').slice(0, 8);
        this.setState({
            tabs: [...this.state.tabs, { id, label: '' }],
            activeTab: id,
        }, () => this._persist());
    };

    _onRenameTab = (id) => {
        const cur = this.state.tabs.find(t => t.id === id);
        const name = prompt('Имя треда:', cur?.label || '');
        if (name == null || name === cur?.label) return;
        this.props.app.send({ type: 'transport', method: 'thread_rename', uuid: id, name });
    };

    _openTabFromHistory = (id) => {
        const label = this.state.threads[id] || '';
        this.setState({
            tabs: [...this.state.tabs, { id, label }],
            activeTab: id,
        }, () => this._persist());
        Dialog.close();
    };

    _onShowHistory = () => {
        const openIds = new Set(this.state.tabs.map(t => t.id));
        const closed = Object.entries(this.state.threads).filter(([id]) => !openIds.has(id));
        Dialog.open(html`
            <div class=${cl.historyHeader}>История тредов</div>
            ${closed.length === 0
                ? html`<div class=${cl.historyEmpty}>Все треды уже открыты</div>`
                : html`<div class=${cl.historyList}>${closed.map(([id, label]) => html`
                    <div key=${id} class=${cl.historyItem} onClick=${() => this._openTabFromHistory(id)}>
                        <span class=${label ? '' : cl.untitled}>${label || 'Untitled'}</span>
                    </div>`)}
                </div>`}
        `);
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
        } else if (m === 'thread_rename') {
            this.setState(({ tabs, threads }) => ({
                tabs: tabs.map(t => t.id === ev.uuid ? { ...t, label: ev.name } : t),
                threads: { ...threads, [ev.uuid]: ev.name },
            }), () => this._persist());
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

    render({ connected, className, threadsEnabled = true }, { tabs, activeTab, messagesByThread, processingByThread }) {
        const tabsRender = tabs.map(t => ({
            id: t.id,
            label: html`<span class=${t.label ? '' : cl.untitled}>${t.label || 'Untitled'}</span>${processingByThread[t.id] ? html`<span class=${cl.tabSpinner}></span>` : null}`,
            closable: threadsEnabled,
        }));
        return html`
            <div class="${cl.chat} ${className || ''}">
                <div class=${cl.tabBar}>
                    <${Tabs} tabs=${tabsRender} active=${activeTab}
                             onSelect=${this._onSelectTab}
                             onClose=${this._onCloseTab}
                             contextItems=${threadsEnabled ? (t => [{ label: 'Переименовать', action: () => this._onRenameTab(t.id) }]) : null} />
                    ${threadsEnabled && html`
                        <button class=${cl.headerBtn} title="История тредов" onClick=${this._onShowHistory}>${ICON_CLOCK}</button>
                        <button class=${cl.headerBtn} title="Новый тред" onClick=${this._onAddTab}>${ICON_PLUS}</button>`}
                </div>
                ${tabs.map(t => html`
                    <${ChatDialog}
                        key=${t.id}
                        connected=${connected}
                        className=${t.id === activeTab ? '' : cl.hidden}
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
cl.untitled = css`color: var(--text-dim); font-style: italic;`;
cl.historyHeader = css`
  padding: 12px 16px; font-size: 14px; font-weight: 500; color: var(--text);
  border-bottom: 1px solid var(--border);
`;
cl.historyList = css`
  min-width: 280px; max-height: 60vh; overflow-y: auto;
`;
cl.historyItem = css`
  padding: 10px 16px; cursor: pointer; font-size: 13px; color: var(--text);
  &:hover { background: var(--surface2); }
`;
cl.historyEmpty = css`padding: 16px; font-size: 13px; color: var(--text-dim); text-align: center;`;
cl.tabSpinner = css`
  display: inline-block; width: 8px; height: 8px;
  margin-left: 6px; vertical-align: middle;
  border: 1.5px solid var(--surface3); border-top-color: var(--accent);
  border-radius: 50%; animation: ${spin} 0.8s linear infinite;
`;
