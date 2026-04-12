import { render, html, Component, css } from './lib.js';
import { Chat } from './components/common/Chat.js';
import { Resizer } from './components/common/Resizer.js';

const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const cl = {};

class App extends Component {
    constructor(props) {
        super(props);
        this.state = { connected: false, tab: 'agent', logs: { agent: [], memory: [], transport: [] } };
        this._chat = null;
    }

    componentDidMount() { this._connect(); }

    _connect() {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        this._ws = new WebSocket(proto + location.host + location.pathname + 'ws');
        this._ws.onopen = () => this.setState({ connected: true });
        this._ws.onclose = () => { this.setState({ connected: false }); setTimeout(() => this._connect(), 2000); };
        this._ws.onerror = () => this._ws.close();
        this._ws.onmessage = e => this._onMessage(JSON.parse(e.data));
    }

    send(msg) {
        if (this._ws && this._ws.readyState === WebSocket.OPEN)
            this._ws.send(JSON.stringify(msg));
    }

    _onMessage(ev) {
        if (ev.type === 'transport')
            this._chat?.handleMessage(ev);
        else if (ev.type === 'log')
            this.setState(({ logs }) => {
                const cat = ev.category;
                if (!logs[cat]) return {};
                return { logs: { ...logs, [cat]: [...logs[cat], { level: ev.level, text: ev.text }] } };
            });
    }

    render() {
        const { connected, tab, logs } = this.state;
        const tabs = ['agent', 'memory', 'transport'];

        return html`
            <div class=${cl.app}>
                <div class=${cl.header}>
                    ${tabs.map(t => html`
                        <div class=${'tab' + (tab === t ? ' active' : '')} onClick=${() => this.setState({ tab: t })}>
                            ${t.charAt(0).toUpperCase() + t.slice(1)}
                        </div>
                    `)}
                    <span class=${'status' + (connected ? ' ok' : '')}>${connected ? 'connected' : 'disconnected'}</span>
                </div>
                <div class=${cl.main}>
                    <div class=${cl.left}>
                        <div class=${cl.pane}>
                            ${(logs[tab] || []).map(l => {
                                const m = l.text.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),?\d*\s+(.*)/s);
                                const ts = m ? m[1] : '';
                                const body = m ? m[2] : l.text;
                                return html`<div class="${cl.log} ${l.level}">${ts && html`<span class="ts">${ts}</span>`}${esc(body)}</div>`;
                            })}
                        </div>
                    </div>
                    <${Resizer} side="right" />
                    <${Chat} ref=${c => this._chat = c} app=${this} connected=${connected} />
                </div>
            </div>
        `;
    }
}

render(html`<${App} />`, document.body);

// --- styles ---

cl.app = css`display: flex; flex-direction: column; height: 100vh;`;
cl.header = css`
  background: var(--surface); border-bottom: 1px solid var(--border);
  display: flex; align-items: center;
  & .tab { padding: 10px 24px; cursor: pointer; font-size: 13px; color: var(--text-dim); border-bottom: 2px solid transparent; }
  & .tab:hover { color: var(--text); }
  & .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  & .status { font-size: 12px; color: var(--text-dim); margin-left: auto; padding-right: 16px; }
  & .status.ok { color: var(--green); }
`;
cl.main = css`flex: 1; display: flex; overflow: hidden;`;
cl.left = css`flex: 1; display: flex; flex-direction: column; overflow: hidden;`;
cl.pane = css`flex: 1; overflow: auto; padding: 12px;`;
cl.log = css`
  font-size: 12px; line-height: 1.7; white-space: pre;
  & .ts { color: var(--text-dim); margin-right: 8px; font-size: 11px; }
  &.DEBUG { color: var(--text-dim); }
  &.INFO { color: var(--green); }
  &.WARNING { color: var(--warn); }
  &.ERROR { color: var(--red); }
  &.CRITICAL { color: var(--red); font-weight: bold; }
`;

