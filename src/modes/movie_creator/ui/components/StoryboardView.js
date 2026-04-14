// Storyboard grid for a selected scene. Click a card to open ShotView.
import { html, useRef, useEffect, css } from '../lib.js';
import { app, base } from '../app.js';
import { Lightbox } from '../common/Lightbox.js';
import { Button, editorCls } from '../common/FormView.js';

const cl = {};

export function StoryboardView() {
    const sp = app.state.selectedPath;
    const sceneId = sp?.[0] === 'scenes' ? sp[1] : null;
    const scene = sceneId ? (app.state.project.scenes[sceneId] || null) : null;
    if (!scene) {
        return html`<div class=${editorCls.centerEmpty}>Select a scene to start storyboarding</div>`;
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
        <div class=${editorCls.editor}>
            <div class=${editorCls.header}>
                <h2>Storyboard — ${scene.title || 'Untitled'}</h2>
                <${Button} sm variant="primary" onClick=${createShot}>+ Add shot<//>
            </div>
            <div class=${editorCls.body} ref=${bodyRef}>
                ${shots.length === 0
                    ? html`<div class=${editorCls.centerEmpty}>No shots yet</div>`
                    : html`<div class=${cl.grid}>
                        ${shots.map((shot, i) => html`
                            <${ShotCard} key=${shot.id} scene=${scene} shot=${shot} index=${i} />
                        `)}
                    </div>`}
            </div>
        </div>
    `;
}

function ShotCard({ scene, shot, index }) {
    const primary = shot.generations?.[shot.primary_generation_id];
    const thumb = primary?.file ? `${base}/api/asset/400x400/${primary.poster || primary.file}` : null;
    const full = primary?.file ? `${base}/api/asset/${primary.file}` : null;
    const description = shot.description || '(empty)';

    function edit(e) {
        e.stopPropagation();
        app.select(['scenes', scene.id, 'shots', shot.id]);
    }

    function del(e) {
        e.stopPropagation();
        if (!confirm('Delete this shot?')) return;
        app.send({ type: 'delete', path: ['scenes', scene.id, 'shots', shot.id] });
    }

    return html`
        <div class=${cl.card} onClick=${edit}
            onContextMenu=${e => { if (thumb) { e.preventDefault(); Lightbox.open(e.currentTarget); } }}>
            <div class="thumb">
                ${thumb ? html`<img src=${thumb} data-full=${full} data-lightbox="storyboard" />` : null}
            </div>
            <div class="info">${index + 1}. ${description}</div>
            <${Button} sm variant="danger" class="delete" title="Delete" onClick=${del}>\u2715<//>
            <div class="desc-hover">${index + 1}. ${description}</div>
        </div>
    `;
}

// --- styles ---

cl.grid = css`
  display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 10px;
`;

cl.card = css`
  position: relative; overflow: hidden;
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: 8px; cursor: pointer;
  & .thumb {
    width: 100%; height: 0; padding-bottom: 56.25%;
    position: relative; background: var(--surface3);
  }
  & .thumb img {
    position: absolute; inset: 0;
    width: 100%; height: 100%; object-fit: cover;
  }
  & .info {
    font-size: 10px; color: var(--text-dim);
    padding: 4px 6px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  & .delete {
    position: absolute; bottom: 4px; right: 4px;
    display: none; z-index: 2;
    width: 20px; height: 20px; padding: 0;
    border-radius: 3px; font-size: 11px;
    align-items: center; justify-content: center;
  }
  &:hover .delete { display: flex; }
  & .desc-hover {
    display: none; position: absolute; inset: 0;
    background: rgba(30,30,46,0.9); color: var(--text-dim);
    font-size: 11px; padding: 8px; line-height: 1.4;
    overflow: hidden; pointer-events: none;
  }
  &:hover .desc-hover { display: block; }
`;
