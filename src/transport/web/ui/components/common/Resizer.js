import { html, css, persist, useState, useRef, useLayoutEffect } from '../../lib.js';

export function Resizer({ side, persistKey, className, collapseLabel }) {
    const ref = useRef(null);
    const lastMouseDown = useRef(0);
    const [collapsed, setCollapsed] = useState(
        !!(persistKey && persist.get(`collapsed.${persistKey}`))
    );

    function target() {
        const el = ref.current;
        if (!el) return null;
        return side === 'left' ? el.previousElementSibling : el.nextElementSibling;
    }

    useLayoutEffect(() => {
        const t = target();
        if (!t) return;
        t.style.display = collapsed ? 'none' : '';
        if (!collapsed && persistKey) {
            const w = persist.get(`resizer.${persistKey}`);
            if (w) { t.style.flex = 'none'; t.style.width = w + 'px'; }
        }
    }, [collapsed]);

    function toggle() {
        setCollapsed(c => {
            const next = !c;
            if (persistKey) persist.set(`collapsed.${persistKey}`, next);
            return next;
        });
    }

    function onMouseDown(e) {
        if (collapsed) return;
        const now = Date.now();
        if (now - lastMouseDown.current < 400) {
            lastMouseDown.current = 0;
            toggle();
            return;
        }
        lastMouseDown.current = now;
        e.preventDefault();
        const t = target();
        if (!t) return;
        const startX = e.clientX;
        const startW = t.getBoundingClientRect().width;
        // Без overlay'я iframe в центральной панели перехватывает mousemove
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed;inset:0;z-index:99999;cursor:col-resize';
        document.body.appendChild(overlay);
        function onMove(ev) {
            const dx = ev.clientX - startX;
            const w = Math.max(150, startW + (side === 'left' ? dx : -dx));
            t.style.flex = 'none';
            t.style.width = w + 'px';
        }
        function onUp() {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
            overlay.remove();
            if (persistKey) persist.set(`resizer.${persistKey}`, parseInt(t.style.width));
        }
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    }

    if (collapsed) {
        return html`<div class="${clCollapsed} ${className || ''}"
                          ref=${ref} onClick=${toggle}>${collapseLabel || ''}</div>`;
    }
    return html`<div class="${clResizer} ${className || ''}"
                      ref=${ref} onMouseDown=${onMouseDown}></div>`;
}

const clResizer = css`
  width: 2px; flex-shrink: 0; cursor: col-resize;
  background: var(--surface3); position: relative; z-index: 10;
  &:hover { background: var(--accent); }
  &::after {
    content: ''; position: absolute;
    top: 0; bottom: 0; left: 0; right: -10px;
  }
`;

const clCollapsed = css`
  width: 24px; flex-shrink: 0; cursor: pointer; user-select: none;
  background: var(--surface);
  border-left: 1px solid var(--border); border-right: 1px solid var(--border);
  writing-mode: vertical-rl; text-orientation: mixed;
  text-transform: uppercase; letter-spacing: 1px;
  font-size: 11px; line-height: 24px; color: var(--text-dim);
  padding-top: 12px;
  &:hover { color: var(--text); background: var(--surface2); }
`;
