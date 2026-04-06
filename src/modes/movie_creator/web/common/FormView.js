import { html, useState } from '../lib.js';
import { Form } from './Form.js';

export function FormView({ heading, entity, left, right, children }) {
    const [draft, setDraft] = useState(() => ({ ...entity }));

    const btn = ({ label, cls, onClick }) =>
        html`<button class=${'btn' + (cls ? ' btn-' + cls : '')} onClick=${onClick}>${label}</button>`;

    return html`
        <div class="editor">
            <div class="editor-header"><h2>${heading}</h2></div>
            <div class="editor-body">
                <${Form} draft=${draft} onChange=${setDraft}>
                    ${children}
                <//>
            </div>
            <div class="editor-footer">
                ${(left?.(draft) || []).map(btn)}
                <div class="spacer"></div>
                ${(right?.(draft) || []).map(btn)}
            </div>
        </div>
    `;
}
