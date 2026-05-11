// Темы и размер шрифта для дашборда. Все цвета — в CSS-переменных
// (см. index.html). Monaco-тема собирается на лету из тех же переменных
// через buildMonacoTheme(), так что один источник правды для всего UI.
import { persist } from './lib.js';

export const THEMES = ['dark-vscode', 'dark-catppuccin', 'light-vscode', 'light-catppuccin'];
export const THEME_LABELS = {
    'dark-vscode':      'Dark · VS Code',
    'dark-catppuccin':  'Dark · Catppuccin Mocha',
    'light-vscode':     'Light · VS Code',
    'light-catppuccin': 'Light · Catppuccin Latte',
};

const DEFAULT_THEME = 'dark-vscode';
const DEFAULT_FONT_SIZE = 13;
export const FONT_SIZE_MIN = 10;
export const FONT_SIZE_MAX = 20;

const KEY_THEME = 'theme';
const KEY_FONT  = 'fontSize';

const _editorRefs = new Set();

export function registerEditor(editor) {
    _editorRefs.add(editor);
    editor.onDidDispose?.(() => _editorRefs.delete(editor));
}

export function getTheme() {
    const v = persist.get(KEY_THEME, null);
    return THEMES.includes(v) ? v : DEFAULT_THEME;
}

export function getFontSize() {
    const v = persist.get(KEY_FONT, null);
    if (Number.isFinite(v) && v >= FONT_SIZE_MIN && v <= FONT_SIZE_MAX) return v;
    return DEFAULT_FONT_SIZE;
}

// Собирает Monaco-тему из текущих CSS-переменных :root. Вызывать ПОСЛЕ
// applyTheme (когда data-theme уже выставлен и vars обновились).
export function buildMonacoTheme() {
    const cs = getComputedStyle(document.documentElement);
    const v = (name) => cs.getPropertyValue(name).trim();
    const tok = (name) => v(name).replace(/^#/, '');  // Monaco token rules — без '#'.
    return {
        base: v('--monaco-base') || 'vs-dark',
        inherit: true,
        rules: [
            { token: 'comment',   foreground: tok('--syntax-comment'), fontStyle: 'italic' },
            { token: 'keyword',   foreground: tok('--syntax-keyword') },
            { token: 'string',    foreground: tok('--syntax-string') },
            { token: 'number',    foreground: tok('--syntax-number') },
            { token: 'type',      foreground: tok('--syntax-type') },
            { token: 'function',  foreground: tok('--syntax-function') },
            { token: 'variable',  foreground: tok('--syntax-variable') },
            { token: 'operator',  foreground: tok('--syntax-operator') },
            { token: 'delimiter', foreground: tok('--syntax-delimiter') },
        ],
        colors: {
            'editor.background':                       v('--bg'),
            'editor.foreground':                       v('--text'),
            'editor.lineHighlightBackground':          v('--surface2'),
            'editor.selectionBackground':              v('--editor-selection'),
            'editor.inactiveSelectionBackground':      v('--editor-selection-inactive'),
            'editorCursor.foreground':                 v('--editor-cursor'),
            'editorLineNumber.foreground':             v('--text-dim'),
            'editorLineNumber.activeForeground':       v('--text'),
            'editorIndentGuide.background':            v('--editor-indent-guide'),
            'editorIndentGuide.activeBackground':      v('--editor-indent-guide-active'),
            'editorWidget.background':                 v('--surface'),
            'editorWidget.border':                     v('--border'),
            'minimap.background':                      v('--bg'),
            'scrollbarSlider.background':              v('--editor-scrollbar'),
            'scrollbarSlider.hoverBackground':         v('--editor-scrollbar-hover'),
        },
    };
}

// Имя Monaco-темы — одно для всех; при applyTheme тема переопределяется
// под текущие CSS-переменные. Никаких N тем держать не надо.
const MONACO_THEME_NAME = 'vs-slon';

export function applyTheme(name) {
    if (!THEMES.includes(name)) name = DEFAULT_THEME;
    document.documentElement.dataset.theme = name;
    persist.set(KEY_THEME, name);
    if (window.monaco) {
        window.monaco.editor.defineTheme(MONACO_THEME_NAME, buildMonacoTheme());
        window.monaco.editor.setTheme(MONACO_THEME_NAME);
    }
}

export function getMonacoThemeName() {
    return MONACO_THEME_NAME;
}

export function applyFontSize(px) {
    px = Math.max(FONT_SIZE_MIN, Math.min(FONT_SIZE_MAX, px | 0));
    document.documentElement.style.setProperty('--font-size-mono', px + 'px');
    persist.set(KEY_FONT, px);
    for (const ed of _editorRefs) ed.updateOptions({ fontSize: px });
}

export function applyAll() {
    applyTheme(getTheme());
    applyFontSize(getFontSize());
}
