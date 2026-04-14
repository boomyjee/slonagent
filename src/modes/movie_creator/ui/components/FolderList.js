import { html } from '../lib.js';
import { EntityList } from '../common/EntityList.js';

export function FolderList() {
    return html`<${EntityList}
        title="Folders"
        collection="library"
        canCreate=${true}
        renderItem=${folder => html`<span class="name">${folder.name || 'Untitled'}</span>`}
    />`;
}
