import { html, Component } from '../lib.js';
import { app } from '../app.js';

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

        return html`
            <span class=${'gen-indicator' + (pending ? ' active' : active ? ' has-items' : '')} onClick=${active ? () => this.setState({ open: !open }) : null}>
                ${active ? `${done}/${total}` : '0'} \u2699
                ${open && active && html`
                    <div class="gen-dropdown">
                        ${items.map(g => html`
                            <div class="gen-dropdown-item" onClick=${e => { e.stopPropagation(); app.select(g.ownerPath); this.setState({ open: false }); }}>
                                <span class=${'gen-item-icon ' + g.status}>\u2699</span>
                                <div class="gen-item-body">
                                    <div class="gen-prompt">${(g.prompt || '').slice(0, 80) || '(no prompt)'}</div>
                                    <div class="gen-model">${g.model}</div>
                                </div>
                                ${g.status !== 'generating' && html`<span class="gen-item-dismiss" onClick=${e => dismiss(e, g.id)}>\u2715</span>`}
                            </div>
                        `)}
                    </div>
                `}
            </span>
        `;
    }
}
