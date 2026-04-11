/**
 * Shared chat widget — used by both run.js (bookmarklet) and ext.js (extension
 * sidepanel iframe). Wraps the dashboard's <Chat/> and forwards `action` RPCs
 * to a caller-provided PageController (real one in run.js, postMessage shim
 * in ext.js).
 *
 * Factory (not top-level imports) so the bookmarklet can set
 * `stylesHost.target = shadowRoot` BEFORE Chat.js module-eval runs its
 * top-level `css` calls.
 */

export async function createWidgetApp({ agentId, host, protocol, page, onSuperseded }) {
    const DASH = `${protocol}//${host}/${agentId}/dashboard`;
    const WS_URL = `${protocol === 'https:' ? 'wss' : 'ws'}://${host}/${agentId}/web_agent/ws`;

    const { html, Component } = await import(`${DASH}/lib.js`);
    const { Chat } = await import(`${DASH}/components/Chat.js`);

    return class WidgetApp extends Component {
        constructor(props) {
            super(props);
            this.state = { connected: false };
            this._chat = null;
        }

        componentDidMount() { this._connect(); }

        _connect() {
            this._ws = new WebSocket(WS_URL);
            this._ws.onopen = () => { this.setState({ connected: true }); };
            this._ws.onclose = e => {
                this.setState({ connected: false });
                // 4001 = superseded — another widget instance took over.
                if (e.code === 4001) { onSuperseded?.(); return; }
                setTimeout(() => this._connect(), 2000);
            };
            this._ws.onerror = () => this._ws.close();
            this._ws.onmessage = async e => {
                const ev = JSON.parse(e.data);
                if (ev.type === 'transport') {
                    // send_processing brackets a task on the Python side —
                    // toggle the PageController mask + AI cursor in sync.
                    if (ev.method === 'send_processing') {
                        if (ev.active) page?.showMask?.();
                        else page?.hideMask?.();
                    }
                    this._chat?.handleMessage(ev);
                } else if (ev.type === 'action') {
                    let result, error;
                    try {
                        const fn = page?.[ev.method];
                        if (typeof fn !== 'function') throw new Error(`unknown action: ${ev.method}`);
                        result = await fn.apply(page, ev.args || []);
                    } catch (err) {
                        error = err?.message || String(err);
                    }
                    this.send({ type: 'action_result', request_id: ev.request_id, result, error });
                }
            };
        }

        send(msg) {
            if (this._ws?.readyState === WebSocket.OPEN) this._ws.send(JSON.stringify(msg));
        }

        render(_, { connected }) {
            return html`<${Chat} ref=${c => this._chat = c} app=${this} connected=${connected} />`;
        }
    };
}
