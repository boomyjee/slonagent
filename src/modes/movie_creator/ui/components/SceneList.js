import { html } from '../lib.js';
import { app } from '../app.js';
import { EntityList } from '../common/EntityList.js';

export function SceneList() {
    return html`<${EntityList}
        title="Scenes"
        collection="scenes"
        canCreate=${app.state.tab === 'screenplay'}
        renderItem=${(scene, i) => html`
            <span class="num">${i + 1}</span>
            <span class="name">${scene.title || 'Untitled'}</span>
        `}
    />`;
}
