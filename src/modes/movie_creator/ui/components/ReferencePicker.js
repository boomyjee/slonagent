// Pick reference images from across the project.
// Shows all generations with "done" status, grouped by entity.
// Selected images appear in a separate sortable strip above the grid.
import { html, useState, useRef } from '../lib.js';
import { app, base } from '../app.js';
import { useField } from '../common/Form.js';
import { Lightbox } from '../common/Lightbox.js';

function gather(images, collection, group, labelFn) {
    for (const [id, entity] of Object.entries(collection || {})) {
        for (const g of Object.values(entity.generations || {})) {
            if (g.status === 'done' && g.file) {
                const isVideo = g.media_type === 'video';
                const thumb = isVideo ? (g.poster || g.file) : g.file;
                images.push({ file: g.file, thumb, isVideo, label: labelFn(entity, id), group, entityId: id });
            }
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
    const dragIdx = useRef(null);

    function add(file) {
        if (!selected.includes(file)) f.set([...selected, file]);
    }
    function remove(file) {
        f.set(selected.filter(x => x !== file));
    }
    function onDragStart(i) { dragIdx.current = i; }
    function onDragOver(e, i) {
        e.preventDefault();
        if (dragIdx.current === null || dragIdx.current === i) return;
        const reordered = [...selected];
        const [moved] = reordered.splice(dragIdx.current, 1);
        reordered.splice(i, 0, moved);
        dragIdx.current = i;
        f.set(reordered);
    }
    function onDragEnd() { dragIdx.current = null; }

    const imageByFile = {};
    for (const img of images) imageByFile[img.file] = img;

    return html`
        <div class="ref-picker">
            <div class="ref-picker-header">
                <label>References${selected.length ? ` (${selected.length})` : ''}</label>
                <div class="ref-filter">
                    ${GROUPS.map(g => html`
                        <button
                            class=${'btn btn-sm' + (filter === g.key ? ' btn-primary' : '')}
                            onClick=${() => setFilter(g.key)}
                        >${g.label}</button>
                    `)}
                </div>
            </div>
            ${selected.length > 0 && html`
                <div class="ref-selected">
                    ${selected.map((file, i) => {
                        const img = imageByFile[file];
                        return html`
                            <div
                                class="ref-selected-item"
                                draggable="true"
                                onDragStart=${() => onDragStart(i)}
                                onDragOver=${e => onDragOver(e, i)}
                                onDragEnd=${onDragEnd}
                                title=${img?.label || file}
                            >
                                <img src=${`${base}/api/asset/200x200/` + (img?.thumb || file)} />
                                ${img?.isVideo && html`<div class="ref-video-badge">\u25B6</div>`}
                                <button class="ref-remove" onClick=${() => remove(file)}>\u2715</button>
                                <div class="ref-order">${i + 1}</div>
                            </div>
                        `;
                    })}
                </div>
            `}
            <div class="ref-grid">
                ${filtered.length === 0
                    ? html`<div class="ref-empty">No images</div>`
                    : filtered.map(img => html`
                        <div
                            class=${'ref-thumb' + (selected.includes(img.file) ? ' selected' : '')}
                            onClick=${() => selected.includes(img.file) ? remove(img.file) : add(img.file)}
                            onContextMenu=${e => { e.preventDefault(); Lightbox.open(e.currentTarget); }}
                            title=${img.label}
                        >
                            <img src=${`${base}/api/asset/200x200/` + img.thumb} data-full=${`${base}/api/asset/` + (img.isVideo ? img.thumb : img.file)} data-lightbox="refs" />
                            ${img.isVideo && html`<div class="ref-video-badge">\u25B6</div>`}
                        </div>
                    `)}
            </div>
        </div>
    `;
}
