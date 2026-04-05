import { html, useState } from '../lib.js';

const SEP = '\n\n---\n\n';

export function BulkShotsModal({ initialText, onApprove, onReject }) {
    const [text, setText] = useState(initialText || '');
    const count = text.split(SEP).filter(s => s.trim()).length;
    return html`
        <div class="modal-backdrop" onClick=${onReject}>
            <div class="modal" onClick=${e => e.stopPropagation()}>
                <div class="modal-header"><h2>AI Proposal — Storyboard (${count} shots)</h2></div>
                <div class="modal-body">
                    <div class="field grow">
                        <label>Shot descriptions (separated by <code>---</code>)</label>
                        <textarea
                            value=${text}
                            onInput=${e => setText(e.target.value)}
                        ></textarea>
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-danger" onClick=${onReject}>Reject</button>
                    <div class="spacer"></div>
                    <button class="btn btn-primary" onClick=${() => onApprove(text)}>
                        Approve ${count} shots
                    </button>
                </div>
            </div>
        </div>
    `;
}
