// Single-field prompt modal — for image generation and similar flows.
// Owns its own draft state. Parent passes initial text + onSubmit/onCancel.
import { html, useState } from '../lib.js';

export function PromptModal({ title, initial, approval, onSubmit, onCancel }) {
    const [prompt, setPrompt] = useState(initial || '');
    return html`
        <div class="modal-backdrop" onClick=${onCancel}>
            <div class="modal" onClick=${e => e.stopPropagation()}>
                <div class="modal-header"><h2>${title}</h2></div>
                <div class="modal-body">
                    <div class="field grow">
                        <label>Prompt</label>
                        <textarea
                            value=${prompt}
                            onInput=${e => setPrompt(e.target.value)}
                            placeholder="Describe the image..."
                        ></textarea>
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="btn" onClick=${onCancel}>${approval ? 'Reject' : 'Cancel'}</button>
                    <div class="spacer"></div>
                    <button class="btn btn-primary" onClick=${() => onSubmit(prompt)}>
                        ${approval ? 'Approve & Generate' : 'Generate'}
                    </button>
                </div>
            </div>
        </div>
    `;
}
