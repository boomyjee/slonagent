// ApproveDialog.open(approval_message) — opens the right approval form in Dialog.
//
// Wires the movie wire protocol (`approval_response` events) into the generic
// ApproveView. The kind→variant mapping lives here because widths are
// movie-specific; ApproveView itself is agnostic.
import { html } from '../lib.js';
import { app } from '../app.js';
import { Dialog } from '../common/Dialog.js';
import { ApproveView } from '../common/ApproveView.js';
import { Textarea } from '../common/Form.js';
import { editorCls } from '../common/FormView.js';
import { SceneForm } from './SceneForm.js';
import { CharacterForm } from './CharacterForm.js';
import { ShotForm } from './ShotForm.js';

// Approval kinds that need a wider modal (big textareas).
const WIDEST_KINDS = new Set(['create_scene', 'update_scene', 'create_shots_bulk']);

function handlers() {
    return {
        onApprove: draft  => app.send({ type: 'approval_response', action: 'approve', data: draft }),
        onReject:  reason => app.send({ type: 'approval_response', action: 'reject', reason }),
    };
}

export const ApproveDialog = {
    open(m) {
        const kind = m.approvalKind;
        const entity = m.data.fields || m.data || {};
        const variant = WIDEST_KINDS.has(kind) ? 'widest' : 'wide';
        const props = { entity, variant, ...handlers() };
        let view;

        if (kind === 'create_scene' || kind === 'update_scene')
            view = html`<${ApproveView} label="Scene" ...${props}><${SceneForm} /><//>`;
        else if (kind === 'create_character' || kind === 'update_character')
            view = html`<${ApproveView} label="Character" ...${props}><${CharacterForm} /><//>`;
        else if (kind === 'create_shot' || kind === 'update_shot')
            view = html`<${ApproveView} label="Shot" ...${props}><${ShotForm} /><//>`;
        else if (kind === 'create_shots_bulk')
            view = html`<${ApproveView} label="Storyboard" ...${props}>
                <${Textarea} name="text" label="Shot descriptions (separated by ---)" grow />
            <//>`;
        else if (kind === 'generate_portrait')
            view = html`<${ApproveView} label="Portrait" ...${props}>
                <${Textarea} name="prompt" label="Prompt" placeholder="Describe the image..." grow />
            <//>`;

        Dialog.open(view || html`<div class=${editorCls.centerEmpty}>Unknown approval kind: ${kind}</div>`);
    },
};
