# Основы скилла

## Где лежит файл

- `/workspace/tools/<имя>.py` — одиночный файл.
- `/workspace/tools/<имя>/__init__.py` — пакет, если тулу нужны
  под-модули (UI, хелперы). Остальные файлы пакета живут рядом.

Sandbox сканирует папку перед каждым вызовом — новый тул появится без
рестарта агента.

## Класс

Наследуйся от `Skill` (`from agent import Skill, tool`). Из имени класса
отрезается суффикс `Skill` / `Memory` / `Provider`, остаток в lowercase
идёт как префикс имени тула:

| Класс                | Префикс       |
|----------------------|---------------|
| `WeatherSkill`       | `weather`     |
| `CodingModeSkill`    | `codingmode`  |
| `EditorProvider`     | `editor`      |

Полное имя тула изнутри контейнера: `{префикс}_{метод}`. Снаружи LLM
видит его же, но с добавленным префиксом `sandbox_`.

Пример: `WeatherSkill.get` → `weather_get` → LLM зовёт как
`sandbox_weather_get`.

## Метод

Любое имя, украшенное `@tool("описание для LLM")`. Может быть `async`
или sync — оба варианта работают.

```python
class WeatherSkill(Skill):
    @tool("Получить погоду.")
    async def get(self, city: Annotated[str, "Название города."]) -> dict:
        return {"temp": 20}
```

## Параметры

Аннотируй через `Annotated[T, "описание"]`.

Разрешённые базовые типы `T`:
- `str`, `int`, `float`, `bool`
- `list[str]`, `list[int]`

Параметры без дефолта → `required` в JSON-schema, с дефолтом → `optional`.
Описание из `Annotated` попадает в schema как `description` и видно LLM.

```python
@tool("Поиск в базе.")
def search(
    self,
    query: Annotated[str, "Что ищем."],
    limit: Annotated[int, "Максимум результатов."] = 10,
    tags: Annotated[list[str], "Фильтр по тэгам."] = None,
): ...
```

## Что можно вернуть

- `dict`, `list`, `str`, `int`, `float`, `bool`, `None` — уйдут в LLM
  как JSON.
- Всё остальное превратится в `str(v)` при отправке.

## Ошибки

Если операция провалилась — **просто рейзни исключение**:

```python
if not api_key:
    raise RuntimeError("OPENAI_API_KEY не задан в окружении")
```

LLM на следующем шаге получит результат вида:

```
{"error": "Traceback ... File \"/workspace/tools/your.py\", line 11, ...
 RuntimeError: OPENAI_API_KEY не задан в окружении"}
```

— и сама починит. Поэтому:

- **Пиши осмысленные сообщения.** Плохо: `raise Exception("error")`.
  Хорошо: `raise RuntimeError("файл config.yaml не найден в /workspace")`.
- **Не глотай исключения в try/except:** traceback — это для LLM
  чек-лист как чинить, его потеря усложнит отладку.
- **Лови только легитимные failure-модусы.** HTTP 404 при поиске — это
  `{"found": False}`. А `KeyError` в твоём dict — пусть падает, это баг.

## Стриминг и индикация

Долгие операции:

```python
await self.agent.transport.send_processing(True)
try:
    ... долгая работа ...
finally:
    await self.agent.transport.send_processing(False)
```

Частые апдейты в один «пузырь» чата (удобно для прогресса):

```python
sid = "job-42"  # любой уникальный токен
await self.agent.transport.send_message("Шаг 1…", stream_id=sid, final=False)
await self.agent.transport.send_message("Шаг 1, 2…", stream_id=sid, final=False)
await self.agent.transport.send_message("Готово.", stream_id=sid, final=True)
```

UI склеит все чанки с одинаковым `stream_id` в одно сообщение.

## Состояние между вызовами

Каждый tool-call стартует новый процесс. Глобальные переменные модуля,
открытые соединения, in-memory кэш — всё обнулится. Сохраняй состояние:

- В файлах на `/workspace/` (persisted между вызовами).
- Через `self.agent.memory.add_turn(...)` (в истории диалога).
- В БД/Redis/etc — коннектись заново при каждом вызове.

## Импорты

- `from agent import Skill, tool` — всегда.
- `from typing import Annotated`.
