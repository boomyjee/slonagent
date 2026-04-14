// Movie Creator — Preact app entry.
//
// App is a class component exposed as module-level `app` so any module can
// read state (app.state.project, app.state.tab, ...) and call methods.
// `base` is the URL prefix the movie transport is mounted at
// (e.g. "/agent_42/movie") — exported so asset URLs can be prefixed.
import { render, html, Component } from './lib.js';
import { Resizer } from './components/common/Resizer.js';
import { Chat } from './components/common/Chat.js';
import { SceneList } from './components/SceneList.js';
import { CharacterList } from './components/CharacterList.js';
import { EntityView } from './common/EntityView.js';
import { SceneForm } from './components/SceneForm.js';
import { CharacterForm } from './components/CharacterForm.js';
import { StoryboardView } from './components/StoryboardView.js';
import { ShotForm } from './components/ShotForm.js';
import { FolderList } from './components/FolderList.js';
import { FolderForm } from './components/FolderForm.js';
import { ApproveDialog } from './components/ApproveDialog.js';
import './common/Dialog.js';
import './common/Lightbox.js';
import { GenerationIndicator } from './components/GenerationIndicator.js';

export const base = location.pathname.replace(/\/+$/, '');
export let app = null;

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
            centerView = html`<div class="center-empty">Select an entity</div>`;
        }

        if (!connected) return html`<div class="disconnected">App disconnected</div>`;

        return html`
            <div class="tabs">
                ${['screenplay', 'characters', 'storyboard', 'library'].map(t => html`
                    <div class=${'tab' + (tab === t ? ' active' : '')} onClick=${() => this.setState({ tab: t })}>
                        ${t.charAt(0).toUpperCase() + t.slice(1)}
                    </div>
                `)}
                <${GenerationIndicator} />
            </div>
            <div class="main">
                <div class="sidebar">${sidebarView}</div>
                <${Resizer} side="left" persistKey="movie-left" />
                <div class="center">${centerView}</div>
                <${Resizer} side="right" persistKey="movie-right" />
                <${Chat} app=${this} connected=${connected} ref=${c => this._chat = c} />
            </div>
        `;
    }
}

render(html`<${App} />`, document.body);
