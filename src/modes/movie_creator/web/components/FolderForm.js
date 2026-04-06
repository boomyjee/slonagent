import { html } from '../lib.js';
import { Text } from '../common/Form.js';
import { Gallery } from './Gallery.js';

export function FolderForm() {
    return html`<${Text} name="name" label="Name" placeholder="Folder name" />`;
}

export const folderExtra = (folder, path) => html`
    <${Gallery} entity=${folder} path=${path} kind="reference" defaultPrompt=${() => ''} />
`;
