import { html } from '../lib.js';
import { app } from '../app.js';
import { FormView } from './FormView.js';
import { Dialog } from './Dialog.js';

// Approval kinds that need a wider modal (big textareas).
const WIDEST_KINDS = new Set(['create_scene', 'update_scene', 'create_shots_bulk']);

function resolve(msg) {
    msg.resolved = true;
    app.forceUpdate();
    Dialog.close();
}

export function ApproveView({ label, approval_message, children }) {
    const entity = approval_message.data.fields || approval_message.data || {};
    const variant = WIDEST_KINDS.has(approval_message.approvalKind) ? 'widest' : 'wide';

    return html`<${FormView}
        heading=${'AI Proposal — ' + label}
        entity=${entity}
        variant=${variant}
        left=${() => [{ label: 'Reject', cls: 'danger', onClick: () => {
            app.send({ type: 'approval_response', action: 'reject', reason: prompt('Reason (optional):') || '' });
            resolve(approval_message);
        }}]}
        right=${draft => [{ label: 'Approve', cls: 'primary', onClick: () => {
            app.send({ type: 'approval_response', action: 'approve', data: draft });
            resolve(approval_message);
        }}]}
    >${children}<//>`;
}
