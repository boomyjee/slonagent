import { render, html, Component, css, persist } from './lib.js';
import { Chat } from './components/common/Chat.js';
import { IdeContext } from './components/common/ChatDialog.js';
import { Resizer } from './components/common/Resizer.js';
import { FileTree, refreshTree, setTreeScope, isInGitRepo } from './components/FileTree.js';
import { openChangeRootDialog } from './components/ChangeRootDialog.js';
import { Editor } from './components/Editor.js';
import { Tabs } from './components/common/Tabs.js';
import { Logs } from './components/Logs.js';
import { Git } from './components/Git.js';
import { WebView } from './components/WebView.js';
import { currentRoot, setCurrentRoot } from './components/api.js';
import { applyAll } from './theme.js';
import { openSettingsDialog } from './components/SettingsDialog.js';

// Применяем сохранённые тему и размер шрифта до первого рендера, чтобы избежать flash.
applyAll();

const cl = {};
const LOGS_TAB = { id: 'logs', label: '☰', closable: false };

// Stable cheap hash → 8-char base36 — enough to scope localStorage keys
// per root so paths from different host folders don't collide.
function rootHash(root) {
    if (!root) return '';
    let h = 0;
    for (let i = 0; i < root.length; i++) h = ((h << 5) - h + root.charCodeAt(i)) | 0;
    return (h >>> 0).toString(36);
}
function rootBasename(root) {
    if (!root) return '';
    return root.replace(/[\/\\]+$/, '').split(/[\/\\]/).pop() || root;
}

class App extends Component {
    constructor(props) {
        super(props);
        const root = currentRoot();
        const rootKey = rootHash(root);
        setTreeScope(rootKey);
        const savedTabs = persist.get(`tabs:${rootKey}`, []).filter(t => t.id !== 'logs');
        const savedActive = persist.get(`activeTab:${rootKey}`, 'logs');
        this.state = {
            connected: false,
            root,
            rootKey,
            tabs: [LOGS_TAB, ...savedTabs.map(t => ({ ...t, closable: true }))],
            activeTab: savedActive,
            logs: { agent: [], memory: [], transport: [] },
            gitRefreshKey: 0,
            // CSS @media drives viewport-based layout; this state only
            // says which pane is active when the viewport is mobile.
            mobileView: persist.get('dashboard.mobileView', 'editor'),
            // IDE-контекст: bag, в который consumer'ы пишут напрямую и зовут
            // change(). Тот персистит enabled и пересоздаёт bag с новой
            // ссылкой через setState, чтоб Preact-context propagation сработал.
            ideContext: {
                selection: null,
                enabled: persist.get('ide.enabled', true),
                change: () => {
                    persist.set('ide.enabled', this.state.ideContext.enabled);
                    this.setState(({ ideContext }) => ({ ideContext: { ...ideContext } }));
                },
            },
        };
        this._chat = null;
        this._editor = null;
    }

