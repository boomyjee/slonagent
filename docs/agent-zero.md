# Agent-Zero: разбор архитектуры агента и памяти

> Репозиторий: https://github.com/frdel/agent-zero  
> Исходники анализировались из `lib/agent-zero/`

---

## 1. Общий цикл работы агента

### Monologue — основная единица работы

**Monologue** — это цикл обработки одного пользовательского сообщения до финального ответа. Внутри может быть много итераций (вызовов LLM + тулов).

```python
# lib/agent-zero/agent.py:383
async def monologue(self):
    while True:
        await self.call_extensions("monologue_start", ...)
        # ... message loop ...
        await self.call_extensions("monologue_end", ...)
```

**Структура одного monologue:**
```
пользователь → сообщение
    ↓
monologue_start extensions  (инициализация памяти, etc.)
    ↓
message loop:
    LLM вызов → JSON с tool_name + tool_args
    → выполнение тула
    → если break_loop=True → финальный ответ
    → иначе → снова LLM
    ↓
monologue_end extensions  (сохранение памяти, etc.)
    ↓
пользователь ← ответ
```

### Как вызываются тулы — ReAct без function calling

Agent-zero **не использует** нативный function calling API. Вместо этого LLM обязана отвечать строго JSON:

```markdown
# lib/agent-zero/prompts/agent.system.main.communication.md
```json
{
    "thoughts": ["думаю...", "делаю..."],
    "headline": "Краткое описание действия",
    "tool_name": "memory_save",
    "tool_args": {
        "text": "User's name is John"
    }
}
```

Парсинг через `DirtyJson` — прощает мусор, незакрытые кавычки, комментарии:

```python
# lib/agent-zero/agent.py:855
async def process_tools(self, msg: str):
    tool_request = extract_tools.json_parse_dirty(msg)
    # → вызов Python-метода соответствующего тула
```

**Зачем:** совместимость с любыми моделями, не только теми что поддерживают tool_use API.

---

## 2. Система памяти

### Хранилище: одна FAISS база, три области

Всё хранится в одной FAISS базе (`usr/memory/`), но делится на три логических области через метаданные:

| Область | Что хранится | Кто пишет |
|---|---|---|
| `MAIN` | Общие факты, знания | Агент явно через `memory_save` + knowledge файлы |
| `FRAGMENTS` | Факты извлечённые из диалогов | Автоматически после каждого monologue |
| `SOLUTIONS` | Успешные технические решения | Автоматически после каждого monologue |

**Физическое хранение — два файла одной базы:**
- `index.faiss` — векторный индекс (только числа)
- `index.pkl` — docstore: текст + метаданные документов

```python
# lib/agent-zero/python/helpers/memory.py:204
index = faiss.IndexFlatIP(len(embedder.embed_query("example")))
db = MyFaiss(
    embedding_function=embedder,
    index=index,
    docstore=InMemoryDocstore(),
    distance_strategy=DistanceStrategy.COSINE,
)
```

При поиске: FAISS находит ближайшие векторы → по индексу берёт ID → достаёт текст из pkl.

### Инструменты памяти (вызываются агентом)

```python
# lib/agent-zero/python/tools/memory_save.py:7
# memory_save(text, area="main") → сохраняет, возвращает ID

# lib/agent-zero/python/tools/memory_load.py
# memory_load(query, threshold=0.7, limit=10) → семантический поиск

# lib/agent-zero/python/tools/memory_forget.py
# memory_forget(query, threshold=0.7) → удалить по семантике

