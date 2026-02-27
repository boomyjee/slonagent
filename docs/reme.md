# ReMe — Анализ системы памяти

**Источник:** `lib/ReMe` (github.com/agentscope-ai/ReMe)  
**Версия:** latest (pypi: `reme-ai`)  
**Авторы:** Alibaba / AgentScope team (10 человек, alibaba-inc.com)  
**Дата анализа:** 2026-02-27  
**Статус:** Beta, есть arxiv paper 2512.10696

---

## Концепция

ReMe — **модульный kit управления памятью** для AI-агентов. Формула:

```
Agent Memory = Long-Term Memory    +  Short-Term Memory
             = (Personal + Task + Tool) Memory + Working Memory
```

Ключевое отличие от других систем: **всё проходит через LLM-трансформацию до попадания в хранилище**. Сырые реплики не хранятся — хранятся только извлечённые факты и паттерны. Оригинал теряется (в отличие от Hindsight, где хранится исходник).

**Уникальная идея:** Task Memory и Tool Memory — типы памяти без аналогов в других системах. Агент накапливает опыт из собственных действий и учится на нём.

---

## Зависимости

Все зависимости **жёсткие** (не optional), `pip install reme-ai` тянет всё:

```
chromadb>=1.3.5        # vector store (local или remote)
qdrant-client>=1.16.0  # альтернативный vector store
elasticsearch>=9.2.0   # альтернативный vector store
transformers>=4.57.3   # embedding модели
litellm>=1.80.0        # LLM backend (любой провайдер)
openai>=2.8.1          # LLM backend
fastapi>=0.121.3       # HTTP сервис
fastmcp>=2.14.1        # MCP сервис
sqlite-vec>=0.1.6      # локальный vector store
```

Требует LLM API (extraction, reranking) + Embedding API (vector search). Без этого не работает.

---

## Физическое хранение

### Схема данных

Единица хранения — `VectorNode`:

```python
VectorNode(
    vector_id   = "dd0b9a45...",        # UUID памяти
    workspace_id = "my_workspace",      # изоляция по workspace
    content     = "when_to_use text",   # ← ЭТО индексируется эмбеддингом
    vector      = [0.023, -0.115, ...], # эмбеддинг content
    metadata    = {
        "memory_type": "task",          # task / personal / tool
        "content": "сам факт/паттерн",  # реальное содержание (в metadata!)
        "time_created": "...",
        "author": "qwen3-8b",
        ...
    }
)
```

**Важно:** эмбеддинг строится по `content` (= `when_to_use` — условие применения), а не по содержанию самой памяти. Поиск идёт по смыслу запроса → матчит условие применения → содержание достаётся из metadata.

### Бэкенды vector store

| Бэкенд | Индекс | Когда использовать |
|--------|--------|-------------------|
| `local` (sqlite-vec) | brute-force cosine | разработка, сотни записей |
| `chroma` (local) | HNSW, `cosine` space | тысячи записей, без сервера |
| `chroma` (HTTP/Cloud) | HNSW | production |
| `qdrant` | HNSW, ANN | production, production |
| `elasticsearch` | approximate KNN | если уже используется ES |
| `pgvector` | IVFFlat / HNSW | если уже есть PostgreSQL |

**Local backend (default для разработки):**
```
./local_vector_store/
  {collection_name}/
    {memory_id}.json   ← один файл = одна память + вектор
```
Поиск: читает все JSON, считает cosine similarity для каждого. O(N).

**ChromaDB (production):**
```python
collection = client.get_or_create_collection(
    name=collection_name,
    metadata={"hnsw:space": "cosine"},  # HNSW граф
)
```
ANN поиск, O(log N). Поддерживает фильтрацию по metadata.

---

## Четыре типа памяти

### 1. Personal Memory — факты о пользователе

**Pipeline записи** (6 шагов):

