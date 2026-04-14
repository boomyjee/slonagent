# Свой веб-UI и WebSocket

Если тулу нужен собственный интерфейс — редактор кода, дашборд с
графиками, канвас-игра, живой чат — наследуйся от `WebTransport`. Хост
автоматически поднимет FastAPI-роуты, WebSocket и раздачу статики.
Работают одновременно все инстансы — каждый под своим `/{agent_id}{prefix}/`.

**Стек фронта**: Preact + htm + bau-css (динамический CSS-in-JS),
все заинлайнены в `lib.js`. Ни React, ни сборщиков — просто `<script
type="module" src="app.js">` и оно работает.

## Структура файлов

```
/workspace/tools/myeditor/
├── __init__.py          ← скилл и WebTransport-класс
└── ui/
    └── app.js           ← компоненты на Preact/htm
```

Достаточно одного `app.js`. Хост отдаёт эту папку по
`/{agent_id}{prefix}/`; чего не нашёл у тебя — берёт из общего
`WebTransport/ui/` как фолбэк. Оттуда приедут:

- `index.html` — стандартная обёртка со стилями слонагента, грузит `app.js`.
- `lib.js` — Preact + htm + bau-css, всё inline.
- `components/common/Chat.js`, `Resizer.js` — готовые компоненты.

Свой `index.html` пиши только если нужны нестандартные мета-теги,
кастомный `<title>` или дополнительные стили в `<head>`.

## `ui/app.js`

Вот такой минимальный `app.js` работает без сборки. `./lib.js` —
фолбэк на шаред-UI, где лежит Preact.

```js
import { render, html, Component, css } from './lib.js';

const cl = {};

class App extends Component {
    constructor() {
        super();
        this.state = { messages: [], input: '', connected: false };
    }

    componentDidMount() {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        // location.pathname уже содержит /{agent_id}/{prefix}/
        this._ws = new WebSocket(proto + location.host + location.pathname + 'ws');
        this._ws.onopen  = () => this.setState({ connected: true });
        this._ws.onclose = () => this.setState({ connected: false });
        this._ws.onmessage = (ev) => {
            const msg = JSON.parse(ev.data);
            if (msg.type === 'chat') {
                this.setState(({ messages }) => ({
                    messages: [...messages, msg.text],
                }));
            }
        };
    }

    _send = () => {
        const text = this.state.input.trim();
        if (!text || !this._ws) return;
        this._ws.send(JSON.stringify({ type: 'chat', text }));
        this.setState({ input: '' });
    };

    render(_, { messages, input, connected }) {
        return html`
            <div class=${cl.app}>
                <div class=${cl.hdr}>
                    Editor ${connected ? '(online)' : '(offline)'}
                </div>
                <div class=${cl.log}>
                    ${messages.map(m => html`<div class=${cl.msg}>${m}</div>`)}
                </div>
                <input
                    class=${cl.input}
                    value=${input}
                    onInput=${e => this.setState({ input: e.target.value })}
                    onKeyDown=${e => { if (e.key === 'Enter') this._send(); }}
                    placeholder="Напиши что-нибудь…"
                />
            </div>
        `;
    }
}

cl.app = css`display: flex; flex-direction: column; height: 100vh;`;
cl.hdr = css`padding: 12px 16px; border-bottom: 1px solid var(--border); color: var(--text-dim);`;
cl.log = css`flex: 1; overflow-y: auto; padding: 12px;`;
cl.msg = css`padding: 8px 12px; margin-bottom: 8px; background: var(--surface2); border-radius: 8px;`;
cl.input = css`padding: 12px 16px; background: var(--surface); border: none;
               border-top: 1px solid var(--border); color: var(--text); font: inherit;`;

render(html`<${App}/>`, document.body);
```

## Используй готовый `Chat.js`

В `WebTransport/ui/components/common/Chat.js` лежит полноценный компонент
чата — он понимает стандартный wire-protocol транспорта и поддерживает
стриминг, «мысли», кнопки инструментов. Подключай его вместо своей
реализации, если нужен обычный чат:

```js
import { render, html, Component, css } from './lib.js';
import { Chat } from './components/common/Chat.js';

class App extends Component {
    constructor() {
        super();
        this.state = { connected: false };
    }
    componentDidMount() {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        this._ws = new WebSocket(proto + location.host + location.pathname + 'ws');
        this._ws.onopen  = () => this.setState({ connected: true });
        this._ws.onclose = () => this.setState({ connected: false });
        this._ws.onmessage = (ev) => this._chat?.handleMessage(JSON.parse(ev.data));
    }
    // Chat дёргает this.props.app.send(obj) чтобы послать message на сервер
    send = (obj) => this._ws?.send(JSON.stringify(obj));

    render(_, { connected }) {
        return html`
            <${Chat} app=${this} connected=${connected}
                     ref=${c => this._chat = c} />
        `;
    }
}

render(html`<${App}/>`, document.body);
```

## Питон-часть: WebTransport и роуты

