import { render, html, Component, css, persist } from './lib.js';
import { Chat } from './components/common/Chat.js';
import { Resizer } from './components/common/Resizer.js';

// --- Monaco Editor (AMD loader) ---

await new Promise(resolve => {
    const s = document.createElement('script');
    s.src = 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.52.2/min/vs/loader.min.js';
    s.onload = resolve;
    document.head.appendChild(s);
});
const monaco = await new Promise(resolve => {
    require.config({ paths: { vs: 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.52.2/min/vs' }});
    require(['vs/editor/editor.main'], () => resolve(window.monaco));
});

monaco.editor.defineTheme('catppuccin', {
    base: 'vs-dark',
    inherit: true,
    rules: [
        { token: 'comment', foreground: '6c7086', fontStyle: 'italic' },
        { token: 'keyword', foreground: 'cba6f7' },
        { token: 'string', foreground: 'a6e3a1' },
        { token: 'number', foreground: 'fab387' },
        { token: 'type', foreground: 'f9e2af' },
        { token: 'function', foreground: '89b4fa' },
        { token: 'variable', foreground: 'cdd6f4' },
        { token: 'operator', foreground: '89dceb' },
        { token: 'delimiter', foreground: '9399b2' },
    ],
    colors: {
        'editor.background': '#1e1e2e',
        'editor.foreground': '#cdd6f4',
        'editor.lineHighlightBackground': '#2a2a3d',
        'editor.selectionBackground': '#45475a',
        'editor.inactiveSelectionBackground': '#313147',
        'editorCursor.foreground': '#89b4fa',
        'editorLineNumber.foreground': '#6c7086',
        'editorLineNumber.activeForeground': '#cdd6f4',
        'editorIndentGuide.background': '#313147',
        'editorIndentGuide.activeBackground': '#45475a',
        'editorWidget.background': '#252536',
        'editorWidget.border': '#333350',
        'minimap.background': '#1e1e2e',
        'scrollbarSlider.background': '#31314780',
        'scrollbarSlider.hoverBackground': '#45475a80',
    },
});

const LANG = {py:'python',js:'javascript',ts:'typescript',jsx:'javascript',tsx:'typescript',json:'json',md:'markdown',html:'html',css:'css',yaml:'yaml',yml:'yaml',sh:'shell',bash:'shell',rs:'rust',go:'go',java:'java',rb:'ruby',c:'c',cpp:'cpp',h:'c',hpp:'cpp',toml:'ini',cfg:'ini',txt:'plaintext'};
const BASE = new URL('./', location.href).href;
const api = (path, opts) => fetch(BASE + path, opts).then(r => r.json());

const cl = {};

// --- File Tree Store ---

const FILE_ICONS = {
    py: ['Py', '#4584b6'], js: ['JS', '#f7df1e'], ts: ['TS', '#3178c6'],
    jsx: ['JX', '#61dafb'], tsx: ['TX', '#3178c6'], json: ['{}', '#f9e2af'],
    html: ['<>', '#e34c26'], css: ['#', '#264de4'], md: ['M', '#6c7086'],
    yaml: ['Y', '#cb171e'], yml: ['Y', '#cb171e'], toml: ['T', '#9c4121'],
    sh: ['$', '#a6e3a1'], bash: ['$', '#a6e3a1'], bat: ['$', '#a6e3a1'],
    rs: ['Rs', '#dea584'], go: ['Go', '#00add8'], java: ['Jv', '#b07219'],
    rb: ['Rb', '#cc342d'], c: ['C', '#555555'], cpp: ['C+', '#f34b7d'],
    h: ['H', '#555555'], hpp: ['H+', '#f34b7d'], txt: ['T', '#6c7086'],
    xml: ['<>', '#e34c26'], svg: ['Sv', '#ffb13b'], png: ['Im', '#a580e2'],
    jpg: ['Im', '#a580e2'], gif: ['Im', '#a580e2'], sql: ['SQ', '#e38c00'],
    env: ['Ev', '#f38ba8'], cfg: ['Cf', '#6c7086'], ini: ['In', '#6c7086'],
    lock: ['Lk', '#6c7086'], gitignore: ['Gi', '#f05033'],
};
function fileIcon(name) {
    const ext = name.includes('.') ? name.split('.').pop() : '';
    const dotName = name.startsWith('.') ? name.slice(1) : '';
    const entry = FILE_ICONS[ext] || FILE_ICONS[dotName];
    if (entry) return html`<span class=${cl.fileIcon} style=${{color: entry[1]}}>${entry[0]}</span>`;
    return html`<span class=${cl.fileIcon} style=${{color: 'var(--text-dim)'}}>F</span>`;
}

const tree = {
    expanded: new Set(persist.get('tree.expanded', [])),
    children: {},
    _listener: null,

    isOpen(path) { return this.expanded.has(path); },

    async toggle(path) {
        if (this.expanded.has(path)) {
            this.expanded.delete(path);
        } else {
            this.expanded.add(path);
            if (!this.children[path]) await this._fetch(path);
        }
        persist.set('tree.expanded', [...this.expanded]);
        this._notify();
    },

    async _fetch(path) {
        const data = await api(`api/files?path=${encodeURIComponent(path)}`);
        if (!data.error) {
            this.children[path] = data.entries.sort((a, b) =>
                a.is_dir !== b.is_dir ? (a.is_dir ? -1 : 1) : a.name.localeCompare(b.name));
        } else {
            this.expanded.delete(path);
            delete this.children[path];
        }
    },

    async refresh(changedPaths) {
        const dirs = new Set();
        for (const p of changedPaths) {
            const dir = p.substring(0, p.lastIndexOf('/')) || '/';
            if (this.expanded.has(dir)) dirs.add(dir);
        }
        await Promise.all([...dirs].map(d => this._fetch(d)));
        this._notify();
    },

    async restoreExpanded() {
        await Promise.all([...this.expanded].map(p => this._fetch(p)));
        this._notify();
    },

    _notify() { this._listener?.(); },
};

// --- File Tree Components ---

function DirNode({ path, name, depth, onOpen }) {
    const open = tree.isOpen(path);
    const children = tree.children[path];
    const pad = (8 + depth * 8) + 'px';
    return html`<div>
        <div class=${cl.node} style=${{paddingLeft: pad}} onClick=${() => tree.toggle(path)}>
            <span class=${cl.chevron}>${open ? '\u25BE' : '\u25B8'}</span><span>${name}</span>
        </div>
        ${open && children && children.map(e => e.is_dir
            ? html`<${DirNode} key=${e.path} path=${e.path} name=${e.name} depth=${depth + 1} onOpen=${onOpen} />`
            : html`<div class=${cl.node} style=${{paddingLeft: (8 + (depth+1) * 8) + 'px'}}
                        onClick=${() => onOpen(e.path, e.name)}>
                        <span class=${cl.chevron}></span>${fileIcon(e.name)}<span>${e.name}</span></div>`
        )}
    </div>`;
}

class FileTree extends Component {
    componentDidMount() {
        tree._listener = () => this.forceUpdate();
        tree.restoreExpanded();
    }
    componentWillUnmount() { tree._listener = null; }
    render({ rootPath, onOpen }) {
        return html`<${DirNode} path=${rootPath} name=${rootPath} depth=${0} onOpen=${onOpen} />`;
    }
}

// --- Editor ---

class Editor extends Component {
    constructor(props) {
        super(props);
        this.state = { tabs: [], activeIdx: -1 };
        this._editor = null;
    }
    componentDidMount() {
        this._editor = monaco.editor.create(this._el, {
            value: '', language: 'plaintext', theme: 'catppuccin',
            minimap: { enabled: true }, fontSize: 13,
            automaticLayout: true, scrollBeyondLastLine: false,
        });
        const saved = persist.get('tabs', []);
        if (saved.length) this._restoreTabs(saved);
    }
    async _restoreTabs(paths) {
        for (const p of paths) {
            const name = p.split('/').pop();
            await this.openFile(p, name);
        }
    }
    _persistTabs() {
        persist.set('tabs', this.state.tabs.map(t => t.path));
        persist.set('activeTab', this.state.tabs[this.state.activeIdx]?.path || null);
    }
    async openFile(path, name) {
        const { tabs } = this.state;
        const idx = tabs.findIndex(t => t.path === path);
        if (idx >= 0) { this._activate(idx); return; }

        const ext = path.split('.').pop();
        const model = monaco.editor.createModel('Loading...', LANG[ext] || 'plaintext');
        const tab = { path, name, model, saved: '', dirty: false, diskChanged: false };
        const next = [...tabs, tab];
        this.setState({ tabs: next, activeIdx: next.length - 1 }, () => this._persistTabs());
        this._editor.setModel(model);

        const data = await api(`api/file?path=${encodeURIComponent(path)}`);
        if (data.error) { model.setValue(`Error: ${data.error}`); return; }
        tab.saved = data.content;
        model.setValue(data.content);
        model.onDidChangeContent(() => {
            const was = tab.dirty;
            tab.dirty = model.getValue() !== tab.saved;
            if (tab.dirty !== was) this.forceUpdate();
        });
    }
    _activate(idx) {
        this.setState({ activeIdx: idx }, () => this._persistTabs());
        this._editor.setModel(this.state.tabs[idx].model);
    }
    _close(idx) {
        const { tabs, activeIdx } = this.state;
        tabs[idx].model.dispose();
        const next = tabs.filter((_, i) => i !== idx);
        let act = activeIdx;
        if (idx === activeIdx) {
            act = Math.min(idx, next.length - 1);
            if (act >= 0) this._editor.setModel(next[act].model);
        } else if (idx < activeIdx) act--;
        this.setState({ tabs: next, activeIdx: act }, () => this._persistTabs());
    }
    async save() {
        const { tabs, activeIdx } = this.state;
        if (activeIdx < 0) return;
        const tab = tabs[activeIdx];
        if (tab.diskChanged && !confirm('File was changed on disk. Overwrite?')) return;
        const content = tab.model.getValue();
        const data = await api('api/file', {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ path: tab.path, content }),
        });
        if (data.error) return;
        tab.saved = content;
        tab.dirty = false;
        tab.diskChanged = false;
        this.forceUpdate();
    }
    handleFilesChanged(paths) {
        const changed = new Set(paths);
        for (const tab of this.state.tabs) {
            if (!changed.has(tab.path)) continue;
            if (tab.dirty) { tab.diskChanged = true; this.forceUpdate(); }
            else api(`api/file?path=${encodeURIComponent(tab.path)}`).then(d => {
                if (d.error) return;
                tab.saved = d.content;
                tab.model.setValue(d.content);
            });
        }
    }
    render(_, { tabs, activeIdx }) {
        return html`
            <div class=${cl.editor}>
                <div class=${cl.tabs}>
                    ${tabs.map((t, i) => html`
                        <div class="${cl.tab}${i === activeIdx ? ' active' : ''}${t.diskChanged ? ' disk' : t.dirty ? ' dirty' : ''}"
                             onClick=${() => this._activate(i)}>
                            <span class="dot"></span><span>${t.name}</span>
                            <span class="close" onClick=${e => { e.stopPropagation(); this._close(i); }}>\u00d7</span>
                        </div>`)}
                </div>
                ${!tabs.length && html`<div class=${cl.welcome}>Select a file to view</div>`}
                <div class=${cl.monacoWrap} style=${{display: tabs.length ? 'block' : 'none'}}
                     ref=${el => this._el = el}></div>
            </div>`;
    }
}

// --- App ---

class App extends Component {
    constructor(props) {
        super(props);
        this.state = { connected: false, rootPath: '/' };
        this._chat = null;
        this._editor = null;
    }

    componentDidMount() {
        this._connect();
        api('api/config').then(c => { if (c.root_path) this.setState({ rootPath: c.root_path }); });
        document.addEventListener('keydown', e => {
            if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); this._editor?.save(); }
        });
    }

    _connect() {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        this._ws = new WebSocket(proto + location.host + location.pathname + 'ws');
        this._ws.onopen = () => this.setState({ connected: true });
        this._ws.onclose = () => { this.setState({ connected: false }); setTimeout(() => this._connect(), 2000); };
        this._ws.onerror = () => this._ws.close();
        this._ws.onmessage = e => {
            const msg = JSON.parse(e.data);
            if (msg.type === 'transport') this._chat?.handleMessage(msg);
            else if (msg.type === 'files_changed') {
                this._editor?.handleFilesChanged(msg.paths);
                if (msg.tree) tree.refresh(msg.paths);
            }
        };
    }

    send(msg) {
        if (this._ws?.readyState === WebSocket.OPEN)
            this._ws.send(JSON.stringify(msg));
    }

    render(_, { connected, rootPath }) {
        return html`
            <div class=${cl.app}>
                <div class=${cl.sidebar}>
                    <div class=${cl.sidebarHdr}>Explorer</div>
                    <div class=${cl.tree}>
                        <${FileTree} rootPath=${rootPath}
                                     onOpen=${(p, n) => this._editor?.openFile(p, n)} />
                    </div>
                </div>
                <${Resizer} side="left" persistKey="sidebar" />
                <${Editor} ref=${c => this._editor = c} />
                <${Resizer} side="right" persistKey="chat" />
                <${Chat} ref=${c => this._chat = c} app=${this} connected=${connected} />
            </div>`;
    }
}

