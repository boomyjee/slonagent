import { html, Component, css, persist } from '../lib.js';
import { api, currentRoot } from './api.js';

const cl = {};

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

// Tree state scoped by root: paths from /Users/me/projectA mean nothing
// under /Users/me/projectB. setScope() rebinds expanded to the per-root
// persistence slot. Initial value is `null` (uninitialised) so the first
// call — even with the empty default-root key — runs the load.
let _scope = null;
const _expandedKey = () => `tree.expanded:${_scope}`;

const tree = {
    expanded: new Set(),
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
        persist.set(_expandedKey(), [...this.expanded]);
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

function DirNode({ entry, depth, onOpen, onContextMenu, onFileClick, selected, dnd, dragOver }) {
    const open = tree.isOpen(entry.path);
    const children = tree.children[entry.path];
    const pad = (8 + depth * 8) + 'px';
    const dirHover = dragOver === entry.path ? ' ' + cl.dropTarget : '';
    return html`<div>
        <div class="${cl.node}${dirHover}"
             style=${{paddingLeft: pad}}
             draggable="true"
             onDragStart=${ev => depth > 0 ? dnd.start(ev, entry) : ev.preventDefault()}
             onDragOver=${ev => dnd.over(ev, entry)}
             onDragLeave=${ev => dnd.leave(ev, entry)}
             onDrop=${ev => dnd.drop(ev, entry)}
             onClick=${() => tree.toggle(entry.path)}
             onContextMenu=${e => onContextMenu(e, entry)}>
            <span class=${cl.chevron}>${open ? '▾' : '▸'}</span><span>${entry.name}</span>
        </div>
        ${open && children && children.map(e => e.is_dir
            ? html`<${DirNode} key=${e.path} entry=${e} depth=${depth + 1} onOpen=${onOpen} onContextMenu=${onContextMenu} onFileClick=${onFileClick} selected=${selected} dnd=${dnd} dragOver=${dragOver} />`
            : html`<div class="${cl.node}${selected.has(e.path) ? ' ' + cl.selected : ''}"
                        style=${{paddingLeft: (8 + (depth+1) * 8) + 'px'}}
                        draggable="true"
                        onDragStart=${ev => dnd.start(ev, e)}
                        onClick=${ev => onFileClick(ev, e)}
                        onContextMenu=${ev => onContextMenu(ev, e)}>
                        <span class=${cl.chevron}></span>${fileIcon(e.name)}<span>${e.name}</span></div>`
        )}
    </div>`;
}

export class FileTree extends Component {
    constructor(props) {
        super(props);
        this.state = { menu: null, selected: new Set(), dragOver: null };
    }
    componentDidMount() {
        tree._listener = () => this.forceUpdate();
        if (this.props.rootPath) tree.restoreExpanded();
        document.addEventListener('click', this._closeMenu);
    }
    componentDidUpdate(prev) {
        if (!prev.rootPath && this.props.rootPath) tree.restoreExpanded();
    }
    componentWillUnmount() {
        tree._listener = null;
        document.removeEventListener('click', this._closeMenu);
    }

    _closeMenu = () => { if (this.state.menu) this.setState({ menu: null }); };

    _onFileClick = (e, entry) => {
        // Ctrl/Cmd-click — toggle selection. Plain click — clear selection
        // and open file as before.
        if (e.ctrlKey || e.metaKey) {
            const selected = new Set(this.state.selected);
            if (selected.has(entry.path)) selected.delete(entry.path);
            else selected.add(entry.path);
            this.setState({ selected });
            return;
        }
        if (this.state.selected.size) this.setState({ selected: new Set() });
        this.props.onOpen(entry.path, entry.name);
    };

    _onContextMenu = (e, entry) => {
        const isRoot = entry.path === this.props.rootPath;
        const selected = this.state.selected;
        // Multi-select context menu: только когда >1 выделенных и кликнули по
        // одному из них (по файлу не из выборки — игнорим выборку, обычное меню).
        if (selected.size > 1 && selected.has(entry.path)) {
            e.preventDefault();
            this.setState({ menu: { x: e.clientX, y: e.clientY, items: [
                { label: `Delete ${selected.size} selected`, action: () => this._deleteSelected() },
            ]}});
            return;
        }
        const items = [];
        if (entry.is_dir) {
            items.push({ label: 'New file', action: () => this._create(entry, 'file') });
            items.push({ label: 'New folder', action: () => this._create(entry, 'folder') });
        }
        if (!isRoot) {
            items.push({ label: 'Rename', action: () => this._rename(entry) });
            items.push({ label: 'Delete', action: () => this._delete(entry) });
        }
        if (entry.is_dir && entry.has_git) {
            items.push({ label: 'Open git commit', action: () => this.props.onOpenGit?.(entry.path, entry.name) });
        }
        if (entry.url) {
            items.push({ label: 'Open location', action: () => {
                this.props.onOpenUrl?.(entry.url);
            }});
        }
        if (!entry.is_dir) {
            items.push({ label: 'Git blame', action: () => this.props.onOpenGitBlame?.(entry.path, entry.name) });
            items.push({ label: 'Download', action: () => this._download(entry) });
        }
        if (isRoot && this.props.onChangeRoot) {
            items.push({ label: 'Change Root…', action: () => this.props.onChangeRoot() });
        }
        if (!items.length) return;
        e.preventDefault();
        this.setState({ menu: { x: e.clientX, y: e.clientY, items } });
    };

    _onDragStart = (e, entry) => {
        // Если тащим элемент из выборки — тащим всю выборку, иначе только этот.
        const sel = this.state.selected;
        const paths = sel.has(entry.path) ? [...sel] : [entry.path];
        e.dataTransfer.setData("application/x-tree-paths", JSON.stringify(paths));
        e.dataTransfer.effectAllowed = "copyMove";
    };

    _onDragOver = (e, dirEntry) => {
        if (!dirEntry.is_dir) return;
        if (!e.dataTransfer.types.includes("application/x-tree-paths")) return;
        e.preventDefault();
        // Ctrl/Cmd → copy, иначе move. Влияет на курсор.
        e.dataTransfer.dropEffect = (e.ctrlKey || e.metaKey) ? "copy" : "move";
        if (this.state.dragOver !== dirEntry.path) {
            this.setState({ dragOver: dirEntry.path });
        }
    };

    _onDragLeave = (e, dirEntry) => {
        if (this.state.dragOver === dirEntry.path) {
            this.setState({ dragOver: null });
        }
    };

    _onDrop = async (e, dirEntry) => {
        if (!dirEntry.is_dir) return;
        e.preventDefault();
        this.setState({ dragOver: null });
        const raw = e.dataTransfer.getData("application/x-tree-paths");
        if (!raw) return;
        let paths;
        try { paths = JSON.parse(raw); } catch { return; }
        // Не дропаем папку саму в себя или внутрь себя.
        const blocked = paths.filter(p => dirEntry.path === p || dirEntry.path.startsWith(p + "/"));
        if (blocked.length) return;
        // Не дропаем в родительскую папку (no-op).
        const usable = paths.filter(p => (p.substring(0, p.lastIndexOf("/")) || "/") !== dirEntry.path);
        if (!usable.length) return;
        const op = (e.ctrlKey || e.metaKey) ? "copy" : "move";
        const data = await api(`api/file/${op}`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ paths: usable, dest_dir: dirEntry.path }),
        });
        if (data.error) { alert(data.error); return; }
        const errs = data.errors || {};
        if (Object.keys(errs).length) {
            alert(`Some failed:\n${Object.entries(errs).map(([p,err]) => `${p}: ${err}`).join('\n')}`);
        }
        // tree.refresh достаёт parent из каждого path: src → исходные,
        // dest sentinel → целевая папка.
        await tree.refresh([dirEntry.path + '/_', ...usable]);
        this.setState({ selected: new Set() });
    };

    async _deleteSelected() {
        const paths = [...this.state.selected];
        if (!confirm(`Delete ${paths.length} files?`)) return;
        const errors = [];
        for (const path of paths) {
            const data = await api(`api/file?path=${encodeURIComponent(path)}`, { method: 'DELETE' });
            if (data.error) errors.push(`${path}: ${data.error}`);
        }
        if (errors.length) alert(`Failed:\n${errors.join('\n')}`);
        await tree.refresh(paths);
        this.setState({ selected: new Set() });
    }

    async _create(entry, type) {
        const name = prompt(`New ${type} name:`);
        if (!name) return;
        const path = entry.path.replace(/\/$/, '') + '/' + name;
        const data = await api('api/file/create', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ path, type }),
        });
        if (data.error) { alert(data.error); return; }
        // Expand parent if collapsed (toggle does its own fetch+notify),
        // otherwise just refetch its listing.
        if (!tree.isOpen(entry.path)) await tree.toggle(entry.path);
        else await tree.refresh([path]);
    }

    async _rename(entry) {
        const name = prompt('New name:', entry.name);
        if (!name || name === entry.name) return;
        const data = await api('api/file/rename', {
            method: 'PATCH', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ path: entry.path, name }),
        });
        if (data.error) { alert(data.error); return; }
        await tree.refresh([entry.path]);
    }

    _download(entry) {
        const root = currentRoot();
        const url = `api/file/raw?path=${encodeURIComponent(entry.path)}${root ? `&root=${encodeURIComponent(root)}` : ''}`;
        const a = document.createElement('a');
        a.href = url;
        a.download = entry.name;
        document.body.appendChild(a);
        a.click();
        a.remove();
    }

    async _delete(entry) {
        if (!confirm(`Delete ${entry.name}?`)) return;
        const data = await api(`api/file?path=${encodeURIComponent(entry.path)}`, { method: 'DELETE' });
        if (data.error) { alert(data.error); return; }
        await tree.refresh([entry.path]);
    }

    render({ rootPath, rootName, onOpen }, { menu, selected, dragOver }) {
        if (!rootPath) return html`<div class=${cl.empty}>No root</div>`;
        const rootHasGit = (tree.children[rootPath] || []).some(e => e.name === '.git');
        const root = { path: rootPath, name: rootName || rootPath, is_dir: true, has_git: rootHasGit };
        const dnd = {
            start: this._onDragStart, over: this._onDragOver,
            leave: this._onDragLeave, drop: this._onDrop,
        };
        return html`<div class=${cl.treeRoot}>
            <${DirNode} entry=${root} depth=${0} onOpen=${onOpen}
                        onContextMenu=${this._onContextMenu}
                        onFileClick=${this._onFileClick}
                        selected=${selected}
                        dnd=${dnd} dragOver=${dragOver} />
            ${menu && html`
                <div class=${cl.menu} style=${{left: menu.x + 'px', top: menu.y + 'px'}}>
                    ${menu.items.map(i => html`
                        <div class=${cl.menuItem} onClick=${() => { i.action(); this._closeMenu(); }}>
                            ${i.label}
                        </div>`)}
                </div>`}
        </div>`;
    }
}

