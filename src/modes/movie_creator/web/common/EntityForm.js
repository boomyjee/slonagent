// Stateless form fields from a schema. Parent owns `draft` and `onChange`.
// No wrapper, no header, no footer — just the <div class="field"> rows.
// Remount (via key) is how callers reset draft after selection changes.
import { html } from '../lib.js';

export function EntityForm({ schema, draft, onChange }) {
    const setField = (name, value) => onChange({ ...draft, [name]: value });
    return html`${schema.fields.map(f => html`
        <div class=${'field' + (f.grow ? ' grow' : '')}>
            <label>${f.label}</label>
            ${f.type === 'textarea'
                ? html`<textarea
                    placeholder=${f.placeholder || ''}
                    value=${draft[f.name] || ''}
                    onInput=${e => setField(f.name, e.target.value)}
                ></textarea>`
                : html`<input
                    type="text"
                    placeholder=${f.placeholder || ''}
                    value=${draft[f.name] || ''}
                    onInput=${e => setField(f.name, e.target.value)}
                />`}
        </div>
    `)}`;
}
