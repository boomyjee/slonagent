// Scene editor — just a form. No gallery (scenes don't own media).
import { html, SCENE_SCHEMA } from '../lib.js';
import { EntityForm } from './EntityForm.js';

export function SceneView({ scene, isNew, send, onClose }) {
    const initial = isNew ? {} : {
        title: scene.title || '',
        location: scene.location || '',
        text: scene.text || '',
    };

    function submit(data) {
        if (isNew) {
            send({ type: 'create', path: ['scenes'], data });
        } else {
            send({ type: 'update', path: ['scenes', scene.id], data });
        }
        onClose();
    }

    function del() {
        if (!confirm('Delete this scene?')) return;
        send({ type: 'delete', path: ['scenes', scene.id] });
        onClose();
    }

    return html`<${EntityForm}
        schema=${SCENE_SCHEMA}
        initial=${initial}
        mode=${isNew ? 'create' : 'edit'}
        onSubmit=${submit}
        onCancel=${onClose}
        onDelete=${del}
    />`;
}
