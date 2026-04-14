import { html } from '../lib.js';
import { app } from '../app.js';
import { FormView } from './FormView.js';
import { Dialog } from './Dialog.js';

function resolve(msg) {
    msg.resolved = true;
    app.forceUpdate();
    Dialog.close();
}

export function ApproveView({ label, approval_message, children }) {
    const entity = approval_message.data.fields || approval_message.data || {};

    return html`<${FormView}
        heading=${'AI Proposal — ' + label}
        entity=${entity}
        className=${approval_message.approvalKind}
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
