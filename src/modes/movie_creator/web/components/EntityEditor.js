import { html } from '../lib.js';

export function EntityEditor({ schema, editing, onChange, onSave, onCancel, onDelete, onReject, onGeneratePortrait }) {
    const { data, isNew, approval } = editing;
    const title = approval
        ? `AI Proposal — ${schema.label}`
        : isNew
            ? `New ${schema.label}`
            : `${schema.label}: ${data[schema.titleField] || schema.emptyTitle}`;

    function setField(name, value) {
        onChange({ ...data, [name]: value });
    }

    return html`
        <div class="editor">
            <div class="editor-header">
                <h2>${title}</h2>
            </div>
            <div class="editor-body">
                ${schema.portrait ? html`
                    <${PortraitSection}
                        char=${data}
                        disabled=${isNew || approval}
                        onGenerate=${onGeneratePortrait}
                    />
                ` : null}
                ${schema.fields.map(f => html`
                    <div class=${'field' + (f.grow ? ' grow' : '')}>
                        <label>${f.label}</label>
                        ${f.type === 'textarea'
                            ? html`<textarea
                                placeholder=${f.placeholder}
                                value=${data[f.name] || ''}
                                onInput=${e => setField(f.name, e.target.value)}
                            ></textarea>`
                            : html`<input
                                type="text"
                                placeholder=${f.placeholder}
                                value=${data[f.name] || ''}
                                onInput=${e => setField(f.name, e.target.value)}
                            />`}
                    </div>
                `)}
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

function PortraitSection({ char, disabled, onGenerate }) {
    const hasImg = !!char.image;
    const src = hasImg ? `/api/asset/${char.image}?t=${Date.now()}` : null;
    return html`
        <div class="char-portrait">
            ${hasImg
                ? html`<img src=${src} />`
                : html`<div class="placeholder">No portrait</div>`}
            ${!disabled ? html`
                <button class="btn btn-sm" onClick=${onGenerate}>
                    ${hasImg ? 'Regenerate portrait' : 'Generate portrait'}
                </button>
            ` : null}
        </div>
    `;
}