    componentDidMount() {
        this._lastSeenId = -1;
        this._unread = 0;
        navigator.clearAppBadge?.();
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) {
                this._unread = 0;
                navigator.clearAppBadge?.();
            }
        });
        this._connect();
        document.addEventListener('keydown', e => {
            if ((e.ctrlKey || e.metaKey) && e.key === 's') {
                e.preventDefault();
                const id = this.state.activeTab;
                if (id !== 'logs' && !id.startsWith('git:')) this._editor?.save(id);
            }
        });
        // Перехват кликов по http(s)-ссылкам по всему дашборду — открываем
        // во встроенной вкладке. Ctrl/Cmd/Shift/middle = дефолт браузера
        // (новое окно). Не-http схемы (mailto:, tg:, ...) пропускаем как есть.
        document.addEventListener('click', e => {
            if (e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1) return;
            const a = e.target.closest?.('a');
            if (!a) return;
            const href = a.getAttribute('href');
            if (!href || !/^https?:/i.test(href)) return;
            e.preventDefault();
            this._openUrl(href);
        });
    }

    _setMobileView = (v) => {
        persist.set('dashboard.mobileView', v);
        this.setState({ mobileView: v });
    };

    componentDidUpdate(_, prev) {
        if (prev.tabs !== this.state.tabs || prev.activeTab !== this.state.activeTab) {
            const key = this.state.rootKey;  // '' is fine — saves under "tabs:" / "activeTab:"
            const tabs = this.state.tabs.filter(t => t.id !== 'logs').map(t => ({ id: t.id, label: t.label }));
            persist.set(`tabs:${key}`, tabs);
            persist.set(`activeTab:${key}`, this.state.activeTab);
        }
    }

    _connect() {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        this._ws = new WebSocket(proto + location.host + location.pathname + 'ws');
        this._ws.onopen = () => {
            this.setState({ connected: true });
            this.send({ type: 'replay', last_seen_id: this._lastSeenId });
            this._subscribeWatcher();
        };
        this._ws.onclose = () => { this.setState({ connected: false }); setTimeout(() => this._connect(), 2000); };
        this._ws.onerror = () => this._ws.close();
        this._ws.onmessage = e => this._onMessage(JSON.parse(e.data));
    }

    _subscribeWatcher() {
        // Server replaces any prior subscription on this WS — empty root
        // gets resolved to the sandbox default on the server side.
        this.send({ type: 'watch', root: this.state.root });
    }

    _onChangeRoot = () => openChangeRootDialog({
        initialPath: this.state.root,
        onConfirm: this._onRootConfirmed,
    });

    _onRootConfirmed = (newRoot) => {
        setCurrentRoot(newRoot);
        const rootKey = rootHash(newRoot);
        setTreeScope(rootKey);
        this._editor?.purge();
        const savedTabs = persist.get(`tabs:${rootKey}`, []).filter(t => t.id !== 'logs');
        const savedActive = persist.get(`activeTab:${rootKey}`, 'logs');
        this.setState({
            root: newRoot,
            rootKey,
            tabs: [LOGS_TAB, ...savedTabs.map(t => ({ ...t, closable: true }))],
            activeTab: savedActive,
        }, () => this._subscribeWatcher());
    };

    send(msg) {
        if (this._ws?.readyState === WebSocket.OPEN) this._ws.send(JSON.stringify(msg));
    }

    _onMessage(ev) {
        if (ev.type === 'replay') {
            for (const e of ev.events || []) this._onMessage(e);
            return;
        }
        if (ev.id != null && ev.id > this._lastSeenId) this._lastSeenId = ev.id;
        if (ev.type === 'transport') {
            this._chat?.handleMessage(ev);
            if (document.hidden && ev.method === 'send_message' && ev.final !== false) {
                navigator.setAppBadge?.(++this._unread);
            }
        } else if (ev.type === 'log') {
            this.setState(({ logs }) => {
                const cat = ev.category;
                if (!logs[cat]) return {};
                return { logs: { ...logs, [cat]: [...logs[cat], { level: ev.level, text: ev.text }] } };
            });
        } else if (ev.type === 'files_changed') {
            this._editor?.handleFilesChanged(ev.paths);
            if (ev.tree) refreshTree(ev.paths);
            this.setState(({ gitRefreshKey }) => ({ gitRefreshKey: gitRefreshKey + 1 }));
        } else if (ev.type === 'open_tab') {
            this._openUri(ev.uri);
        }
    }

    _openFile = (path, name, line) => {
        this.setState(({ tabs }) => {
            const next = tabs.some(t => t.id === path)
                ? tabs
                : [...tabs, { id: path, label: name, closable: true }];
            return { tabs: next, activeTab: path };
        });
        this._setMobileView('editor');
        if (line) this._editor?.revealLine(path, line);
    };

    _openGit = (path, name) => {
        const id = `git:${path}`;
        this.setState(({ tabs }) => {
            const next = tabs.some(t => t.id === id)
                ? tabs
                : [...tabs, { id, label: `⎇ ${name}`, closable: true }];
            return { tabs: next, activeTab: id };
        });
        this._setMobileView('editor');
    };

    _openGitShow = (repo, ref, file, line) => {
        const id = `git-show:${encodeURIComponent(repo)}:${encodeURIComponent(ref)}:${encodeURIComponent(file)}`;
        const name = file.split('/').pop();
        this.setState(({ tabs }) => {
            const next = tabs.some(t => t.id === id)
                ? tabs
                : [...tabs, { id, label: `${name} @ ${ref.slice(0,7)}`, closable: true }];
            return { tabs: next, activeTab: id };
        });
        this._setMobileView('editor');
        if (line) this._editor?.revealLine(id, line);
    };

    _openGitBlame = (path, name) => {
        const id = `git-blame:${path}`;
        this.setState(({ tabs }) => {
            const next = tabs.some(t => t.id === id)
                ? tabs
                : [...tabs, { id, label: `blame: ${name}`, closable: true }];
            return { tabs: next, activeTab: id };
        });
        this._setMobileView('editor');
    };

    _openUrl = (url) => {
        const id = `url:${url}`;
        let host;
        try { host = new URL(url).host; } catch { host = url; }
        this.setState(({ tabs }) => {
            const next = tabs.some(t => t.id === id)
                ? tabs
                : [...tabs, { id, label: host, tooltip: url, closable: true }];
            return { tabs: next, activeTab: id };
        });
        this._setMobileView('editor');
        // Уже открытая вкладка — рефреш через ref WebView'а.
        this._webViews?.[id]?.refresh();
    };

    _onWebViewTitle = (id, title) => {
        this.setState(({ tabs }) => ({
            tabs: tabs.map(t => t.id === id ? { ...t, label: title } : t),
        }));
    };

    /**
     * Универсальный «открой что угодно по URI». LLM/чат вызывают это.
     * Поддерживаемые схемы повторяют tab id'ы:
     *   /<path>                       — файл
     *   git:<path>                    — git-панель
     *   git-show:<repo>:<ref>:<file>  — файл на коммите
     *   git-blame:<path>              — blame
     *   url:<full url>                — iframe
     *   logs                          — лог-панель
     */
    _openUri = (uri) => {
        if (uri === 'logs') return this.setState({ activeTab: 'logs' });
        if (uri.startsWith('url:')) return this._openUrl(uri.slice(4));
        if (uri.startsWith('git:')) return this._openGit(uri.slice(4), uri.slice(4).split(/[\/\\]/).pop());
        // git-show:/git-blame: и просто пути уходят в Editor — там уже есть
        // диспатч по префиксу. Открытие = добавить таб + revealLine не делаем.
        const name = uri.split(/[\/\\:]/).pop();
        this.setState(({ tabs }) => {
            const next = tabs.some(t => t.id === uri)
                ? tabs
                : [...tabs, { id: uri, label: name, closable: true }];
            return { tabs: next, activeTab: uri };
        });
        this._setMobileView('editor');
        this._editor?.reload?.(uri);
    };

    _closeTab = (id) => {
        if (id === 'logs') return;
        this.setState(({ tabs, activeTab }) => {
            const idx = tabs.findIndex(t => t.id === id);
            const next = tabs.filter(t => t.id !== id);
            const active = activeTab === id ? (idx > 0 ? tabs[idx - 1].id : 'logs') : activeTab;
            return { tabs: next, activeTab: active };
        });
        if (!id.startsWith('git:')) this._editor?.closeFile(id);
    };

    _closeOthers = (keepId) => {
        const closing = this.state.tabs.filter(t => t.closable !== false && t.id !== keepId);
        for (const t of closing) {
            if (!t.id.startsWith('git:')) this._editor?.closeFile(t.id);
        }
        this.setState(({ tabs }) => ({
            tabs: tabs.filter(t => t.closable === false || t.id === keepId),
            activeTab: keepId,
        }));
    };

    _closeAll = () => {
        const closing = this.state.tabs.filter(t => t.closable !== false);
        for (const t of closing) {
            if (!t.id.startsWith('git:')) this._editor?.closeFile(t.id);
        }
        this.setState(({ tabs }) => ({
            tabs: tabs.filter(t => t.closable === false),
            activeTab: 'logs',
        }));
    };

    _reorderTab = (srcId, insertAt) => {
        this.setState(({ tabs }) => {
            const srcIdx = tabs.findIndex(t => t.id === srcId);
            if (srcIdx < 0) return {};
            const src = tabs[srcIdx];
            const filtered = tabs.filter(t => t.id !== srcId);
            // After removing src, insertions past srcIdx shift left by one.
            const target = insertAt > srcIdx ? insertAt - 1 : insertAt;
            return { tabs: [...filtered.slice(0, target), src, ...filtered.slice(target)] };
        });
    };

    _tabContextItems = (tab) => {
        const closable = tab.closable !== false;
        const isFile = tab.id.startsWith('/') && !tab.id.startsWith('git-');
        const isUrl = tab.id.startsWith('url:');
        const blameable = isFile && isInGitRepo(tab.id);
        const others = this.state.tabs.filter(t => t.closable !== false && t.id !== tab.id);
        return [
            { label: 'Close', disabled: !closable, action: () => this._closeTab(tab.id) },
            { label: 'Close Others', disabled: !others.length, action: () => this._closeOthers(tab.id) },
            { label: 'Close All', action: () => this._closeAll() },
            ...(blameable ? [{ label: 'Git Blame', action: () => this._openGitBlame(tab.id, tab.label) }] : []),
            ...(isUrl ? [
                { label: 'Refresh', action: () => this._webViews?.[tab.id]?.refresh() },
                { label: 'Copy URL', action: () => navigator.clipboard?.writeText(tab.id.slice(4)) },
            ] : []),
        ];
    };

    _onDirtyChange = (path, dirty, diskChanged) => {
        this.setState(({ tabs }) => ({
            tabs: tabs.map(t => t.id === path ? { ...t, dirty, diskChanged } : t),
        }));
    };

    render(_, { connected, root, rootKey, tabs, activeTab, logs, gitRefreshKey, mobileView, ideContext }) {
        const isLogs = activeTab === 'logs';
        const isGit = activeTab.startsWith('git:');
        const isUrl = activeTab.startsWith('url:');
        const activeFile = !isLogs && !isGit && !isUrl ? activeTab : null;
        this._webViews = this._webViews || {};
        return html`
            <${IdeContext.Provider} value=${ideContext}>
            <div class="${cl.app} mobile-${mobileView}">
                <div class="${cl.sidebar} dash-sidebar">
                    <div class=${cl.sidebarHdr}>
                        <span>Explorer</span>
                        <button class=${cl.gear} onClick=${() => openSettingsDialog()}
                                title="Settings">⚙</button>
                    </div>
                    <div class=${cl.tree}>
                        <${FileTree} rootPath="/"
                                     rootName=${rootBasename(root)}
                                     onOpen=${this._openFile}
                                     onOpenGit=${this._openGit}
                                     onOpenGitBlame=${this._openGitBlame}
                                     onOpenUrl=${this._openUrl}
                                     onChangeRoot=${this._onChangeRoot} />
                    </div>
                </div>
                <${Resizer} side="left" persistKey="sidebar" collapseLabel="Explorer" className="dash-resizer" />
                <div class="${cl.main} dash-main">
                    <${Tabs} tabs=${tabs} active=${activeTab}
                             onSelect=${id => this.setState({ activeTab: id })}
                             onClose=${this._closeTab}
                             onReorder=${this._reorderTab}
                             contextItems=${this._tabContextItems} />
                    <div class=${cl.content}>
                        <div class=${cl.pane} style=${{display: isLogs ? 'flex' : 'none'}}>
                            <${Logs} logs=${logs} />
                        </div>
                        ${tabs.filter(t => t.id.startsWith('git:')).map(t => html`
                            <div key=${t.id} class=${cl.pane}
                                 style=${{display: activeTab === t.id ? 'flex' : 'none'}}>
                                <${Git} path=${t.id.slice(4)}
                                        rootKey=${rootKey}
                                        refreshKey=${gitRefreshKey}
                                        onOpenFile=${this._openFile}
                                        onOpenGitShow=${this._openGitShow} />
                            </div>`)}
                        ${tabs.filter(t => t.id.startsWith('url:')).map(t => html`
                            <div key=${t.id} class=${cl.pane}
                                 style=${{display: activeTab === t.id ? 'flex' : 'none'}}>
                                <${WebView} url=${t.id.slice(4)}
                                            ref=${c => { this._webViews[t.id] = c; }}
                                            onTitle=${title => this._onWebViewTitle(t.id, title)} />
                            </div>`)}
                        <div class=${cl.pane} style=${{display: activeFile ? 'flex' : 'none'}}>
                            <${Editor} ref=${c => this._editor = c}
                                       active=${activeFile}
                                       rootKey=${rootKey}
                                       onDirtyChange=${this._onDirtyChange} />
                        </div>
                    </div>
                </div>
                <${Resizer} side="right" persistKey="chat" collapseLabel="Chat" className="dash-resizer" />
                <${Chat} ref=${c => this._chat = c} app=${this} connected=${connected}
                         className="dash-chat" />
                <div class=${cl.bottomNav}>
                    ${[['files', '▤ Files'], ['editor', '✎ Editor'], ['chat', '✉ Chat']].map(([k, label]) => html`
                        <button class="${cl.navBtn}${mobileView === k ? ' active' : ''}"
                                onClick=${() => this._setMobileView(k)}>${label}</button>`)}
                </div>
            </div>
            </${IdeContext.Provider}>
        `;
    }
}

