import { html } from '../lib.js';

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
    return html`<div class="resizer" onMouseDown=${onMouseDown}></div>`;
}
