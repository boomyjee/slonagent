import { html, Component, css, keyframes } from '../lib.js';
import { app } from '../app.js';

const cl = {};

function collectGenerations(project) {
    const result = {};
    function walk(obj, path) {
        if (!obj || typeof obj !== 'object') return;
        for (const [key, val] of Object.entries(obj)) {
            if (!val || typeof val !== 'object') continue;
            if (val.status && val.id) {
                result[val.id] = { id: val.id, ownerPath: path.slice(0, -1), status: val.status, model: val.model, prompt: val.prompt };
            } else {
                walk(val, [...path, key]);
            }
        }
    }
    walk(project, []);
    return result;
}

export class GenerationIndicator extends Component {
    constructor(props) {
        super(props);
        this.state = { open: false };
        this._tracked = new Set();
        this._dismissed = new Set();
    }

    render() {
        const all = collectGenerations(app.state.project);

        // auto-track new generating items
        for (const [id, gen] of Object.entries(all)) {
            if (gen.status === 'generating' && !this._dismissed.has(id)) {
                this._tracked.add(id);
            }
        }

        // build visible list
        const items = [];
        for (const id of this._tracked) {
            if (this._dismissed.has(id)) continue;
            const gen = all[id];
            if (gen) items.push(gen);
        }

        const done = items.filter(g => g.status !== 'generating').length;
        const total = items.length;
        const active = total > 0;
        const pending = done < total;
        const { open } = this.state;

        const dismiss = (e, id) => {
            e.stopPropagation();
            this._dismissed.add(id);
            this._tracked.delete(id);
            this.forceUpdate();
        };

        const indicatorCls = cl.indicator
            + (pending ? ' pending' : active ? ' has-items' : '');

        return html`
            <span class=${indicatorCls} onClick=${active ? () => this.setState({ open: !open }) : null}>
                ${active ? `${done}/${total}` : '0'} \u2699
                ${open && active && html`
                    <div class=${cl.dropdown}>
                        ${items.map(g => html`
                            <div class=${cl.item} onClick=${e => { e.stopPropagation(); app.select(g.ownerPath); this.setState({ open: false }); }}>
                                <span class=${cl.icon + ' ' + g.status}>\u2699</span>
                                <div class=${cl.body}>
                                    <div class=${cl.prompt}>${(g.prompt || '').slice(0, 80) || '(no prompt)'}</div>
                                    <div class=${cl.model}>${g.model}</div>
                                </div>
                                ${g.status !== 'generating' && html`<span class=${cl.dismiss} onClick=${e => dismiss(e, g.id)}>\u2715</span>`}
                            </div>
                        `)}
                    </div>
                `}
            </span>
        `;
    }
}

// --- styles ---

const genPulse = keyframes`0%,100% { color: var(--text-dim); } 50% { color: var(--warn); }`;

cl.indicator = css`
  font-size: 12px; color: var(--text-dim);
  padding: 6px 16px; position: relative;
  user-select: none; margin-left: auto; margin-right: 8px;
  &.has-items { color: var(--green); cursor: pointer; }
  &.pending { color: var(--warn); cursor: pointer; animation: ${genPulse} 1.5s infinite; }
  &.has-items:hover, &.pending:hover { color: var(--text); }
`;

cl.dropdown = css`
  position: absolute; right: 16px; top: 100%;
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: 6px; min-width: 260px; z-index: 100;
  padding: 4px 0; box-shadow: 0 4px 12px rgba(0,0,0,.4);
`;

cl.item = css`
  padding: 6px 12px; cursor: pointer;
  display: flex; align-items: flex-start; gap: 8px;
  &:hover { background: var(--surface3); }
`;

cl.icon = css`
  font-size: 14px; flex-shrink: 0; line-height: 1;
  &.generating { color: var(--warn); animation: ${genPulse} 1.5s infinite; }
  &.done { color: var(--green); }
  &.failed { color: var(--red); }
`;

cl.body = css`min-width: 0; flex: 1;`;

cl.dismiss = css`
  color: var(--text-dim); font-size: 11px;
  flex-shrink: 0; padding: 0 2px; cursor: pointer; line-height: 1;
  &:hover { color: var(--red); }
`;

cl.model = css`color: var(--text-dim); font-size: 10px;`;

cl.prompt = css`
  font-size: 11px; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
`;
