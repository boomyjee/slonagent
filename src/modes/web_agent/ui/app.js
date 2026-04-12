import { render, html, css, createGlobalStyles } from './lib.js';

const runUrl = new URL('run.js', location.href).href;
const baseUrl = new URL('./', location.href).href;
const bookmarkletJs = `javascript:import('${runUrl}')`;
const embedSnippet = `<script type="module" src="${runUrl}"><\/script>`;
const userscriptSnippet =
    `// ==UserScript==\n` +
    `// @name         SlonAgent Web Agent\n` +
    `// @match        *://*/*\n` +
    `// @grant        none\n` +
    `// @run-at       document-end\n` +
    `// ==/UserScript==\n\n` +
    `import('${runUrl}');\n`;

// Styles must be declared before render() — App is a functional component
// with no state, so it renders once and never re-renders to pick up late cl values.

createGlobalStyles`
    body { background: #1e1e2e; color: #cdd6f4;
           font: 14px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           margin: 0; padding: 48px; line-height: 1.6; }
    h1 { font-size: 22px; margin: 0 0 24px; color: #89b4fa; }
    h2 { font-size: 16px; color: #89b4fa; margin: 40px 0 12px; }
    p { color: #a6adc8; }
    ol { color: #a6adc8; padding-left: 22px; }
    a { color: #89b4fa; }
    code { background: #2a2a3d; padding: 2px 6px; border-radius: 4px; font-size: 12px; color: #f9e2af; }
    pre { background: #2a2a3d; padding: 14px 16px; border-radius: 8px;
          overflow-x: auto; font-size: 12px; color: #f9e2af; margin: 8px 0;
          border-left: 3px solid #89b4fa; padding-right: 90px; }
    hr { border: none; border-top: 1px solid #313147; margin: 32px 0; }
    .bookmarklet {
        display: inline-block; margin: 24px 0; padding: 14px 28px;
        background: #89b4fa; color: #1e1e2e; text-decoration: none;
        border-radius: 8px; font-weight: bold; font-size: 15px;
        cursor: grab; user-select: none;
    }
    .bookmarklet:hover { background: #b4befe; }
    .hint { color: #6c7086; font-size: 12px; margin-top: 8px; }
`;

const cl = {
    panel: css`max-width: 640px; margin: 0 auto;`,
    snippet: css`position: relative;`,
    copyBtn: css`
        position: absolute; top: 14px; right: 12px;
        background: #313147; color: #cdd6f4; border: 1px solid #45475a;
        border-radius: 6px; padding: 4px 12px; font-size: 11px;
        cursor: pointer; font-family: inherit;
        &:hover { background: #45475a; }
    `,
};

async function copyText(text, btn) {
    try { await navigator.clipboard.writeText(text); }
    catch { const ta = document.createElement('textarea'); ta.value = text;
        document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy', 1500);
}

const Snippet = ({ text }) => html`
    <div class=${cl.snippet}>
        <pre>${text}</pre>
        <button class=${cl.copyBtn} onClick=${e => copyText(text, e.target)}>Copy</button>
    </div>`;

const App = () => html`
    <div class=${cl.panel}>
        <h1>\u{1F418} SlonAgent Web Agent</h1>
        <p>Основной способ — Chrome-расширение со side panel. Чат живёт в боковой панели браузера и переживает навигацию между страницами, так что агент может прокликивать целые сценарии без перезагрузки виджета.</p>

        <h2 style="margin-top:16px">Установка расширения</h2>
        <ol>
            <li>Возьмите папку <code>src/modes/web_agent/extension/</code> из репозитория slonagent (склонируйте репо или скачайте её содержимое любым способом).</li>
            <li>Откройте <code>chrome://extensions</code>, включите <b>Developer mode</b> в правом верхнем углу.</li>
            <li>Нажмите <b>Load unpacked</b> и укажите ту самую папку <code>extension/</code>.</li>
            <li>В панели инструментов появится иконка <b>Slonagent</b> — кликните по ней, откроется side panel.</li>
            <li>На первом запуске вставьте туда URL этой страницы (без <code>index.html</code>):
                <${Snippet} text=${baseUrl} />
            </li>
        </ol>
        <div class="hint">После «Connect» расширение подключится к этому саб-агенту по WebSocket и будет управлять активной вкладкой через встроенный PageController. Сменить URL позже можно кнопкой <b>change URL</b> в правом верхнем углу панели.</div>

        <hr />

        <h2>Альтернатива: букмарклет</h2>
        <p>Если ставить расширение лень, перетащите кнопку ниже на панель закладок браузера. Клик по ней на любой странице откроет чат-виджет, но только в рамках текущей страницы — при навигации виджет пропадёт.</p>
        <a class="bookmarklet" href=${bookmarkletJs}>\u{1F4CC} Slon Web Agent</a>
        <div class="hint">Или создайте закладку вручную и вставьте в поле адреса: <code>${bookmarkletJs}</code></div>

        <hr />

        <h2>Альтернатива: встроить на свой сайт</h2>
        <p>Если у вас есть доступ к HTML собственного сайта, добавьте этот тег в\u00a0<code>&lt;head&gt;</code> или перед закрывающим\u00a0<code>&lt;/body&gt;</code> — виджет появится автоматически при загрузке любой страницы:</p>
        <${Snippet} text=${embedSnippet} />
        <div class="hint">Виджет изолирован Shadow DOM — он не конфликтует со стилями и скриптами хост-страницы.</div>

        <hr />

        <h2>Альтернатива: userscript</h2>
        <p>Если хочется, чтобы виджет автоматически появлялся на чужих сайтах, поставьте userscript-менеджер — например <a href="https://www.tampermonkey.net/" target="_blank">Tampermonkey</a> или <a href="https://violentmonkey.github.io/" target="_blank">Violentmonkey</a>, — и добавьте такой скрипт:</p>
        <${Snippet} text=${userscriptSnippet} />
        <div class="hint">Поменяйте <code>@match</code> на нужный URL-паттерн, чтобы виджет грузился только там, где он вам нужен.</div>
    </div>`;

render(html`<${App} />`, document.body);
