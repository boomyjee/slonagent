// Generic sidebar list. Caller provides title, collection key, canCreate flag,
// and renderItem(item, index, isActive) for custom row content.
import { html } from '../lib.js';
import { app } from '../app.js';

export function EntityList({ title, collection, canCreate, renderItem }) {
    const items = Object.values(app.state.project[collection] || {});
    const selectedId = app.state.selected[collection];
    return html`
        <div class="sidebar-header">
            <span>${title}</span>
            ${canCreate ? html`<button class="btn btn-sm btn-primary" onClick=${() => app.selectEntity(collection, '__new__')}>+ Add</button>` : null}
        </div>
        <div class="entity-list">
            ${items.length === 0
                ? html`<div class="list-empty">No ${title.toLowerCase()} yet</div>`
                : items.map((item, i) => html`
                    <div
                        class=${'entity-item' + (item.id === selectedId ? ' active' : '')}
                        onClick=${() => app.selectEntity(collection, item.id)}
                    >${renderItem(item, i)}</div>
                `)}
        </div>
    `;
}
