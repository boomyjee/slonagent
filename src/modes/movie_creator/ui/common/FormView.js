import { html, useState, css } from '../lib.js';
import { Form } from './Form.js';

const cl = {};

// Variant → scoped width modifier class. `undefined` = no width (fills center).
const VARIANT_CLS = {};

// Styled button. `variant` picks the color scheme ('primary' | 'danger'),
// `sm` flips compact size, `class` appends extra scoped modifiers (e.g. for
// parent positioning rules via `& .delete`).
export function Button({ variant, sm, class: extra, title, onClick, children }) {
    const mods = cl.btn
        + (sm      ? ' btn-sm'             : '')
        + (variant ? ' btn-' + variant     : '')
        + (extra   ? ' ' + extra           : '');
    return html`<button class=${mods} title=${title} onClick=${onClick}>${children}</button>`;
}

export function FormView({ heading, entity, left, right, variant, children }) {
    const [draft, setDraft] = useState(() => ({ ...entity }));

    const btn = ({ label, cls, onClick }) =>
        html`<${Button} variant=${cls} onClick=${onClick}>${label}<//>`;

    const editorCls = cl.editor + (VARIANT_CLS[variant] ? ' ' + VARIANT_CLS[variant] : '');

    return html`
        <div class=${editorCls}>
            <div class=${cl.header}><h2>${heading}</h2></div>
            <div class=${cl.body}>
                <${Form} draft=${draft} onChange=${setDraft}>
                    ${children}
                <//>
            </div>
            <div class=${cl.footer}>
                ${(left?.(draft) || []).map(btn)}
                <div class="spacer"></div>
                ${(right?.(draft) || []).map(btn)}
            </div>
        </div>
    `;
}

// --- styles ---

cl.editor = css`
  flex: 1; display: flex; flex-direction: column; overflow: hidden;
`;

cl.centerEmpty = css`
  flex: 1; display: flex; align-items: center; justify-content: center;
  color: var(--text-dim); font-size: 14px;
`;

// Variants used inside Dialog (modal). Widths are set on the editor — the
// modal shell adapts to content via max-width.
cl.wide     = css`width: 480px;`;
cl.widest   = css`width: 720px;`;
cl.generate = css`width: 800px;`;

VARIANT_CLS.wide = cl.wide;
VARIANT_CLS.widest = cl.widest;
VARIANT_CLS.generate = cl.generate;

cl.header = css`
  display: flex; align-items: center;
  padding: 8px 20px; border-bottom: 1px solid var(--border); gap: 10px;
  & h2 { font-size: 16px; font-weight: 500; flex: 1; }
`;

cl.body = css`
  flex: 1; padding: 20px; overflow-y: auto;
  display: flex; flex-direction: column; gap: 16px; min-height: 0;
`;

cl.footer = css`
  display: flex; gap: 8px; padding: 12px 20px; border-top: 1px solid var(--border);
  & .spacer { flex: 1; }
`;

cl.btn = css`
  padding: 6px 14px; border: 1px solid var(--border); border-radius: 6px;
  cursor: pointer; font-size: 13px; background: var(--surface2);
  color: var(--text); font-family: inherit;
  &:hover { background: var(--surface3); }
  &.btn-sm { padding: 2px 8px; font-size: 11px; }
  &.btn-primary { background: var(--accent); color: #1e1e2e; border-color: var(--accent); }
  &.btn-primary:hover { background: var(--accent); opacity: 0.85; }
  &.btn-danger { background: var(--red); color: #1e1e2e; border-color: var(--red); }
  &.btn-danger:hover { background: var(--red); opacity: 0.85; }
`;

// Editor shell classes so other views (StoryboardView etc) can compose the
// same chrome without going through FormView's draft/state machinery.
// `centerEmpty` is the "nothing selected" placeholder shown in the center pane.
export const editorCls = {
    editor:      cl.editor,
    header:      cl.header,
    body:        cl.body,
    footer:      cl.footer,
    centerEmpty: cl.centerEmpty,
};
