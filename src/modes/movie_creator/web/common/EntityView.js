import { html, useState, send } from '../lib.js';
import { app } from '../app.js';
import { Form } from './Form.js';

export function EntityView({ collection, label, children, extra }) {
    const { project, selected } = app.state;
    const sel = selected[collection];
    const entity = sel === '__new__' ? {} : (project[collection]?.[sel] || null);
    if (!entity) {
        return html`<div class="center-empty">Select or create a ${label.toLowerCase()}</div>`;
    }
    const isNew = !entity.id;
    const [draft, setDraft] = useState(() => ({ ...entity }));

    function submit() {
        if (isNew) {
            send({ type: 'create', path: [collection], data: draft });
        } else {
            send({ type: 'update', path: [collection, entity.id], data: draft });
        }
        app.selectEntity(collection, null);
    }

    function del() {
        if (!confirm(`Delete this ${label.toLowerCase()}?`)) return;
        send({ type: 'delete', path: [collection, entity.id] });
        app.selectEntity(collection, null);
    }

    const heading = isNew ? `New ${label}` : `${label}: ${draft.name || draft.title || 'Untitled'}`;

    return html`
        <div class="editor">
            <div class="editor-header"><h2>${heading}</h2></div>
            <div class="editor-body">
                <${Form} draft=${draft} onChange=${setDraft}>
                    ${children}
                <//>
                ${!isNew && extra ? extra(entity) : null}
            </div>
            <div class="editor-footer">
                ${!isNew ? html`<button class="btn btn-danger" onClick=${del}>Delete</button>` : null}
                <div class="spacer"></div>
                <button class="btn" onClick=${() => app.selectEntity(collection, null)}>Cancel</button>
                <button class="btn btn-primary" onClick=${submit}>Save</button>
            </div>
        </div>
    `;
}
