# Agent-Zero: разбор архитектуры агента и памяти

> Репозиторий: https://github.com/frdel/agent-zero  
> Исходники анализировались из `lib/agent-zero/`

---

## Быстрый ответ на три ключевых вопроса

### 1. Когда диалог попадает в память?

Два независимых механизма:

**А) Автоматически — в конце каждого monologue (каждого ответа агента)**  
`monologue_end` extensions запускаются в фоне (`DeferredTask`) после каждого ответа:
- Utility LLM читает **всю** историю → извлекает факты → сохраняет в FRAGMENTS
- Utility LLM читает **всю** историю → извлекает решения → сохраняет в SOLUTIONS  
Триггер: завершение цикла `monologue()`, не по токенам.  
→ `lib/agent-zero/python/extensions/monologue_end/_50_memorize_fragments.py`  
→ `lib/agent-zero/python/extensions/monologue_end/_51_memorize_solutions.py`

**Б) Явно — агент сам вызывает тул `memory_save`**  
Агент может в любой момент сохранить что-то через `{"tool_name": "memory_save", "tool_args": {"text": "...", "area": "main"}}`.  
→ `lib/agent-zero/python/tools/memory_save.py`

---

### 2. Как обрабатывается диалог и что получается на выходе?

```
Вся история (concat_messages)
        ↓
Utility LLM + промпт "найди факты"
        ↓
JSON: ["Факт 1", "Факт 2", ...]
        ↓
Для каждого факта: поиск похожих в FAISS
        ↓
Utility LLM + промпт "что делать с дублями?"
        ↓
JSON: {"action": "merge|replace|skip...", ...}
        ↓
FAISS: удалить старые / вставить новый / пропустить
```

На выходе: отдельные текстовые документы в векторной базе FAISS.  
Не граф, не блоки — просто строки с метаданными (область, timestamp, теги).

---

### 3. Как память попадает в контекст?

**А) Автоматически — каждые N итераций message loop**  
Extension `_50_recall_memories.py` срабатывает при `iteration % memory_recall_interval == 0`:
1. Строит поисковый запрос из сообщения пользователя + последних N токенов истории
2. (Опционально) Utility LLM улучшает запрос
3. Семантический поиск в FAISS по MAIN+FRAGMENTS и отдельно по SOLUTIONS
4. (Опционально) LLM фильтрует нерелевантные результаты
5. Вставляет в системный промпт через `extras["memories"]` и `extras["solutions"]`

**Б) Явно — агент вызывает тул `memory_load`**  
`{"tool_name": "memory_load", "tool_args": {"query": "...", "threshold": 0.7, "limit": 10}}`

---

## Общий цикл работы агента

### Monologue — основная единица работы

**Monologue** — цикл обработки одного пользовательского сообщения до финального ответа. Внутри может быть много итераций (вызовов LLM + тулов).

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
monologue_start extensions  (инициализация памяти)
    ↓
message loop:
    ├─ каждые N итераций → recall: поиск памяти → вставка в промпт
    ├─ LLM вызов → JSON с tool_name + tool_args
    ├─ выполнение тула Python
    └─ если break_loop=True → финальный ответ, иначе → следующая итерация
    ↓
monologue_end extensions (сохранение памяти в фоне)
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

**Зачем:** совместимость с любыми моделями, не только поддерживающими tool_use API.

---

## Система памяти

### Хранилище: одна FAISS база, три области

Всё хранится в одной FAISS базе (`usr/memory/`), делится на три логических области через метаданные:

| Область | Что хранится | Кто пишет |
|---|---|---|
| `MAIN` | Общие факты, знания | Агент через `memory_save` + knowledge файлы |
| `FRAGMENTS` | Факты из диалогов | Автоматически после каждого monologue |
| `SOLUTIONS` | Успешные технические решения | Автоматически после каждого monologue |

**Физически — два файла одной базы:**
- `index.faiss` — векторный индекс (только числа)
- `index.pkl` — docstore: текст + метаданные

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

### Инструменты памяти

```python
# lib/agent-zero/python/tools/memory_save.py
# memory_save(text, area="main")  → сохраняет, возвращает ID

# lib/agent-zero/python/tools/memory_load.py
# memory_load(query, threshold=0.7, limit=10)  → семантический поиск

# lib/agent-zero/python/tools/memory_forget.py
# memory_forget(query, threshold=0.7)  → удалить по семантике

# lib/agent-zero/python/tools/memory_delete.py
# memory_delete(ids="id1,id2")  → удалить по ID
```

---

## Промпты

### Извлечение фактов из истории

**Системный промпт** (`prompts/memory.memories_sum.sys.md`):

