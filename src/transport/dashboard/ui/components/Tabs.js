import { html, Component, css } from '../lib.js';

const cl = {};

// Generic tab bar. Each tab: { id, label, closable?, dirty?, diskChanged? }.
// `closable: false` hides the close button (used for pinned tabs like Logs).
//
// `contextItems(tab) → [{label, action, disabled?}]` — supplies the right-click
// menu. Items with disabled:true render greyed out and don't fire.
export class Tabs extends Component {
    constructor(props) {
        super(props);
        this.state = { menu: null, dragInsertAt: null };
    }
    componentDidMount() {
        document.addEventListener('click', this._closeMenu);
    }
    componentWillUnmount() {
        document.removeEventListener('click', this._closeMenu);
        this._stopDrag();
    }
    _closeMenu = () => { if (this.state.menu) this.setState({ menu: null }); };

    _onContextMenu = (e, tab) => {
        const items = this.props.contextItems?.(tab) || [];
        if (!items.length) return;
        e.preventDefault();
        this.setState({ menu: { x: e.clientX, y: e.clientY, items } });
    };

    _startDrag = (e, tab) => {
        // Only left button, only on draggable tabs, never on the close ×.
        if (e.button !== 0 || tab.closable === false) return;
        if (e.target.classList?.contains('close')) return;
        e.preventDefault();
        this._dragId = tab.id;
        document.addEventListener('mousemove', this._onMouseMove);
        document.addEventListener('mouseup', this._onMouseUp);
        document.body.style.cursor = 'grabbing';
    };

    _onMouseMove = (e) => {
        if (!this._dragId || !this._barEl) return;
        const insertAt = this._computeInsertAt(e.clientX);
        if (insertAt !== this.state.dragInsertAt) this.setState({ dragInsertAt: insertAt });
    };

    _computeInsertAt(clientX) {
        // Position is an index in [0..N] — slot between els[i-1] and els[i].
        // Skip slots that would land on the moving tab itself (no-op).
        const els = [...this._barEl.querySelectorAll('[data-tab-id]')];
        for (let i = 0; i < els.length; i++) {
            const r = els[i].getBoundingClientRect();
            if (clientX < r.left + r.width / 2) {
                if (els[i].dataset.tabId === this._dragId) return null;
                if (i > 0 && els[i - 1].dataset.tabId === this._dragId) return null;
                return i;
            }
        }
        // Past the right edge of the last tab.
        if (els.length && els[els.length - 1].dataset.tabId === this._dragId) return null;
        return els.length;
    }

    _onMouseUp = () => {
        const at = this.state.dragInsertAt;
        if (this._dragId && at != null) {
            this.props.onReorder?.(this._dragId, at);
        }
        this._stopDrag();
    };

    _stopDrag = () => {
        this._dragId = null;
        document.removeEventListener('mousemove', this._onMouseMove);
        document.removeEventListener('mouseup', this._onMouseUp);
        document.body.style.cursor = '';
        if (this.state.dragInsertAt !== null) this.setState({ dragInsertAt: null });
    };

    render({ tabs, active, onSelect, onClose }, { menu, dragInsertAt }) {
        return html`<div class=${cl.tabs} ref=${el => this._barEl = el}>
            ${tabs.map((t, i) => {
                const insertHere = dragInsertAt === i ? ' drag-before' :
                    (dragInsertAt === tabs.length && i === tabs.length - 1) ? ' drag-after' : '';
                return html`<div key=${t.id}
                     data-tab-id=${t.id}
                     class="${cl.tab}${active === t.id ? ' active' : ''}${t.diskChanged ? ' disk' : t.dirty ? ' dirty' : ''}${insertHere}"
                     onMouseDown=${e => this._startDrag(e, t)}
                     onClick=${() => onSelect(t.id)}
                     onContextMenu=${e => this._onContextMenu(e, t)}>
                    <span class="dot"></span>
                    <span>${t.label}</span>
                    ${t.closable !== false && html`
                        <span class="close" onClick=${e => { e.stopPropagation(); onClose(t.id); }}>×</span>`}
                </div>`;
            })}
            ${menu && html`<div class=${cl.menu} style=${{left: menu.x + 'px', top: menu.y + 'px'}}
                                onClick=${e => e.stopPropagation()}>
                ${menu.items.map((it, i) => html`
                    <div key=${i} class="${cl.menuItem}${it.disabled ? ' disabled' : ''}"
                         onClick=${() => { if (!it.disabled) { it.action(); this._closeMenu(); } }}>
                        ${it.label}
                    </div>`)}
            </div>`}
        </div>`;
    }
}

cl.tabs = css`
  display: flex; background: var(--surface); border-bottom: 1px solid var(--border);
  min-height: 35px; overflow-x: auto; flex-shrink: 0;
  min-width: 0;
`;
cl.tab = css`
  display: flex; align-items: center; padding: 0 12px; height: 35px; cursor: pointer;
  border-right: 1px solid var(--border); font-size: 13px; white-space: nowrap;
  color: var(--text-dim); gap: 6px; user-select: none;
  &.active { background: var(--bg); color: var(--text); }
  & .close { font-size: 14px; opacity: 0.5; cursor: pointer; }
  & .close:hover { opacity: 1; }
  & .dot { display: none; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  &.dirty .dot { display: block; background: var(--text-dim); }
  &.disk .dot { display: block; background: var(--warn); }
  &.drag-before { box-shadow: inset 2px 0 0 var(--accent); }
  &.drag-after { box-shadow: inset -2px 0 0 var(--accent); }
`;
cl.menu = css`
  position: fixed; z-index: 200; min-width: 160px;
  background: var(--surface2); border: 1px solid var(--border);
  box-shadow: 0 4px 12px rgba(0,0,0,0.4); padding: 4px 0;
`;
cl.menuItem = css`
  padding: 6px 12px; font-size: 12px; color: var(--text); cursor: pointer;
  &:hover { background: var(--accent); color: #1e1e2e; }
  &.disabled { color: var(--text-dim); cursor: default; }
  &.disabled:hover { background: transparent; color: var(--text-dim); }
`;
