import { html, css } from '../lib.js';

const cl = {};

// Generic tab bar. Each tab: { id, label, closable?, dirty?, diskChanged? }.
// `closable: false` hides the close button (used for pinned tabs like Logs).
export function Tabs({ tabs, active, onSelect, onClose }) {
    return html`<div class=${cl.tabs}>
        ${tabs.map(t => html`
            <div key=${t.id}
                 class="${cl.tab}${active === t.id ? ' active' : ''}${t.diskChanged ? ' disk' : t.dirty ? ' dirty' : ''}"
                 onClick=${() => onSelect(t.id)}>
                <span class="dot"></span>
                <span>${t.label}</span>
                ${t.closable !== false && html`
                    <span class="close" onClick=${e => { e.stopPropagation(); onClose(t.id); }}>×</span>`}
            </div>`)}
    </div>`;
}

cl.tabs = css`
  display: flex; background: var(--surface); border-bottom: 1px solid var(--border);
  min-height: 35px; overflow-x: auto; flex-shrink: 0;
`;
cl.tab = css`
  display: flex; align-items: center; padding: 0 12px; height: 35px; cursor: pointer;
  border-right: 1px solid var(--border); font-size: 13px; white-space: nowrap;
  color: var(--text-dim); gap: 6px;
  &.active { background: var(--bg); color: var(--text); }
  & .close { font-size: 14px; opacity: 0.5; cursor: pointer; }
  & .close:hover { opacity: 1; }
  & .dot { display: none; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  &.dirty .dot { display: block; background: var(--text-dim); }
  &.disk .dot { display: block; background: var(--warn); }
`;