```
# Assistant's job
1. The assistant receives a HISTORY of conversation between USER and AGENT
2. Assistant searches for relevant information from the HISTORY worth memorizing
3. Assistant writes notes about information worth memorizing for further use

# Format
- The response format is a JSON array of text notes containing facts to memorize
- If the history does not contain any useful information, the response will be an empty JSON array.

# Output example
[
  "User's name is John Doe",
  "User's dog's name is Max",
]

# Rules
- Only memorize complete information that is helpful in the future
- Never memorize vague or incomplete information
- Focus only on relevant details and facts like names, IDs, events, opinions etc.
- Do not memorize facts that change like time, date etc.

# Merging and cleaning
- Do not break information related to the same subject into multiple memories, keep them as one text
- Example: Instead of "User's dog is Max", "Max is 6 years old", "Max is white and brown"
  create one: "User's dog is Max, 6 years old, white and brown."

# WRONG examples (never output):
> Dog Information                          ← нет фактов
> User greeted with 'hi'                   ← просто разговор
> Today is Monday                          ← изменяемый факт
> Respond with a warm greeting...          ← инструкции ИИ
```

**Вход:** `concat_messages(self.agent.history)` — вся сжатая история  
**Выход:** `["факт1", "факт2"]`

---

### Извлечение решений из истории

**Системный промпт** (`prompts/memory.solutions_sum.sys.md`):

```
# Assistant's job
1. The assistant receives a history of conversation between USER and AGENT
2. Assistant searches for successful technical solutions by the AGENT
3. Assistant writes notes about the successful solutions for memorization

# Format
JSON array with "problem" and "solution" properties:
[
  {
    "problem": "Task is to download a video from YouTube.",
    "solution": "1. pip install yt-dlp\n2. yt-dlp YT_URL"
  }
]

# Rules
- !! Only consider solutions that have been SUCCESSFULLY EXECUTED, never speculate
- Only memorize complex solutions with key details required for reproduction
- Never memorize simple operations like file handling, web search etc.
- Focus on: libraries used, code, encountered issues, error fixing

# Wrong examples:
> Problem: User asked to create a text file.  ← слишком простая операция
> Problem: The user has greeted me with 'hi'. ← не техническая задача
```

**Вход:** вся история  
**Выход:** `[{"problem": "...", "solution": "..."}]`

---

### Консолидация памяти (дедупликация)

**Системный промпт** (`prompts/memory.consolidation.sys.md`):

```
You are an intelligent memory consolidation specialist.
Analyze new memory against existing similar memories.

Similarity Score Awareness:
- >0.9 → very similar, suitable for REPLACE
- 0.7-0.9 → related but distinct, use caution with REPLACE
- <0.7 → different content, avoid REPLACE

Output format:
{
  "action": "merge|replace|keep_separate|update|skip",
  "memories_to_remove": ["id1", "id2"],
  "memories_to_update": [{"id": "...", "new_content": "..."}],
  "new_memory_content": "final consolidated text",
  "reasoning": "brief explanation"
}

Scenarios:
- merge: "Alpine.js form validation should use x-on:submit.prevent"
         + "Alpine.js forms need proper event handling"
         → объединить в одно

- replace: "Updated API endpoint is now /api/v2/users"
           + "User API endpoint is /api/users"  (similarity >0.9)
           → заменить старое новым

- keep_separate: "Python async/await syntax"
                 + "Python list comprehensions"
                 → оба нужны, разные концепции
```

**Сообщение** (`prompts/memory.consolidation.msg.md`):
```
Process the consolidation for this scenario:

Memory Area: {{area}}
Current Timestamp: {{current_timestamp}}

New Memory to Process:
{{new_memory}}

Existing Similar Memories:
{{similar_memories}}
```

---

### Компрессия истории (суммаризация)

**Системный промпт** (`prompts/fw.topic_summary.sys.md`):

```
# AI role
You are AI summarization assistant
You are provided with a conversation history
You must return a single summary of all records

# Expected output
- Maximum one paragraph, approximately 100 words
- Shorter if original is shorter
- No intro, no conclusion, no formatting
- Only the summary text is returned
```

**Сообщение** (`prompts/fw.topic_summary.msg.md`):
```
# Message history to summarize:
{{content}}
```

---

### Шаблоны вставки памяти в промпт

**Воспоминания** (`prompts/agent.system.memories.md`):
```
# Memories on the topic
- following are memories about current topic
- do not overly rely on them they might not be relevant

{{memories}}
```

**Решения** (`prompts/agent.system.solutions.md`):
```
# Solutions from the past
- following are memories about successful solutions of related problems
- do not overly rely on them they might not be relevant

{{solutions}}
```

---

## Recall: автоматическая загрузка памяти в контекст

```python
# lib/agent-zero/python/extensions/message_loop_prompts_after/_50_recall_memories.py
```

**Срабатывает:** каждые `memory_recall_interval` итераций message loop.

