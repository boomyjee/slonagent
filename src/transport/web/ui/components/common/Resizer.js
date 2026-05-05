import { html, css, persist } from '../../lib.js';

const clResizer = css`
  width: 2px; cursor: col-resize; flex-shrink: 0; background: var(--surface3);
  &:hover { background: var(--accent); }
`;

export function Resizer({ side, persistKey, className }) {
    function onMouseDown(e) {
        e.preventDefault();
        const el = side === 'left'
            ? e.target.previousElementSibling
            : e.target.nextElementSibling;
        if (!el) return;
        const startX = e.clientX;
        const startW = el.getBoundingClientRect().width;
        // Без overlay'я iframe в центральной панели перехватывает mousemove
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed;inset:0;z-index:99999;cursor:col-resize';
        document.body.appendChild(overlay);
        function onMove(ev) {
            const dx = ev.clientX - startX;
            const w = Math.max(150, startW + (side === 'left' ? dx : -dx));
            el.style.flex = 'none';
            el.style.width = w + 'px';
        }
        function onUp() {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
            overlay.remove();
            if (persistKey) persist.set(`resizer.${persistKey}`, parseInt(el.style.width));
        }
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    }
    return html`<div class="${clResizer} ${className || ''}" onMouseDown=${onMouseDown}
        ref=${el => {
            if (!el || !persistKey) return;
            const w = persist.get(`resizer.${persistKey}`);
            if (w) {
                const target = side === 'left' ? el.previousElementSibling : el.nextElementSibling;
                if (target) { target.style.flex = 'none'; target.style.width = w + 'px'; }
            }
        }}></div>`;
}
