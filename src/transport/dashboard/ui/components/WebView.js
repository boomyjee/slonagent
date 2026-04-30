import { html, Component, css } from '../lib.js';

const cl = {};

export class WebView extends Component {
    refresh() {
        // Cross-origin iframe — contentWindow.location.reload() кидает
        // SecurityError. Trick: снять и поставить src обратно — браузер
        // делает свежий запрос, не зависит от same-origin.
        if (!this._iframe) return;
        const src = this._iframe.src;
        this._iframe.src = "about:blank";
        requestAnimationFrame(() => { this._iframe.src = src; });
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

    render({ url }) {
        return html`<iframe ref=${e => this._iframe = e}
                            src=${url}
                            onLoad=${this._onLoad}
                            sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox"
                            class=${cl.frame} />`;
    }
}

cl.frame = css`flex: 1; border: 0; background: white;`;
