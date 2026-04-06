// Pick reference images from across the project.
// Shows all generations with "done" status, grouped by entity.
// Selected images are tracked by file path.
import { html, useState } from '../lib.js';
import { app } from '../app.js';

function collectImages(project) {
    const images = [];
    for (const [id, char] of Object.entries(project.characters || {})) {
        for (const g of Object.values(char.generations || {})) {
            if (g.status === 'done' && g.file)
                images.push({ file: g.file, label: char.name || 'Character', group: 'characters', entityId: id });
        }
    }
    for (const [id, scene] of Object.entries(project.scenes || {})) {
        for (const g of Object.values(scene.generations || {})) {
            if (g.status === 'done' && g.file)
                images.push({ file: g.file, label: scene.title || 'Scene', group: 'scenes', entityId: id });
        }
        for (const [shotId, shot] of Object.entries(scene.shots || {})) {
            for (const g of Object.values(shot.generations || {})) {
                if (g.status === 'done' && g.file)
                    images.push({ file: g.file, label: shot.description?.slice(0, 40) || `Shot ${shotId}`, group: 'shots', entityId: shotId });
            }
        }
    }
    return images;
}

const GROUPS = [
    { key: '', label: 'All' },
    { key: 'characters', label: 'Characters' },
    { key: 'scenes', label: 'Scenes' },
    { key: 'shots', label: 'Shots' },
];

export function ReferencePicker({ selected, onToggle }) {
    const [filter, setFilter] = useState('');
    const images = collectImages(app.state.project);
    const filtered = filter ? images.filter(i => i.group === filter) : images;

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
                            onClick=${() => onToggle(img.file)}
                            title=${img.label}
                        >
                            <img src=${'/api/asset/' + img.file} />
                        </div>
                    `)}
            </div>
        </div>
    `;
}
