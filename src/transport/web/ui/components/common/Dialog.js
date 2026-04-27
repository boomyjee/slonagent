// Imperative singleton dialog. Self-mounting — no need to place anything in the tree.
//   Dialog.open(html`<MyContent .../>`)
//   Dialog.close()
import { html, render, Component, css } from '../../lib.js';

const cl = {};

let _host = null;

class DialogHost extends Component {
    constructor(props) {
        super(props);
        _host = this;
        this.state = { content: null };
    }

    render() {
        const { content } = this.state;
        if (!content) return null;
        return html`
            <div class=${cl.backdrop}
                onMouseDown=${e => { this._downTarget = e.target; }}
                onMouseUp=${e => { if (e.target === this._downTarget) Dialog.close(); }}>
                <div class=${cl.modal} onMouseDown=${e => e.stopPropagation()}>${content}</div>
            </div>
        `;
    }
}

const _root = document.createElement('div');
document.body.appendChild(_root);
render(html`<${DialogHost} />`, _root);

export const Dialog = {
    open(content) { _host.setState({ content }); },
    close() { _host.setState({ content: null }); },
};

// --- styles ---

cl.backdrop = css`
  position: fixed; inset: 0; background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center; z-index: 100;
`;

cl.modal = css`
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  max-width: 90vw; max-height: 90vh;
  display: flex; flex-direction: column; overflow: hidden;
`;
