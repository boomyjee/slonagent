// Scene editor — form + footer. Reads scene from app.state.
import { html, useState, SCENE_SCHEMA, send } from '../lib.js';
import { app } from '../app.js';
import { EntityForm } from '../common/EntityForm.js';

export function SceneView() {
    const { project, selected } = app.state;
    const sel = selected.scenes;
    const scene = sel === '__new__' ? {} : (project.scenes[sel] || null);

    if (!scene) {
        return html`<div class="center-empty">Select a scene or create a new one</div>`;
    }
    const isNew = !scene.id;
    const [draft, setDraft] = useState(() => ({
        title: scene.title || '',
        location: scene.location || '',
        text: scene.text || '',
    }));

    function submit() {
        if (isNew) {
            send({ type: 'create', path: ['scenes'], data: draft });
        } else {
            send({ type: 'update', path: ['scenes', scene.id], data: draft });
        }
        app.selectEntity('scenes', null);
    }

    function del() {
        if (!confirm('Delete this scene?')) return;
        send({ type: 'delete', path: ['scenes', scene.id] });
        app.selectEntity('scenes', null);
    }

    const title = isNew
        ? 'New Scene'
        : `Scene: ${draft.title || 'Untitled'}`;

    return html`
        <div class="editor">
            <div class="editor-header"><h2>${title}</h2></div>
            <div class="editor-body">
                <${EntityForm} schema=${SCENE_SCHEMA} draft=${draft} onChange=${setDraft} />
            </div>
            <div class="editor-footer">
                ${!isNew ? html`<button class="btn btn-danger" onClick=${del}>Delete</button>` : null}
                <div class="spacer"></div>
                <button class="btn" onClick=${() => app.selectEntity('scenes', null)}>Cancel</button>
                <button class="btn btn-primary" onClick=${submit}>Save</button>
            </div>
        </div>
    `;
}
