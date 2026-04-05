import { html, useState } from '../lib.js';
import { GenerationGallery } from './GenerationGallery.js';

export function StoryboardView({
    scene, shots, onCreate, onUpdate, onDelete,
    onNewGeneration, onRemixGeneration, onSetPrimary, onDeleteGeneration,
}) {
    const [mode, setMode] = useState('compact');

    return html`
        <div class="editor">
            <div class="editor-header">
                <h2>Storyboard — ${scene.title || 'Untitled'}</h2>
                <div class="sb-modes">
                    <button
                        class=${'btn btn-sm' + (mode === 'compact' ? ' btn-primary' : '')}
                        onClick=${() => setMode('compact')}
                    >Compact</button>
                    <button
                        class=${'btn btn-sm' + (mode === 'full' ? ' btn-primary' : '')}
                        onClick=${() => setMode('full')}
                    >Full</button>
                </div>
                <button class="btn btn-sm btn-primary" onClick=${onCreate}>+ Add shot</button>
            </div>
            <div class="editor-body">
                ${shots.length === 0
                    ? html`<div class="center-empty">No shots yet</div>`
                    : shots.map((shot, i) => html`
                        <${ShotCard}
                            key=${shot.id}
                            shot=${shot}
                            index=${i}
                            mode=${mode}
                            onUpdate=${desc => onUpdate(shot, desc)}
                            onDelete=${() => onDelete(shot)}
                            onNewGeneration=${() => onNewGeneration(shot)}
                            onRemixGeneration=${gen => onRemixGeneration(shot, gen)}
                            onSetPrimary=${gen => onSetPrimary(shot, gen)}
                            onDeleteGeneration=${gen => onDeleteGeneration(shot, gen)}
                        />
                    `)}
            </div>
        </div>
    `;
}

function ShotCard({
    shot, index, mode, onUpdate, onDelete,
    onNewGeneration, onRemixGeneration, onSetPrimary, onDeleteGeneration,
}) {
    const [draft, setDraft] = useState(null);
    const value = draft != null ? draft : (shot.description || '');
    const thumb = shot.image ? `/api/asset/${shot.image}` : null;

    function commit() {
        if (draft != null && draft !== shot.description) onUpdate(draft);
        setDraft(null);
    }

    if (mode === 'compact') {
        const preview = (shot.description || '').split('\n')[0] || '(empty)';
        return html`
            <div class="shot-card compact">
                <span class="shot-num">${index + 1}</span>
                <div class="shot-thumb">
                    ${thumb ? html`<img src=${thumb} />` : null}
                </div>
                <div class="shot-preview">${preview}</div>
            </div>
        `;
    }

    return html`
        <div class="shot-card full">
            <div class="shot-row">
                <span class="shot-num">${index + 1}</span>
                <textarea
                    class="shot-desc"
                    value=${value}
                    placeholder="Shot description — framing, action, camera, dialogue..."
                    onInput=${e => setDraft(e.target.value)}
                    onBlur=${commit}
                ></textarea>
                <button class="btn btn-sm btn-danger" onClick=${onDelete}>\u2715</button>
            </div>
            <${GenerationGallery}
                owner=${shot}
                kind="frame"
                generations=${shot.generations || []}
                onNew=${onNewGeneration}
                onRemix=${onRemixGeneration}
                onSetPrimary=${onSetPrimary}
                onDelete=${onDeleteGeneration}
            />
        </div>
    `;
}
