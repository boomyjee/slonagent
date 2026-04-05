// Reusable form shell for a single entity.
//
// Owns its own draft state — initialized ONCE from `initial` and then detached.
// External updates to the same entity (e.g. AI edits arriving via WS) do NOT
// overwrite the user's in-progress edits. Resetting the draft is done by
// remounting the component — pass a fresh `key` to <EntityForm/>.
//
// Footer mode:
//   'edit'     — Cancel / Delete / Save
//   'create'   — Cancel / Save
//   'approval' — Reject / Approve (applies user-edited draft as approval data)
//
// `children` is rendered below the fields — for composing sibling blocks
// (e.g. a <Gallery/>) that live alongside the form but don't participate
// in its draft state.

import { html, useState } from '../lib.js';

export function EntityForm({ schema, initial, mode, onSubmit, onCancel, onDelete, children }) {
    const [draft, setDraft] = useState(() => ({ ...(initial || {}) }));

    function setField(name, value) {
        setDraft(d => ({ ...d, [name]: value }));
    }

    const title = mode === 'approval'
        ? `AI Proposal — ${schema.label}`
        : mode === 'create'
            ? `New ${schema.label}`
            : `${schema.label}: ${draft[schema.titleField] || schema.emptyTitle}`;

    return html`
        <div class="editor">
            <div class="editor-header"><h2>${title}</h2></div>
            <div class="editor-body">
                ${schema.fields.map(f => html`
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
                `)}
                ${children}
            </div>
            <div class="editor-footer">
                ${mode === 'approval' ? html`
                    <button class="btn btn-danger" onClick=${onCancel}>Reject</button>
                    <div class="spacer"></div>
                    <button class="btn btn-primary" onClick=${() => onSubmit(draft)}>Approve</button>
                ` : html`
                    ${mode === 'edit' ? html`<button class="btn btn-danger" onClick=${onDelete}>Delete</button>` : null}
                    <div class="spacer"></div>
                    <button class="btn" onClick=${onCancel}>Cancel</button>
                    <button class="btn btn-primary" onClick=${() => onSubmit(draft)}>Save</button>
                `}
            </div>
        </div>
    `;
}
