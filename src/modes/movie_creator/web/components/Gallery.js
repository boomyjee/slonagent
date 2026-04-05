// Generation gallery for any owner entity (character, shot, ...).
//
// Reads `entity.generations` directly from the live project state passed via
// props — so generations arriving externally (AI streaming results via WS)
// show up automatically without touching any sibling form's draft state.
//
// Knows its own WS path and issues generate/set_primary/delete messages
// directly. No "million callbacks" from the parent.

import { html, useState } from '../lib.js';

export function Gallery({ entity, path, kind, defaultPrompt, send, openPromptModal }) {
    const [lightbox, setLightbox] = useState(null);
    const gens = Object.values(entity.generations || {}).filter(g => g.kind === kind);
    const primary = entity.image || '';

    function openPrompt(initial) {
        openPromptModal({
            title: `Generate: ${entity.name || entity.title || entity.description || 'item'}`,
            initial,
            onSubmit: prompt => {
                send({ type: 'generate', path, kind, prompt });
                openPromptModal(null);
            },
            onCancel: () => openPromptModal(null),
        });
    }

    function newGen() { openPrompt(defaultPrompt ? defaultPrompt() : ''); }
    function remix(g) { openPrompt(g.prompt || ''); }
    function setPrimary(g) {
        send({ type: 'set_primary', path: [...path, 'generations', g.id] });
    }
    function deleteGen(g) {
        if (!confirm('Delete this generation?')) return;
        send({ type: 'delete', path: [...path, 'generations', g.id] });
    }

    return html`
        <div class="gen-gallery">
            <div class="gen-gallery-header">
                <span>${kind}s</span>
                <button class="btn btn-sm btn-primary" onClick=${newGen}>+ New generation</button>
            </div>
            ${gens.length === 0
                ? html`<div class="gen-empty">No generations yet</div>`
                : html`
                    <div class="gen-grid">
                        ${gens.map(g => html`
                            <${Tile}
                                key=${g.id}
                                gen=${g}
                                isPrimary=${!!g.file && g.file === primary}
                                onZoom=${src => setLightbox(src)}
                                onSetPrimary=${() => setPrimary(g)}
                                onRemix=${() => remix(g)}
                                onDelete=${() => deleteGen(g)}
                            />
                        `)}
                    </div>
                `}
            ${lightbox ? html`
                <div class="lightbox" onClick=${() => setLightbox(null)}>
                    <img src=${lightbox} onClick=${e => e.stopPropagation()} />
                </div>
            ` : null}
        </div>
    `;
}

function Tile({ gen, isPrimary, onZoom, onSetPrimary, onRemix, onDelete }) {
    const done = gen.status === 'done' && gen.file;
    const failed = gen.status === 'failed';
    const src = done ? `/api/asset/${gen.file}` : null;
    return html`
        <div class=${'gen-tile' + (isPrimary ? ' primary' : '')}>
            <div class="gen-image">
                ${done
                    ? html`<img src=${src} onClick=${() => onZoom(src)} />`
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
