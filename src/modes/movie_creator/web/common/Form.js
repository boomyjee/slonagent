// Form provides draft context to children. Field components (Text, Textarea)
// read value by name and call onChange. Usage:
//
//   <${Form} draft=${draft} onChange=${setDraft}>
//       <${Text} name="title" label="Title" placeholder="..." />
//       <${Textarea} name="text" label="Text" placeholder="..." grow />
//   <//>
import { html, createContext, useContext } from '../lib.js';

const FormCtx = createContext();

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
        <div class="field">
            <label>${label}</label>
            <select value=${f.value} onChange=${e => f.set(e.target.value)}>
                ${options.map(o => html`<option value=${o.id}>${o.label}</option>`)}
            </select>
        </div>
    `;
}

export function Text({ name, label, placeholder }) {
    const f = useField(name);
    return html`
        <div class="field">
            <label>${label}</label>
            <input type="text" placeholder=${placeholder || ''} value=${f.value} onInput=${e => f.set(e.target.value)} />
        </div>
    `;
}

export function Textarea({ name, label, placeholder, grow }) {
    const f = useField(name);
    return html`
        <div class=${'field' + (grow ? ' grow' : '')}>
            <label>${label}</label>
            <textarea placeholder=${placeholder || ''} value=${f.value} onInput=${e => f.set(e.target.value)}></textarea>
        </div>
    `;
}
