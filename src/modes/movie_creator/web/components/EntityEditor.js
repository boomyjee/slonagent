import { html } from '../lib.js';
import { GenerationGallery } from './GenerationGallery.js';

export function EntityEditor({
    schema, data, isNew, approval,
    onFieldChange, onSave, onCancel, onDelete, onReject,
    onNewGeneration, onRemixGeneration, onSetPrimary, onDeleteGeneration,
}) {
    const title = approval
        ? `AI Proposal — ${schema.label}`
        : isNew
            ? `New ${schema.label}`
            : `${schema.label}: ${data[schema.titleField] || schema.emptyTitle}`;

    return html`
        <div class="editor">
            <div class="editor-header">
                <h2>${title}</h2>
            </div>
            <div class="editor-body">
                ${schema.fields.map(f => html`
                    <div class=${'field' + (f.grow ? ' grow' : '')}>
                        <label>${f.label}</label>
                        ${f.type === 'textarea'
                            ? html`<textarea
                                placeholder=${f.placeholder}
                                value=${data[f.name] || ''}
                                onInput=${e => onFieldChange(f.name, e.target.value)}
                            ></textarea>`
                            : html`<input
                                type="text"
                                placeholder=${f.placeholder}
                                value=${data[f.name] || ''}
                                onInput=${e => onFieldChange(f.name, e.target.value)}
                            />`}
                    </div>
                `)}
                ${schema.gallery && !isNew && !approval ? html`
                    <${GenerationGallery}
                        owner=${data}
                        kind=${schema.gallery}
                        generations=${data.generations || []}
                        onNew=${onNewGeneration}
                        onRemix=${onRemixGeneration}
                        onSetPrimary=${onSetPrimary}
                        onDelete=${onDeleteGeneration}
                    />
                ` : null}
            </div>
            <div class="editor-footer">
                ${approval ? html`
                    <button class="btn btn-danger" onClick=${onReject}>Reject</button>
                    <div class="spacer"></div>
                    <button class="btn btn-primary" onClick=${onSave}>Approve</button>
                ` : html`
                    ${!isNew ? html`<button class="btn btn-danger" onClick=${onDelete}>Delete</button>` : null}
                    <div class="spacer"></div>
                    <button class="btn" onClick=${onCancel}>Cancel</button>
                    <button class="btn btn-primary" onClick=${onSave}>Save</button>
                `}
            </div>
        </div>
    `;
}
