import { html, Component, css } from '../lib.js';

const cl = {};
const LANG = {py:'python',js:'javascript',ts:'typescript',jsx:'javascript',tsx:'typescript',json:'json',md:'markdown',html:'html',css:'css',yaml:'yaml',yml:'yaml',sh:'shell',bash:'shell',rs:'rust',go:'go',java:'java',rb:'ruby',c:'c',cpp:'cpp',h:'c',hpp:'cpp',toml:'ini',cfg:'ini',txt:'plaintext'};
const BASE = new URL('./', location.href).href;
const api = (path, opts) => fetch(BASE + path, opts).then(r => r.json());

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

    async _activate(path) {
        const existing = this.models[path];
        if (existing) {
            this._editor.setModel(existing.model);
            return;
        }
        const ext = path.split('.').pop();
        const model = this.monaco.editor.createModel('Loading...', LANG[ext] || 'plaintext');
        const tab = { model, saved: '', dirty: false, diskChanged: false };
        this.models[path] = tab;
        this._editor.setModel(model);

        const data = await api(`api/file?path=${encodeURIComponent(path)}`);
        if (data.error) { model.setValue(`Error: ${data.error}`); return; }
        tab.saved = data.content;
        model.setValue(data.content);
        model.onDidChangeContent(() => {
            const was = tab.dirty;
            tab.dirty = model.getValue() !== tab.saved;
            if (tab.dirty !== was) this.props.onDirtyChange?.(path, tab.dirty, tab.diskChanged);
        });
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
