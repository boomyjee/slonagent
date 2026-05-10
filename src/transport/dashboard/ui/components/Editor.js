import { html, Component, css, persist } from '../lib.js';
import { api, currentRoot } from './api.js';
import { IdeContext } from './common/ChatDialog.js';

const cl = {};
const LANG = {py:'python',js:'javascript',ts:'typescript',jsx:'javascript',tsx:'typescript',json:'json',md:'markdown',html:'html',css:'css',yaml:'yaml',yml:'yaml',sh:'shell',bash:'shell',rs:'rust',go:'go',java:'java',rb:'ruby',c:'c',cpp:'cpp',h:'c',hpp:'cpp',toml:'ini',cfg:'ini',txt:'plaintext',php:'php',sql:'sql',xml:'xml',svg:'xml',dockerfile:'dockerfile'};

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
export function fileLang(file) {
    return LANG[file.split('.').pop()] || 'plaintext';
}

const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico']);
const VIDEO_EXT = new Set(['mp4', 'webm', 'mov', 'mkv', 'avi']);
const AUDIO_EXT = new Set(['mp3', 'wav', 'ogg', 'flac', 'm4a']);
function mediaKind(path) {
    const ext = path.split('.').pop().toLowerCase();
    if (IMAGE_EXT.has(ext)) return 'image';
    if (VIDEO_EXT.has(ext)) return 'video';
    if (AUDIO_EXT.has(ext)) return 'audio';
    return null;
}

let monacoPromise = null;
export function loadMonaco() {
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
        monaco.editor.defineTheme('vs-slon', {
            base: 'vs-dark',
            inherit: true,
            rules: [
                { token: 'comment', foreground: '6a9955', fontStyle: 'italic' },
                { token: 'keyword', foreground: '569cd6' },
                { token: 'string', foreground: 'ce9178' },
                { token: 'number', foreground: 'b5cea8' },
                { token: 'type', foreground: '4ec9b0' },
                { token: 'function', foreground: 'dcdcaa' },
                { token: 'variable', foreground: '9cdcfe' },
                { token: 'operator', foreground: 'd4d4d4' },
                { token: 'delimiter', foreground: 'd4d4d4' },
            ],
            colors: {
                'editor.background': '#1e1e1e',
                'editor.foreground': '#d4d4d4',
                'editor.lineHighlightBackground': '#2d2d30',
                'editor.selectionBackground': '#264f78',
                'editor.inactiveSelectionBackground': '#3a3d41',
                'editorCursor.foreground': '#aeafad',
                'editorLineNumber.foreground': '#858585',
                'editorLineNumber.activeForeground': '#d4d4d4',
                'editorIndentGuide.background': '#404040',
                'editorIndentGuide.activeBackground': '#707070',
                'editorWidget.background': '#252526',
                'editorWidget.border': '#3c3c3c',
                'minimap.background': '#1e1e1e',
                'scrollbarSlider.background': '#79797966',
                'scrollbarSlider.hoverBackground': '#646464b3',
            },
        });
        // Делаем тему глобальной — monaco.editor.colorize() (без своего instance)
        // не принимает theme в options и читает её из активной глобальной.
        monaco.editor.setTheme('vs-slon');
        return monaco;
    })();
    return monacoPromise;
}

// Multi-tab Monaco container. Owns models for opened paths but does NOT
// render its own tab bar — the parent provides the bar and switches the
// active model via the `active` prop. Parent gets dirty/disk-changed
// state through `onDirtyChange(path, dirty, diskChanged)`.
export class Editor extends Component {
    static contextType = IdeContext;

    constructor(props) {
        super(props);
        this.state = { ready: false, mediaPath: null };
        this.models = {};
    }

    async componentDidMount() {
        this.monaco = await loadMonaco();
        this._editor = this.monaco.editor.create(this._el, {
            value: '', language: 'plaintext', theme: 'vs-slon',
            minimap: { enabled: true }, fontSize: 13,
            automaticLayout: true, scrollBeyondLastLine: false,
        });
        this._editor.onDidChangeCursorSelection(() => this._emitIdeContext());
        this.setState({ ready: true });
        this._handleActive();
        // Persist scroll/cursor on F5/close — _handleActive only fires on
        // tab switch, so without this an in-place reload loses position.
        this._onUnload = () => this._saveViewState(this._lastActive);
        window.addEventListener('beforeunload', this._onUnload);
        // Drop focus whenever a tap lands outside the editor DOM —
        // mobile only dismisses the soft keyboard when the hidden
        // <textarea> actually loses focus, and Monaco doesn't do that
        // by itself when you tap, say, the same tab again.
        this._onPointerDown = (e) => {
            if (this._el && !this._el.contains(e.target)) this.blur();
        };
        document.addEventListener('pointerdown', this._onPointerDown);
    }

    componentWillUnmount() {
        if (this._onUnload) window.removeEventListener('beforeunload', this._onUnload);
        if (this._onPointerDown) document.removeEventListener('pointerdown', this._onPointerDown);
    }

