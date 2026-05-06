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


// Контейнер: владеет вкладками-тредами. Сообщения и стримы лежат в каждом
// ChatDialog отдельно — Chat лишь маршрутизирует входящие WS-события в
// нужный диалог по thread_id (через ref). Глобально здесь обрабатываются
// только тред-уровневые события: send_processing (спиннер на табе) и
// thread_rename. Все треды держим в DOM — скрываем неактивные через
// display:none, чтоб сохранять scroll/sticky-bottom/раскрытые блоки.
export class Chat extends Component {
    constructor(props) {
        super(props);
        const enabled = props.threadsEnabled !== false;
        const saved = enabled ? persist.get('chat:tabs', null) : null;
        const tabs = saved?.tabs?.length ? saved.tabs : [''];
        const active = saved?.active != null ? saved.active : tabs[0];
        this.state = {
            tabs,                                   // [id, id, ...]
            activeTab: active,
            processingByThread: {},                 // tid → bool (для спиннера на табе)
            threads: {},                            // uuid → label
            ready: false,                           // false до завершения thread_list
        };
        this._dialogs = {};      // tid → ChatDialog instance
    }

    componentDidMount() {
        this._fetchThreadList();
    }

    async _fetchThreadList() {
        let list = {};
        try {
            const r = await fetch('api/thread_list');
            if (r.ok) list = await r.json();    // {uuid: {name, ...}}
        } catch (e) {
            console.warn('thread_list fetch failed', e);
        }
        const threads = Object.fromEntries(
            Object.entries(list).map(([id, info]) => [id, info?.name || ''])
        );
        this.setState(({ tabs, activeTab }) => {
            // Выкидываем tabs, которых нет в server registry. '' (primary)
            let valid = tabs.filter(id => id === '' || id in threads);
            if (!valid.length) valid = [''];
            return {
                threads, ready: true,
                tabs: valid,
                activeTab: valid.includes(activeTab) ? activeTab : valid[0],
            };
        }, () => this._persist());
        // _pending дренит componentDidUpdate когда видит ready=true.
    }

    componentDidUpdate() {
        if (this.state.ready && this._pending?.length)
            for (const ev of this._pending.splice(0)) this.handleMessage(ev);
    }

    handleMessage(ev) {
        if (!this.state.ready) return (this._pending ??= []).push(ev);

        const tid = ev.thread_id || '';
        const m = ev.method;
        if (m === 'thread_rename') {
            this.setState(({ threads }) => ({
                threads: { ...threads, [ev.uuid]: ev.name },
            }));
            return;
        }
        if (m === 'thread_delete') {
            this.setState(({ tabs, threads, activeTab, processingByThread }) => {
                const t = { ...threads }; delete t[ev.uuid];
                const p = { ...processingByThread }; delete p[ev.uuid];
                const next = tabs.filter(id => id !== ev.uuid);
                const active = activeTab === ev.uuid ? (next[0] || '') : activeTab;
                return { threads: t, tabs: next, activeTab: active, processingByThread: p };
            }, () => this._persist());
            return;
        }
        if (m === 'send_processing') {
            this.setState(({ processingByThread }) => ({
                processingByThread: { ...processingByThread, [tid]: !!ev.active },
            }));
            return;
        }
        this._dialogs[tid]?.handleEvent(ev);
    }    

    _persist() {
        if (this.props.threadsEnabled === false) return;
        persist.set('chat:tabs', { tabs: this.state.tabs, active: this.state.activeTab });
    }

    _onSelectTab = (id) => {
        this.setState({ activeTab: id }, () => this._persist());
    };

    _onCloseTab = (id) => {
        const next = this.state.tabs.filter(t => t !== id);
        const active = this.state.activeTab === id ? (next[0] || '') : this.state.activeTab;
        this.setState(({ processingByThread }) => {
            const p = { ...processingByThread }; delete p[id];
            return { tabs: next, activeTab: active, processingByThread: p };
        }, () => this._persist());
    };

