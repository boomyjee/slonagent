import { html } from '../lib.js';

export function PortraitModal({ modal, onChange, onGenerate, onReject, onCancel }) {
    return html`
        <div class="modal-backdrop" onClick=${onCancel}>
            <div class="modal" onClick=${e => e.stopPropagation()}>
                <div class="modal-header">
                    <h2>${modal.approval ? 'AI Proposal — Portrait' : 'Generate Portrait'}: ${modal.charName || ''}</h2>
                </div>
                <div class="modal-body">
                    <div class="field grow">
                        <label>Prompt</label>
                        <textarea
                            value=${modal.prompt}
                            onInput=${e => onChange(e.target.value)}
                            placeholder="Describe the portrait..."
                        ></textarea>
                    </div>
                </div>
                <div class="modal-footer">
                    ${modal.approval ? html`
                        <button class="btn btn-danger" onClick=${onReject}>Reject</button>
                        <div class="spacer"></div>
                        <button class="btn btn-primary" onClick=${onGenerate}>Approve & Generate</button>
                    ` : html`
                        <div class="spacer"></div>
                        <button class="btn" onClick=${onCancel}>Cancel</button>
                        <button class="btn btn-primary" onClick=${onGenerate}>Generate</button>
                    `}
                </div>
            </div>
        </div>
    `;
}
