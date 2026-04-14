import { html, createContext, useContext } from '../lib.js';
import { FormView, editorCls } from './FormView.js';

// Context carrying a path-based CRUD store. Consumers (EntityList, EntityView)
// read via `useEntityStore()`; the host app provides a concrete implementation.
//
// Store shape:
//   resolve(path): entity | null         — walk a path, null if missing
//   list(collection): entity[]           — items in a top-level collection
//   create(collectionPath, data): void
//   update(path, data): void
//   delete(path): void
//   get selectedPath: string[] | null    — current selection
//   select(path): void                   — set selection (path or null)
export const EntityStoreCtx = createContext(null);
export const useEntityStore = () => useContext(EntityStoreCtx);

const EntityCtx = createContext(null);
export function useEntity() { return useContext(EntityCtx); }

export function EntityView({ path, label, back, children }) {
    const store = useEntityStore();
    const isNew = path[path.length - 1] === '__new__';
    const entity = isNew ? {} : store.resolve(path);
    if (!entity) {
        return html`<div class=${editorCls.centerEmpty}>Select or create a ${label.toLowerCase()}</div>`;
    }
    const close = () => store.select(back || null);

    function submit(draft) {
        if (isNew) store.create(path.slice(0, -1), draft);
        else store.update(path, draft);
        close();
    }

    function del() {
        if (!confirm(`Delete this ${label.toLowerCase()}?`)) return;
        store.delete(path);
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
