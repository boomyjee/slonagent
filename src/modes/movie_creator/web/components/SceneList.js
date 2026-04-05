// Sidebar list of scenes. Operates on live dict (insertion order = display order).
import { html } from '../lib.js';

export function SceneList({ scenes, selectedId, onSelect, onAdd }) {
    const items = Object.values(scenes || {});
    return html`
        <div class="sidebar-header">
            <span>Scenes</span>
            ${onAdd ? html`<button class="btn btn-sm btn-primary" onClick=${onAdd}>+ Add</button>` : null}
        </div>
        <div class="scene-list">
            ${items.length === 0
                ? html`<div class="list-empty">No scenes yet</div>`
                : items.map((scene, i) => html`
                    <div
                        class=${'scene-item' + (scene.id === selectedId ? ' active' : '')}
                        onClick=${() => onSelect(scene.id)}
                    >
                        <span class="num">${i + 1}</span>
                        <span class="name">${scene.title || 'Untitled'}</span>
                    </div>
                `)}
        </div>
    `;
}
