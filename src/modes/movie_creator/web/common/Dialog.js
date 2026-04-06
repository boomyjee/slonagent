// Imperative singleton dialog. Self-mounting — no need to place anything in the tree.
//   Dialog.open(html`<MyContent .../>`)
//   Dialog.close()
import { html, render, Component } from '../lib.js';

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
            <div class="modal-backdrop" onClick=${() => Dialog.close()}>
                <div class="modal" onClick=${e => e.stopPropagation()}>${content}</div>
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
