// Generation gallery for any owner entity (character, shot, ...).
//
// Reads `entity.generations` directly from the live project state passed via
// props — so generations arriving externally (AI streaming results via WS)
// show up automatically without touching any sibling form's draft state.
//
// Knows its own WS path and issues generate/set_primary/delete messages
// directly. No "million callbacks" from the parent.

import { html, useState, css, keyframes } from '../lib.js';
import { app, base } from '../app.js';
import { Lightbox } from '../common/Lightbox.js';
import { useEntity } from '../common/EntityView.js';
import { Select, Text, Textarea, Toggle, useField } from '../common/Form.js';
import { Dialog } from '../common/Dialog.js';
import { FormView, Button } from '../common/FormView.js';
import { ReferencePicker } from './ReferencePicker.js';

let lastModel = 'gemini-image';
const cl = {};

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
        <div class=${cl.gallery + (dragover ? ' dragover' : '')}
            tabindex="0"
            onDragOver=${e => { e.preventDefault(); setDragover(true); }}
            onDragLeave=${() => setDragover(false)}
            onDrop=${onDrop}
            onPaste=${onPaste}
        >
            <div class=${cl.header}>
                <div class=${cl.filter}>
                    <${Button} sm variant=${filter === 'all'   ? 'primary' : ''} onClick=${() => setFilter('all')}>All (${allGens.length})<//>
                    <${Button} sm variant=${filter === 'image' ? 'primary' : ''} onClick=${() => setFilter('image')}>Images (${imageCount})<//>
                    <${Button} sm variant=${filter === 'video' ? 'primary' : ''} onClick=${() => setFilter('video')}>Videos (${videoCount})<//>
                </div>
                <${Button} sm variant="primary" onClick=${newGen}>+ Generate<//>
            </div>
            ${gens.length === 0
                ? html`<div class=${cl.empty}>No generations yet</div>`
                : html`
                    <div class=${cl.grid}>
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
        <div class=${cl.tile + (isPrimary ? ' primary' : '')}>
            <div class=${cl.image}>
                ${done
                    ? html`<img src=${thumb} data-full=${full} data-video=${isVideo ? '1' : undefined}
                        data-lightbox="gallery" onClick=${e => Lightbox.open(e.target)} />`
                    : failed
                        ? html`<div class=${cl.status + ' failed'}><div class="fail-label">failed</div><div class=${cl.error}>${gen.error || ''}</div></div>`
                        : html`<div class=${cl.status + ' ' + gen.status}>${gen.status}</div>`}
                ${isPrimary ? html`<div class=${cl.primaryBadge}>primary</div>` : null}
                ${gen.model ? html`<div class="model-badge">${gen.model}</div>` : null}
                ${isVideo && done ? html`<div class=${cl.videoBadge}>\u25B6</div>` : null}
            </div>
            <div class=${cl.prompt} title=${gen.prompt}>${gen.prompt}</div>
            <div class=${cl.actions}>
                ${canSetPrimary && done && !isPrimary
                    ? html`<button class=${cl.act} title="Set primary" onClick=${onSetPrimary}>\u2713</button>`
                    : null}
                ${done && html`<button class=${cl.act} title="Use as reference" onClick=${onUseAsRef}>\u29C9</button>`}
                <button class=${cl.act} title="Remix" onClick=${onRemix}>\u21BB</button>
                <div class="spacer"></div>
                <button class=${cl.act + ' danger'} title="Delete" onClick=${onDelete}>\u2715</button>
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

// GenerateBody reads model via useField so it re-renders when the user flips
// the image/video toggle. FormView owns the draft state.
function GenerateBody() {
    const model = useField('model');
    const isVideo = VIDEO_MODELS.some(m => m.id === model.value);
    const models = isVideo ? VIDEO_MODELS : IMAGE_MODELS;
    const currentModel = models.find(m => m.id === model.value);
    return html`
        <div class=${cl.typeTabs}>
            <${Button} sm variant=${!isVideo ? 'primary' : ''}
                onClick=${() => isVideo && model.set(IMAGE_MODELS[0].id)}>Image<//>
            <${Button} sm variant=${isVideo ? 'primary' : ''}
                onClick=${() => !isVideo && model.set(VIDEO_MODELS[0].id)}>Video<//>
        </div>
        <${Select} name="model" label="Model" options=${models} />
        <${Textarea} name="prompt" label="Prompt" placeholder="Describe the image or video..." grow />
        ${currentModel?.form && html`
            <div class=${cl.extraParams}><${currentModel.form} /></div>
        `}
        <${ReferencePicker} name="references" />
    `;
}

function GenerateDialog({ title, initial, path, kind }) {
    function generate(draft) {
        lastModel = draft.model;
        const msg = { type: 'generate', path, kind, ...draft, references: draft.references || [] };
        app.send(msg);
        Dialog.close();
    }
    return html`<${FormView}
        heading=${title}
        entity=${initial}
        variant="generate"
        left=${() => [{ label: 'Cancel', onClick: () => Dialog.close() }]}
        right=${draft => [{ label: 'Generate', cls: 'primary', onClick: () => generate(draft) }]}
    >
        <${GenerateBody} />
    <//>`;
}

