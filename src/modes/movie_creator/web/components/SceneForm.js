import { html } from '../lib.js';
import { Text, Textarea } from '../common/Form.js';

export function SceneForm() {
    return html`
        <${Text} name="title" label="Title" placeholder="Scene title" />
        <${Text} name="location" label="Location" placeholder="INT. APARTMENT - NIGHT" />
        <${Textarea} name="text" label="Scene text" placeholder="Scene description and dialogue..." grow />
    `;
}
