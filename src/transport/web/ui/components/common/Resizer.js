import { html, css } from '../../lib.js';

const clResizer = css`
  width: 2px; cursor: col-resize; flex-shrink: 0; background: var(--surface3);
  &:hover { background: var(--accent); }
`;

export function Resizer({ side }) {
    function onMouseDown(e) {
        e.preventDefault();
        const el = side === 'left'
            ? e.target.previousElementSibling
            : e.target.nextElementSibling;
        if (!el) return;
        const startX = e.clientX;
        const startW = el.getBoundingClientRect().width;
        function onMove(ev) {
            const dx = ev.clientX - startX;
            const w = startW + (side === 'left' ? dx : -dx);
            el.style.flex = 'none';
            el.style.width = Math.max(150, w) + 'px';
        }
        function onUp() {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
        }
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    }
    return html`<div class=${clResizer} onMouseDown=${onMouseDown}></div>`;
}
