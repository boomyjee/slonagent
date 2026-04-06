// Imperative singleton dialog. Self-mounting — no need to place anything in the tree.
//   Dialog.open(html`<MyContent .../>`)
//   Dialog.close()
import { html, render, useState, useEffect } from '../lib.js';

let _content = null;
const _listeners = new Set();
function _notify() { _listeners.forEach(l => l()); }

function DialogHost() {
    const [, force] = useState(0);
    useEffect(() => {
        const l = () => force(n => n + 1);
        _listeners.add(l);
        return () => _listeners.delete(l);
    }, []);
    if (!_content) return null;
    return html`
        <div class="modal-backdrop" onClick=${() => Dialog.close()}>
            <div class="modal" onClick=${e => e.stopPropagation()}>${_content}</div>
        </div>
    `;
}

const _root = document.createElement('div');
document.body.appendChild(_root);
render(html`<${DialogHost} />`, _root);

export const Dialog = {
    open(content) { _content = content; _notify(); },
    close() { _content = null; _notify(); },
};
