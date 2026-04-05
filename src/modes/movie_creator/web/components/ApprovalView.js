// All AI approvals as inner dialog content. Entity approvals (scene/character/
// shot) use EntityForm for fields; shots_bulk is a single textarea; portrait
// is a prompt field. Caller passes onApprove(data) and onReject() — these
// are invoked before Dialog closes (close handled here).
import { html, useState, APPROVAL_SCHEMAS } from '../lib.js';
import { EntityForm } from '../common/EntityForm.js';
import { Dialog } from '../common/Dialog.js';

const BULK_SEP = '\n\n---\n\n';

export function ApprovalView({ kind, data, onApprove, onReject }) {
    const approve = payload => { onApprove(payload); Dialog.close(); };
    const reject = () => { onReject(); Dialog.close(); };

    if (kind === 'shots_bulk') {
        return html`<${BulkShotsApproval} data=${data} onApprove=${approve} onReject=${reject} />`;
    }
    if (kind === 'portrait') {
        return html`<${PortraitApproval} data=${data} onApprove=${approve} onReject=${reject} />`;
    }
    const schema = APPROVAL_SCHEMAS[kind];
    if (!schema) {
        return html`<${Shell} title=${'Unknown approval: ' + kind} onReject=${reject}>
            <div class="field">Cannot render approval of kind "${kind}".</div>
        <//>`;
    }
    return html`<${EntityApproval} schema=${schema} data=${data} onApprove=${approve} onReject=${reject} />`;
}

function EntityApproval({ schema, data, onApprove, onReject }) {
    const [draft, setDraft] = useState(() => ({ ...(data || {}) }));
    return html`<${Shell}
        title=${`AI Proposal — ${schema.label}`}
        onReject=${onReject}
        onApprove=${() => onApprove(draft)}
    >
        <${EntityForm} schema=${schema} draft=${draft} onChange=${setDraft} />
    <//>`;
}

function BulkShotsApproval({ data, onApprove, onReject }) {
    const [text, setText] = useState(data?.text || '');
    const count = text.split(BULK_SEP).filter(s => s.trim()).length;
    return html`<${Shell}
        title=${`AI Proposal — Storyboard (${count} shots)`}
        approveLabel=${`Approve ${count} shots`}
        onReject=${onReject}
        onApprove=${() => onApprove({ text, scene_id: data?.scene_id })}
    >
        <div class="field grow">
            <label>Shot descriptions (separated by <code>---</code>)</label>
            <textarea value=${text} onInput=${e => setText(e.target.value)}></textarea>
        </div>
    <//>`;
}

function PortraitApproval({ data, onApprove, onReject }) {
    const [prompt, setPrompt] = useState(data?.prompt || '');
    return html`<${Shell}
        title=${`Portrait: ${data?.character_name || ''}`}
        approveLabel="Approve & Generate"
        onReject=${onReject}
        onApprove=${() => onApprove({
            prompt,
            character_id: data?.character_id,
            character_name: data?.character_name,
        })}
    >
        <div class="field grow">
            <label>Prompt</label>
            <textarea
                value=${prompt}
                onInput=${e => setPrompt(e.target.value)}
                placeholder="Describe the image..."
            ></textarea>
        </div>
    <//>`;
}

function Shell({ title, onReject, onApprove, approveLabel = 'Approve', children }) {
    return html`
        <div class="modal-header"><h2>${title}</h2></div>
        <div class="modal-body">${children}</div>
        <div class="modal-footer">
            <button class="btn btn-danger" onClick=${onReject}>Reject</button>
            <div class="spacer"></div>
            ${onApprove ? html`<button class="btn btn-primary" onClick=${onApprove}>${approveLabel}</button>` : null}
        </div>
    `;
}
