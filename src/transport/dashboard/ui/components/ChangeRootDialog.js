import { html, Component, css } from '../lib.js';
import { Dialog } from './common/Dialog.js';
import { api } from './api.js';

const cl = {};

class ChangeRootContent extends Component {
    constructor(props) {
        super(props);
        this.state = { path: '', parent: null, entries: [], error: '', loading: true };
    }
    componentDidMount() {
        // Open the picker one level above the current root so siblings are
        // visible. Falls back to the drive list / "/" if root isn't set.
        const start = this.props.initialPath || '';
        const parent = start ? start.replace(/[\/\\]+$/, '').split(/[\/\\]/).slice(0, -1).join('/') : '';
        this._navigate(parent || start);
    }

    async _navigate(path) {
        this.setState({ loading: true, error: '' });
        const data = await api(`api/dirs?path=${encodeURIComponent(path || '')}`);
        if (data.error) { this.setState({ error: data.error, loading: false }); return; }
        this.setState({
            path: data.path, parent: data.parent, entries: data.entries || [],
            loading: false, error: '',
        });
    }

    _confirm(targetPath) {
        const root = targetPath ?? this.state.path;
        if (!root) return;
        this.props.onConfirm?.(root);
        Dialog.close();
    }

    render(_, { path, parent, entries, error, loading }) {
        return html`<div class=${cl.dialog}>
            <div class=${cl.header}>
                <button class=${cl.btn} onClick=${() => this._navigate(parent ?? '')}
                        disabled=${parent === null}>↑</button>
                <span class=${cl.path}>${path || '(drives)'}</span>
            </div>
            ${error && html`<div class=${cl.error}>${error}</div>`}
            <div class=${cl.list}>
                ${loading && html`<div class=${cl.empty}>loading…</div>`}
                ${!loading && !entries.length && html`<div class=${cl.empty}>(no subfolders)</div>`}
                ${entries.map(e => html`
                    <div key=${e.path} class=${cl.entry}
                         onClick=${() => this._navigate(e.path)}
                         onDblClick=${() => this._confirm(e.path)}>
                        ▸ ${e.name}
                    </div>`)}
            </div>
            <div class=${cl.footer}>
                <button class=${cl.btn} onClick=${() => Dialog.close()}>Cancel</button>
                <button class=${cl.btn} onClick=${() => this._confirm()}
                        disabled=${!path}>Use this folder</button>
            </div>
        </div>`;
    }
}

export function openChangeRootDialog({ initialPath, onConfirm }) {
    Dialog.open(html`<${ChangeRootContent} initialPath=${initialPath} onConfirm=${onConfirm} />`);
}

cl.dialog = css`
  width: 520px; height: 480px;
  display: flex; flex-direction: column;
`;
cl.header = css`
  display: flex; align-items: center; gap: 8px; padding: 8px 12px;
  border-bottom: 1px solid var(--border); flex-shrink: 0;
`;
cl.path = css`
  flex: 1; font-family: monospace; font-size: 12px; color: var(--text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
`;
cl.btn = css`
  background: var(--surface2); color: var(--text); border: 1px solid var(--border);
  padding: 4px 12px; cursor: pointer; font-size: 12px;
  &:hover:not(:disabled) { background: var(--surface3); }
  &:disabled { opacity: 0.5; cursor: default; }
`;
cl.error = css`
  padding: 6px 12px; color: var(--red); background: var(--surface2);
  font-size: 12px; border-bottom: 1px solid var(--border);
`;
cl.list = css`flex: 1; overflow: auto; padding: 4px 0;`;
cl.entry = css`
  padding: 4px 12px; cursor: pointer; font-size: 13px; color: var(--text);
  user-select: none;
  &:hover { background: var(--surface2); }
`;
cl.empty = css`padding: 16px; color: var(--text-dim); font-size: 12px; font-style: italic; text-align: center;`;
cl.footer = css`
  display: flex; justify-content: flex-end; gap: 8px; padding: 8px 12px;
  border-top: 1px solid var(--border); flex-shrink: 0;
`;
