// Renders AI-proposed scene/character/shot as an editable form for user
// approval. Same EntityForm shell — only the footer changes via mode.
import { html, APPROVAL_SCHEMAS } from '../lib.js';
import { EntityForm } from './EntityForm.js';

export function ApprovalView({ kind, data, onApprove, onReject }) {
    const schema = APPROVAL_SCHEMAS[kind];
    if (!schema) {
        return html`<div class="center-empty">Unknown approval: ${kind}</div>`;
    }
    return html`<${EntityForm}
        schema=${schema}
        initial=${data}
        mode="approval"
        onSubmit=${onApprove}
        onCancel=${onReject}
    />`;
}
