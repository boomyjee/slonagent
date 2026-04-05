import { html } from '../lib.js';

export function GenerationGallery({ owner, kind, generations, onNew, onRemix, onSetPrimary, onDelete }) {
    const items = (generations || []).filter(g => g.kind === kind);
    const primaryFile = owner.image || '';

    return html`
        <div class="gen-gallery">
            <div class="gen-gallery-header">
                <span>${kind}s</span>
                <button class="btn btn-sm btn-primary" onClick=${onNew}>+ New generation</button>
            </div>
            ${items.length === 0
                ? html`<div class="gen-empty">No generations yet</div>`
                : html`
                    <div class="gen-grid">
                        ${items.map(g => html`
                            <${GenerationTile}
                                gen=${g}
                                isPrimary=${!!g.file && g.file === primaryFile}
                                onSetPrimary=${() => onSetPrimary(g)}
                                onRemix=${() => onRemix(g)}
                                onDelete=${() => onDelete(g)}
                            />
                        `)}
                    </div>
                `}
        </div>
    `;
}

function GenerationTile({ gen, isPrimary, onSetPrimary, onRemix, onDelete }) {
    const done = gen.status === 'done' && gen.file;
    const failed = gen.status === 'failed';
    const src = done ? `/api/asset/${gen.file}` : null;

    return html`
        <div class=${'gen-tile' + (isPrimary ? ' primary' : '')}>
            <div class="gen-image">
                ${done
                    ? html`<img src=${src} />`
                    : failed
                        ? html`<div class="gen-status failed" title=${gen.error || ''}>failed</div>`
                        : html`<div class=${'gen-status ' + gen.status}>${gen.status}</div>`}
                ${isPrimary ? html`<div class="gen-primary-badge">primary</div>` : null}
            </div>
            <div class="gen-prompt" title=${gen.prompt}>${gen.prompt}</div>
            <div class="gen-actions">
                ${done && !isPrimary
                    ? html`<button class="btn btn-sm" onClick=${onSetPrimary}>Set primary</button>`
                    : null}
                <button class="btn btn-sm" onClick=${onRemix}>Remix</button>
                <button class="btn btn-sm btn-danger" onClick=${onDelete}>\u2715</button>
            </div>
        </div>
    `;
}
