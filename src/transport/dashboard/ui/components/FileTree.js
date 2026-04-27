import { html, Component, css, persist } from '../lib.js';

const cl = {};
const BASE = new URL('./', location.href).href;
const api = (path) => fetch(BASE + path).then(r => r.json());

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

function DirNode({ path, name, depth, onOpen }) {
    const open = tree.isOpen(path);
    const children = tree.children[path];
    const pad = (8 + depth * 8) + 'px';
    return html`<div>
        <div class=${cl.node} style=${{paddingLeft: pad}} onClick=${() => tree.toggle(path)}>
            <span class=${cl.chevron}>${open ? '▾' : '▸'}</span><span>${name}</span>
        </div>
        ${open && children && children.map(e => e.is_dir
            ? html`<${DirNode} key=${e.path} path=${e.path} name=${e.name} depth=${depth + 1} onOpen=${onOpen} />`
            : html`<div class=${cl.node} style=${{paddingLeft: (8 + (depth+1) * 8) + 'px'}}
                        onClick=${() => onOpen(e.path, e.name)}>
                        <span class=${cl.chevron}></span>${fileIcon(e.name)}<span>${e.name}</span></div>`
        )}
    </div>`;
}

export class FileTree extends Component {
    componentDidMount() {
        tree._listener = () => this.forceUpdate();
        if (this.props.rootPath) tree.restoreExpanded();
    }
    componentDidUpdate(prev) {
        if (!prev.rootPath && this.props.rootPath) tree.restoreExpanded();
    }
    componentWillUnmount() { tree._listener = null; }
    render({ rootPath, onOpen }) {
        if (!rootPath) return html`<div class=${cl.empty}>No sandbox</div>`;
        return html`<${DirNode} path=${rootPath} name=${rootPath} depth=${0} onOpen=${onOpen} />`;
    }
}

// Imperative entry for the websocket "files_changed" event.
export const refreshTree = (paths) => tree.refresh(paths);

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
cl.empty = css`padding: 16px; color: var(--text-dim); font-size: 12px; font-style: italic;`;
