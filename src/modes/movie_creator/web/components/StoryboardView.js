// Storyboard for a selected scene. Reads shots from scene.shots dict (live
// project state). Each shot is either a compact row or a full card with
// inline-editable description + its own gallery.
import { html, useState } from '../lib.js';

function shotPrompt(shot) {
    return `Cinematic film still. ${shot.description || ''}. Cinematic lighting, shallow depth of field.`;
}
import { app } from '../app.js';
import { Gallery } from './Gallery.js';

export function StoryboardView() {
    const scene = app.state.project.scenes[app.state.selected.scenes] || null;
    const [mode, setMode] = useState('compact');
    if (!scene) {
        return html`<div class="center-empty">Select a scene to start storyboarding</div>`;
    }
    const shots = Object.values(scene.shots || {});

    function createShot() {
        app.send({
            type: 'create',
            path: ['scenes', scene.id, 'shots'],
            data: { description: '' },
        });
    }

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
                <button class="btn btn-sm btn-primary" onClick=${createShot}>+ Add shot</button>
            </div>
            <div class="editor-body">
                ${shots.length === 0
                    ? html`<div class="center-empty">No shots yet</div>`
                    : shots.map((shot, i) => html`
                        <${ShotCard}
                            key=${shot.id}
                            scene=${scene}
                            shot=${shot}
                            index=${i}
                            mode=${mode}
                        />
                    `)}
            </div>
        </div>
    `;
}

function ShotCard({ scene, shot, index, mode }) {
    const [draft, setDraft] = useState(null);
    const value = draft != null ? draft : (shot.description || '');
    const primary = shot.generations?.[shot.primary_generation_id];
    const thumb = primary?.file ? `/api/asset/${primary.file}` : null;
    const shotPath = ['scenes', scene.id, 'shots', shot.id];

    function commit() {
        if (draft != null && draft !== shot.description) {
            app.send({ type: 'update', path: shotPath, data: { description: draft } });
        }
        setDraft(null);
    }
    function del() {
        if (!confirm('Delete this shot?')) return;
        app.send({ type: 'delete', path: shotPath });
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
                <button class="btn btn-sm btn-danger" onClick=${del}>\u2715</button>
            </div>
            <${Gallery}
                entity=${shot}
                path=${shotPath}
                kind="frame"
                defaultPrompt=${() => shotPrompt(shot)}
            />
        </div>
    `;
}