    _onAddTab = () => {
        // Тред регистрируется на сервере лениво — при первом сообщении (через make_agent
        // → Agent.start) или явном rename. Закрытие пустого таба не оставит хвостов.
        const id = (crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`).replace(/-/g, '').slice(0, 8);
        this.setState({
            tabs: [...this.state.tabs, id],
            activeTab: id,
        }, () => this._persist());
    };

    _onRenameTab = (id) => {
        const cur = this.state.threads[id] || '';
        const name = prompt('Имя треда:', cur);
        if (name == null || name === cur) return;
        this.props.app.send({ type: 'transport', method: 'thread_rename', uuid: id, name });
    };

    _openTabFromHistory = (id) => {
        this.setState({
            tabs: [...this.state.tabs, id],
            activeTab: id,
        }, () => this._persist());
        Dialog.close();
    };

    _deleteThread = (id, label) => {
        if (!confirm(`Удалить тред "${label || 'Untitled'}"?`)) return;
        this.props.app.send({ type: 'transport', method: 'thread_delete', uuid: id });
        // Перерисуем диалог после обновления state (thread_delete event обновит threads)
        setTimeout(() => this._onShowHistory(), 100);
    };

    _onShowHistory = () => {
        const openIds = new Set(this.state.tabs);
        const closed = Object.entries(this.state.threads).filter(([id]) => !openIds.has(id));
        Dialog.open(html`
            <div class=${cl.historyHeader}>История тредов</div>
            ${closed.length === 0
                ? html`<div class=${cl.historyEmpty}>Все треды уже открыты</div>`
                : html`<div class=${cl.historyList}>${closed.map(([id, label]) => html`
                    <div key=${id} class=${cl.historyItem}>
                        <span class=${label ? '' : cl.untitled} onClick=${() => this._openTabFromHistory(id)}>${label || 'Untitled'}</span>
                        <button class=${cl.deleteBtn} title="Удалить" onClick=${(e) => { e.stopPropagation(); this._deleteThread(id, label); }}><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>
                    </div>`)}
                </div>`}
        `);
    };

    _onSend = (tid) => (parts) => {
        this.props.app.send({
            type: 'transport', method: 'process_message',
            thread_id: tid,
            content_parts: parts,
        });
    };

    _setDialogRef = (id) => (c) => {
        if (c) this._dialogs[id] = c;
        else delete this._dialogs[id];
    };

    render({ connected, className, threadsEnabled = true }, { tabs, activeTab, processingByThread, threads, ready }) {
        if (!ready) return null;
        const tabsRender = tabs.map(id => {
            const name = threads[id] || '';
            return {
                id,
                tooltip: id || '(primary)',
                label: html`<span class=${name ? '' : cl.untitled}>${name || 'Untitled'}</span>${processingByThread[id] ? html`<span class=${cl.tabSpinner}></span>` : null}`,
                closable: threadsEnabled,
            };
        });
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
                ${tabs.map(id => html`
                    <${ChatDialog}
                        key=${id}
                        ref=${this._setDialogRef(id)}
                        connected=${connected}
                        className=${id === activeTab ? '' : cl.hidden}
                        threadId=${id}
                        onSend=${this._onSend(id)} />`)}
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
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  &:hover { background: var(--surface2); }
  & > span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
`;
cl.deleteBtn = css`
  flex: 0 0 auto; background: transparent; border: none; color: var(--text-dim);
  cursor: pointer; font-size: 16px; padding: 4px 8px; border-radius: 4px;
  &:hover { background: var(--red, #e64553); color: #fff; }
`;
cl.historyEmpty = css`padding: 16px; font-size: 13px; color: var(--text-dim); text-align: center;`;
cl.tabSpinner = css`
  display: inline-block; width: 8px; height: 8px;
  margin-left: 6px; vertical-align: middle;
  border: 1.5px solid var(--surface3); border-top-color: var(--accent);
  border-radius: 50%; animation: ${spin} 0.8s linear infinite;
`;
