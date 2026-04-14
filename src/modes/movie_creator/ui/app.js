// Movie Creator — Preact app entry.
//
// App is a class component exposed as module-level `app` so any module can
// read state (app.state.project, app.state.tab, ...) and call methods.
// `base` is the URL prefix the movie transport is mounted at
// (e.g. "/agent_42/movie") — exported so asset URLs can be prefixed.
import { render, html, Component, css } from './lib.js';
import { Resizer } from './components/common/Resizer.js';
import { Chat } from './components/common/Chat.js';
import { SceneList } from './components/SceneList.js';
import { CharacterList } from './components/CharacterList.js';
import { EntityView, EntityStoreCtx } from './common/EntityView.js';
import { SceneForm } from './components/SceneForm.js';
import { CharacterForm } from './components/CharacterForm.js';
import { StoryboardView } from './components/StoryboardView.js';
import { ShotForm } from './components/ShotForm.js';
import { FolderList } from './components/FolderList.js';
import { FolderForm } from './components/FolderForm.js';
import { ApproveDialog } from './components/ApproveDialog.js';
import { editorCls } from './common/FormView.js';
import './common/Dialog.js';
import './common/Lightbox.js';
import { GenerationIndicator } from './components/GenerationIndicator.js';

export const base = location.pathname.replace(/\/+$/, '');
export let app = null;

const cl = {};

// Path-based store implementation fed to EntityList/EntityView via EntityStoreCtx.
// Methods access `app.state.project` at call time (not construction), so they
// always see the latest WS-synced snapshot.
function walk(obj, path) {
    for (const seg of path) {
        if (obj == null) return null;
        obj = obj[seg];
    }
    return obj ?? null;
}

const movieStore = {
    resolve:  path            => walk(app.state.project, path),
    list:     collection      => Object.values(app.state.project[collection] || {}),
    create:   (colPath, data) => app.send({ type: 'create', path: colPath, data }),
    update:   (path, data)    => app.send({ type: 'update', path, data }),
    delete:   path            => app.send({ type: 'delete', path }),
    get selectedPath() { return app.state.selectedPath; },
    select:   path            => app.select(path),
};

class App extends Component {
    constructor(props) {
        super(props);
        app = this;
        this.state = {
            connected: false,
            project: { title: '', scenes: {}, characters: {}, library: {} },
            tab: 'screenplay',
            selectedPath: null,
        };
        this._chat = null;
    }

    componentDidMount() {
        const wsProto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        this._ws = new WebSocket(wsProto + location.host + base + '/ws');
        this._ws.onopen = () => this.setState({ connected: true });
        this._ws.onclose = () => this.setState({ connected: false });
        this._ws.onmessage = e => this.handleMessage(JSON.parse(e.data));
    }

    send(msg) {
        if (this._ws && this._ws.readyState === 1) this._ws.send(JSON.stringify(msg));
    }

    handleMessage(msg) {
        if (msg.type === 'transport') {
            this._chat?.handleMessage(msg);
        } else if (msg.type === 'project_updated') {
            this.setState({ project: msg.project });
        } else if (msg.type === 'approval_request') {
            ApproveDialog.open({ approvalKind: msg.kind, data: msg.data });
        } else {
            console.warn('Unknown WS message type:', msg.type, msg);
        }
    }

    select(path) {
        this.setState({ selectedPath: path });
    }

    componentDidUpdate(_, prev) {
        const { tab, selectedPath } = this.state;
        if (tab !== prev.tab)
            this.send({ type: 'tab_changed', tab });
        if (selectedPath !== prev.selectedPath)
            this.send({ type: 'selected_changed', path: selectedPath });
    }

    render() {
        const { connected, tab, selectedPath } = this.state;
        const selKey = selectedPath ? selectedPath.join('/') : '';
        const collection = selectedPath?.[0];

        let sidebarView = null, centerView;
        if (tab === 'screenplay' || tab === 'storyboard') {
            sidebarView = html`<${SceneList} />`;
        } else if (tab === 'characters') {
            sidebarView = html`<${CharacterList} />`;
        } else if (tab === 'library') {
            sidebarView = html`<${FolderList} />`;
        }

        if (selectedPath?.length === 4 && selectedPath[2] === 'shots') {
            centerView = html`<${EntityView} path=${selectedPath} label="Shot" back=${selectedPath.slice(0, 2)} key=${'shot-' + selKey}><${ShotForm} /><//>`;
        } else if (collection === 'scenes' && tab === 'storyboard') {
            centerView = html`<${StoryboardView} key=${'sb-' + selKey} />`;
        } else if (collection === 'scenes') {
            centerView = html`<${EntityView} path=${selectedPath} label="Scene" key=${'scene-' + selKey}><${SceneForm} /><//>`;
        } else if (collection === 'characters') {
            centerView = html`<${EntityView} path=${selectedPath} label="Character" key=${'char-' + selKey}><${CharacterForm} /><//>`;
        } else if (collection === 'library') {
            centerView = html`<${EntityView} path=${selectedPath} label="Folder" key=${'folder-' + selKey}><${FolderForm} /><//>`;
        } else {
            centerView = html`<div class=${editorCls.centerEmpty}>Select an entity</div>`;
        }

        if (!connected) return html`<div class=${cl.disconnected}>App disconnected</div>`;

        return html`<${EntityStoreCtx.Provider} value=${movieStore}>
            <div class=${cl.root}>
                <div class=${cl.tabs}>
                    ${['screenplay', 'characters', 'storyboard', 'library'].map(t => html`
                        <div class=${cl.tab + (tab === t ? ' active' : '')} onClick=${() => this.setState({ tab: t })}>
                            ${t.charAt(0).toUpperCase() + t.slice(1)}
                        </div>
                    `)}
                    <${GenerationIndicator} />
                </div>
                <div class=${cl.main}>
                    <div class=${cl.sidebar}>${sidebarView}</div>
                    <${Resizer} side="left" persistKey="movie-left" />
                    <div class=${cl.center}>${centerView}</div>
                    <${Resizer} side="right" persistKey="movie-right" />
                    <${Chat} app=${this} connected=${connected} ref=${c => this._chat = c} />
                </div>
            </div>
        <//>`;
    }
}

render(html`<${App} />`, document.body);

// --- styles ---

cl.root = css`
  display: flex; flex-direction: column; height: 100vh;
`;

cl.disconnected = css`
  display: flex; align-items: center; justify-content: center;
  height: 100vh; color: var(--text-dim); font-size: 16px;
`;

cl.tabs = css`
  display: flex; align-items: center;
  background: var(--surface); border-bottom: 1px solid var(--border);
  flex-shrink: 0;
`;

cl.tab = css`
  padding: 10px 24px; cursor: pointer;
  font-size: 13px; color: var(--text-dim);
  border-bottom: 2px solid transparent;
  &:hover { color: var(--text); }
  &.active { color: var(--accent); border-bottom-color: var(--accent); }
`;

cl.main = css`
  display: flex; flex: 1; overflow: hidden;
`;

cl.sidebar = css`
  width: 260px; background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column; flex-shrink: 0;
`;

cl.center = css`
  flex: 1 1 0; display: flex; flex-direction: column;
  overflow: hidden; min-width: 200px; min-height: 0;
`;