```
Диалог (список messages)
  │
  ├─ GetObservationOp
  │    Фильтрует реплики без временных слов
  │    LLM prompt: "извлеки факты о пользователе из каждой реплики"
  │    Пример вывода: <1> <> <пользователь аллергичен на кошек> <аллергия, кошки>
  │    → список PersonalMemory(when_to_use=keywords, content=fact)
  │
  ├─ GetObservationWithTimeOp
  │    То же, но для реплик с временной привязкой ("завтра", "вчера")
  │    Добавляет timestamp к факту
  │
  ├─ LoadTodayMemoryOp
  │    Подгружает существующие памяти за сегодня из vector store
  │    (для дедупликации)
  │
  ├─ ContraRepeatOp
  │    Объединяет: новые факты + сегодняшние существующие
  │    LLM проверяет каждую пару:
  │      "矛盾/Contradiction" → новый факт противоречит старому → удалить старый
  │      "被包含/Contained"   → новый факт включает старый → удалить старый
  │      "None"              → оба уникальны → оставить
  │    → deleted_memory_ids + filtered memory_list
  │
  ├─ UpdateInsightOp (опционально)
  │    Берёт "insight" памяти (обобщения) из vector store
  │    Релевантность через Jaccard similarity по ключевым словам
  │    Если overlap > 0.3 → LLM обновляет insight новыми наблюдениями
  │    (insight = "пользователь предпочитает краткие ответы")
  │
  └─ UpdateVectorStoreOp
       DELETE противоречащих старых памятей
       INSERT новых памятей
```

**Pipeline retrieval:**

```
query
  │
  ├─ ExtractTimeOp
  │    Проверяет есть ли временная привязка в запросе
  │    "что я говорил вчера" → {day: -1, ...}
  │
  ├─ RetrieveMemoryOp
  │    vector_store.async_search(query, top_k)
  │    Поиск по when_to_use эмбеддингам
  │
  └─ FuseRerankOp
       Умножает cosine score на коэффициенты:
         insight:        × 2.0  (обобщения важнее)
         obs_customized: × 1.2
         observation:    × 1.0  (обычные факты)
         conversation:   × 0.5  (сырые реплики)
       + time_ratio × 2.0 если временная метка совпадает с запросом
       → top-K по итоговому score
```

**Типы PersonalMemory:**

| Тип | Что хранит | Пример |
|-----|-----------|--------|
| `observation` | атомарный факт из реплики | "пользователь не любит персики" |
| `observation_with_time` | факт с датой | "2026-02-15: встреча с Алексом" |
| `insight` | обобщённый вывод по паттернам | "пользователь интроверт, предпочитает письменный стиль" |

---

### 2. Task Memory — опыт выполнения задач

**Идея:** агент выполнил задачу → LLM анализирует что сработало/не сработало → извлекает переиспользуемые паттерны → при похожих задачах достаёт их и руководствуется.

**Pipeline записи:**

```
Trajectory(messages=[...], score=0.0..1.0, metadata={query: "..."})
  │
  ├─ TrajectorySegmentationOp (для длинных траекторий)
  │    LLM делит на смысловые сегменты (шаги)
  │
  ├─ SuccessExtractionOp (если score > threshold)
  │    Для каждого сегмента LLM извлекает:
  │    {
  │      "when_to_use":  "при работе с пагинированными API",
  │      "experience":   "сначала проверяй total_count, потом итерируй страницы",
  │      "tags":         ["api", "pagination"],
  │      "confidence":   0.85,
  │      "step_type":    "action",
  │      "tools_used":   ["http_get"]
  │    }
  │
  ├─ FailureExtractionOp (если score < threshold)
  │    То же, но формат "что НЕ делать и почему"
  │
  ├─ ComparativeExtractionOp
  │    Сравнивает успешные и неуспешные траектории
  │    LLM извлекает: "вот в чём разница между успехом и неудачей"
  │
  ├─ MemoryDeduplicationOp
  │    Embedding similarity против существующих памятей (threshold=0.5)
  │    + против других памятей в текущем батче
  │    Пропускает если cosine > порога
  │
  ├─ MemoryValidationOp
  │    LLM проверяет: "эта память реально применима к будущим задачам?"
  │    Отфильтровывает тривиальные ("не забывай быть вежливым")
  │
  └─ UpdateVectorStoreOp → vector store
```

