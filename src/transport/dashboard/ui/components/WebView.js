import { html, Component, css } from '../lib.js';

const cl = {};

export class WebView extends Component {
    refresh() {
        // Свои страницы — reload(). Чужим он запрещён: about:blank и обратно, адрес — только после load
        // (раньше адрес с # уходит в якорь, потом встаёт about:blank — белая вкладка).
        const f = this._iframe;
        if (!f) return;
        if (this._sameOrigin) {
            try { if (f.contentWindow.location.href !== "about:blank") return f.contentWindow.location.reload(); } catch (_) {}
        }
        const src = this._url || f.src;
        const back = () => {
            let blank = false;
            try { blank = f.contentWindow.location.href === "about:blank"; } catch (_) {}
            if (!blank) return;
            f.removeEventListener("load", back);
            f.src = src;
        };
        f.addEventListener("load", back);
        f.src = "about:blank";
    }

    _onLoad = () => {
        // Same-origin: подменяем лейбл вкладки на <title> страницы. Cross-origin
        // — contentDocument.title бросит SecurityError, ловим и оставляем
        // hostname-дефолт.
        try {
            const title = this._iframe.contentDocument?.title;
            if (title && this.props.onTitle) this.props.onTitle(title);
        } catch (_) {}
    };

    render({ url, is_local }) {
        if (is_local) url = location.origin + new URL(url).pathname;
        this._url = url;
        // Same-origin URLs (workspace files, /~/...) — наши, sandbox мешает:
        // ломает <a target="..."> между вложенными фреймами (frameset-сайты
        // открывают ссылки в новом окне вместо смены фрейма). Внешние URL
        // (url:-вкладки) могут быть untrusted — там sandbox оставляем.
        const sameOrigin = url.startsWith('/') || url.startsWith(location.origin);
        this._sameOrigin = sameOrigin;
        const props = sameOrigin ? {} : {
            sandbox: "allow-scripts allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox",
        };
        return html`<iframe ref=${e => this._iframe = e}
                            src=${url}
                            onLoad=${this._onLoad}
                            ...${props}
                            class=${cl.frame} />`;
    }
}

cl.frame = css`flex: 1; border: 0;`;
