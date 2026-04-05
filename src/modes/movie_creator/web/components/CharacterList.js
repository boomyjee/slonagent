import { html } from '../lib.js';
import { EntityList } from '../common/EntityList.js';

export function CharacterList() {
    return html`<${EntityList}
        title="Characters"
        collection="characters"
        canCreate=${true}
        renderItem=${char => {
            const primary = char.generations?.[char.primary_generation_id];
            const thumb = primary?.file ? `/api/asset/${primary.file}` : null;
            return html`
                ${thumb
                    ? html`<img class="thumb" src=${thumb} />`
                    : html`<div class="thumb"></div>`}
                <span class="name">${char.name || 'Unnamed'}</span>
            `;
        }}
    />`;
}
