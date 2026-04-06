import { html } from '../lib.js';
import { Textarea } from '../common/Form.js';
import { Gallery } from './Gallery.js';

function shotPrompt(shot) {
    return `Cinematic film still. ${shot.description || ''}. Cinematic lighting, shallow depth of field.`;
}

export function ShotForm() {
    return html`<${Textarea} name="description" label="Description" placeholder="Framing, action, camera, dialogue..." grow />`;
}

export const shotExtra = (shot, path) => html`
    <${Gallery} entity=${shot} path=${path} kind="frame" defaultPrompt=${() => shotPrompt(shot)} />
`;
