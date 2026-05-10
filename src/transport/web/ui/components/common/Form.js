// Form provides draft context to children. Field components (Text, Textarea)
// read value by name and call onChange. Usage:
//
//   <${Form} draft=${draft} onChange=${setDraft}>
//       <${Text} name="title" label="Title" placeholder="..." />
//       <${Textarea} name="text" label="Text" placeholder="..." grow />
//   <//>
import { html, createContext, useContext, css } from '../../lib.js';

const FormCtx = createContext();
const cl = {};

export function Form({ draft, onChange, children }) {
    return html`<${FormCtx.Provider} value=${{ draft, onChange }}>${children}<//>`;
}

export function useField(name) {
    const { draft, onChange } = useContext(FormCtx);
    return {
        value: draft[name] || '',
        set: v => onChange({ ...draft, [name]: v }),
    };
}

export function Select({ name, label, options }) {
    const f = useField(name);
    return html`
        <div class=${cl.field}>
            <label>${label}</label>
            <select value=${f.value} onChange=${e => f.set(e.target.value)}>
                ${options.map(o => html`<option value=${o.id}>${o.label}</option>`)}
            </select>
        </div>
    `;
}

export function Text({ name, label, placeholder, type, min, max, step }) {
    const f = useField(name);
    const isNumber = type === 'number';
    return html`
        <div class=${cl.field}>
            <label>${label}</label>
            <input type=${type || 'text'} placeholder=${placeholder || ''} value=${f.value}
                min=${min} max=${max} step=${step}
                onInput=${e => f.set(isNumber ? Number(e.target.value) : e.target.value)} />
        </div>
    `;
}

export function Slider({ name, label, min, max, step, unit }) {
    const f = useField(name);
    return html`
        <div class=${cl.field}>
            <label>
                <span>${label}</span>
                <span class=${cl.sliderValue}>${f.value}${unit || ''}</span>
            </label>
            <input type="range" class=${cl.sliderInput}
                min=${min} max=${max} step=${step || 1}
                value=${f.value}
                onInput=${e => f.set(parseInt(e.target.value, 10))} />
        </div>
    `;
}

export function Toggle({ name, label }) {
    const f = useField(name);
    return html`
        <div class=${cl.field}>
            <label>${label}</label>
            <div class=${cl.toggle + (f.value ? ' on' : '')} onClick=${() => f.set(!f.value)}>
                <div class="thumb" />
            </div>
        </div>
    `;
}

export function Textarea({ name, label, placeholder, grow }) {
    const f = useField(name);
    return html`
        <div class=${cl.field + (grow ? ' ' + cl.grow : '')}>
            <label>${label}</label>
            <textarea placeholder=${placeholder || ''} value=${f.value} onInput=${e => f.set(e.target.value)}></textarea>
        </div>
    `;
}

// --- styles ---

cl.field = css`
  display: flex; flex-direction: column; gap: 4px;
  & label { font-size: 11px; color: var(--text-dim);
            text-transform: uppercase; letter-spacing: 0.5px;
            display: flex; justify-content: space-between; align-items: baseline; }
  & input, & textarea, & select {
    background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 10px; font-size: 13px; font-family: inherit;
    width: 100%; box-sizing: border-box;
  }
  & input:focus, & textarea:focus, & select:focus {
    outline: none; border-color: var(--accent);
  }
  & select {
    appearance: none; padding-right: 32px;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23888' stroke-width='1.5' fill='none'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 10px center;
  }
  & textarea { resize: vertical; min-height: 120px; line-height: 1.6; }
`;

cl.grow = css`
  flex: 1 0 auto; min-height: 140px;
  & textarea { flex: 1; resize: none; min-height: 300px; }
`;

cl.sliderValue = css`
  font-family: monospace; color: var(--text); font-size: 12px;
  text-transform: none; letter-spacing: 0;
`;
cl.sliderInput = css`
  width: 100%; accent-color: var(--accent);
`;

cl.toggle = css`
  width: 40px; height: 22px; background: #555; border-radius: 11px;
  position: relative; cursor: pointer; transition: background .2s;
  &.on { background: var(--accent); }
  & .thumb {
    width: 18px; height: 18px; background: #fff; border-radius: 50%;
    position: absolute; top: 2px; left: 2px; transition: left .2s;
  }
  &.on .thumb { left: 20px; }
`;