// Imperative entry for the websocket "files_changed" event.
export const refreshTree = (paths) => tree.refresh(paths);

// Bind tree state to a specific root (e.g. hash of root host path).
// Pulls saved expansions for that scope; the children cache is dropped
// since paths repeat across roots but mean different files.
export function setTreeScope(key) {
    if (key === _scope) return;
    _scope = key;
    tree.expanded = new Set(persist.get(_expandedKey(), []));
    tree.children = {};
    tree._notify();
    tree.restoreExpanded();
}

// True if any cached ancestor of `path` is a git repo. Two signals:
//   - `entry.has_git` set by the server on the parent's listing
//   - a `.git` child visible in the ancestor's own listing
// The second covers the root (no parent listing) and folders whose listing
// is loaded but whose parent's isn't.
export function isInGitRepo(path) {
    let cur = path;
    while (cur.includes('/')) {
        cur = cur.substring(0, cur.lastIndexOf('/')) || '/';
        if ((tree.children[cur] || []).some(e => e.name === '.git')) return true;
        if (cur !== '/') {
            const parent = cur.substring(0, cur.lastIndexOf('/')) || '/';
            const entry = (tree.children[parent] || []).find(e => e.path === cur);
            if (entry?.has_git) return true;
        }
        if (cur === '/') break;
    }
    return false;
}