render(html`<${App} />`, document.body);

cl.app = css`
  display: flex; height: 100dvh; overflow: hidden;
  @media (max-width: 768px) {
    flex-direction: column;
    & .dash-resizer { display: none; }
    & .dash-sidebar, & .dash-main, & .dash-chat {
      flex: 1 1 auto !important; width: 100% !important;
      min-width: 0; min-height: 0;
    }
    & .dash-sidebar, & .dash-main, & .dash-chat { display: none; }
    &.mobile-files .dash-sidebar { display: flex !important; }
    &.mobile-editor .dash-main { display: flex; }
    &.mobile-chat .dash-chat { display: flex !important; }
  }
`;
cl.sidebar = css`
  width: 220px; background: var(--surface); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; overflow: hidden; flex-shrink: 0;
`;
cl.sidebarHdr = css`
  padding: 10px 12px; font-size: 11px; text-transform: uppercase;
  letter-spacing: 1px; color: var(--text-dim); border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
`;
cl.gear = css`
  background: transparent; border: none; cursor: pointer;
  color: var(--text-dim); font-size: 14px; padding: 0 2px;
  line-height: 1; user-select: none;
  &:hover { color: var(--text); }
`;
cl.tree = css`flex: 1; overflow-y: auto; padding: 4px 0;`;
cl.main = css`flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0;`;
cl.content = css`flex: 1; display: flex; min-height: 0; position: relative;`;
cl.pane = css`flex: 1; flex-direction: column; min-height: 0; min-width: 0; overflow: hidden;`;

cl.bottomNav = css`
  display: none;  /* hidden on desktop; the @media block below flips this */
  @media (max-width: 768px) {
    display: flex; order: -1;  /* put nav at the top — bottom edge has system UI on phones */
    background: var(--surface); border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
`;
cl.navBtn = css`
  flex: 1; padding: 10px 8px; background: transparent; border: none;
  color: var(--text-dim); cursor: pointer; font-size: 13px;
  border-bottom: 2px solid transparent;
  &.active, &.active:focus { color: var(--accent); border-bottom-color: var(--accent); }
  &:hover { color: var(--text); }
`;
