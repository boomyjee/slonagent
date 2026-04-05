// Sidebar list of characters with circular portrait thumbs.
import { html } from '../lib.js';

export function CharacterList({ characters, selectedId, onSelect, onAdd }) {
    const items = Object.values(characters || {});
    return html`
        <div class="sidebar-header">
            <span>Characters</span>
            <button class="btn btn-sm btn-primary" onClick=${onAdd}>+ Add</button>
        </div>
        <div class="scene-list">
            ${items.length === 0
                ? html`<div class="list-empty">No characters yet</div>`
                : items.map(char => {
                    const thumb = char.image ? `/api/asset/${char.image}` : null;
                    return html`
                        <div
                            class=${'char-item' + (char.id === selectedId ? ' active' : '')}
                            onClick=${() => onSelect(char.id)}
                        >
                            ${thumb
                                ? html`<img class="thumb" src=${thumb} />`
                                : html`<div class="thumb"></div>`}
                            <span class="name">${char.name || 'Unnamed'}</span>
                        </div>
                    `;
                })}
        </div>
    `;
}
