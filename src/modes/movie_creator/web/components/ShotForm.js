import { html } from '../lib.js';
import { Textarea } from '../common/Form.js';
import { Gallery } from './Gallery.js';

export function ShotForm() {
    return html`
        <${Textarea} name="description" label="Description" placeholder="Framing, action, camera, dialogue..." grow />
        <${Gallery} kind="frame" defaultPrompt=${shot =>
            `Cinematic film still. ${shot.description || ''}. Cinematic lighting, shallow depth of field.`
        } />
    `;
}
