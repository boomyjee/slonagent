// Темы и размер шрифта для дашборда. Все цвета — в CSS-переменных
// (см. index.html). Monaco-тема собирается на лету из тех же переменных
// через buildMonacoTheme(), так что один источник правды для всего UI.
// Кроме 4 пресетов поддерживается custom-тема — генерируется из 1-2
// цветов и применяется как inline styles на :root.
import { persist } from './lib.js';

// ─── Color math ──────────────────────────────────────────────────────────────

function hexToRgb(hex) {
    hex = hex.replace('#', '');
    if (hex.length === 3) hex = hex[0]+hex[0]+hex[1]+hex[1]+hex[2]+hex[2];
    const n = parseInt(hex, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHex(r, g, b) {
    return '#' + [r, g, b].map(c => Math.round(Math.max(0, Math.min(255, c)))
        .toString(16).padStart(2, '0')).join('');
}

function rgbToHsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    let h, s, l = (max + min) / 2;
    if (max === min) { h = s = 0; }
    else {
        const d = max - min;
        s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
        if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
        else if (max === g) h = ((b - r) / d + 2) / 6;
        else h = ((r - g) / d + 4) / 6;
    }
    return [h * 360, s * 100, l * 100];
}

function hslToRgb(h, s, l) {
    h /= 360; s /= 100; l /= 100;
    if (s === 0) { const v = Math.round(l * 255); return [v, v, v]; }
    const hue2rgb = (p, q, t) => {
        if (t < 0) t += 1; if (t > 1) t -= 1;
        if (t < 1/6) return p + (q - p) * 6 * t;
        if (t < 1/2) return q;
        if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
        return p;
    };
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    return [
        Math.round(hue2rgb(p, q, h + 1/3) * 255),
        Math.round(hue2rgb(p, q, h) * 255),
        Math.round(hue2rgb(p, q, h - 1/3) * 255),
    ];
}

function _hsl(h, s, l) { return rgbToHex(...hslToRgb(h, s, l)); }
function _hsla(h, s, l, a) {
    const [r, g, b] = hslToRgb(h, s, l);
    const aa = Math.round(Math.max(0, Math.min(1, a)) * 255).toString(16).padStart(2, '0');
    return rgbToHex(r, g, b) + aa;
}

// WCAG relative luminance
function luminance(hex) {
    const [r, g, b] = hexToRgb(hex).map(c => {
        c /= 255;
        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function rotHue(h, deg) { return ((h + deg) % 360 + 360) % 360; }

// ─── Generative theme ────────────────────────────────────────────────────────

/**
 * Generate a full theme from 1-2 colors.
 * @param {string} primary  - hex color, determines hue + light/dark
 * @param {string} [accent] - optional hex for accent; defaults to primary
 * @returns {Record<string, string>} CSS variable name → value
 */
function generateTheme(primary, accent) {
    const [pH, pS] = rgbToHsl(...hexToRgb(primary));
    const dark = luminance(primary) < 0.4;
    const accentHex = accent || primary;
    const [aH] = rgbToHsl(...hexToRgb(accentHex));

    // HSL is not perceptually uniform: at high lightness (light themes)
    // the same saturation value looks much less colorful to the eye.
    // Compensate by allowing higher saturation for light backgrounds.
    const bgH = pH;
    const darkBgS = Math.min(pS, 20);   // dark mode: subtle tint
    const lightBgS = Math.min(pS, 40);  // light mode: needs 2x for same perceived tint

    let vars;

    if (dark) {
        const bgS = darkBgS;
        vars = {
            '--bg':       _hsl(bgH, bgS, 12),
            '--surface':  _hsl(bgH, bgS, 15),
            '--surface2': _hsl(bgH, bgS, 18),
            '--surface3': _hsl(bgH, bgS, 25),
            '--border':   _hsl(bgH, bgS, 24),
            '--text':     _hsl(bgH, 8, 85),
            '--text-dim': _hsl(bgH, 8, 55),
            '--accent':   accentHex,
            '--accent-fg': luminance(accentHex) > 0.4 ? _hsl(bgH, bgS, 12) : '#ffffff',
            '--green':    _hsl(140, 50, 65),
            '--warn':     _hsl(45, 60, 75),
            '--red':      _hsl(0, 70, 65),
            '--diff-add-bg':       _hsla(140, 50, 50, 0.18),
            '--diff-add-bg-hover': _hsla(140, 50, 50, 0.32),
            '--diff-del-bg':       _hsla(0, 60, 45, 0.18),
            '--diff-del-bg-hover': _hsla(0, 60, 45, 0.32),
            '--shadow':   'rgba(0,0,0,0.4)',
            '--user-msg-bg': accentHex,
            '--user-msg-fg': luminance(accentHex) > 0.4 ? _hsl(bgH, bgS, 12) : '#ffffff',
            '--syntax-keyword':   _hsl(aH, 55, 72),
            '--syntax-function':  _hsl(rotHue(aH, 180), 50, 74),
            '--syntax-string':    _hsl(rotHue(aH, 90), 45, 70),
            '--syntax-type':      _hsl(rotHue(aH, 270), 50, 72),
            '--syntax-number':    _hsl(rotHue(aH, 45), 50, 75),
            '--syntax-comment':   _hsl(aH, 10, 48),
            '--syntax-variable':  _hsl(rotHue(aH, 30), 50, 78),
            '--syntax-operator':  _hsl(bgH, 5, 85),
            '--syntax-delimiter': _hsl(bgH, 5, 65),
            '--editor-selection':          _hsla(aH, 40, 40, 0.55),
            '--editor-selection-inactive': _hsl(bgH, bgS, 25),
            '--editor-cursor':             _hsl(bgH, 5, 80),
            '--editor-indent-guide':       _hsl(bgH, bgS, 25),
            '--editor-indent-guide-active':_hsl(bgH, bgS, 44),
            '--editor-scrollbar':          _hsla(bgH, 5, 50, 0.4),
            '--editor-scrollbar-hover':    _hsla(bgH, 5, 50, 0.7),
            '--monaco-base': 'vs-dark',
        };
    } else {
        const bgS = lightBgS;
        vars = {
            '--bg':       _hsl(bgH, bgS, 97),
            '--surface':  _hsl(bgH, bgS, 94),
            '--surface2': _hsl(bgH, bgS, 90),
            '--surface3': _hsl(bgH, bgS, 84),
            '--border':   _hsl(bgH, bgS, 87),
            '--text':     _hsl(bgH, 15, 18),
            '--text-dim': _hsl(bgH, 12, 45),
            '--accent':   accentHex,
            '--accent-fg': luminance(accentHex) < 0.4 ? _hsl(bgH, bgS, 97) : _hsl(bgH, 15, 18),
            '--green':    _hsl(140, 55, 38),
            '--warn':     _hsl(38, 70, 48),
            '--red':      _hsl(0, 70, 45),
            '--diff-add-bg':       _hsla(140, 55, 45, 0.18),
            '--diff-add-bg-hover': _hsla(140, 55, 45, 0.32),
            '--diff-del-bg':       _hsla(0, 60, 45, 0.18),
            '--diff-del-bg-hover': _hsla(0, 60, 45, 0.32),
            '--shadow':   'rgba(0,0,0,0.15)',
            '--user-msg-bg': _hsl(aH, 45, 84),
            '--user-msg-fg': _hsl(bgH, 15, 18),
            '--syntax-keyword':   _hsl(aH, 70, 38),
            '--syntax-function':  _hsl(rotHue(aH, 180), 60, 35),
            '--syntax-string':    _hsl(rotHue(aH, 90), 55, 38),
            '--syntax-type':      _hsl(rotHue(aH, 270), 55, 40),
            '--syntax-number':    _hsl(rotHue(aH, 45), 65, 42),
            '--syntax-comment':   _hsl(aH, 8, 58),
            '--syntax-variable':  _hsl(rotHue(aH, 30), 50, 32),
            '--syntax-operator':  _hsl(bgH, 5, 25),
            '--syntax-delimiter': _hsl(bgH, 5, 42),
            '--editor-selection':          _hsla(aH, 35, 70, 0.5),
            '--editor-selection-inactive': _hsl(bgH, bgS, 90),
            '--editor-cursor':             _hsl(bgH, 10, 18),
            '--editor-indent-guide':       _hsl(bgH, bgS, 87),
            '--editor-indent-guide-active':_hsl(bgH, bgS, 68),
            '--editor-scrollbar':          _hsla(bgH, 5, 50, 0.4),
            '--editor-scrollbar-hover':    _hsla(bgH, 5, 50, 0.7),
            '--monaco-base': 'vs',
        };
    }

    return vars;
}

const THEME_VARS = Object.keys(generateTheme('#569cd6'));

// ─── Theme presets & persistence ─────────────────────────────────────────────

export const THEMES = ['dark-vscode', 'dark-catppuccin', 'light-vscode', 'light-catppuccin'];
export const THEME_LABELS = {
    'dark-vscode':      'Dark · VS Code',
    'dark-catppuccin':  'Dark · Catppuccin Mocha',
    'light-vscode':     'Light · VS Code',
    'light-catppuccin': 'Light · Catppuccin Latte',
};

const ALL_THEMES = [...THEMES, 'custom'];

const DEFAULT_THEME = 'dark-vscode';
const DEFAULT_FONT_SIZE = 13;
export const FONT_SIZE_MIN = 10;
export const FONT_SIZE_MAX = 20;

const KEY_THEME = 'theme';
const KEY_FONT  = 'fontSize';
const KEY_PRIMARY = 'customPrimary';
const KEY_ACCENT  = 'customAccent';

const _editorRefs = new Set();

export function registerEditor(editor) {
    _editorRefs.add(editor);
    editor.onDidDispose?.(() => _editorRefs.delete(editor));
}

export function getTheme() {
    const v = persist.get(KEY_THEME, null);
    return ALL_THEMES.includes(v) ? v : DEFAULT_THEME;
}

export function getCustomColors() {
    return {
        primary: persist.get(KEY_PRIMARY, '#569cd6'),
        accent:  persist.get(KEY_ACCENT, null),
    };
}

export function setCustomColors(primary, accent) {
    persist.set(KEY_PRIMARY, primary);
    persist.set(KEY_ACCENT, accent || null);
}

export function getFontSize() {
    const v = persist.get(KEY_FONT, null);
    if (Number.isFinite(v) && v >= FONT_SIZE_MIN && v <= FONT_SIZE_MAX) return v;
    return DEFAULT_FONT_SIZE;
}

// ─── Monaco theme ────────────────────────────────────────────────────────────

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

// ─── Apply ───────────────────────────────────────────────────────────────────

function _clearCustomVars() {
    const el = document.documentElement;
    for (const v of THEME_VARS) el.style.removeProperty(v);
}

function _applyCustomVars(primary, accent) {
    const vars = generateTheme(primary, accent || undefined);
    const el = document.documentElement;
    for (const [k, v] of Object.entries(vars)) el.style.setProperty(k, v);
}

export function applyTheme(name) {
    if (!ALL_THEMES.includes(name)) name = DEFAULT_THEME;
    _clearCustomVars();
    if (name === 'custom') {
        const { primary, accent } = getCustomColors();
        document.documentElement.dataset.theme = 'custom';
        _applyCustomVars(primary, accent);
    } else {
        document.documentElement.dataset.theme = name;
    }
    persist.set(KEY_THEME, name);
    const cs = getComputedStyle(document.documentElement);
    const isDark = (cs.getPropertyValue('--monaco-base').trim() || 'vs-dark') === 'vs-dark';
    document.documentElement.style.colorScheme = isDark ? 'dark' : 'light';
    if (window.monaco) {
        window.monaco.editor.defineTheme(MONACO_THEME_NAME, buildMonacoTheme());
        window.monaco.editor.setTheme(MONACO_THEME_NAME);
    }
}

export function applyCustomTheme(primary, accent) {
    setCustomColors(primary, accent);
    applyTheme('custom');
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
