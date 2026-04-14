// Generation gallery for any owner entity (character, shot, ...).
//
// Reads `entity.generations` directly from the live project state passed via
// props — so generations arriving externally (AI streaming results via WS)
// show up automatically without touching any sibling form's draft state.
//
// Knows its own WS path and issues generate/set_primary/delete messages
// directly. No "million callbacks" from the parent.

import { html, useState } from '../lib.js';
import { app, base } from '../app.js';
import { Lightbox } from '../common/Lightbox.js';
import { useEntity } from '../common/EntityView.js';
import { Form, Select, Text, Textarea, Toggle } from '../common/Form.js';
import { Dialog } from '../common/Dialog.js';
import { ReferencePicker } from './ReferencePicker.js';

let lastModel = 'gemini-image';

async function uploadFile(file, path, kind) {
    const form = new FormData();
    form.append('file', file);
    const params = new URLSearchParams({ path: path.join('/'), kind });
    await fetch(`${base}/api/upload?` + params, { method: 'POST', body: form });
}

export function Gallery({ kind, defaultPrompt }) {
    const ctx = useEntity();
    if (!ctx?.entity?.id) return null;
    const { entity, path } = ctx;
    const [dragover, setDragover] = useState(false);
    const [filter, setFilter] = useState('all');
    const allGens = Object.values(entity.generations || {}).filter(g => g.kind === kind);
    const gens = filter === 'all' ? allGens
        : filter === 'image' ? allGens.filter(g => g.media_type !== 'video')
        : allGens.filter(g => g.media_type === 'video');
    const hasPrimary = 'primary_generation_id' in entity;
    const primaryId = hasPrimary ? (entity.primary_generation_id || '') : '';
    const imageCount = allGens.filter(g => g.media_type !== 'video').length;
    const videoCount = allGens.filter(g => g.media_type === 'video').length;

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
            title=${title} path=${path} kind=${kind}
            initial=${{ prompt: initial, model: model || lastModel, references: initialRefs || [], duration: 5, aspect_ratio: '16:9', resolution: '720p' }}
        />`);
    }

    function newGen() { openPrompt(defaultPrompt ? defaultPrompt(entity) : ''); }
    function remix(g) { openPrompt(g.prompt || '', g.model || lastModel, g.references || []); }
    function useAsRef(g) { openPrompt('', lastModel, [g.file]); }
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
                <div class="gen-filter">
                    <button class=${'btn btn-sm' + (filter === 'all' ? ' btn-primary' : '')} onClick=${() => setFilter('all')}>All (${allGens.length})</button>
                    <button class=${'btn btn-sm' + (filter === 'image' ? ' btn-primary' : '')} onClick=${() => setFilter('image')}>Images (${imageCount})</button>
                    <button class=${'btn btn-sm' + (filter === 'video' ? ' btn-primary' : '')} onClick=${() => setFilter('video')}>Videos (${videoCount})</button>
                </div>
                <button class="btn btn-sm btn-primary" onClick=${newGen}>+ Generate</button>
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
                                onSetPrimary=${() => setPrimary(g)}
                                onRemix=${() => remix(g)}
                                onUseAsRef=${() => useAsRef(g)}
                                onDelete=${() => deleteGen(g)}
                            />
                        `)}
                    </div>
                `}
        </div>
    `;
}