cl.treeRoot = css`
  user-select: none; -webkit-user-select: none; -webkit-touch-callout: none;
`;
cl.node = css`
  display: flex; align-items: center; padding: 2px 8px; cursor: pointer;
  white-space: nowrap; font-size: 13px;
  user-select: none; -webkit-user-select: none; -webkit-touch-callout: none;
  &:hover { background: var(--surface2); }
`;
cl.chevron = css`width: 12px; text-align: center; font-size: 10px; flex-shrink: 0; color: var(--text-dim);`;
cl.fileIcon = css`
  width: 18px; text-align: center; margin-right: 3px; flex-shrink: 0;
  font-size: 9px; font-weight: 700; font-family: monospace; letter-spacing: -0.5px;
`;
cl.empty = css`padding: 16px; color: var(--text-dim); font-size: 12px; font-style: italic;`;
cl.menu = css`
  position: fixed; z-index: 100; min-width: 160px;
  background: var(--surface2); border: 1px solid var(--border);
  box-shadow: 0 4px 12px rgba(0,0,0,0.4); padding: 4px 0;
  user-select: none; -webkit-user-select: none; -webkit-touch-callout: none;
`;
cl.menuItem = css`
  padding: 6px 12px; font-size: 12px; color: var(--text); cursor: pointer;
  user-select: none; -webkit-user-select: none;
  &:hover { background: var(--accent); color: #1e1e2e; }
`;
cl.selected = css`
  background: var(--accent);
  color: #1e1e2e;
  &:hover { background: var(--accent); }
`;
cl.dropTarget = css`
  outline: 2px solid var(--accent);
  outline-offset: -2px;
`;
