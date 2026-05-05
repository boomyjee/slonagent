# Как писать скиллы для sandbox

Прочитай этот файл перед созданием скрипта в `/workspace/tools/` — он
короткий. Подробности — по ссылкам в `/slonagent/docs/*.md`.

## Минимальный скилл

```python
# /workspace/tools/weather.py
from typing import Annotated
from agent import Skill, tool


class WeatherSkill(Skill):
    @tool("Получить погоду в городе.")
    async def get(self, city: Annotated[str, "Название города."]) -> dict:
        await self.agent.transport.send_message(f"Проверяю {city}…")
        return {"city": city, "temp": 20}
```

После сохранения файла тул автоматически появится под именем
`sandbox_weather_get`. Никакой регистрации не нужно.

Правила именования, типы параметров, что можно возвращать, стриминг —
[docs/skill-basics.md](docs/skill-basics.md).

## Что доступно в скилле

- `self.agent` — текущий агент: `id`, `transport`, `memory`,
  `spawn_subagent(...)`, `next_message()`, `loop()`, `skills`.
- `self.agent.transport` — транспорт: `send_message`, `send_thinking`,
  `send_processing`, `on_tool_call`, `on_tool_result`, `inject_message`,
  `send_app_url`.
- `self.agent.memory` — память: `clear()`, `add_turn(turn)`.

Полные таблицы с параметрами — [docs/agent-api.md](docs/agent-api.md).

## Нюансы работы

- `self.agent` и всё что от него возвращается — **живёт на хосте**, в
  твоём коде это прозрачные ссылки. Работает и из sync, и из async —
  в async пиши `await`, в sync просто вызывай.
- Не лови исключения «чтобы красиво» — они прилетают на следующий шаг
  LLM с полным traceback'ом (`File "/workspace/tools/your.py", line N…`),
  и LLM сама их чинит. Лови только легитимные failure-модусы (HTTP 404
  при поиске — это результат, а не краш).
- Каждый вызов тула — **свежий процесс контейнера**. Глобальные
  переменные модуля обнулятся. Состояние храни в `/workspace/` или
  через `self.agent.memory`.

## Чеклист

- [ ] Файл в `/workspace/tools/`, класс наследуется от `Skill`.
- [ ] Tool-методы помечены `@tool("описание")`, параметры — `Annotated[T, "..."]`.
- [ ] Долгие операции: `send_processing(True/False)`, стрим — через
      `send_message(..., stream_id=..., final=False)`.

## Файлы и монтирование

- `/workspace` — read/write рабочая директория. Тут твои тулы и артефакты.
- `/slonagent` — read-only: этот SKILLS.md, `agent.py`, `docs/*.md`.
  Импорты `from agent import ...` работают отсюда.
- Смонтированные папки хоста (read-only) — см. секцию Sandbox в
  системном промпте.
