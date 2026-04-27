import { html, Component, css } from '../lib.js';

const cl = {};
const LANG = {py:'python',js:'javascript',ts:'typescript',jsx:'javascript',tsx:'typescript',json:'json',md:'markdown',html:'html',css:'css',yaml:'yaml',yml:'yaml',sh:'shell',bash:'shell',rs:'rust',go:'go',java:'java',rb:'ruby',c:'c',cpp:'cpp',h:'c',hpp:'cpp',toml:'ini',cfg:'ini',txt:'plaintext'};
const BASE = new URL('./', location.href).href;
const api = (path, opts) => fetch(BASE + path, opts).then(r => r.json());

// Virtual paths used to view files at git refs / with blame decorations.
// Format: <scheme>:<encoded-repo>:<encoded-ref>:<encoded-file>.
export function makeGitPath(scheme, repo, ref, file) {
    return `${scheme}:${encodeURIComponent(repo)}:${encodeURIComponent(ref)}:${encodeURIComponent(file)}`;
}
function parseGitPath(path) {
    const colon = path.indexOf(':');
    const parts = path.slice(colon + 1).split(':');
    return {
        scheme: path.slice(0, colon),
        repo: decodeURIComponent(parts[0] || ''),
        ref: decodeURIComponent(parts[1] || ''),
        file: decodeURIComponent(parts[2] || ''),
    };
}
function fileLang(file) {
    return LANG[file.split('.').pop()] || 'plaintext';
}

