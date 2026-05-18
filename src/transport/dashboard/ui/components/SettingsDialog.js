import { html, Component, css } from '../lib.js';
import { Dialog } from './common/Dialog.js';
import { Form, Select, Slider } from './common/Form.js';
import {
    THEMES, THEME_LABELS, FONT_SIZE_MIN, FONT_SIZE_MAX,
    getTheme, getFontSize, applyTheme, applyFontSize,
    getCustomColors, applyCustomTheme,
} from '../theme.js';

const cl = {};

const THEME_OPTIONS = [
    ...THEMES.map(id => ({ id, label: THEME_LABELS[id] })),
    { id: 'custom', label: 'Custom' },
];

class SettingsContent extends Component {
    constructor(props) {
        super(props);
        const { primary, accent } = getCustomColors();
        this.state = {
            theme: getTheme(),
            fontSize: getFontSize(),
            primary,
            accent: accent || '',
            useAccent: !!accent,
        };
    }

    _onChange = (next) => {
        if (next.theme !== this.state.theme) {
            if (next.theme === 'custom') {
                applyCustomTheme(this.state.primary, this.state.useAccent ? this.state.accent : null);
            } else {
                applyTheme(next.theme);
            }
        }
        if (next.fontSize !== this.state.fontSize) applyFontSize(next.fontSize);
        this.setState(next);
    };

    _onPrimary = (e) => {
        const primary = e.target.value;
        this.setState({ primary });
        applyCustomTheme(primary, this.state.useAccent ? this.state.accent : null);
    };

    _onAccent = (e) => {
        const accent = e.target.value;
        this.setState({ accent });
        applyCustomTheme(this.state.primary, accent);
    };

    _toggleAccent = (e) => {
        const useAccent = e.target.checked;
        const accent = useAccent ? (this.state.accent || this.state.primary) : '';
        this.setState({ useAccent, accent });
        applyCustomTheme(this.state.primary, useAccent ? accent : null);
    };

    render(_, draft) {
        const isCustom = draft.theme === 'custom';
        return html`<div class=${cl.dialog}>
            <div class=${cl.header}>Settings</div>
            <div class=${cl.body}>
                <${Form} draft=${draft} onChange=${this._onChange}>
                    <${Select} name="theme" label="Theme" options=${THEME_OPTIONS} />
                    <${Slider} name="fontSize" label="Font size" unit="px"
                               min=${FONT_SIZE_MIN} max=${FONT_SIZE_MAX} />
                </${Form}>
                ${isCustom && html`
                    <div class=${cl.colorSection}>
                        <label class=${cl.colorRow}>
                            <span>Primary</span>
                            <input type="color" value=${this.state.primary}
                                   onInput=${this._onPrimary} />
                        </label>
                        <label class=${cl.colorRow}>
                            <span>
                                <input type="checkbox" checked=${this.state.useAccent}
                                       onChange=${this._toggleAccent} />
                                ${' '}Accent
                            </span>
                            <input type="color" value=${this.state.accent || this.state.primary}
                                   onInput=${this._onAccent}
                                   disabled=${!this.state.useAccent} />
                        </label>
                    </div>
                `}
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
cl.colorSection = css`
  display: flex; flex-direction: column; gap: 10px;
  padding: 10px 0 0;
  border-top: 1px solid var(--border);
`;
cl.colorRow = css`
  display: flex; align-items: center; justify-content: space-between;
  font-size: 13px; color: var(--text);
  & input[type="color"] {
    width: 48px; height: 28px; border: 1px solid var(--border);
    border-radius: 4px; cursor: pointer; background: transparent; padding: 1px;
  }
  & input[type="color"]:disabled { opacity: 0.4; cursor: default; }
  & input[type="checkbox"] { margin-right: 4px; }
`;
cl.preview = css`
  display: flex; gap: 3px; height: 20px; border-radius: 4px; overflow: hidden;
  & > div { flex: 1; }
`;
