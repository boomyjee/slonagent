# API хоста

Всё ниже — методы, которые ты вызываешь на `self.agent` и его детях.
В async-коде пиши `await`; из sync просто вызывай — подождёт.

## `self.agent`

| Что                                                               | Описание                                                       |
|-------------------------------------------------------------------|----------------------------------------------------------------|
| `id`                                                              | ID текущего (под-)агента. У sub-agent формат `"main:<name>"`. |
| `transport`                                                       | Транспорт хоста (см. таблицу ниже).                            |
| `memory`                                                          | Память; доступны `clear()`, `add_turn(turn)`.                  |
| `spawn_subagent(name, memory_providers=[], skills=[...], transport=...)` | Создаёт под-агента. Возвращает его handle.             |
| `next_message()`                                                  | Ждёт следующее сообщение от пользователя (для режимов).        |
| `loop()`                                                          | Запускает основной loop под-агента.                            |
| `skills`                                                          | Список скиллов.                                                |

### `spawn_subagent` — типичный паттерн

```python
from src.transport.multi import MultiTransport

sub = await self.agent.spawn_subagent(
    "helper",
    skills=["sandbox", "memory"],
    transport=MultiTransport([self.agent.transport, my_web_transport]),
)
await sub.loop()  # блокирующий loop — ок, под-агент будет жить до выхода
```

Под-агенту с веб-UI обычно дают `MultiTransport([self.agent.transport, web])`
— чтобы сообщения летели и в основной чат (Telegram/Dashboard), и в
твой WebSocket.

## `self.agent.transport`

| Метод                                         | Для чего                                                                                    |
|-----------------------------------------------|---------------------------------------------------------------------------------------------|
| `send_message(text, stream_id=None, final=True)` | Сообщение пользователю. Для стрима — несколько вызовов с одним `stream_id` и `final=False`, последний с `final=True`. |
| `send_thinking(text, stream_id=None, final=False)` | То же, но для «мыслей» (отдельный серый блок в UI).                                       |
| `send_system_prompt(text)`                    | Показать системный промпт в UI (для дебага).                                               |
| `send_processing(active: bool)`               | Показать/скрыть индикатор «агент думает».                                                  |
| `on_tool_call(name, args)`                    | Сообщить UI что начался вызов тула.                                                        |
| `on_tool_result(name, result)`                | Сообщить UI результат тула.                                                                |
| `inject_message(text)`                        | Вставить системное сообщение в ленту (напр. «пользователь подключился»).                   |
| `send_app_url(url, text, button="")`          | Показать кнопку с URL — обычно ссылку на свой веб-UI.                                       |

## `self.agent.memory`

| Метод               | Описание                                             |
|---------------------|------------------------------------------------------|
| `clear()`           | Очистить историю диалога.                            |
| `add_turn(turn)`    | Добавить реплику (`{"role": ..., "parts": [...]}`).  |