render(html`<${App} />`, document.body);

// --- styles ---

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
cl.node = css`
  display: flex; align-items: center; padding: 2px 8px; cursor: pointer;
  white-space: nowrap; user-select: none; font-size: 13px;
  &:hover { background: var(--surface2); }
`;
cl.chevron = css`width: 12px; text-align: center; font-size: 10px; flex-shrink: 0; color: var(--text-dim);`;
cl.fileIcon = css`
  width: 18px; text-align: center; margin-right: 3px; flex-shrink: 0;
  font-size: 9px; font-weight: 700; font-family: monospace; letter-spacing: -0.5px;
`;

cl.editor = css`flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0;`;
cl.tabs = css`
  display: flex; background: var(--surface); border-bottom: 1px solid var(--border);
  min-height: 35px; overflow-x: auto;
`;
cl.tab = css`
  display: flex; align-items: center; padding: 0 12px; height: 35px; cursor: pointer;
  border-right: 1px solid var(--border); font-size: 13px; white-space: nowrap;
  color: var(--text-dim); gap: 6px;
  &.active { background: var(--bg); color: var(--text); }
  & .close { font-size: 14px; opacity: 0.5; cursor: pointer; }
  & .close:hover { opacity: 1; }
  & .dot { display: none; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  &.dirty .dot { display: block; background: var(--text-dim); }
  &.disk .dot { display: block; background: var(--warn); }
`;
cl.welcome = css`
  flex: 1; display: flex; align-items: center; justify-content: center;
  color: var(--text-dim); font-size: 16px;
`;
cl.monacoWrap = css`flex: 1;`;
