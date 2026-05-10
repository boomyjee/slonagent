import { html, Component, css } from '../lib.js';
import { Dialog } from './common/Dialog.js';
import { Form, Select, Slider } from './common/Form.js';
import {
    THEMES, THEME_LABELS, FONT_SIZE_MIN, FONT_SIZE_MAX,
    getTheme, getFontSize, applyTheme, applyFontSize,
} from '../theme.js';

const cl = {};

const THEME_OPTIONS = THEMES.map(id => ({ id, label: THEME_LABELS[id] }));

class SettingsContent extends Component {
    constructor(props) {
        super(props);
        this.state = { theme: getTheme(), fontSize: getFontSize() };
    }

    _onChange = (next) => {
        if (next.theme !== this.state.theme) applyTheme(next.theme);
        if (next.fontSize !== this.state.fontSize) applyFontSize(next.fontSize);
        this.setState(next);
    };

    render(_, draft) {
        return html`<div class=${cl.dialog}>
            <div class=${cl.header}>Settings</div>
            <div class=${cl.body}>
                <${Form} draft=${draft} onChange=${this._onChange}>
                    <${Select} name="theme" label="Theme" options=${THEME_OPTIONS} />
                    <${Slider} name="fontSize" label="Font size" unit="px"
                               min=${FONT_SIZE_MIN} max=${FONT_SIZE_MAX} />
                </${Form}>
            </div>
            <div class=${cl.footer}>
                <button class=${cl.btn} onClick=${() => Dialog.close()}>Close</button>
            </div>
        </div>`;
    }
}

export function openSettingsDialog() {
    Dialog.open(html`<${SettingsContent} />`, { transparent: true });
}

cl.dialog = css`
  width: 420px; max-width: 100%;
  display: flex; flex-direction: column;
`;
cl.header = css`
  padding: 10px 14px; font-size: 14px; font-weight: 600;
  color: var(--text); border-bottom: 1px solid var(--border);
`;
cl.body = css`
  padding: 14px; display: flex; flex-direction: column; gap: 14px;
`;
cl.footer = css`
  display: flex; justify-content: flex-end; gap: 8px;
  padding: 10px 14px; border-top: 1px solid var(--border);
`;
cl.btn = css`
  background: var(--surface2); color: var(--text); border: 1px solid var(--border);
  padding: 4px 14px; cursor: pointer; font-size: 12px;
  &:hover { background: var(--surface3); }
`;
