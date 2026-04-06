// Pick reference images from across the project.
// Shows all generations with "done" status, grouped by entity.
// Selected images are tracked by file path.
import { html, useState } from '../lib.js';
import { app } from '../app.js';
import { useField } from '../common/Form.js';

function gather(images, collection, group, labelFn) {
    for (const [id, entity] of Object.entries(collection || {})) {
        for (const g of Object.values(entity.generations || {})) {
            if (g.status === 'done' && g.file)
                images.push({ file: g.file, label: labelFn(entity, id), group, entityId: id });
        }
    }
}

function collectImages(project) {
    const images = [];
    gather(images, project.characters, 'characters', c => c.name || 'Character');
    gather(images, project.library, 'library', f => f.name || 'Folder');
    for (const [id, scene] of Object.entries(project.scenes || {})) {
        gather(images, { [id]: scene }, 'scenes', s => s.title || 'Scene');
        gather(images, scene.shots, 'shots', (s, sid) => s.description?.slice(0, 40) || `Shot ${sid}`);
    }
    return images;
}

const GROUPS = [
    { key: '', label: 'All' },
    { key: 'characters', label: 'Characters' },
    { key: 'scenes', label: 'Scenes' },
    { key: 'shots', label: 'Shots' },
    { key: 'library', label: 'Library' },
];

export function ReferencePicker({ name }) {
    const f = useField(name);
    const selected = f.value || [];
    const [filter, setFilter] = useState('');
    const images = collectImages(app.state.project);
    const filtered = filter ? images.filter(i => i.group === filter) : images;

    function toggle(file) {
        f.set(selected.includes(file) ? selected.filter(x => x !== file) : [...selected, file]);
    }

    return html`
        <div class="ref-picker">
            <div class="ref-picker-header">
                <label>References</label>
                <div class="ref-filter">
                    ${GROUPS.map(g => html`
                        <button
                            class=${'btn btn-sm' + (filter === g.key ? ' btn-primary' : '')}
                            onClick=${() => setFilter(g.key)}
                        >${g.label}</button>
                    `)}
                </div>
            </div>
            <div class="ref-grid">
                ${filtered.length === 0
                    ? html`<div class="ref-empty">No images</div>`
                    : filtered.map(img => html`
                        <div
                            class=${'ref-thumb' + (selected.includes(img.file) ? ' selected' : '')}
                            onClick=${() => toggle(img.file)}
                            title=${img.label}
                        >
                            <img src=${'/api/asset/' + img.file} />
                        </div>
                    `)}
            </div>
        </div>
    `;
}
