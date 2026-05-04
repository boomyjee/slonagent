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
        const tabs = saved?.tabs?.length ? saved.tabs : [{ id: '', label: '' }];
        const active = saved?.active != null ? saved.active : tabs[0].id;
        this.state = {
            tabs,                                   // [{id, label}] — открытые табы
            activeTab: active,
            processingByThread: {},                 // tid → bool (для спиннера на табе)
            threads: {},                            // uuid → label, всё что прислал сервер
        };
        this._dialogs = {};      // tid → ChatDialog instance
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

    handleMessage(ev) {
        const tid = ev.thread_id || '';
        const m = ev.method;
        if (m === 'thread_rename') {
            this.setState(({ tabs, threads }) => ({
                tabs: tabs.map(t => t.id === ev.uuid ? { ...t, label: ev.name } : t),
                threads: { ...threads, [ev.uuid]: ev.name },
            }), () => this._persist());
            return;
        }
        if (m === 'send_processing') {
            this.setState(({ processingByThread }) => ({
                processingByThread: { ...processingByThread, [tid]: !!ev.active },
            }));
            return;
        }
        // Всё остальное — внутрь конкретного диалога. Если таб не открыт,
        // событие отбрасывается: бэкенд уже записал его в WEB_<tid>.json,
        // так что при открытии таба ChatDialog подтянет историю с диска.
        this._dialogs[tid]?.handleEvent(ev);
    }

    render({ connected, className, threadsEnabled = true }, { tabs, activeTab, processingByThread }) {
        const tabsRender = tabs.map(t => ({
            id: t.id,
            tooltip: t.id || '(primary)',
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
                        ref=${this._setDialogRef(t.id)}
                        connected=${connected}
                        className=${t.id === activeTab ? '' : cl.hidden}
                        threadId=${t.id}
                        onSend=${this._onSend(t.id)} />`)}
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
