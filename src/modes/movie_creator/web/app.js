// Movie Creator — Preact app entry.
//
// App is a class component exposed as module-level `app` so any module can
// read state (app.state.project, app.state.tab, ...) and call methods.
import { render, html, Component, createWS, send } from './lib.js';
import { Resizer } from './common/Resizer.js';
import { SceneList } from './components/SceneList.js';
import { CharacterList } from './components/CharacterList.js';
import { SceneView } from './components/SceneView.js';
import { CharacterView } from './components/CharacterView.js';
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
            messages: [],
        };
        this._streamEls = {};
        this._sidebarRef = null;
        this._chatRef = null;
    }

    componentDidMount() {
        createWS(msg => this.handleMessage(msg), c => this.setState({ connected: c }));
    }

    handleMessage(msg) {
        if (msg.type === 'project_updated') {
            this.setState({ project: msg.project });
        } else if (msg.type === 'message') {
            this.setState(({ messages }) => {
                if (msg.stream_id != null && this._streamEls[msg.stream_id] != null) {
                    const idx = this._streamEls[msg.stream_id];
                    const next = [...messages];
                    next[idx] = { ...next[idx], text: msg.text, final: msg.final };
                    return { messages: next };
                }
                const next = [...messages, { kind: 'msg', role: msg.role, text: msg.text, stream_id: msg.stream_id, final: msg.final }];
                if (msg.stream_id != null) this._streamEls[msg.stream_id] = next.length - 1;
                return { messages: next };
            });
        } else if (msg.type === 'tool_call') {
            this.setState(({ messages }) => ({ messages: [...messages, { kind: 'tool', name: msg.name }] }));
        } else if (msg.type === 'processing') {
            this.setState(({ messages }) => ({ messages: [...messages, { kind: 'processing' }] }));
        } else if (msg.type === 'processing_done') {
            this.setState(({ messages }) => ({ messages: messages.filter(m => m.kind !== 'processing') }));
        } else if (msg.type === 'approval_request') {
            this.setState(({ messages }) => ({
                messages: [...messages, {
                    kind: 'approval',
                    approvalKind: msg.kind,
                    data: msg.data,
                    resolved: false,
                    idx: messages.length,
                }],
            }));
        }
    }

    switchTab(newTab) {
        this.setState({ tab: newTab });
        send({ type: 'tab_changed', tab: newTab });
        const scope = newTab === 'storyboard'
            ? { scene_id: this.state.selected.scenes || '' }
            : {};
        send({ type: 'scope_changed', scope });
    }

    selectEntity(collection, id) {
        this.setState(({ selected }) => ({ selected: { ...selected, [collection]: id } }));
        if (this.state.tab === 'storyboard' && collection === 'scenes') {
            send({ type: 'scope_changed', scope: { scene_id: id } });
        }
    }


    render() {
        const { connected, tab, selected } = this.state;

        let sidebarView = null, centerView;
        if (tab === 'screenplay') {
            sidebarView = html`<${SceneList} />`;
            centerView = html`<${SceneView} key=${'scene-' + selected.scenes} />`;
        } else if (tab === 'characters') {
            sidebarView = html`<${CharacterList} />`;
            centerView = html`<${CharacterView} key=${'char-' + selected.characters} />`;
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
                    <div class=${'tab' + (tab === t ? ' active' : '')} onClick=${() => this.switchTab(t)}>
                        ${t.charAt(0).toUpperCase() + t.slice(1)}
                    </div>
                `)}
            </div>
            <div class="main">
                <div class="sidebar" ref=${el => this._sidebarRef = el}>${sidebarView}</div>
                <${Resizer} targetRef=${{ current: this._sidebarRef }} side="left" />
                <div class="center">${centerView}</div>
                <${Resizer} targetRef=${{ current: this._chatRef }} side="right" />
                <${Chat} rootRef=${el => this._chatRef = el} />
            </div>
        `;
    }
}

render(html`<${App} />`, document.body);