let monacoPromise = null;
function loadMonaco() {
    if (monacoPromise) return monacoPromise;
    monacoPromise = (async () => {
        await new Promise(resolve => {
            const s = document.createElement('script');
            s.src = 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.52.2/min/vs/loader.min.js';
            s.onload = resolve;
            document.head.appendChild(s);
        });
        const monaco = await new Promise(resolve => {
            window.require.config({ paths: { vs: 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.52.2/min/vs' }});
            window.require(['vs/editor/editor.main'], () => resolve(window.monaco));
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
        return monaco;
    })();
    return monacoPromise;
}

// Multi-tab Monaco container. Owns models for opened paths but does NOT
// render its own tab bar — the parent provides the bar and switches the
// active model via the `active` prop. Parent gets dirty/disk-changed
// state through `onDirtyChange(path, dirty, diskChanged)`.
export class Editor extends Component {
    constructor(props) {
        super(props);
        this.state = { ready: false };
        this.models = {};
    }

    async componentDidMount() {
        this.monaco = await loadMonaco();
        this._editor = this.monaco.editor.create(this._el, {
            value: '', language: 'plaintext', theme: 'catppuccin',
            minimap: { enabled: true }, fontSize: 13,
            automaticLayout: true, scrollBeyondLastLine: false,
        });
        this.setState({ ready: true });
        if (this.props.active) this._activate(this.props.active);
    }

    componentDidUpdate(prevProps) {
        if (this.state.ready && prevProps.active !== this.props.active && this.props.active) {
            this._activate(this.props.active);
        }
    }

    _applyTabOptions(tab) {
        this._editor.updateOptions({
            readOnly: !!tab.readOnly,
            lineNumbers: tab.lineNumbers || 'on',
            lineNumbersMinChars: tab.lineNumbersMinChars || 5,
        });
    }

    async _activate(path) {
        const existing = this.models[path];
        if (existing) {
            this._editor.setModel(existing.model);
            this._applyTabOptions(existing);
            this._restoreCursor(path);
            return;
        }
        const tab = await this._load(path);
        this.models[path] = tab;
        this._editor.setModel(tab.model);
        this._applyTabOptions(tab);
        this._restoreCursor(path);
    }

    _restoreCursor(path) {
        const pending = this._pendingReveal?.[path];
        if (!pending) return;
        delete this._pendingReveal[path];
        const pos = { lineNumber: pending.line, column: 1 };
        this._editor.focus();
        this._editor.setPosition(pos);
        this._editor.revealLineInCenter(pos.lineNumber);
    }

    revealLine(path, line) {
        // Called externally (e.g. from a diff click) to position the cursor
        // once `path` becomes active.
        this._pendingReveal = this._pendingReveal || {};
        this._pendingReveal[path] = { line };
        if (this.props.active === path && this.models[path]) this._restoreCursor(path);
    }

    async _load(path) {
        if (path.startsWith('git-show:')) return this._loadGitShow(path);
        if (path.startsWith('git-blame:')) return this._loadGitBlame(path);
        return this._loadFile(path);
    }

    async _loadFile(path) {
        const model = this.monaco.editor.createModel('Loading...', fileLang(path));
        const tab = { model, saved: '', dirty: false, diskChanged: false };
        const data = await api(`api/file?path=${encodeURIComponent(path)}`);
        if (data.error) { model.setValue(`Error: ${data.error}`); return tab; }
        tab.saved = data.content;
        model.setValue(data.content);
        model.onDidChangeContent(() => {
            const was = tab.dirty;
            tab.dirty = model.getValue() !== tab.saved;
            if (tab.dirty !== was) this.props.onDirtyChange?.(path, tab.dirty, tab.diskChanged);
        });
        return tab;
    }

    async _loadGitShow(path) {
        const { repo, ref, file } = parseGitPath(path);
        const q = `path=${encodeURIComponent(repo)}&ref=${encodeURIComponent(ref)}&file=${encodeURIComponent(file)}`;
        const data = await api(`api/git/show?${q}`);
        const content = data.error ? `Error: ${data.error}` : (data.content || '');
        const model = this.monaco.editor.createModel(content, fileLang(file));
        return { model, saved: content, dirty: false, diskChanged: false, readOnly: true };
    }

    async _loadGitBlame(path) {
        // Two flavours:
        //   git-blame:<workspace path>           — auto-detect repo, blame HEAD
        //   git-blame:<repo>:<ref>:<file>        — explicit repo/ref (from Git panel)
        const body = path.slice('git-blame:'.length);
        const parts = body.split(':');
        let url, displayFile;
        if (parts.length >= 3) {
            const { repo, ref, file } = parseGitPath(path);
            displayFile = file;
            url = `api/git/blame?path=${encodeURIComponent(repo)}&ref=${encodeURIComponent(ref)}&file=${encodeURIComponent(file)}`;
        } else {
            displayFile = body;
            url = `api/git/blame_at?path=${encodeURIComponent(body)}`;
        }
        const data = await api(url);
        if (data.error) {
            const model = this.monaco.editor.createModel(`Error: ${data.error}`, 'plaintext');
            return { model, saved: '', dirty: false, diskChanged: false, readOnly: true };
        }
        const lines = data.lines || [];
        const content = lines.map(l => l.content).join('\n');
        const labels = lines.map((l, i) => `${l.sha.slice(0,8)} ${l.author} ${l.date} ${i+1}`);
        const maxLen = labels.reduce((m, s) => Math.max(m, s.length), 5);
        const model = this.monaco.editor.createModel(content, fileLang(displayFile));
        return {
            model, saved: content, dirty: false, diskChanged: false, readOnly: true,
            lineNumbers: (n) => labels[n-1] || String(n),
            lineNumbersMinChars: maxLen,
        };
    }

    closeFile(path) {
        this.models[path]?.model.dispose();
        delete this.models[path];
    }

    async save(path) {
        const tab = this.models[path];
        if (!tab) return;
        if (tab.diskChanged && !confirm('File was changed on disk. Overwrite?')) return;
        const content = tab.model.getValue();
        const data = await api('api/file', {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ path, content }),
        });
        if (data.error) return;
        tab.saved = content;
        tab.dirty = false;
        tab.diskChanged = false;
        this.props.onDirtyChange?.(path, false, false);
    }

    handleFilesChanged(paths) {
        const changed = new Set(paths);
        for (const [path, tab] of Object.entries(this.models)) {
            if (!changed.has(path)) continue;
            if (tab.dirty) {
                tab.diskChanged = true;
                this.props.onDirtyChange?.(path, true, true);
            } else {
                api(`api/file?path=${encodeURIComponent(path)}`).then(d => {
                    if (d.error) return;
                    tab.saved = d.content;
                    tab.model.setValue(d.content);
                });
            }
        }
    }

    render({ active }) {
        return html`<div class=${cl.editor}>
            ${!active && html`<div class=${cl.welcome}>Select a file to view</div>`}
            <div class=${cl.monacoWrap} style=${{display: active ? 'block' : 'none'}}
                 ref=${el => this._el = el}></div>
        </div>`;
    }
}

cl.editor = css`flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0; min-height: 0;`;
cl.welcome = css`
  flex: 1; display: flex; align-items: center; justify-content: center;
  color: var(--text-dim); font-size: 16px;
`;
cl.monacoWrap = css`flex: 1;`;
