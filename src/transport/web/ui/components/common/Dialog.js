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
        this.state = { content: null, opts: {} };
    }

    render() {
        const { content, opts } = this.state;
        if (!content) return null;
        const backdropClass = opts.transparent
            ? `${cl.backdrop} ${cl.backdropTransparent}`
            : cl.backdrop;
        return html`
            <div class=${backdropClass}
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
    // opts: { transparent?: bool } — прозрачный оверлей нужен для preview
    // (см. SettingsDialog: смена темы/шрифта должна быть видна на UI за диалогом).
    open(content, opts = {}) { _host.setState({ content, opts }); },
    close() { _host.setState({ content: null, opts: {} }); },
};

// --- styles ---

cl.backdrop = css`
  position: fixed; inset: 0; background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center; z-index: 100;
`;
cl.backdropTransparent = css`
  background: transparent;
`;

cl.modal = css`
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  max-width: 90vw; max-height: 90vh;
  display: flex; flex-direction: column; overflow: hidden;
`;