```python
# /workspace/tools/myeditor/__init__.py
from typing import Annotated
from agent import Skill, tool
from src.transport.web import WebTransport
from src.transport.multi import MultiTransport


class EditorTransport(WebTransport):
    def __init__(self, root):
        super().__init__(prefix="/editor", verbose=False)
        self.root = root

    def register_routes(self):
        # Свои REST-ручки — до super().
        self.register_json_route("get",  "/api/files", self._list_files)
        self.register_json_route("post", "/api/save",  self._save_file)
        super().register_routes()   # /ws и статика

    async def _list_files(self, query, body, path_params):
        import os
        return {"files": os.listdir(self.root)}

    async def _save_file(self, query, body, path_params):
        # body — уже распарсенный JSON от клиента (для GET будет None).
        path = body["path"]
        with open(path, "w", encoding="utf-8") as f:
            f.write(body["content"])
        return {"ok": True}

    # Если нужен свой WS-протокол (игра, редактор) — перегрузи это.
    # Без override базовый класс сам обработает стандартный чат-протокол.
    async def ws_handle_message(self, msg):
        if msg.get("type") == "chat":
            # эхо всем клиентам
            await self.send({"type": "chat", "text": msg["text"]})


class MyEditorSkill(Skill):
    @tool("Открыть редактор в вебе.")
    async def open(self, path: Annotated[str, "Путь к папке для редактирования."]) -> dict:
        web = EditorTransport(path)
        sub = await self.agent.spawn_subagent(
            "editor",
            skills=[],
            transport=MultiTransport([self.agent.transport, web]),
        )
        url = await web.get_url()
        # Кнопка со ссылкой в основной чат.
        await self.agent.transport.send_app_url(url, "Открыть редактор", "Открыть")
        return {"url": url}
```

### `register_json_route(method, path, handler)`

- `method`: `"get"`, `"post"`, `"put"`, `"patch"`, `"delete"` (lower-case).
- `path`: внутри твоего namespace'а. Полный URL будет
  `/{agent_id}{prefix}{path}`. Для path-параметров используй синтаксис
  Starlette: `"/api/file/{name:path}"`, `"/items/{id}"`.
- `handler(query, body, path_params)` — sync или async.
  - `query: dict[str, str]` — query-string (все значения строки).
  - `body` — распарсенный JSON из тела запроса (POST/PUT/PATCH), либо
    `None` для GET/DELETE или если парсинг упал.
  - `path_params: dict[str, str]` — path-переменные из шаблона пути.
  - Возврат: `dict`/`list` → JSON-ответ, `str` → text/plain. HTTP-статус
    всегда 200 — если нужен другой, клади маркер в body (`{"error": ...}`)
    и обрабатывай на клиенте.

`register_route` (полный FastAPI-контракт с Pydantic/Query/Request) из
sandbox **не работает** — FastAPI не может интроспектировать Proxy-handler
через RPC-границу. Используй `register_json_route`.

Зови `super().register_routes()` **в конце** своего `register_routes`,
чтобы WebSocket-эндпоинт и раздача статики легли поверх.

### `ws_handle_message(msg)`

`msg` — распарсенный JSON от клиента. Перегружай если нужен свой
протокол (игра, совместный редактор, кастомные events). Без override
базовый класс обрабатывает стандартный чат-протокол (см. ниже).

### `self.send(event, replay=False)` (внутри WebTransport)

Ретранслирует event всем подключённым клиентам. `replay=True` кладёт
event в буфер — новые клиенты получат его при connect'е. Полезно для
статуса, состояния игры и т.п.

## Стандартный чат-протокол

Если не переопределяешь `ws_handle_message` и фронт использует готовый
`Chat.js`, протокол такой.

**Сервер → клиент** — события `{"type": "transport", "method": <m>, ...}`:

| method              | Поля                                | Что делает                                        |
|---------------------|-------------------------------------|---------------------------------------------------|
| `send_message`      | `text`, `stream_id`, `final`        | Новое сообщение или апдейт стрима.                |
| `send_thinking`     | `text`, `stream_id`, `final`        | Блок «мысли» (серый).                             |
| `send_processing`   | `active`                            | Индикатор загрузки.                               |
| `on_tool_call`      | `name`, `args`                      | Карточка вызова тула.                             |
| `on_tool_result`    | `name`, `result`                    | Результат тула.                                   |
| `inject_message`    | `text`                              | Системное сообщение в ленте.                      |

Эти события шлются автоматически когда твой скилл зовёт методы
`self.agent.transport.send_message(...)` и т.д.

**Клиент → сервер:**

```json
{"type": "transport", "method": "process_message",
 "content_parts": [{"type": "text", "text": "привет"}],
 "trigger_answer": true}
```

Базовый `ws_handle_message` эхо`ит сообщение всем (чтобы все клиенты
увидели что юзер написал) и зовёт `self.process_message(...)` — это
стандартная точка входа, которая запускает ответ агента.

## Чеклист

- [ ] Наследуюсь от `WebTransport` с внятным `prefix` (напр. `/editor`).
- [ ] В `ui/` рядом со скиллом лежит `app.js` (`index.html` и `lib.js`
      подтянутся из шаред-UI).
- [ ] `app.js` импортирует из `./lib.js`.
- [ ] WebSocket грузится по `location.pathname + 'ws'` — не хардкодь.
- [ ] В `register_routes` — свои ручки, в конце `super().register_routes()`.
- [ ] Если нужен стандартный чат — импортирую `Chat` из
      `./components/common/Chat.js`, свой `ws_handle_message` не пишу.
- [ ] Под-агент получает `MultiTransport([self.agent.transport, web])`.
- [ ] Пользователю отправлена ссылка: `self.agent.transport.send_app_url(url, label, button)`.

## Что НЕ делать

- Не поднимать свой `uvicorn`/`aiohttp` сервер внутри контейнера —
  порт наружу не выставлен, клиент не достучится. `WebTransport`
  цепляется к уже поднятому на хосте FastAPI.
- Не тянуть React/Vue/сборщики. `lib.js` уже содержит Preact+htm+bau-css
  inline — этого хватает на любой UI в проекте.
- Не хардкодить URL вида `ws://localhost:8765/main/editor/ws`. Всегда
  стройся от `location.host + location.pathname` — так один код работает
  и локально, и через туннель, и для любого `agent_id`.