    componentDidUpdate(prevProps) {
        if (prevProps.active !== this.props.active) this._handleActive();
    }

    _viewStateKey(path) {
        return `viewstate:${this.props.rootKey || ''}:${path}`;
    }

    _saveViewState(path) {
        if (!path || !this._editor) return;
        const tab = this.models[path];
        if (!tab) return;
        const vs = this._editor.saveViewState();
        if (!vs) return;
        tab.viewState = vs;
        persist.set(this._viewStateKey(path), vs);
    }

    _loadViewState(path) {
        return persist.get(this._viewStateKey(path), null);
    }

    blur() {
        // Drops focus from Monaco's hidden <textarea>, which is the only
        // way to dismiss the mobile soft keyboard once the editor was tapped.
        this._editor?.getDomNode()?.querySelector('.inputarea')?.blur();
    }

    _handleActive() {
        // Save view state (cursor + scroll) of the tab we're leaving so
        // _activate can restore it on the way back.
        if (this._lastActive && this._lastActive !== this.props.active) {
            this._saveViewState(this._lastActive);
        }
        this._lastActive = this.props.active;

        const path = this.props.active;
        if (!path) { this._emitIdeContext(); return; }
        // Only real workspace paths render as media — virtual schemes
        // (git-show:, git-blame:) keep going through the monaco loader.
        const isFilePath = path.startsWith('/');
        if (isFilePath && mediaKind(path)) {
            this.setState({ mediaPath: path });
        } else if (this._editor) {
            // Use the instance directly — `state.ready` lags one render
            // behind the setState in componentDidMount, which would skip
            // loading a tab that's already active when the editor mounts.
            if (this.state.mediaPath) this.setState({ mediaPath: null });
            this._activate(path);
        }
        this._emitIdeContext();
    }

    // Пишет текущий selection в IdeContext-bag и зовёт change(). Что попадает:
    // рабочий файл с опциональным выделением, либо null для не-файловых табов.
    // file — абсолютный host-путь (currentRoot + project-relative), чтоб LLM
    // знал куда смотреть; sandbox-агент при необходимости транслирует сам.
    _emitIdeContext() {
        const ide = this.context;
        if (!ide) return;
        const path = this._lastActive;
        const set = sel => { ide.selection = sel; ide.change(); };
        const fullPath = () => ((currentRoot() || '') + path).replace(/\\/g, '/');
        if (!path || !path.startsWith('/') || mediaKind(path)) return set(null);
        if (!this._editor || !this._editor.getModel()) return set({ file: fullPath() });
        const sel = this._editor.getSelection();
        if (!sel || sel.isEmpty()) return set({ file: fullPath() });
        set({
            file: fullPath(),
            startLine: sel.startLineNumber,
            endLine: sel.endLineNumber,
            text: this._editor.getModel().getValueInRange(sel),
        });
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
            const vs = existing.viewState || this._loadViewState(path);
            if (vs) this._editor.restoreViewState(vs);
            this._restoreCursor(path);
            return;
        }
        const tab = await this._load(path);
        this.models[path] = tab;
        this._editor.setModel(tab.model);
        this._applyTabOptions(tab);
        const vs = this._loadViewState(path);
        if (vs) this._editor.restoreViewState(vs);
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

    purge() {
        // Drop all cached models — used after Change Root, where the same
        // path now refers to a different file.
        for (const tab of Object.values(this.models)) tab.model.dispose();
        this.models = {};
        if (this._editor) this._editor.setModel(null);
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

    render({ active }, { mediaPath }) {
        const kind = mediaPath ? mediaKind(mediaPath) : null;
        const showMonaco = !!active && !mediaPath;
        const root = currentRoot();
        const src = mediaPath
            ? `api/file/raw?path=${encodeURIComponent(mediaPath)}${root ? `&root=${encodeURIComponent(root)}` : ''}`
            : '';
        return html`<div class=${cl.editor}>
            ${!active && html`<div class=${cl.welcome}>Select a file to view</div>`}
            <div class=${cl.monacoWrap} style=${{display: showMonaco ? 'block' : 'none'}}
                 ref=${el => this._el = el}></div>
            ${mediaPath && html`<div class=${cl.media}>
                ${kind === 'image' && html`<img src=${src} />`}
                ${kind === 'video' && html`<video controls src=${src}></video>`}
                ${kind === 'audio' && html`<audio controls src=${src}></audio>`}
            </div>`}
        </div>`;
    }
}

cl.editor = css`flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0; min-height: 0;`;
cl.welcome = css`
  flex: 1; display: flex; align-items: center; justify-content: center;
  color: var(--text-dim); font-size: 16px;
`;
cl.monacoWrap = css`flex: 1;`;
cl.media = css`
  flex: 1; display: flex; align-items: center; justify-content: center;
  overflow: auto; background: var(--bg); padding: 16px;
  & img, & video { max-width: 100%; max-height: 100%; object-fit: contain; }
  & audio { width: 80%; }
`;