function Tile({ gen, isPrimary, canSetPrimary, onSetPrimary, onRemix, onUseAsRef, onDelete }) {
    const done = gen.status === 'done' && gen.file;
    const failed = gen.status === 'failed';
    const isVideo = gen.media_type === 'video';
    const thumb = done ? `${base}/api/asset/800x800/${gen.poster || gen.file}` : null;
    const full = done ? `${base}/api/asset/${gen.file}` : null;
    return html`
        <div class=${'gen-tile' + (isPrimary ? ' primary' : '')}>
            <div class="gen-image">
                ${done
                    ? html`<img src=${thumb} data-full=${full} data-video=${isVideo ? '1' : undefined}
                        data-lightbox="gallery" onClick=${e => Lightbox.open(e.target)} />`
                    : failed
                        ? html`<div class="gen-status failed"><div class="gen-fail-label">failed</div><div class="gen-error">${gen.error || ''}</div></div>`
                        : html`<div class=${'gen-status ' + gen.status}>${gen.status}</div>`}
                ${isPrimary ? html`<div class="gen-primary-badge">primary</div>` : null}
                ${gen.model ? html`<div class="gen-model">${gen.model}</div>` : null}
                ${isVideo && done ? html`<div class="gen-video-badge">\u25B6</div>` : null}
            </div>
            <div class="gen-prompt" title=${gen.prompt}>${gen.prompt}</div>
            <div class="gen-actions">
                ${canSetPrimary && done && !isPrimary
                    ? html`<button class="gen-act" title="Set primary" onClick=${onSetPrimary}>\u2713</button>`
                    : null}
                ${done && html`<button class="gen-act" title="Use as reference" onClick=${onUseAsRef}>\u29C9</button>`}
                <button class="gen-act" title="Remix" onClick=${onRemix}>\u21BB</button>
                <div class="spacer"></div>
                <button class="gen-act gen-act-danger" title="Delete" onClick=${onDelete}>\u2715</button>
            </div>
        </div>
    `;
}

function SeedanceForm() {
    return html`
        <${Text} name="duration" label="Duration (sec)" type="number" min=${4} max=${15} />
        <${Select} name="aspect_ratio" label="Aspect ratio" options=${[
            { id: '16:9', label: '16:9' }, { id: '9:16', label: '9:16' }, { id: '1:1', label: '1:1' },
            { id: '4:3', label: '4:3' }, { id: '3:4', label: '3:4' }, { id: '21:9', label: '21:9' },
            { id: 'adaptive', label: 'Adaptive' },
        ]} />
        <${Select} name="resolution" label="Resolution" options=${[
            { id: '480p', label: '480p' }, { id: '720p', label: '720p' },
        ]} />
        <${Toggle} name="fast" label="Fast" />
    `;
}

const IMAGE_MODELS = [
    { id: 'gemini-image', label: 'Nano Banana 2' },
    { id: 'evolink-seedream5.0', label: 'Seedream 5.0 Lite' },
    { id: 'muapi-seedance2.0-character', label: 'Seedance Character' },
];

const VIDEO_MODELS = [
    { id: 'evolink-seedance2.0-reference', label: 'Seedance Reference', form: SeedanceForm },
    { id: 'evolink-seedance2.0-first-last', label: 'Seedance First-Last', form: SeedanceForm },
];

function GenerateDialog({ title, initial, path, kind }) {
    const [draft, setDraft] = useState(initial);
    const isVideo = VIDEO_MODELS.some(m => m.id === draft.model);
    const models = isVideo ? VIDEO_MODELS : IMAGE_MODELS;
    const currentModel = models.find(m => m.id === draft.model);

    function generate() {
        lastModel = draft.model;
        const msg = { type: 'generate', path, kind, ...draft, references: draft.references || [] };
        app.send(msg);
        Dialog.close();
    }

    return html`
        <div class="editor generate-dialog">
            <div class="editor-header"><h2>${title}</h2></div>
            <div class="editor-body">
                <${Form} draft=${draft} onChange=${setDraft}>
                    <div class="gen-type-tabs">
                        <button class=${'btn btn-sm' + (!isVideo ? ' btn-primary' : '')}
                            onClick=${() => isVideo && setDraft({ ...draft, model: IMAGE_MODELS[0].id })}>Image</button>
                        <button class=${'btn btn-sm' + (isVideo ? ' btn-primary' : '')}
                            onClick=${() => !isVideo && setDraft({ ...draft, model: VIDEO_MODELS[0].id })}>Video</button>
                    </div>
                    <${Select} name="model" label="Model" options=${models} />
                    <${Textarea} name="prompt" label="Prompt" placeholder="Describe the image or video..." grow />
                    ${currentModel?.form && html`
                        <div class="gen-extra-params"><${currentModel.form} /></div>
                    `}
                    <${ReferencePicker} name="references" />
                <//>
            </div>
            <div class="editor-footer">
                <button class="btn" onClick=${() => Dialog.close()}>Cancel</button>
                <div class="spacer"></div>
                <button class="btn btn-primary" onClick=${generate}>Generate</button>
            </div>
        </div>
    `;
}
