// Generic sidebar list. Caller provides title, collection key, canCreate flag,
// and renderItem(item, index, isActive) for custom row content.
import { html, css } from '../lib.js';
import { app } from '../app.js';
import { Button } from './FormView.js';

const cl = {};

export function EntityList({ title, collection, canCreate, renderItem }) {
    const items = Object.values(app.state.project[collection] || {});
    const sp = app.state.selectedPath;
    const selectedId = sp?.[0] === collection ? sp[1] : null;
    return html`
        <div class=${cl.sidebarHeader}>
            <span>${title}</span>
            ${canCreate ? html`<${Button} sm variant="primary" onClick=${() => app.select([collection, '__new__'])}>+ Add<//>` : null}
        </div>
        <div class=${cl.list}>
            ${items.length === 0
                ? html`<div class=${cl.empty}>No ${title.toLowerCase()} yet</div>`
                : items.map((item, i) => html`
                    <div
                        class=${cl.item + (item.id === selectedId ? ' active' : '')}
                        onClick=${() => app.select([collection, item.id])}
                    >${renderItem(item, i)}</div>
                `)}
        </div>
    `;
}

// --- styles ---

cl.sidebarHeader = css`
  display: flex; align-items: center;
  padding: 8px 12px; border-bottom: 1px solid var(--border);
  gap: 8px; min-height: 38px;
  & span { font-size: 12px; color: var(--text-dim);
           text-transform: uppercase; flex: 1; }
`;

cl.list = css`flex: 1; overflow-y: auto;`;

cl.empty = css`padding: 20px; color: var(--text-dim); font-size: 13px; text-align: center;`;

// Row children (num/name/thumb) use literal classes — only meaningful under
// cl.item, so no cross-scope leak.
cl.item = css`
  display: flex; align-items: center; padding: 10px 12px; cursor: pointer;
  border-bottom: 1px solid var(--border); gap: 8px; font-size: 13px;
  &:hover { background: var(--surface2); }
  &.active { background: var(--accent); color: #1e1e2e; }
  & .num { font-size: 11px; font-weight: 600; min-width: 20px; opacity: 0.6; }
  & .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  & .thumb {
    width: 32px; height: 32px; border-radius: 50%;
    object-fit: cover; flex-shrink: 0; background: var(--surface3);
  }
`;