**Процесс:**

```python
# Шаг 1: Построить поисковый запрос
if set["memory_recall_query_prep"]:
    # Utility LLM генерирует запрос из истории + сообщения пользователя
    query = await call_utility_model(system="memory.memories_query.sys.md", ...)
else:
    # Просто: сообщение + последние N символов истории
    query = user_instruction + "\n\n" + history[-memory_recall_history_len:]

# Шаг 2: Семантический поиск
memories = db.search_similarity_threshold(
    query=query,
    limit=memory_recall_memories_max_search,
    threshold=memory_recall_similarity_threshold,
    filter="area == 'main' or area == 'fragments'",
)
solutions = db.search_similarity_threshold(
    query=query,
    limit=memory_recall_solutions_max_search,
    threshold=memory_recall_similarity_threshold,
    filter="area == 'solutions'",
)

# Шаг 3 (опционально): LLM фильтрует нерелевантные результаты
if set["memory_recall_post_filter"]:
    filter_inds = await call_utility_model(system="memory.memories_filter.sys.md", ...)
    memories = [memories[i] for i in filter_inds if i < len(memories)]

# Шаг 4: Вставить в системный промпт
extras["memories"] = parse_prompt("agent.system.memories.md", memories=memories_txt)
extras["solutions"] = parse_prompt("agent.system.solutions.md", solutions=solutions_txt)
```

---

## История диалогов и компрессия

История не бесконечна. Структура:

```python
# lib/agent-zero/python/helpers/history.py:298
class History:
    bulks    # старые топики → сжаты в суммари (20% лимита)
    topics   # завершённые топики (30% лимита)
    current  # текущий monologue (50% лимита)
```

При переполнении — `compress()`:

```python
# lib/agent-zero/python/helpers/history.py:368
# 1. Сжать большие сообщения в топиках
# 2. Суммаризировать средние сообщения через utility LLM (fw.topic_summary.sys.md)
# 3. Слить старые топики в bulk + суммаризировать
# 4. Удалить старые bulks
```

Константы:
```python
# lib/agent-zero/python/helpers/history.py:12
CURRENT_TOPIC_RATIO = 0.5   # 50% лимита — текущий диалог
HISTORY_TOPIC_RATIO = 0.3   # 30% — недавние топики
HISTORY_BULK_RATIO  = 0.2   # 20% — старые суммари
```

---

## ⚠️ Известные проблемы

### Полная история при каждой консолидации

```python
# lib/agent-zero/python/extensions/monologue_end/_50_memorize_fragments.py:43
msgs_text = self.agent.concat_messages(self.agent.history)  # ← ВСЯ история
```

После каждого monologue utility LLM читает **всю** накопленную историю (с компрессией):
- Все ранее извлечённые факты будут найдены снова → консолидация засмерджит их с `SKIP`
- После monologue №50: LLM платно читает 50 диалогов ради 1-2 новых фактов
- Никакого delta-механизма ("что уже обработано") нет

**Как должно быть:** передавать только `history.current` — текущий monologue.

**Для сравнения** — в `LettaProvider` (`src/memory/letta.py`):
```python
async def _consolidate(self, pending: list):
    transcript = self._build_transcript(pending)  # только новые туры с последней консолидации
```

### Нет блоков "всегда в контексте"

Вся память — только через поиск. Базовые факты ("как зовут пользователя") нужно каждый раз искать и они могут не найтись при плохом запросе. В Letta и нашем `LettaProvider` это решается через core memory блоки — они всегда в системном промпте без поиска.

---

## Сводная схема

```
Сообщение пользователя
        ↓
[monologue_start] → _10_memory_init: инициализация FAISS
        ↓
[message loop] — повторяется до break_loop:
  ├── итерация % N == 0 → [recall]
  │     ├─ query = user_message + история (или utility LLM генерирует запрос)
  │     ├─ FAISS поиск MAIN+FRAGMENTS → extras["memories"] → системный промпт
  │     └─ FAISS поиск SOLUTIONS → extras["solutions"] → системный промпт
  ├── LLM → JSON {"tool_name": "...", "tool_args": {...}}
  ├── DirtyJson парсинг → вызов Python тула
  └── break_loop=True → ответ пользователю
        ↓
[monologue_end] — в фоне (DeferredTask):
  ├── _50_memorize_fragments:
  │     utility LLM(ВСЯ история, memory.memories_sum.sys.md)
  │     → ["факт1", "факт2"]
  │     → для каждого: MemoryConsolidator (memory.consolidation.sys.md) → FAISS
  └── _51_memorize_solutions:
        utility LLM(ВСЯ история, memory.solutions_sum.sys.md)
        → [{"problem": ..., "solution": ...}]
        → для каждого: MemoryConsolidator → FAISS
```
