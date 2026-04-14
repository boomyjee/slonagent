// ApproveDialog.open(approval_message) — opens the right approval form in Dialog.
import { html } from '../lib.js';
import { Dialog } from '../common/Dialog.js';
import { ApproveView } from '../common/ApproveView.js';
import { Textarea } from '../common/Form.js';
import { SceneForm } from './SceneForm.js';
import { CharacterForm } from './CharacterForm.js';
import { ShotForm } from './ShotForm.js';

export const ApproveDialog = {
    open(m) {
        const kind = m.approvalKind;
        let view;

        if (kind === 'create_scene' || kind === 'update_scene')
            view = html`<${ApproveView} label="Scene" approval_message=${m}><${SceneForm} /><//>`;
        else if (kind === 'create_character' || kind === 'update_character')
            view = html`<${ApproveView} label="Character" approval_message=${m}><${CharacterForm} /><//>`;
        else if (kind === 'create_shot' || kind === 'update_shot')
            view = html`<${ApproveView} label="Shot" approval_message=${m}><${ShotForm} /><//>`;
        else if (kind === 'create_shots_bulk')
            view = html`<${ApproveView} label="Storyboard" approval_message=${m}>
                <${Textarea} name="text" label="Shot descriptions (separated by ---)" grow />
            <//>`;
        else if (kind === 'generate_portrait')
            view = html`<${ApproveView} label="Portrait" approval_message=${m}>
                <${Textarea} name="prompt" label="Prompt" placeholder="Describe the image..." grow />
            <//>`;

        Dialog.open(view || html`<div class="center-empty">Unknown approval kind: ${kind}</div>`);
    },
};