# lib/agent-zero/python/tools/memory_delete.py
# memory_delete(ids="id1,id2") → удалить по ID
```

### Автоматическая загрузка памяти в контекст

```python
# lib/agent-zero/python/extensions/message_loop_prompts_after/_50_recall_memories.py
```

Каждые N итераций (настраивается) автоматически ищет релевантные воспоминания по тексту сообщения пользователя и вставляет в системный промпт через `extras["memories"]`.

---

## 3. Автоматическое сохранение памяти (monologue_end)

После каждого monologue запускаются два extension в фоне (`DeferredTask`):

### FRAGMENTS: извлечение фактов

```python
# lib/agent-zero/python/extensions/monologue_end/_50_memorize_fragments.py:42
system = self.agent.read_prompt("memory.memories_sum.sys.md")
msgs_text = self.agent.concat_messages(self.agent.history)  # ← ВСЯ история
memories_json = await self.agent.call_utility_model(system=system, message=msgs_text)
```

Промпт (`prompts/memory.memories_sum.sys.md`) говорит:
> "Прочитай историю диалога, найди факты достойные запоминания. Верни JSON-массив строк."

```json
["User's name is John Doe", "User's dog Max is 6 years old, white and brown"]
```

Промпт специально просит **объединять** связанные факты в одну строку, не дробить.

### SOLUTIONS: извлечение решений

```python
# lib/agent-zero/python/extensions/monologue_end/_51_memorize_solutions.py
# Аналогично, но другой промпт: prompts/memory.solutions_sum.sys.md
```

Промпт ищет только **успешно выполненные** технические решения:
```json
[{"problem": "Download YouTube video", "solution": "1. pip install yt-dlp\n2. yt-dlp URL"}]
```

---

## 4. Консолидация памяти

Перед сохранением каждого нового факта запускается `MemoryConsolidator`:

```python
# lib/agent-zero/python/helpers/memory_consolidation.py
```

**Процесс:**
1. Семантический поиск похожих воспоминаний в FAISS
2. Utility LLM анализирует: новое + похожие → выбирает действие
3. Применяет решение

**Промпт консолидации** (`prompts/memory.consolidation.sys.md`) ожидает JSON:
```json
{
    "action": "merge",
    "memories_to_remove": ["id1", "id2"],
    "memories_to_update": [],
    "new_memory_content": "Объединённый текст",
    "reasoning": "Почему так"
}
```

**Возможные действия:**

| Действие | Когда | Что происходит |
|---|---|---|
| `KEEP_SEPARATE` | Факты разные | Сохранить рядом |
| `MERGE` | Частично пересекаются | Удалить оба, создать объединённый |
| `REPLACE` | Очень похожи (≥0.9) | Заменить старый новым |
| `UPDATE` | Новое дополняет старое | Обновить существующее |
| `SKIP` | Уже есть, ничего нового | Не сохранять |

```python
# lib/agent-zero/python/helpers/memory_consolidation.py:455
analysis_response = await self.agent.call_utility_model(
    system=system_prompt,
    message=message_prompt,
)
result_json = DirtyJson.parse_string(analysis_response)
action = ConsolidationAction(result_json.get('action', 'skip'))
```

---

## 5. История диалогов и компрессия

История не растёт бесконечно. Структура:

```python
# lib/agent-zero/python/helpers/history.py:298
class History:
    bulks   # ← старые топики, сжатые в суммари (20% лимита)
    topics  # ← завершённые топики (30% лимита)
    current # ← текущий monologue (50% лимита)
```

При превышении лимита срабатывает `compress()`:

```python
# lib/agent-zero/python/helpers/history.py:368
async def compress(self):
    # 1. Сжать большие сообщения в топиках
    # 2. Суммаризировать топики через utility LLM
    # 3. Слить старые топики в bulk и суммаризировать
    # 4. Удалить старые bulks
```

---

## 6. Импорт знаний из файлов

Файлы из папки `knowledge/` индексируются в ту же FAISS базу при запуске:

```python
# lib/agent-zero/python/helpers/memory.py:249
async def preload_knowledge(self, log_item, kn_dirs, memory_subdir):
    # Поддерживаемые форматы: TXT, PDF, CSV, HTML, JSON, MD
    # Отслеживание изменений через MD5 checksums (knowledge_import.json)
```

Файлы из корня папки → область `MAIN`.  
Из подпапок `main/`, `fragments/`, `solutions/` → соответствующие области.

Помечаются метаданными `knowledge_source: True` — при консолидации LLM знает что это "авторитетные знания", не перезаписывать воспоминаниями из чата.

---

## 7. ⚠️ Известные проблемы

### Полная история при каждой консолидации

```python
# lib/agent-zero/python/extensions/monologue_end/_50_memorize_fragments.py:43
msgs_text = self.agent.concat_messages(self.agent.history)  # ← ВСЯ история
```

После каждого monologue utility LLM читает **всю** накопленную историю (с компрессией). Это значит:

- После monologue №50 LLM читает 50 сжатых диалогов ради нахождения 1-2 новых фактов
- Все ранее извлечённые факты будут найдены снова — консолидация их засмерджит с `SKIP`/`REPLACE`
- Буквально оплачивать токены за чтение всей истории при каждом сообщении

**Как должно быть:** передавать только `history.current` — текущий monologue (delta). Никакого механизма отслеживания "что уже обработано" нет.

**Для сравнения:** в нашем `LettaProvider` слиптайм-агент получает только `_pending` — туры накопленные с последней консолидации:

```python
# src/memory/letta.py
async def _consolidate(self, pending: list):
    transcript = self._build_transcript(pending)  # только новые туры
```

### Нет блоков "всегда в контексте"

Вся память только через семантический поиск. Базовые факты ("как зовут пользователя") нужно каждый раз искать. В Letta и нашем `LettaProvider` это решается через core memory блоки — они всегда в системном промпте.

---

## 8. Сводная схема

```
Сообщение пользователя
        ↓
[monologue_start] → _10_memory_init: инициализация FAISS
        ↓
[message loop]
  ├── каждые N итераций → recall: семантический поиск → вставка в промпт
  ├── LLM → JSON {tool_name, tool_args}
  ├── выполнение тула (memory_save / memory_load / code_exec / ...)
  └── break_loop=True → ответ пользователю
        ↓
[monologue_end] — в фоне (DeferredTask):
  ├── _50_memorize_fragments:
  │     utility LLM(вся история) → JSON["факт1", "факт2"]
  │     для каждого факта → MemoryConsolidator → FAISS
  └── _51_memorize_solutions:
        utility LLM(вся история) → JSON[{problem, solution}]
        для каждого решения → MemoryConsolidator → FAISS
```
