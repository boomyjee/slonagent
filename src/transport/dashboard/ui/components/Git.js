import { html, Component, css } from '../lib.js';

const cl = {};
const BASE = new URL('./', location.href).href;
const api = (path, opts) => fetch(BASE + path, opts).then(r => r.json());

export class Git extends Component {
    constructor(props) {
        super(props);
        this.state = {
            branch: '',
            files: [],
            message: '',
            expanded: {},   // path -> diff text or null while loading
            error: '',
        };
    }

    componentDidMount() {
        this._refresh();
    }

    componentDidUpdate(prev) {
        // Auto-refresh when watcher signals files_changed.
        if (prev.refreshKey !== this.props.refreshKey) this._refresh();
    }

    async _refresh() {
        const data = await api(`api/git/status?path=${encodeURIComponent(this.props.path)}`);
        if (data.error) { this.setState({ error: data.error }); return; }
        this.setState({ branch: data.branch, files: data.files, error: '' });
    }

    async _toggleStaged(file, staged) {
        await api('api/git/stage', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ path: this.props.path, file, staged }),
        });
        this._refresh();
    }

    async _toggleDiff(file, staged) {
        const { expanded } = this.state;
        if (file in expanded) {
            const next = { ...expanded };
            delete next[file];
            this.setState({ expanded: next });
            return;
        }
        this.setState({ expanded: { ...expanded, [file]: null } });
        const q = `path=${encodeURIComponent(this.props.path)}&file=${encodeURIComponent(file)}&staged=${staged ? 1 : 0}`;
        const data = await api(`api/git/diff?${q}`);
        this.setState(({ expanded: e }) => ({ expanded: { ...e, [file]: data.diff || '(no diff)' } }));
    }

    async _stageAll() {
        const unstaged = this.state.files.filter(f => !f.staged);
        await Promise.all(unstaged.map(f => api('api/git/stage', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ path: this.props.path, file: f.file, staged: true }),
        })));
        this._refresh();
    }

    async _commit() {
        const message = this.state.message.trim();
        if (!message) return;
        const data = await api('api/git/commit', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ path: this.props.path, message }),
        });
        if (data.error || data.status !== 'ok') {
            this.setState({ error: data.error || data.output || 'commit failed' });
            return;
        }
        this.setState({ message: '', error: '' });
        this._refresh();
    }

    render(_, { branch, files, message, expanded, error }) {
        const stagedCount = files.filter(f => f.staged).length;
        return html`<div class=${cl.wrap}>
            <div class=${cl.bar}>
                <span class=${cl.branch}>⎇ ${branch || '(detached)'}</span>
                <button class=${cl.btn} onClick=${() => this._stageAll()}
                        disabled=${!files.some(f => !f.staged)}>Stage all</button>
                <button class=${cl.btn} onClick=${() => this._refresh()}>↻</button>
            </div>
            <div class=${cl.commitRow}>
                <input class=${cl.msg} type="text" placeholder="commit message"
                       value=${message}
                       onInput=${e => this.setState({ message: e.target.value })}
                       onKeyDown=${e => { if (e.key === 'Enter') this._commit(); }} />
                <button class=${cl.btn} onClick=${() => this._commit()}
                        disabled=${stagedCount === 0 || !message.trim()}>Commit</button>
            </div>
            ${error && html`<div class=${cl.error}>${error}</div>`}
            <div class=${cl.list}>
                ${!files.length && html`<div class=${cl.empty}>working tree clean</div>`}
                ${files.map(f => html`
                    <div key=${f.file}>
                        <div class=${cl.row}>
                            <input type="checkbox" checked=${f.staged}
                                   class=${f.partial ? cl.partial : ''}
                                   onChange=${e => this._toggleStaged(f.file, e.target.checked)} />
                            <span class=${cl.state}>${f.state}</span>
                            <span class=${cl.file} onClick=${() => this._toggleDiff(f.file, f.staged && !f.partial)}>
                                ${f.old_file ? `${f.old_file} → ${f.file}` : f.file}
                            </span>
                        </div>
                        ${f.file in expanded && html`
                            <pre class=${cl.diff}>${expanded[f.file] === null ? 'loading…' : renderDiff(expanded[f.file])}</pre>`}
                    </div>`)}
            </div>
        </div>`;
    }
}

// Inline-color a unified diff by line prefix. Returns an array of vnodes.
function renderDiff(text) {
    return text.split('\n').map((line, i) => {
        let cls = '';
        if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ') || line.startsWith('index ')) cls = 'meta';
        else if (line.startsWith('@@')) cls = 'hunk';
        else if (line.startsWith('+')) cls = 'add';
        else if (line.startsWith('-')) cls = 'del';
        return html`<div key=${i} class=${cls}>${line || ' '}</div>`;
    });
}

cl.wrap = css`flex: 1; display: flex; flex-direction: column; overflow: hidden; min-height: 0;`;
cl.bar = css`
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; background: var(--surface);
  border-bottom: 1px solid var(--border); flex-shrink: 0;
`;
cl.branch = css`font-size: 12px; color: var(--text); flex: 1;`;
cl.commitRow = css`
  display: flex; gap: 6px; padding: 6px 10px; border-bottom: 1px solid var(--border);
  background: var(--surface); flex-shrink: 0;
`;
cl.msg = css`
  flex: 1; background: var(--bg); color: var(--text);
  border: 1px solid var(--border); padding: 6px 8px; font-size: 12px; outline: none;
  &:focus { border-color: var(--accent); }
`;
cl.btn = css`
  background: var(--surface2); color: var(--text); border: 1px solid var(--border);
  padding: 4px 10px; cursor: pointer; font-size: 12px;
  &:hover:not(:disabled) { background: var(--surface3); }
  &:disabled { opacity: 0.5; cursor: default; }
`;
cl.error = css`
  padding: 6px 10px; background: var(--surface2); color: var(--red);
  font-size: 12px; white-space: pre-wrap; border-bottom: 1px solid var(--border);
`;
cl.list = css`flex: 1; overflow: auto;`;
cl.empty = css`padding: 16px; color: var(--text-dim); font-size: 12px; font-style: italic;`;
cl.row = css`
  display: flex; align-items: center; gap: 8px; padding: 4px 10px;
  font-size: 12px; border-bottom: 1px solid var(--border);
  &:hover { background: var(--surface2); }
`;
cl.partial = css`opacity: 0.5;`;
cl.state = css`
  font-family: monospace; font-size: 11px; color: var(--accent);
  width: 28px; flex-shrink: 0;
`;
cl.file = css`flex: 1; cursor: pointer; color: var(--text); &:hover { text-decoration: underline; }`;
cl.diff = css`
  margin: 0; padding: 8px 12px; background: var(--bg); font-size: 11px; line-height: 1.4;
  white-space: pre; overflow-x: auto; border-bottom: 1px solid var(--border);
  & .meta { color: var(--text-dim); }
  & .hunk { color: var(--accent); }
  & .add { color: var(--green); }
  & .del { color: var(--red); }
`;
