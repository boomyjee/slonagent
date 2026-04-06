import { html, characterPrompt } from '../lib.js';
import { Text, Textarea } from '../common/Form.js';
import { Gallery } from './Gallery.js';

export function CharacterForm() {
    return html`
        <${Text} name="name" label="Name" placeholder="Character name" />
        <${Textarea} name="description" label="Description" placeholder="Role, personality, motivation..." grow />
        <${Textarea} name="appearance" label="Appearance" placeholder="Age, height, hair, clothing..." grow />
    `;
}

export const characterExtra = char => html`
    <${Gallery}
        entity=${char}
        path=${['characters', char.id]}
        kind="portrait"
        defaultPrompt=${() => characterPrompt(char)}
    />
`;
