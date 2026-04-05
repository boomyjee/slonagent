import { html, useState } from '../lib.js';

export function GenerationModal({ title, initialPrompt, approval, onSubmit, onCancel }) {
    const [prompt, setPrompt] = useState(initialPrompt || '');
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
