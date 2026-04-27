import { render, html, Component, css, persist } from './lib.js';
import { Chat } from './components/common/Chat.js';
import { Resizer } from './components/common/Resizer.js';
import { FileTree, refreshTree } from './components/FileTree.js';
import { Editor } from './components/Editor.js';
import { Tabs } from './components/Tabs.js';
import { Logs } from './components/Logs.js';
import { Git } from './components/Git.js';

const BASE = new URL('./', location.href).href;
const api = (path) => fetch(BASE + path).then(r => r.json());

const cl = {};
const LOGS_TAB = { id: 'logs', label: '☰', closable: false };

class App extends Component {
    constructor(props) {
        super(props);
        const savedTabs = persist.get('dashboard.tabs', []).filter(t => t.id !== 'logs');
        const savedActive = persist.get('dashboard.activeTab', 'logs');
        this.state = {
            connected: false,
            rootPath: null,
            tabs: [LOGS_TAB, ...savedTabs.map(t => ({...t, closable: true}))],
            activeTab: savedActive,
            logs: { agent: [], memory: [], transport: [] },
            gitRefreshKey: 0,
        };
        this._chat = null;
        this._editor = null;
    }

    componentDidMount() {
        this._connect();
        api('api/config').then(c => { if (c.root_path) this.setState({ rootPath: c.root_path }); });
        document.addEventListener('keydown', e => {
            if ((e.ctrlKey || e.metaKey) && e.key === 's') {
                e.preventDefault();
                const id = this.state.activeTab;
                if (id !== 'logs' && !id.startsWith('git:')) this._editor?.save(id);
            }
        });
    }

    componentDidUpdate(_, prev) {
        if (prev.tabs !== this.state.tabs || prev.activeTab !== this.state.activeTab) {
            const tabs = this.state.tabs.filter(t => t.id !== 'logs').map(t => ({ id: t.id, label: t.label }));
            persist.set('dashboard.tabs', tabs);
            persist.set('dashboard.activeTab', this.state.activeTab);
        }
    }

    _connect() {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        this._ws = new WebSocket(proto + location.host + location.pathname + 'ws');
        this._ws.onopen = () => this.setState({ connected: true });
        this._ws.onclose = () => { this.setState({ connected: false }); setTimeout(() => this._connect(), 2000); };
        this._ws.onerror = () => this._ws.close();
        this._ws.onmessage = e => this._onMessage(JSON.parse(e.data));
    }

    send(msg) {
        if (this._ws?.readyState === WebSocket.OPEN) this._ws.send(JSON.stringify(msg));
    }

    _onMessage(ev) {
        if (ev.type === 'transport') {
            this._chat?.handleMessage(ev);
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
        }
    }

    _openFile = (path, name) => {
        this.setState(({ tabs }) => {
            const next = tabs.some(t => t.id === path)
                ? tabs
                : [...tabs, { id: path, label: name, closable: true }];
            return { tabs: next, activeTab: path };
        });
    };

    _openGit = (path, name) => {
        const id = `git:${path}`;
        this.setState(({ tabs }) => {
            const next = tabs.some(t => t.id === id)
                ? tabs
                : [...tabs, { id, label: `⎇ ${name}`, closable: true }];
            return { tabs: next, activeTab: id };
        });
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

    _onDirtyChange = (path, dirty, diskChanged) => {
        this.setState(({ tabs }) => ({
            tabs: tabs.map(t => t.id === path ? { ...t, dirty, diskChanged } : t),
        }));
    };

    render(_, { connected, rootPath, tabs, activeTab, logs, gitRefreshKey }) {
        const isLogs = activeTab === 'logs';
        const isGit = activeTab.startsWith('git:');
        const activeFile = !isLogs && !isGit ? activeTab : null;
        return html`
            <div class=${cl.app}>
                <div class=${cl.sidebar}>
                    <div class=${cl.sidebarHdr}>Explorer</div>
                    <div class=${cl.tree}>
                        <${FileTree} rootPath=${rootPath} onOpen=${this._openFile} onOpenGit=${this._openGit} />
                    </div>
                </div>
                <${Resizer} side="left" persistKey="sidebar" />
                <div class=${cl.main}>
                    <${Tabs} tabs=${tabs} active=${activeTab}
                             onSelect=${id => this.setState({ activeTab: id })}
                             onClose=${this._closeTab} />
                    <div class=${cl.content}>
                        <div class=${cl.pane} style=${{display: isLogs ? 'flex' : 'none'}}>
                            <${Logs} logs=${logs} />
                        </div>
                        ${tabs.filter(t => t.id.startsWith('git:')).map(t => html`
                            <div key=${t.id} class=${cl.pane}
                                 style=${{display: activeTab === t.id ? 'flex' : 'none'}}>
                                <${Git} path=${t.id.slice(4)} refreshKey=${gitRefreshKey} />
                            </div>`)}
                        <div class=${cl.pane} style=${{display: activeFile ? 'flex' : 'none'}}>
                            <${Editor} ref=${c => this._editor = c}
                                       active=${activeFile}
                                       onDirtyChange=${this._onDirtyChange} />
                        </div>
                    </div>
                </div>
                <${Resizer} side="right" persistKey="chat" />
                <${Chat} ref=${c => this._chat = c} app=${this} connected=${connected} />
            </div>
        `;
    }
}

render(html`<${App} />`, document.body);

cl.app = css`display: flex; height: 100vh; overflow: hidden;`;
cl.sidebar = css`
  width: 220px; background: var(--surface); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; overflow: hidden; flex-shrink: 0;
`;
cl.sidebarHdr = css`
  padding: 10px 12px; font-size: 11px; text-transform: uppercase;
  letter-spacing: 1px; color: var(--text-dim); border-bottom: 1px solid var(--border);
`;
cl.tree = css`flex: 1; overflow-y: auto; padding: 4px 0;`;
cl.main = css`flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0;`;
cl.content = css`flex: 1; display: flex; min-height: 0; position: relative;`;
cl.pane = css`flex: 1; flex-direction: column; min-height: 0; min-width: 0; overflow: hidden;`;
