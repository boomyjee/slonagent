import { html, createContext, useContext } from '../lib.js';
import { app } from '../app.js';
import { FormView } from './FormView.js';

const EntityCtx = createContext(null);
export function useEntity() { return useContext(EntityCtx); }

function resolve(obj, path) {
    for (const seg of path) {
        if (obj == null) return null;
        obj = obj[seg];
    }
    return obj ?? null;
}

export function EntityView({ path, label, back, children }) {
    const isNew = path[path.length - 1] === '__new__';
    const entity = isNew ? {} : resolve(app.state.project, path);
    if (!entity) {
        return html`<div class="center-empty">Select or create a ${label.toLowerCase()}</div>`;
    }
    const close = () => app.select(back || null);

    function submit(draft) {
        if (isNew) app.send({ type: 'create', path: path.slice(0, -1), data: draft });
        else app.send({ type: 'update', path, data: draft });
        close();
    }

    function del() {
        if (!confirm(`Delete this ${label.toLowerCase()}?`)) return;
        app.send({ type: 'delete', path });
        close();
    }

    return html`<${EntityCtx.Provider} value=${{ entity, path }}>
        <${FormView}
            heading=${isNew ? `New ${label}` : `${label}: ${entity.name || entity.title || entity.description || 'Untitled'}`}
            entity=${entity}
            left=${() => [
                ...(back ? [{ label: '\u2190 Back', onClick: close }] : []),
                ...(!isNew ? [{ label: 'Delete', cls: 'danger', onClick: del }] : []),
            ]}
            right=${draft => [
                ...(!back ? [{ label: 'Cancel', onClick: close }] : []),
                { label: 'Save', cls: 'primary', onClick: () => submit(draft) },
            ]}
        >
            ${children}
        <//>
    <//>`;
}
