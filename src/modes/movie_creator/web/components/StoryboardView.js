// Storyboard grid for a selected scene. Click a card to open ShotView.
import { html, useState, useRef, useEffect } from '../lib.js';
import { app } from '../app.js';

export function StoryboardView() {
    const sp = app.state.selectedPath;
    const sceneId = sp?.[0] === 'scenes' ? sp[1] : null;
    const scene = sceneId ? (app.state.project.scenes[sceneId] || null) : null;
    const [lightbox, setLightbox] = useState(null);
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

    const bodyRef = useRef(null);
    const prevCount = useRef(shots.length);
    useEffect(() => {
        if (shots.length > prevCount.current && bodyRef.current) {
            bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
        }
        prevCount.current = shots.length;
    }, [shots.length]);

    return html`
        <div class="editor">
            <div class="editor-header">
                <h2>Storyboard — ${scene.title || 'Untitled'}</h2>
                <button class="btn btn-sm btn-primary" onClick=${createShot}>+ Add shot</button>
            </div>
            <div class="editor-body" ref=${bodyRef}>
                ${shots.length === 0
                    ? html`<div class="center-empty">No shots yet</div>`
                    : html`<div class="shot-grid">
                        ${shots.map((shot, i) => html`
                            <${ShotCard} key=${shot.id} scene=${scene} shot=${shot} index=${i} onZoom=${setLightbox} />
                        `)}
                    </div>`}
                ${lightbox ? html`
                    <div class="lightbox" onClick=${() => setLightbox(null)}>
                        <img src=${lightbox} onClick=${e => e.stopPropagation()} />
                    </div>
                ` : null}
            </div>
        </div>
    `;
}

function ShotCard({ scene, shot, index, onZoom }) {
    const primary = shot.generations?.[shot.primary_generation_id];
    const thumb = primary?.file ? `/api/asset/${primary.file}` : null;
    const preview = (shot.description || '').split('\n')[0] || '(empty)';

    function select(e) {
        e.stopPropagation();
        app.select(['scenes', scene.id, 'shots', shot.id]);
    }

    function del(e) {
        e.stopPropagation();
        if (!confirm('Delete this shot?')) return;
        app.send({ type: 'delete', path: ['scenes', scene.id, 'shots', shot.id] });
    }

    return html`
        <div class="shot-card" onClick=${select}>
            <div class="shot-thumb" onClick=${e => { if (thumb) { e.stopPropagation(); onZoom(thumb); } }}>
                ${thumb ? html`<img src=${thumb} />` : null}
                <span class="shot-num">${index + 1}</span>
            </div>
            <div class="shot-compact-footer">
                <div class="shot-preview">${preview}</div>
                <button class="btn-icon btn-danger" onClick=${del}>\u2715</button>
            </div>
        </div>
    `;
}
