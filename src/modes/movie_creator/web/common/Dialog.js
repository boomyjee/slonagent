// Imperative singleton dialog. Self-mounting — no need to place anything in the tree.
//   Dialog.open(html`<MyContent .../>`)   — arbitrary content
//   Dialog.close()
//   Dialog.prompt(title, initial)         — returns Promise<string|null>
import { html, render, useState, useEffect } from '../lib.js';

let _content = null;
const _listeners = new Set();
function _notify() { _listeners.forEach(l => l()); }

function PromptContent({ title, initial, onResult }) {
    const [text, setText] = useState(initial || '');
    return html`
        <div class="modal-header"><h2>${title}</h2></div>
        <div class="modal-body">
            <div class="field grow">
                <label>Prompt</label>
                <textarea
                    value=${text}
                    onInput=${e => setText(e.target.value)}
                    placeholder="Describe the image..."
                ></textarea>
            </div>
        </div>
        <div class="modal-footer">
            <button class="btn" onClick=${() => onResult(null)}>Cancel</button>
            <div class="spacer"></div>
            <button class="btn btn-primary" onClick=${() => onResult(text)}>Generate</button>
        </div>
    `;
}

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
    prompt(title, initial) {
        return new Promise(resolve => {
            function onResult(value) { Dialog.close(); resolve(value); }
            Dialog.open(html`<${PromptContent} title=${title} initial=${initial} onResult=${onResult} />`);
        });
    },
};
