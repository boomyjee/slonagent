import { html } from '../lib.js';
import { Text, Textarea } from '../common/Form.js';
import { Gallery } from './Gallery.js';

export function SceneForm() {
    return html`
        <${Text} name="title" label="Title" placeholder="Scene title" />
        <${Text} name="location" label="Location" placeholder="INT. APARTMENT - NIGHT" />
        <${Textarea} name="text" label="Scene text" placeholder="Scene description and dialogue..." grow />
    `;
}

function scenePrompt(scene) {
    return `Cinematic establishing shot. ${scene.location || ''}. ${scene.title || ''}. Cinematic lighting, wide angle, film still.`;
}

export const sceneExtra = scene => html`
    <${Gallery}
        entity=${scene}
        path=${['scenes', scene.id]}
        kind="location"
        defaultPrompt=${() => scenePrompt(scene)}
    />
`;
