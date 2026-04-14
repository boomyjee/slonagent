import { html, render, Component, useState, useRef, useEffect } from '../lib.js';

let _instance = null;

function CropImage({ src }) {
    const imgRef = useRef(null);
    const [sel, setSel] = useState(null);
    const dragRef = useRef(null);

    useEffect(() => setSel(null), [src]);

    useEffect(() => {
        const onKey = e => {
            if (e.key === 'Escape' && sel) { setSel(null); e.stopPropagation(); }
        };
        window.addEventListener('keydown', onKey, true);
        return () => window.removeEventListener('keydown', onKey, true);
    }, [sel]);

    function onMouseDown(e) {
        if (e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        const img = imgRef.current;
        if (!img) return;
        const rect = img.getBoundingClientRect();
        dragRef.current = { x0: e.clientX - rect.left, y0: e.clientY - rect.top, rect };
        setSel(null);

        const onMove = ev => {
            const d = dragRef.current;
            if (!d) return;
            const cx = Math.max(0, Math.min(ev.clientX - d.rect.left, d.rect.width));
            const cy = Math.max(0, Math.min(ev.clientY - d.rect.top, d.rect.height));
            setSel({
                x: Math.min(d.x0, cx), y: Math.min(d.y0, cy),
                w: Math.abs(cx - d.x0), h: Math.abs(cy - d.y0),
            });
        };
        const onUp = () => {
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onUp);
            dragRef.current = null;
        };
        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', onUp);
    }

    useEffect(() => {
        if (!sel || sel.w <= 5 || sel.h <= 5 || dragRef.current) return;
        const img = imgRef.current;
        if (!img) return;
        const rect = img.getBoundingClientRect();
        const sx = Math.round(sel.x * img.naturalWidth / rect.width);
        const sy = Math.round(sel.y * img.naturalHeight / rect.height);
        const sw = Math.round(sel.w * img.naturalWidth / rect.width);
        const sh = Math.round(sel.h * img.naturalHeight / rect.height);

        const canvas = document.createElement('canvas');
        canvas.width = sw;
        canvas.height = sh;
        canvas.getContext('2d').drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
        canvas.toBlob(blob => {
            navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]).catch(
                err => console.warn('clipboard write failed:', err)
            );
        }, 'image/png');
        setSel(null);
    }, [sel]);

    return html`
        <div class="lb-img-wrap" onClick=${e => e.stopPropagation()}>
            <img ref=${imgRef} src=${src} onMouseDown=${onMouseDown} draggable=${false} />
            ${sel && sel.w > 0 && html`<div class="lb-selection" style=${{
                left: sel.x + 'px', top: sel.y + 'px',
                width: sel.w + 'px', height: sel.h + 'px',
            }} />`}
        </div>
    `;
}

class LightboxView extends Component {
    constructor(props) {
        super(props);
        this.state = { group: null, index: 0 };
        _instance = this;
    }

    _items() {
        return [...document.querySelectorAll(`img[data-lightbox="${this.state.group}"]`)].map(el => ({
            src: el.dataset.full || el.src,
            isVideo: !!el.dataset.video,
        }));
    }

    open(el) {
        const media = el.tagName === 'IMG' ? el : el.querySelector('img[data-lightbox]');
        if (!media?.dataset?.lightbox) return;
        const group = media.dataset.lightbox;
        const all = [...document.querySelectorAll(`img[data-lightbox="${group}"]`)];
        const index = all.indexOf(media);
        this.setState({ group, index: Math.max(0, index) });
    }

    close() {
        this.setState({ group: null });
    }

    componentDidMount() {
        window.addEventListener('keydown', e => {
            if (!this.state.group) return;
            if (e.key === 'ArrowLeft') this.setState(s => ({ index: Math.max(0, s.index - 1) }));
            else if (e.key === 'ArrowRight') {
                const len = this._items().length;
                this.setState(s => ({ index: Math.min(len - 1, s.index + 1) }));
            } else if (e.key === 'Escape') this.close();
        });
    }

    render() {
        const { group, index } = this.state;
        if (!group) return null;
        const items = this._items();
        if (!items.length) return null;
        const item = items[index] || items[0];
        const hasPrev = index > 0;
        const hasNext = index < items.length - 1;

        return html`
            <div class="lightbox"
                onMouseDown=${e => { this._downTarget = e.target; }}
                onMouseUp=${e => { if (e.target === this._downTarget) this.close(); }}>
                ${hasPrev && html`<div class="lb-arrow lb-prev" onMouseDown=${e => e.stopPropagation()}
                    onClick=${() => this.setState({ index: index - 1 })}>\u2039</div>`}
                ${item.isVideo
                    ? html`<video src=${item.src} controls autoplay onMouseDown=${e => e.stopPropagation()} />`
                    : html`<${CropImage} src=${item.src} />`}
                ${hasNext && html`<div class="lb-arrow lb-next" onMouseDown=${e => e.stopPropagation()}
                    onClick=${() => this.setState({ index: index + 1 })}>\u203A</div>`}
                <div class="lb-counter">${index + 1} / ${items.length}</div>
            </div>
        `;
    }
}

// Mount singleton
const _container = document.createElement('div');
document.body.appendChild(_container);
render(html`<${LightboxView} />`, _container);

export const Lightbox = {
    open(el) { _instance?.open(el); },
    close() { _instance?.close(); },
};
