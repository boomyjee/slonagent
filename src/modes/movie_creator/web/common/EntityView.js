import { html } from '../lib.js';
import { app } from '../app.js';
import { FormView } from './FormView.js';

export function EntityView({ collection, label, children, extra }) {
    const { project, selected } = app.state;
    const sel = selected[collection];
    const entity = sel === '__new__' ? {} : (project[collection]?.[sel] || null);
    if (!entity) {
        return html`<div class="center-empty">Select or create a ${label.toLowerCase()}</div>`;
    }
    const isNew = !entity.id;
    const close = () => app.selectEntity(collection, null);

    function submit(draft) {
        if (isNew) app.send({ type: 'create', path: [collection], data: draft });
        else app.send({ type: 'update', path: [collection, entity.id], data: draft });
        close();
    }

    function del() {
        if (!confirm(`Delete this ${label.toLowerCase()}?`)) return;
        app.send({ type: 'delete', path: [collection, entity.id] });
        close();
    }

    return html`<${FormView}
        heading=${isNew ? `New ${label}` : `${label}: ${entity.name || entity.title || 'Untitled'}`}
        entity=${entity}
        left=${() => !isNew ? [{ label: 'Delete', cls: 'danger', onClick: del }] : []}
        right=${draft => [
            { label: 'Cancel', onClick: close },
            { label: 'Save', cls: 'primary', onClick: () => submit(draft) },
        ]}
    >
        ${children}
        ${!isNew && extra ? extra(entity) : null}
    <//>`;
}
