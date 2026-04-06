import { html, useState, send } from '../lib.js';
import { app } from '../app.js';
import { Form } from './Form.js';
import { Dialog } from './Dialog.js';

export function ApproveView({ label, approval_message, children }) {
    const entity = approval_message.data.fields || approval_message.data || {};
    const [draft, setDraft] = useState(() => ({ ...entity }));

    function approve() {
        send({ type: 'approval_response', action: 'approve', data: draft });
        approval_message.resolved = true;
        app.forceUpdate();
        Dialog.close();
    }

    function reject() {
        send({ type: 'approval_response', action: 'reject', reason: prompt('Reason (optional):') || '' });
        approval_message.resolved = true;
        app.forceUpdate();
        Dialog.close();
    }

    return html`
        <div class="editor">
            <div class="editor-header"><h2>AI Proposal — ${label}</h2></div>
            <div class="editor-body">
                <${Form} draft=${draft} onChange=${setDraft}>
                    ${children}
                <//>
            </div>
            <div class="editor-footer">
                <button class="btn btn-danger" onClick=${reject}>Reject</button>
                <div class="spacer"></div>
                <button class="btn btn-primary" onClick=${approve}>Approve</button>
            </div>
        </div>
    `;
}
