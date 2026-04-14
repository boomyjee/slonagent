// Pick reference images from across the project.
// Shows all generations with "done" status, grouped by entity.
// Selected images appear in a separate sortable strip above the grid.
import { html, useState, useRef, css } from '../lib.js';
import { app, base } from '../app.js';
import { useField } from '../common/Form.js';
import { Lightbox } from '../common/Lightbox.js';
import { Button } from '../common/FormView.js';

const cl = {};

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
        <div class=${cl.picker}>
            <div class=${cl.header}>
                <label>References${selected.length ? ` (${selected.length})` : ''}</label>
                <div class=${cl.filter}>
                    ${GROUPS.map(g => html`
                        <${Button} sm variant=${filter === g.key ? 'primary' : ''}
                            onClick=${() => setFilter(g.key)}
                        >${g.label}<//>
                    `)}
                </div>
            </div>
            ${selected.length > 0 && html`
                <div class=${cl.selectedStrip}>
                    ${selected.map((file, i) => {
                        const img = imageByFile[file];
                        return html`
                            <div
                                class=${cl.selectedItem}
                                draggable="true"
                                onDragStart=${() => onDragStart(i)}
                                onDragOver=${e => onDragOver(e, i)}
                                onDragEnd=${onDragEnd}
                                title=${img?.label || file}
                            >
                                <img src=${`${base}/api/asset/200x200/` + (img?.thumb || file)} />
                                ${img?.isVideo && html`<div class="video-badge">\u25B6</div>`}
                                <button class="remove" onClick=${() => remove(file)}>\u2715</button>
                                <div class="order">${i + 1}</div>
                            </div>
                        `;
                    })}
                </div>
            `}
            <div class=${cl.grid}>
                ${filtered.length === 0
                    ? html`<div class=${cl.empty}>No images</div>`
                    : filtered.map(img => html`
                        <div
                            class=${cl.thumb + (selected.includes(img.file) ? ' selected' : '')}
                            onClick=${() => selected.includes(img.file) ? remove(img.file) : add(img.file)}
                            onContextMenu=${e => { e.preventDefault(); Lightbox.open(e.currentTarget); }}
                            title=${img.label}
                        >
                            <img src=${`${base}/api/asset/200x200/` + img.thumb} data-full=${`${base}/api/asset/` + (img.isVideo ? img.thumb : img.file)} data-lightbox="refs" />
                            ${img.isVideo && html`<div class="video-badge">\u25B6</div>`}
                        </div>
                    `)}
            </div>
        </div>
    `;
}

// --- styles ---

cl.picker = css`display: flex; flex-direction: column; gap: 8px;`;

cl.header = css`
  display: flex; align-items: center; gap: 8px;
  & label { font-size: 11px; color: var(--text-dim); text-transform: uppercase; }
`;

cl.filter = css`display: flex; gap: 4px; margin-left: auto;`;

cl.grid = css`
  display: grid; grid-template-columns: repeat(auto-fill, minmax(80px, 1fr));
  grid-auto-rows: 80px; gap: 6px;
  height: 252px; overflow-y: auto;
`;

cl.empty = css`padding: 12px; color: var(--text-dim); font-size: 12px; text-align: center;`;

cl.thumb = css`
  position: relative;
  border: 2px solid transparent; border-radius: 6px; overflow: hidden;
  cursor: pointer; height: 100%; aspect-ratio: 1; justify-self: center;
  & img { width: 100%; height: 100%; object-fit: cover; }
  &:hover { border-color: var(--border); }
  &.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); opacity: .5; }
  & .video-badge {
    position: absolute; bottom: 2px; left: 2px;
    background: rgba(0,0,0,.7); color: #fff; font-size: 9px;
    padding: 1px 4px; border-radius: 3px; pointer-events: none;
  }
`;

cl.selectedStrip = css`
  display: flex; gap: 6px; padding: 8px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 6px; overflow-x: auto;
  min-height: 64px; align-items: center;
`;

cl.selectedItem = css`
  position: relative; width: 56px; height: 56px; flex-shrink: 0;
  border-radius: 4px; overflow: hidden; cursor: grab;
  & img { width: 100%; height: 100%; object-fit: cover; }
  &:active { cursor: grabbing; }
  & .video-badge {
    position: absolute; bottom: 2px; left: 2px;
    background: rgba(0,0,0,.7); color: #fff; font-size: 9px;
    padding: 1px 4px; border-radius: 3px; pointer-events: none;
  }
  & .remove {
    position: absolute; top: 1px; right: 1px; width: 16px; height: 16px;
    border: none; background: rgba(0,0,0,.7); color: var(--red);
    font-size: 10px; cursor: pointer; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    opacity: 0; transition: opacity .15s;
  }
  &:hover .remove { opacity: 1; }
  & .order {
    position: absolute; bottom: 1px; left: 1px;
    font-size: 9px; background: rgba(0,0,0,.7); color: var(--text-dim);
    padding: 0 4px; border-radius: 3px;
  }
`;