// --- styles ---

const pulse = keyframes`0%,100% { opacity: 0.4; } 50% { opacity: 1; }`;

cl.gallery = css`
  display: flex; flex-direction: column; gap: 10px;
  outline: none; border-radius: 8px; padding: 8px;
  transition: outline-color 0.15s;
  &:focus { outline: 1px solid var(--border); }
  &.dragover { outline: 2px dashed var(--accent); background: rgba(137, 180, 250, 0.05); }
`;

cl.header = css`
  display: flex; align-items: center; gap: 8px;
  & > span { flex: 1; font-size: 11px; color: var(--text-dim);
             text-transform: uppercase; letter-spacing: 0.5px; }
`;

cl.filter = css`display: flex; gap: 4px; flex: 1;`;

cl.empty = css`
  padding: 16px; background: var(--surface2); border-radius: 6px;
  color: var(--text-dim); font-size: 12px; text-align: center;
`;

cl.grid = css`
  display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 10px;
`;

cl.tile = css`
  position: relative;
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px;
  display: flex; flex-direction: column; gap: 6px;
  &.primary { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
  & .model-badge {
    display: none; position: absolute; top: 4px; left: 4px;
    background: rgba(0,0,0,0.6); color: #fff; font-size: 10px;
    padding: 2px 6px; border-radius: 4px; pointer-events: none;
  }
  &:hover .model-badge { display: block; }
`;

cl.image = css`
  position: relative; width: 100%; aspect-ratio: 1;
  background: var(--surface3); border-radius: 6px; overflow: hidden;
  display: flex; align-items: center; justify-content: center;
  & img, & video { width: 100%; height: 100%; object-fit: cover; cursor: zoom-in; }
`;

cl.videoBadge = css`
  position: absolute; bottom: 4px; left: 4px;
  background: rgba(0,0,0,.7); color: #fff; font-size: 14px;
  width: 24px; height: 24px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  pointer-events: none;
`;

cl.status = css`
  font-size: 11px; color: var(--text-dim);
  text-transform: uppercase; padding: 4px 8px;
  &.queued { color: var(--text-dim); }
  &.generating { color: var(--warn); animation: ${pulse} 1.5s infinite; }
  &.failed {
    color: var(--red);
    display: flex; flex-direction: column; width: 100%; height: 100%;
  }
  &.failed .fail-label {
    flex: 1; display: flex; align-items: center; justify-content: center;
    font-size: 11px; text-transform: uppercase;
  }
`;

cl.error = css`
  flex: 1; font-size: 9px; color: var(--text-dim); line-height: 1.3;
  padding: 0 0 8px; word-break: break-word; overflow: hidden; text-transform: none;
`;

cl.primaryBadge = css`
  position: absolute; top: 4px; right: 4px;
  background: var(--accent); color: #1e1e2e;
  font-size: 10px; padding: 2px 6px; border-radius: 4px;
  font-weight: 600; text-transform: uppercase;
`;

cl.prompt = css`
  font-size: 11px; color: var(--text-dim); line-height: 1.4;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
`;

cl.actions = css`
  display: flex; gap: 4px; align-items: center; margin-top: auto;
  & .spacer { flex: 1; }
`;

cl.act = css`
  width: 20px; height: 20px; border: none; border-radius: 3px;
  background: var(--accent); color: #1e1e2e; font-size: 11px;
  cursor: pointer; display: flex; align-items: center; justify-content: center;
  padding: 0; opacity: 0.75;
  &:hover { opacity: 1; }
  &.danger { background: var(--red); }
`;

cl.typeTabs = css`display: flex; gap: 4px; margin-bottom: 4px;`;

cl.extraParams = css`
  display: flex; gap: 12px;
  & > div { flex: 1; }
`;