**Pipeline retrieval:**

```
task description / messages
  │
  ├─ BuildQueryOp
  │    LLM переписывает описание задачи как поисковый запрос
  │    ("помоги разобраться с пагинацией API" → "API pagination handling patterns")
  │
  ├─ RetrieveMemoryOp
  │    vector_store.async_search(rewritten_query, top_k)
  │
  ├─ RewriteMemoryOp
  │    LLM адаптирует найденные паттерны под текущий контекст задачи
  │
  └─ RerankMemoryOp
       LLM финально ранжирует по релевантности к конкретной задаче
       → список TaskMemory
```

---

### 3. Tool Memory — опыт использования инструментов

**Идея:** каждый вызов инструмента логируется с результатами → после N вызовов LLM генерирует "living guidelines" → перед следующим вызовом агент получает актуальные советы.

**Структура хранения:**

```python
ToolMemory(
    when_to_use = "web_search",           # имя инструмента
    content     = "## Usage Guidelines\n...\n## Statistics\n...",  # LLM summary
    tool_call_results = [
        ToolCallResult(
            tool_name  = "web_search",
            input      = {"query": "...", "max_results": 10},
            output     = "Found 8 results...",
            success    = True,
            time_cost  = 2.3,
            token_cost = 150,
            score      = 0.9,          # LLM оценка качества результата
            summary    = "краткое резюме",
            evaluation = "детальная оценка",
            is_summarized = True,      # уже учтён в summary
        ),
        ...
    ]
)
```

**Pipeline записи:**

```
tool_call_results (список вызовов)
  │
  ├─ ParseToolCallResultOp
  │    LLM оценивает каждый вызов:
  │    score: 0.0 (провал) / 0.5 (частичный) / 1.0 (успех)
  │    + генерирует summary и evaluation
  │    → ToolMemory с заполненными results
  │
  └─ UpdateVectorStoreOp → vector store (один объект на инструмент)
```

**Pipeline суммаризации** (запускается периодически):

```
tool_name
  │
  ├─ Поиск ToolMemory в vector store по имени инструмента
  │
  ├─ Проверка: есть ли unsummarized calls в последних 30?
  │    Нет → пропустить (is_summarized=True для всех)
  │
  └─ SummaryToolMemoryOp
       LLM анализирует последние 30 вызовов:
       - паттерны успешных параметров
       - типичные ошибки и как их избегать
       - оптимальные значения параметров
       → обновляет tool_memory.content = guidelines + statistics
       → помечает обработанные вызовы is_summarized=True
```

**Статистика** (вычисляется по последним 20 вызовам):

```python
tool_memory.statistic() → {
    "avg_token_cost": 156.3,
    "avg_time_cost":  2.134,
    "success_rate":   0.8333,
    "avg_score":      0.742
}
```

**Pipeline retrieval:**

```
tool_name
  → vector_store.async_search(tool_name, top_k=1) [поиск по имени]
  → ToolMemory.content (guidelines текстом)
  → агент получает перед вызовом инструмента
```

---

### 4. Working Memory — управление контекстом

**Идея:** для длинных диалогов (>20K токенов) большие tool outputs выгружаются во внешние файлы, а при необходимости перезагружаются по запросу.

**Три режима:**

```
COMPACT  → только выгрузка больших tool outputs в файлы
COMPRESS → только LLM-сжатие истории в snapshot
AUTO     → сначала COMPACT, если ratio > 0.75 → ещё COMPRESS
```

**COMPACT pipeline:**

```
messages с большим tool output (50K токенов)
  │
  └─ MessageCompactOp
       Для каждого tool message > threshold:
         → сохранить полное содержание в файл:
           store_dir/{chat_id}/msg_{idx}.txt
         → заменить в messages на:
           "[Offloaded] preview первых 200 символов... [файл: path]"
       Результат: messages с короткими ссылками
```

**COMPRESS pipeline:**

