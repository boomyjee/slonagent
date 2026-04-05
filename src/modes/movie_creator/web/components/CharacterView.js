// Character editor = form (owns draft) + gallery (reads live project state).
// These are siblings inside EntityForm's children slot. The form's draft
// never touches `character.generations`, and the gallery re-reads them on
// every render — so an incoming generation shows up instantly even while
// the user is typing.
import { html, CHARACTER_SCHEMA, characterPrompt } from '../lib.js';
import { EntityForm } from './EntityForm.js';
import { Gallery } from './Gallery.js';

export function CharacterView({ character, isNew, send, onClose, openPromptModal }) {
    const initial = isNew ? {} : {
        name: character.name || '',
        description: character.description || '',
        appearance: character.appearance || '',
    };

    function submit(data) {
        if (isNew) {
            send({ type: 'create', path: ['characters'], data });
        } else {
            send({ type: 'update', path: ['characters', character.id], data });
        }
        onClose();
    }

    function del() {
        if (!confirm('Delete this character?')) return;
        send({ type: 'delete', path: ['characters', character.id] });
        onClose();
    }

    return html`<${EntityForm}
        schema=${CHARACTER_SCHEMA}
        initial=${initial}
        mode=${isNew ? 'create' : 'edit'}
        onSubmit=${submit}
        onCancel=${onClose}
        onDelete=${del}
    >
        ${!isNew ? html`
            <${Gallery}
                entity=${character}
                path=${['characters', character.id]}
                kind="portrait"
                defaultPrompt=${() => characterPrompt(character)}
                send=${send}
                openPromptModal=${openPromptModal}
            />
        ` : null}
    <//>`;
}
