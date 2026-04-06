// Generation gallery for any owner entity (character, shot, ...).
//
// Reads `entity.generations` directly from the live project state passed via
// props — so generations arriving externally (AI streaming results via WS)
// show up automatically without touching any sibling form's draft state.
//
// Knows its own WS path and issues generate/set_primary/delete messages
// directly. No "million callbacks" from the parent.

import { html, useState } from '../lib.js';
import { app } from '../app.js';
import { useEntity } from '../common/EntityView.js';
import { Dialog } from '../common/Dialog.js';
import { ReferencePicker } from './ReferencePicker.js';

const MODELS = [
    { id: 'gemini-image', label: 'Nano Banana 2' },
    { id: 'seedream-v5', label: 'Seedream 5.0' },
];
let lastModel = 'gemini-image';

async function uploadFile(file, path, kind) {
    const form = new FormData();
    form.append('file', file);
    const params = new URLSearchParams({ path: path.join('/'), kind });
    await fetch('/api/upload?' + params, { method: 'POST', body: form });
}

export function Gallery({ kind, defaultPrompt }) {
    const ctx = useEntity();
    if (!ctx?.entity?.id) return null;
    const { entity, path } = ctx;
    const [lightbox, setLightbox] = useState(null);
    const [dragover, setDragover] = useState(false);
    const gens = Object.values(entity.generations || {}).filter(g => g.kind === kind);
    const hasPrimary = 'primary_generation_id' in entity;
    const primaryId = hasPrimary ? (entity.primary_generation_id || '') : '';

    function handleFiles(files) {
        for (const f of files) {
            if (f.type.startsWith('image/')) uploadFile(f, path, kind);
        }
    }

    function onDrop(e) {
        e.preventDefault();
        setDragover(false);
        handleFiles(e.dataTransfer.files);
    }

    function onPaste(e) {
        const files = [...(e.clipboardData?.files || [])];
        if (files.length) { e.preventDefault(); handleFiles(files); }
    }

    function openPrompt(initial, model, initialRefs) {
        const title = `Generate: ${entity.name || entity.title || entity.description || 'item'}`;
        Dialog.open(html`<${GenerateDialog}
            title=${title} initial=${initial} initialModel=${model || lastModel}
            initialRefs=${initialRefs || []} path=${path} kind=${kind}
        />`);
    }

    function newGen() { openPrompt(defaultPrompt ? defaultPrompt(entity) : ''); }
    function remix(g) { openPrompt(g.prompt || '', g.model || lastModel, g.references || []); }
    function setPrimary(g) {
        app.send({ type: 'update', path, data: { primary_generation_id: g.id } });
    }
    function deleteGen(g) {
        if (!confirm('Delete this generation?')) return;
        app.send({ type: 'delete', path: [...path, 'generations', g.id] });
    }

    return html`
        <div class=${'gen-gallery' + (dragover ? ' dragover' : '')}
            tabindex="0"
            onDragOver=${e => { e.preventDefault(); setDragover(true); }}
            onDragLeave=${() => setDragover(false)}
            onDrop=${onDrop}
            onPaste=${onPaste}
        >
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
                                isPrimary=${hasPrimary && g.id === primaryId}
                                canSetPrimary=${hasPrimary}
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

function Tile({ gen, isPrimary, canSetPrimary, onZoom, onSetPrimary, onRemix, onDelete }) {
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
                ${gen.model ? html`<div class="gen-model">${gen.model}</div>` : null}
            </div>
            <div class="gen-prompt" title=${gen.prompt}>${gen.prompt}</div>
            <div class="gen-actions">
                ${canSetPrimary && done && !isPrimary
                    ? html`<button class="btn btn-sm" onClick=${onSetPrimary}>Set primary</button>`
                    : null}
                <button class="btn btn-sm" onClick=${onRemix}>Remix</button>
                <button class="btn btn-sm btn-danger" onClick=${onDelete}>\u2715</button>
            </div>
        </div>
    `;
}

function GenerateDialog({ title, initial, initialModel, initialRefs, path, kind }) {
    const [prompt, setPrompt] = useState(initial || '');
    const [model, setModel] = useState(initialModel || lastModel);
    const [refs, setRefs] = useState(initialRefs || []);

    function toggle(file) {
        setRefs(r => r.includes(file) ? r.filter(f => f !== file) : [...r, file]);
    }

    function generate() {
        lastModel = model;
        app.send({ type: 'generate', path, kind, prompt, model, references: refs });
        Dialog.close();
    }

    return html`
        <div class="editor generate-dialog">
            <div class="editor-header"><h2>${title}</h2></div>
            <div class="editor-body">
                <div class="field">
                    <label>Model</label>
                    <select value=${model} onChange=${e => setModel(e.target.value)}>
                        ${MODELS.map(m => html`<option value=${m.id}>${m.label}</option>`)}
                    </select>
                </div>
                <div class="field grow">
                    <label>Prompt</label>
                    <textarea
                        value=${prompt}
                        onInput=${e => setPrompt(e.target.value)}
                        placeholder="Describe the image or video..."
                    ></textarea>
                </div>
                <${ReferencePicker} selected=${refs} onToggle=${toggle} />
            </div>
            <div class="editor-footer">
                <button class="btn" onClick=${() => Dialog.close()}>Cancel</button>
                <div class="spacer"></div>
                <button class="btn btn-primary" onClick=${generate}>Generate</button>
            </div>
        </div>
    `;
}