```
длинная история messages
  │
  └─ MessageCompressOp
       LLM генерирует compressed snapshot:
         - ключевые решения и договорённости
         - текущее состояние задачи
         - важные факты из диалога
       → один сжатый message вместо N старых
```

**Reload:**

```python
grep_working_memory(query)   # поиск по выгруженным файлам
read_working_memory(path, offset, limit)  # чтение части файла
```

---

## Pretrained Memory Library

Alibaba поставляет предобученные базы памятей для холодного старта:

### `appworld.jsonl` (~100 TaskMemory)

Опыт агента на AppWorld benchmark (управление мобильными приложениями через API). Автор: `qwen3-8b`.

Примеры:
```
when_to_use: "When interacting with APIs that require authentication and multiple steps"
content:     "Always verify the API documentation for required parameters before executing.
              Missing or incorrect parameters lead to failed API calls."

when_to_use: "When encountering persistent errors related to invalid identifiers"
content:     "Validate format and existence of identifiers early. Consider fallback
              strategies if primary identifiers fail."

when_to_use: "When searching for specific data in paginated API responses"
content:     "Ensure query parameters align with expected data format. Validate
              intermediate outputs to confirm relevance before proceeding."
```

### `bfcl_v3.jsonl` (~150 TaskMemory)

Опыт агента на BFCL-V3 benchmark (function calling). Паттерны последовательностей вызовов инструментов.

Примеры:
```
when_to_use: "When user wants to execute stock purchase after reviewing watchlist"
content:     "Follow sequence: get_stock_info → place_order → confirm_order_status.
              Ensures transaction based on up-to-date market data."
tools_used:  ["get_watchlist", "get_stock_info", "place_order", "cancel_order"]

when_to_use: "When user changes mind about pending transaction"
content:     "Efficiently cancel with cancel_order(order_id). Confirm cancellation
              to user, ensure clarity, prevent confusion."
```

**Применение:** загружаешь в vector store → агент на старте уже знает эти паттерны без единой реальной задачи. Для агента с доступом к терминалу и API паттерны про аутентификацию, пагинацию и обработку ошибок API прямо применимы.

---

## Архитектура оператора

Все операции реализованы через `BaseAsyncOp` с `>>` оператором для pipeline:

```python
pipeline = ParseToolCallResultOp() >> UpdateVectorStoreOp()
await pipeline.async_call(tool_call_results=[...], workspace_id="ws")
```

Каждый оп читает из `context` и пишет в `context.response.metadata`. Это позволяет собирать пайплайны из независимых шагов.

---

## Оценка

### Бенчмарки

| Benchmark | Метрика | Без ReMe | С ReMe | Прирост |
|-----------|---------|----------|--------|---------|
| AppWorld (Qwen3-8B) | Pass@4 | 0.329 | 0.363 | +3.5% |
| BFCL-V3 multi-turn | Pass@4 | 0.596 | 0.658 | +6.2% |
| FrozenLake (100 maps) | pass rate | 0.66 | 0.72 | +6.0% |
| Tool Memory bench | Avg Score | 0.672 | 0.772 | +14.9% |

Tool Memory даёт самый большой прирост (+15%) — это самая нетривиальная идея.

### Для нашего проекта

| Тип памяти | Применимость | Примечание |
|-----------|-------------|-----------|
| Personal Memory | ✅ высокая | LLM-based extraction работает с русским |
| Task Memory | ✅ высокая | агент в терминале накапливает опыт API/инструментов |
| Tool Memory | ✅ высокая | tracking вызовов наших инструментов |
| Working Memory | ✅ высокая | у нас уже есть sandbox, но офлоад инструментов полезен |
| Pretrained bases | ⚠️ частично | appworld/bfcl паттерны применимы для API/terminal задач |

**Ключевое ограничение:** нет document store — оригиналы файлов не хранятся, только извлечённые факты. Для задачи "достань весь документ целиком" нужен Hindsight.

**Ключевое преимущество перед другими:** Task Memory и Tool Memory — принципиально новые идеи. Агент не просто помнит что говорили — он учится как лучше действовать.
