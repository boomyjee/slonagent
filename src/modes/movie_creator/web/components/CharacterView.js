// Character editor — form + gallery + footer. Reads character from app.state.
import { html, useState, CHARACTER_SCHEMA, characterPrompt, send } from '../lib.js';
import { app } from '../app.js';
import { EntityForm } from '../common/EntityForm.js';
import { Gallery } from './Gallery.js';

export function CharacterView() {
    const { project, selected } = app.state;
    const sel = selected.characters;
    const character = sel === '__new__' ? {} : (project.characters[sel] || null);

    if (!character) {
        return html`<div class="center-empty">Select a character or create a new one</div>`;
    }
    const isNew = !character.id;
    const [draft, setDraft] = useState(() => ({
        name: character.name || '',
        description: character.description || '',
        appearance: character.appearance || '',
    }));

    function submit() {
        if (isNew) {
            send({ type: 'create', path: ['characters'], data: draft });
        } else {
            send({ type: 'update', path: ['characters', character.id], data: draft });
        }
        app.selectEntity('characters', null);
    }

    function del() {
        if (!confirm('Delete this character?')) return;
        send({ type: 'delete', path: ['characters', character.id] });
        app.selectEntity('characters', null);
    }

    const title = isNew
        ? 'New Character'
        : `Character: ${draft.name || 'Unnamed'}`;

    return html`
        <div class="editor">
            <div class="editor-header"><h2>${title}</h2></div>
            <div class="editor-body">
                <${EntityForm} schema=${CHARACTER_SCHEMA} draft=${draft} onChange=${setDraft} />
                ${!isNew ? html`
                    <${Gallery}
                        entity=${character}
                        path=${['characters', character.id]}
                        kind="portrait"
                        defaultPrompt=${() => characterPrompt(character)}
                    />
                ` : null}
            </div>
            <div class="editor-footer">
                ${!isNew ? html`<button class="btn btn-danger" onClick=${del}>Delete</button>` : null}
                <div class="spacer"></div>
                <button class="btn" onClick=${() => app.selectEntity('characters', null)}>Cancel</button>
                <button class="btn btn-primary" onClick=${submit}>Save</button>
            </div>
        </div>
    `;
}
