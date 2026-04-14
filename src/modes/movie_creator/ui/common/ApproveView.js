// Pure approval UI — renders a form with Reject/Approve buttons. All wiring
// (wire protocol, dialog lifecycle) is the caller's job via onApprove/onReject.
import { html } from '../lib.js';
import { FormView } from './FormView.js';
import { Dialog } from './Dialog.js';

export function ApproveView({ label, entity, variant, onApprove, onReject, children }) {
    return html`<${FormView}
        heading=${'AI Proposal — ' + label}
        entity=${entity}
        variant=${variant}
        left=${() => [{ label: 'Reject', cls: 'danger', onClick: () => {
            onReject(prompt('Reason (optional):') || '');
            Dialog.close();
        }}]}
        right=${draft => [{ label: 'Approve', cls: 'primary', onClick: () => {
            onApprove(draft);
            Dialog.close();
        }}]}
    >${children}<//>`;
}
