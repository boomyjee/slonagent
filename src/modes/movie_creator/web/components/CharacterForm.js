import { html } from '../lib.js';
import { Text, Textarea } from '../common/Form.js';
import { Gallery } from './Gallery.js';

export function CharacterForm() {
    return html`
        <${Text} name="name" label="Name" placeholder="Character name" />
        <${Textarea} name="description" label="Description" placeholder="Role, personality, motivation..." grow />
        <${Textarea} name="appearance" label="Appearance" placeholder="Age, height, hair, clothing..." grow />
        <${Gallery} kind="portrait" defaultPrompt=${char => {
            const appearance = char.appearance || 'a film character';
            return `Cinematic portrait of ${char.name || 'character'}. ${appearance}. Head and shoulders, cinematic lighting, film still, shallow depth of field.`;
        }} />
    `;
}
