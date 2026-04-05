import { html } from '../lib.js';

export function EntityList({ schema, items, selectedId, onSelect, onAdd }) {
    return html`
        <div class="sidebar-header">
            <span>${schema.plural}</span>
            <button class="btn btn-sm btn-primary" onClick=${onAdd}>+ Add</button>
        </div>
        <div class="scene-list">
            ${items.length === 0 ? html`
                <div class="list-empty">${schema.emptyText}</div>
            ` : items.map((item, i) => html`
                <${EntityListItem}
                    schema=${schema}
                    item=${item}
                    index=${i}
                    active=${item.id === selectedId}
                    onClick=${() => onSelect(item)}
                />
            `)}
        </div>
    `;
}

function EntityListItem({ schema, item, index, active, onClick }) {
    const title = item[schema.titleField] || schema.emptyTitle;
    if (schema.thumb) {
        const thumbSrc = item[schema.thumb] ? `/api/asset/${item[schema.thumb]}` : null;
        return html`
            <div class=${'char-item' + (active ? ' active' : '')} onClick=${onClick}>
                ${thumbSrc
                    ? html`<img class="thumb" src=${thumbSrc} />`
                    : html`<div class="thumb"></div>`}
                <span class="name">${title}</span>
            </div>
        `;
    }
    return html`
        <div class=${'scene-item' + (active ? ' active' : '')} onClick=${onClick}>
            ${schema.numbered ? html`<span class="num">${index + 1}</span>` : null}
            <span class="name">${title}</span>
        </div>
    `;
}
