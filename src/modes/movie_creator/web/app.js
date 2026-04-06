// Movie Creator — Preact app entry.
//
// App is a class component exposed as module-level `app` so any module can
// read state (app.state.project, app.state.tab, ...) and call methods.
import { render, html, Component } from './lib.js';
import { Resizer } from './common/Resizer.js';
import { SceneList } from './components/SceneList.js';
import { CharacterList } from './components/CharacterList.js';
import { EntityView } from './common/EntityView.js';
import { SceneForm, sceneExtra } from './components/SceneForm.js';
import { CharacterForm, characterExtra } from './components/CharacterForm.js';
import { StoryboardView } from './components/StoryboardView.js';
import './common/Dialog.js';
import { Chat } from './components/Chat.js';

export let app = null;

class App extends Component {
    constructor(props) {
        super(props);
        app = this;
        this.state = {
            connected: false,
            project: { title: '', scenes: {}, characters: {} },
            tab: 'screenplay',
            selected: { scenes: null, characters: null },
        };
        this._chat = null;
    }

    componentDidMount() {
        this._ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
        this._ws.onopen = () => this.setState({ connected: true });
        this._ws.onclose = () => this.setState({ connected: false });
        this._ws.onmessage = e => this.handleMessage(JSON.parse(e.data));
    }

    send(msg) {
        if (this._ws && this._ws.readyState === 1) this._ws.send(JSON.stringify(msg));
    }

    handleMessage(msg) {
        const t = msg.type;
        if (t === 'project_updated')
            this.setState({ project: msg.project });
        else if (t === 'message' || t === 'tool_call' || t === 'processing' || t === 'processing_done' || t === 'approval_request')
            this._chat?.handleMessage(msg);
        else
            console.warn('Unknown WS message type:', t, msg);
    }

    selectEntity(collection, id) {
        this.setState(({ selected }) => ({ selected: { ...selected, [collection]: id } }));
    }

    componentDidUpdate(_, prev) {
        const { tab, selected } = this.state;
        if (tab !== prev.tab)
            this.send({ type: 'tab_changed', tab });
        if (selected !== prev.selected)
            this.send({ type: 'selected_changed', selected });
    }


    render() {
        const { connected, tab, selected } = this.state;

        let sidebarView = null, centerView;
        if (tab === 'screenplay') {
            sidebarView = html`<${SceneList} />`;
            centerView = html`<${EntityView} collection="scenes" label="Scene" extra=${sceneExtra} key=${'scene-' + selected.scenes}><${SceneForm} /><//>`;

        } else if (tab === 'characters') {
            sidebarView = html`<${CharacterList} />`;
            centerView = html`<${EntityView} collection="characters" label="Character" extra=${characterExtra} key=${'char-' + selected.characters}><${CharacterForm} /><//>`;

        } else if (tab === 'storyboard') {
            sidebarView = html`<${SceneList} />`;
            centerView = html`<${StoryboardView} key=${'sb-' + selected.scenes} />`;
        } else {
            centerView = html`<div class="center-empty">Generation (coming soon)</div>`;
        }

        return html`
            <div class="header">
                <h1>Movie Creator</h1>
                <span class="status" style=${{ color: connected ? 'var(--green)' : 'var(--red)' }}>
                    ${connected ? 'connected' : 'disconnected'}
                </span>
            </div>
            <div class="tabs">
                ${['screenplay', 'characters', 'storyboard', 'generation'].map(t => html`
                    <div class=${'tab' + (tab === t ? ' active' : '')} onClick=${() => this.setState({ tab: t })}>
                        ${t.charAt(0).toUpperCase() + t.slice(1)}
                    </div>
                `)}
            </div>
            <div class="main">
                <div class="sidebar">${sidebarView}</div>
                <${Resizer} side="left" />
                <div class="center">${centerView}</div>
                <${Resizer} side="right" />
                <${Chat} ref=${c => this._chat = c} />
            </div>
        `;
    }
}

render(html`<${App} />`, document.body);
