import { render, html, Component, css } from './lib.js';
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

const LANG = {py:'python',js:'javascript',ts:'typescript',jsx:'javascript',tsx:'typescript',json:'json',md:'markdown',html:'html',css:'css',yaml:'yaml',yml:'yaml',sh:'shell',bash:'shell',rs:'rust',go:'go',java:'java',rb:'ruby',c:'c',cpp:'cpp',h:'c',hpp:'cpp',toml:'ini',cfg:'ini',txt:'plaintext'};
const BASE = new URL('./', location.href).href;
const api = (path, opts) => fetch(BASE + path, opts).then(r => r.json());

const cl = {};

// --- File Tree ---

class DirNode extends Component {
    constructor(props) {
        super(props);
        this.state = { open: false, children: null };
    }
    async toggle() {
        if (!this.state.open && !this.state.children) {
            const data = await api(`api/files?path=${encodeURIComponent(this.props.path)}`);
            if (!data.error) {
                const sorted = data.entries.sort((a, b) =>
                    a.is_dir !== b.is_dir ? (a.is_dir ? -1 : 1) : a.name.localeCompare(b.name));
                this.setState({ children: sorted, open: true });
                return;
            }
        }
        this.setState(s => ({ open: !s.open }));
    }
    render({ name, depth, onOpen }, { open, children }) {
        return html`<div>
            <div class=${cl.node} style=${{paddingLeft: (12 + depth * 16) + 'px'}} onClick=${() => this.toggle()}>
                <span class=${cl.icon}>${open ? '\u25BC' : '\u25B6'}</span><span>${name}</span>
            </div>
            ${open && children && children.map(e => e.is_dir
                ? html`<${DirNode} key=${e.path} path=${e.path} name=${e.name} depth=${depth + 1} onOpen=${onOpen} />`
                : html`<div class=${cl.node} style=${{paddingLeft: (12 + (depth+1) * 16) + 'px'}}
                            onClick=${() => onOpen(e.path, e.name)}><span class=${cl.icon}></span><span>${e.name}</span></div>`
            )}
        </div>`;
    }
}

class FileTree extends Component {
    render({ rootPath, onOpen, treeKey }) {
        return html`<${DirNode} key=${treeKey} path=${rootPath} name=${rootPath} depth=${0} onOpen=${onOpen} />`;
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
            value: '', language: 'plaintext', theme: 'vs-dark',
            minimap: { enabled: true }, fontSize: 13,
            automaticLayout: true, scrollBeyondLastLine: false,
        });
    }
    async openFile(path, name) {
        const { tabs } = this.state;
        const idx = tabs.findIndex(t => t.path === path);
        if (idx >= 0) { this._activate(idx); return; }

        const ext = path.split('.').pop();
        const model = monaco.editor.createModel('Loading...', LANG[ext] || 'plaintext');
        const tab = { path, name, model, saved: '', dirty: false, diskChanged: false };
        const next = [...tabs, tab];
        this.setState({ tabs: next, activeIdx: next.length - 1 });
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
        this.setState({ activeIdx: idx });
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
        this.setState({ tabs: next, activeIdx: act });
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
        this.state = { connected: false, rootPath: '/', treeKey: 0 };
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
                if (msg.tree) this.setState(s => ({ treeKey: s.treeKey + 1 }));
            }
        };
    }

    send(msg) {
        if (this._ws?.readyState === WebSocket.OPEN)
            this._ws.send(JSON.stringify(msg));
    }

    render(_, { connected, rootPath, treeKey }) {
        return html`
            <div class=${cl.app}>
                <div class=${cl.sidebar}>
                    <div class=${cl.sidebarHdr}>Explorer</div>
                    <div class=${cl.tree}>
                        <${FileTree} rootPath=${rootPath} treeKey=${treeKey}
                                     onOpen=${(p, n) => this._editor?.openFile(p, n)} />
                    </div>
                </div>
                <${Resizer} side="left" />
                <${Editor} ref=${c => this._editor = c} />
                <${Resizer} side="right" />
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
cl.icon = css`width: 16px; text-align: center; margin-right: 4px; font-size: 12px; flex-shrink: 0;`;

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
