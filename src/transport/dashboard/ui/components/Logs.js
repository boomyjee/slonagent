import { html, Component, css } from '../lib.js';

const cl = {};
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const CATS = ['agent', 'memory', 'transport'];

export class Logs extends Component {
    constructor(props) {
        super(props);
        this.state = { category: 'agent' };
    }
    render({ logs }, { category }) {
        const items = logs[category] || [];
        return html`<div class=${cl.wrap}>
            <div class=${cl.bar}>
                ${CATS.map(c => html`
                    <div class="${cl.btn}${category === c ? ' active' : ''}"
                         onClick=${() => this.setState({category: c})}>
                        ${c.charAt(0).toUpperCase() + c.slice(1)}
                    </div>`)}
            </div>
            <div class=${cl.pane}>
                ${items.map(l => {
                    const m = l.text.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),?\d*\s+(.*)/s);
                    const ts = m ? m[1] : '';
                    const body = m ? m[2] : l.text;
                    return html`<div class="${cl.log} ${l.level}">${ts && html`<span class="ts">${ts}</span>`}${esc(body)}</div>`;
                })}
            </div>
        </div>`;
    }
}

cl.wrap = css`flex: 1; display: flex; flex-direction: column; overflow: hidden; min-height: 0;`;
cl.bar = css`
  display: flex; background: var(--surface); border-bottom: 1px solid var(--border);
  flex-shrink: 0;
`;
cl.btn = css`
  padding: 6px 16px; cursor: pointer; font-size: 12px; color: var(--text-dim);
  border-bottom: 2px solid transparent;
  &:hover { color: var(--text); }
  &.active { color: var(--accent); border-bottom-color: var(--accent); }
`;
cl.pane = css`flex: 1; overflow: auto; padding: 12px;`;
cl.log = css`
  font-size: 12px; line-height: 1.7; white-space: pre;
  & .ts { color: var(--text-dim); margin-right: 8px; font-size: 11px; }
  &.DEBUG { color: var(--text-dim); }
  &.INFO { color: var(--green); }
  &.WARNING { color: var(--warn); }
  &.ERROR { color: var(--red); }
  &.CRITICAL { color: var(--red); font-weight: bold; }
`;
