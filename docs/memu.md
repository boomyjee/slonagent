# Технический анализ проекта memU

> `lib/memU` — фреймворк долгосрочной проактивной памяти для AI-агентов.  
> Пакет: `memu-py`, центральный класс: `MemoryService` (алиас `MemUService`).

---

## Структура проекта

```
src/memu/
├── app/           — главный сервис + бизнес-логика (memorize, retrieve, crud, settings)
├── workflow/      — движок пайплайнов (pipeline, step, runner, interceptor)
├── database/      — хранилища данных
│   ├── models.py  — Pydantic-модели данных
│   ├── interfaces.py — Protocol Database
│   ├── inmemory/  — In-memory реализация + векторный поиск
│   ├── sqlite/    — SQLite реализация
│   └── postgres/  — PostgreSQL + pgvector
├── llm/           — клиенты для LLM (OpenAI SDK, httpx, LazyLLM)
├── embedding/     — клиенты для эмбеддингов
├── prompts/       — все промпты (по типам памяти, препроцессингу, поиску)
├── utils/         — вспомогательные утилиты
└── integrations/  — LangGraph интеграция
```

---

## Трёхуровневая иерархия памяти

Авторы используют аналогию с файловой системой:

| Файловая система | memU | Описание |
|---|---|---|
| Точки монтирования | **Resource** | Исходный документ/диалог/изображение |
| Файлы | **MemoryItem** | Извлечённый факт/событие/навык |
| Папки | **MemoryCategory** | Авто-тематические группы с LLM-суммари |

**MemoryCategory** — 10 предопределённых тем по умолчанию:
`personal_info`, `preferences`, `relationships`, `activities`, `goals`, `experiences`, `knowledge`, `opinions`, `habits`, `work_life`.

Категории прошиты в коде и инициализируются при старте через `get_or_create_category()`. Пользователь может передать свой список при конфигурировании сервиса.

---

## 6 типов памяти (MemoryType)

| Тип | Макс. длина | Что извлекается |
|---|---|---|
| `profile` | 30 слов | Стабильные факты: возраст, работа, предпочтения, привычки |
| `event` | 50 слов | Конкретные события с временем, местом, участниками |
| `knowledge` | 50 слов | Факты и концепции без привязки к пользователю |
| `behavior` | — | Повторяющиеся паттерны поведения и рутины |
| `skill` | >300 слов | Markdown-профиль навыка с примерами |
| `tool` | — | История вызовов инструментов с метриками успешности |

По умолчанию активны только `["profile", "event"]`.

---

## Хранилища

| Провайдер | Метаданные | Векторный индекс |
|---|---|---|
| `inmemory` | Python-dict | brute-force cosine (numpy) |
| `sqlite` | SQLite (SQLAlchemy) | brute-force cosine |
| `postgres` | PostgreSQL (SQLAlchemy async) | pgvector |

---

## 1. Нарезка данных (Chunking / Segmentation)

### Где и как происходит

Сегментация происходит на **шаге `preprocess_multimodal`** пайплайна `memorize`. Диспетчер `_preprocess_resource_url` разделяет логику по `modality`:

```
conversation → _preprocess_conversation()
document     → _preprocess_document()
video        → _preprocess_video()
audio        → _prepare_audio_text() + _preprocess_audio()
image        → _preprocess_image()
```

### Conversation segmentation (самый сложный случай)

**Шаг 1 — нормализация.** Функция `format_conversation_for_preprocess()` в `utils/conversation.py` принимает JSON-диалог двух форматов:
- список сообщений: `[{"role": "...", "content": "...", "created_at": "..."}]`
- словарь с ключом content: `{"content": [...]}`

Каждое сообщение преобразуется в одну строку вида:
```
[0] 2024-01-15 [user]: Привет, как дела
[1] 2024-01-15 [assistant]: Хорошо, спасибо
```
Переносы строк внутри контента коллапсируются в пробел — одно сообщение = одна строка.

**Шаг 2 — сегментация через LLM.** Готовый текст вставляется в промпт из `prompts/preprocess/conversation.py`:

```
Analyze a conversation with message indices and divide it into multiple meaningful segments based on topic changes, time gaps, or natural breaks.

Rules:
- Each segment must contain ≥ 20 messages
- Maintain a coherent theme
- Have a clear boundary from adjacent segments

Output Format:
{"segments": [{"start": x, "end": x}, ...]}

Conversation Content:
{conversation}
```

LLM возвращает JSON вида `{"segments": [{"start": 0, "end": 42}, {"start": 43, "end": 89}]}`.

**Шаг 3 — нарезка и caption.** Для каждого сегмента:
1. Извлекаются строки с индексами `[start..end]` функцией `_extract_segment_text()`
2. Отдельным LLM-вызовом генерируется caption: `"Summarize the given conversation segment in 1-2 concise sentences. Focus on the main topic or theme discussed."`
3. Каждый сегмент возвращается как `{"text": segment_text, "caption": caption}`

**Шаг 4 — каждый сегмент → отдельный Resource.** В `_memorize_extract_items()`:
```python
for idx, prep in enumerate(preprocessed_resources):
    res_url = self._segment_resource_url(base_url, idx, total_segments)
    # res_url = "dialog_#segment_0.json", "dialog_#segment_1.json", ...
```

### Document, Audio

Документы **не нарезаются** — LLM сжимает весь текст в condensed version + caption (XML-теги `<processed_content>` и `<caption>`). Audio сначала транскрибируется (через `client.transcribe()`), затем обрабатывается как документ.

### Video, Image

Видео: ffmpeg извлекает средний кадр → Vision API анализирует кадр → `<detailed_description>` + `<caption>`. Изображение аналогично.

---

## 2. Превращение сырых данных в факты — промпты и парсинг

### Архитектура extraction

На шаге `extract_items` для каждого preprocessed resource вызывается `_generate_structured_entries()`, а внутри него — `_generate_entries_from_text()`. Туда передаётся список `memory_types` (от 1 до 6 типов), и для **каждого типа** параллельно отправляется свой LLM-запрос:

```python
tasks = [client.chat(prompt_text) for prompt_text in valid_prompts]
responses = await asyncio.gather(*tasks)
```

### Промпт для profile (User Information)

Финальный PROMPT склеивается из 7 блоков в таком порядке:

```
# Task Objective
You are a professional User Memory Extractor. Your core task is to extract independent
user memory items about the user (e.g., basic info, preferences, habits, other long-term
stable traits).

# Workflow
Read the full conversation to understand topics and meanings.
## Extract memories
Select turns that contain valuable User Information and extract user info memory items.
## Review & validate
Merge semantically similar items.
Resolve contradictions by keeping the latest / most certain item.
## Final output
Output User Information.

# Rules
## General requirements (must satisfy all)
- Use "user" to refer to the user consistently.
- Each memory item must be complete and self-contained, written as a declarative descriptive sentence.
- Each memory item must be < 30 words worth of length
- A single memory item must NOT contain timestamps.
Important: Extract only facts directly stated or confirmed by the user. No guesses.
Important: Do not record temporary/one-off situational information; focus on meaningful, persistent information.

## Special rules for User Information
- Any event-related item is forbidden in User Information.

## Memory Categories:
{categories_str}

# Output Format (XML)
<item>
    <memory>
        <content>User memory item content 1</content>
        <categories>
            <category>Category Name</category>
        </categories>
    </memory>
</item>

# Original Resource:
<resource>
{resource}
</resource>
```

Аналогичные блочные промпты для других типов:
- **event** — события с временем/местом/участниками, `< 50 words`, без паттернов поведения
- **knowledge** — факты и концепции, `< 50 words`, без личного опыта
- **behavior** — повторяющиеся паттерны и рутины, без one-time events
- **skill** — развёрнутый Markdown-профиль навыка `> 300 words` с frontmatter (name, description, category, demonstrated-in) и разделами Core Principles, Implementation Guide, Success Patterns, Common Pitfalls
- **tool** — паттерны использования инструментов с when_to_use хинтом

### Парсинг ответа

Метод `_parse_memory_type_response_xml()` ищет корневые теги в порядке: `<item>`, `<profile>`, `<behaviors>`, `<events>`, `<knowledge>`, `<skills>`. Находит первое совпадение, вырезает XML-блок, парсит через `defusedxml.ElementTree`:

```python
root_tags = ["item", "profile", "behaviors", "events", "knowledge", "skills"]
for tag in root_tags:
    opening = f"<{tag}>"
    closing = f"</{tag}>"
    start_idx = raw.find(opening)
    end_idx = raw.rfind(closing)
    ...
root = ET.fromstring(xml_content)
for memory_elem in root.findall("memory"):
    content = memory_elem.find("content").text.strip()
    categories = [cat.text.strip() for cat in memory_elem.find("categories").findall("category")]
```

Результат — список `(memory_type, content_string, [category_names])`.

Есть и легаси-формат JSON с ключом `memories_items` (`_parse_memory_type_response()`), но основной путь — XML.

---

## 3. Структура данных факта (MemoryItem)

Pydantic-модель из `database/models.py`:

```python
class MemoryItem(BaseRecord):
    resource_id: str | None       # ссылка на Resource (источник)
    memory_type: str              # "profile"|"event"|"knowledge"|"behavior"|"skill"|"tool"
    summary: str                  # текст факта (одна декларативная фраза)
    embedding: list[float] | None # векторное представление summary
    happened_at: datetime | None  # для событий (event type)
    extra: dict[str, Any] = {}    # расширяемый словарь

# BaseRecord добавляет:
    id: str            # UUID4
    created_at: datetime
    updated_at: datetime
```

**Поле `extra`** — основное хранилище метаданных, используется по-разному:

| Ключ | Тип | Назначение |
|---|---|---|
| `content_hash` | str | SHA-256[:16] для дедупликации |
| `reinforcement_count` | int | сколько раз тот же факт встретился |
| `last_reinforced_at` | str (ISO) | дата последнего подтверждения |
| `ref_id` | str | короткий ID `(uuid[:6])` для ссылок из category summary |
| `when_to_use` | str | для tool-type памяти: хинт когда использовать |
| `metadata` | dict | для tool-type: статистика (avg_success_rate и пр.) |
| `tool_calls` | list[dict] | история вызовов инструмента |

**Связь MemoryItem ↔ категории** хранится через `CategoryItem` (join-таблица): `item_id` + `category_id`. Один item может принадлежать нескольким категориям.

**Resource** — источник, к которому привязан item:
```python
class Resource(BaseRecord):
    url: str           # оригинальный URL или "dialog_#segment_0.json"
    modality: str      # "conversation"|"document"|"video"|"image"|"audio"
    local_path: str    # локальный путь после скачивания
    caption: str | None     # краткое описание
    embedding: list[float] | None  # embedding caption'а
```

**MemoryCategory** — контейнер фактов:
```python
class MemoryCategory(BaseRecord):
    name: str
    description: str
    embedding: list[float] | None   # embedding "name: description"
    summary: str | None             # синтетический Markdown-саммари всех фактов
```

---

## 4. Как назначается категория

### Ответ: LLM, не векторный поиск

Категория назначается **LLM при извлечении факта**. В каждый extraction-промпт передаётся `{categories_str}` — список категорий с описаниями:

```
## Memory Categories:
- Personal Information: User's age, name, occupation, location and other basic facts
- Travel: Travel plans, trips taken, destinations visited
- Food Preferences: Dietary habits and food likes/dislikes
```

LLM самостоятельно решает, в какие категории отнести каждый факт, и возвращает их в `<categories><category>...</category></categories>`.

**Правила категоризации в промпте:**
- Можно относить один факт к нескольким категориям если они семантически подходят
- Нельзя создавать новые категории — только из предоставленного списка
- Если ни одна категория не подходит — оставить список пустым
- Дубли между категориями допустимы, но в каждой может быть разный акцент

### Маппинг имён в ID

После парсинга LLM-ответа `_map_category_names_to_ids()` делает lookup по `ctx.category_name_to_id` (dict, ключ — lowercase имя категории). Категории инициализируются при старте через `_initialize_categories()`, где каждая категория эмбеддируется и сохраняется в БД с `get_or_create_category()`.

### Category summary (шаг persist_index)

После записи всех items для каждой затронутой категории запускается `_update_category_summaries()`. Промпт из `prompts/category_summary/category.py` обновляет Markdown-саммари категории:

```
Topic: {category}

Original content:
<content>{original_content}</content>

New memory items:
<item>
- The user works as PM at internet company
- The user is 30 years old
</item>
```

LLM выполняет merge/update: добавляет новые факты, разрешает конфликты (более новый/конкретный побеждает), удаляет one-off события. Результат — Markdown-документ, который становится полем `MemoryCategory.summary`.

---

## 5. RAG Retrieval — векторный поиск на трёх уровнях

### Пайплайн `retrieve_rag` (7 шагов)

```
route_intention
    ↓
route_category  ──→ sufficiency_after_category ──(если нужно больше)──→
    ↓
recall_items ──→ sufficiency_after_items ──(если нужно больше)──→
    ↓
recall_resources
    ↓
build_context
```

### Уровень 1: Категории

В `_rag_route_category()` → `_rank_categories_by_summary()`:

1. Все категории у которых есть `summary` собираются в список
2. Тексты summary **динамически эмбеддируются** (не хранятся в БД)
3. `cosine_topk(query_vec, corpus, k=top_k)` — brute-force поиск по косинусному расстоянию

Это важный момент: **категории поиска через embedding их summary**, не через embedding имени/описания.

### Sufficiency check после категорий

В `_rag_category_sufficiency()` вызывается `_decide_if_retrieval_needed()` с промптом:

**SYSTEM_PROMPT:**
```
Determine whether the current query requires retrieving information from memory or can be
answered directly without retrieval. If retrieval is required, rewrite the query to include
relevant contextual information.

NO_RETRIEVE for:
  - Greetings, casual chat, acknowledgments
  - Questions about only the current conversation/context
  - General knowledge questions

RETRIEVE for:
  - Questions about past events, conversations
  - Queries about user preferences, habits
  - Requests to recall specific information

Output Format:
<decision>RETRIEVE or NO_RETRIEVE</decision>
<rewritten_query>...</rewritten_query>
```

**USER_PROMPT:**
```
Query Context:
{conversation_history}

Current Query:
{query}

Retrieved Content:
{retrieved_content}
```

Если `decision == "RETRIEVE"` → `proceed_to_items = True`, query перезаписывается `rewritten_query`, вектор запроса пересчитывается.

### Уровень 2: Items

В `_rag_recall_items()` вызывается `memory_item_repo.vector_search_items()`:

```python
def vector_search_items(query_vec, top_k, where=None, *, ranking="similarity", recency_decay_days=30.0):
    pool = self.list_items(where)
    if ranking == "salience":
        # salience = cosine_similarity × reinforcement_factor × recency_factor
        corpus = [(id, embedding, reinforcement_count, last_reinforced_at) for ...]
        return cosine_topk_salience(query_vec, corpus, k=top_k, recency_decay_days=...)
    # по умолчанию — чистое косинусное расстояние
    return cosine_topk(query_vec, [(i.id, i.embedding) for i in pool.values()], k=top_k)
```

Режим `salience` комбинирует: сходство × `reinforcement_count` × `exp(-decay * days_since_reinforcement)`.

После уровня items — снова sufficiency check с тем же промптом, но `retrieved_content` теперь содержит тексты найденных items.

### Уровень 3: Resources

В `_rag_recall_resources()` → `cosine_topk()` по `resource.embedding` (embedding caption'а ресурса). Ресурс — самый детальный уровень, содержащий полный текст сегмента.

---

## 6. LLM Retrieval Mode — отдельные промпты, без итеративного цикла

### Отличие от RAG

В LLM-режиме вместо векторного поиска LLM **сам ранжирует** объекты по текстовому описанию. Архитектура пайплайна идентична (те же 7 шагов), но handler'ы разные.

### Уровень 1 LLM: Category ranker

Промпт `llm_category_ranker.py`:
```
Task: Search through the provided categories and identify the most relevant ones for the
given query, then rank them by relevance.

Rules:
- Only include categories that are actually relevant to the query.
- Include at most {top_k} categories.
- Ranking matters: the first category must be the most relevant.
- Do not invent or modify category IDs.

Output: {"analysis": "...", "categories": ["cat_id_1", "cat_id_2"]}

Available Categories:
ID: abc123
Name: Travel
Description: ...
Summary: ...
---
```

LLM получает текстовый дамп категорий и возвращает список ID, отсортированных по релевантности.

### Уровень 2 LLM: Item ranker

Промпт `llm_item_ranker.py`:
```
Task: Search through the provided memory items and identify the most relevant ones for the
given query, based on the already identified relevant categories.

Input:
Query: {query}
Available Memory Items: (только items из найденных категорий)
ID: xxx
Type: profile
Summary: The user works as a PM
---
These memory items belong to the following relevant categories: {relevant_categories}

Output: {"analysis": "...", "items": ["item_id_1", "item_id_2"]}
```

В LLM-режиме есть **опциональная оптимизация через ref_ids**: если `use_category_references=True`, то вместо всех items загружаются только те, которые упоминаются в category summary через паттерн `[ref:xxxxxx]` (короткий ID = первые 6 символов UUID без дефисов).

### Уровень 3 LLM: Resource ranker

Промпт `llm_resource_ranker.py`:
```
Input:
Query: {query}
Context Info: (уже найденные категории и items)
Available Resources: (только ресурсы, связанные с найденными items)
ID: ...
URL: dialog.json
Modality: conversation
Caption: Discussion about travel plans
---

Output: {"analysis": "...", "resources": ["res_id_1"]}
```

### Итерация: цикла нет, но есть query rewriting

В LLM-режиме между уровнями также вызывается `_decide_if_retrieval_needed()` — тот же sufficiency check. Если `RETRIEVE` → query переписывается и переходим на следующий уровень. Это не итеративный цикл (нет возврата назад), а **линейная цепочка с ранним выходом**: если категорий достаточно → items и resources не запрашиваются.

### Итоговая схема обоих режимов

```
RAG:  embed(query) → cosine_topk(categories.summaries) → sufficiency → cosine_topk(items) → sufficiency → cosine_topk(resources.captions)
LLM:  LLM(query + categories_text) → sufficiency → LLM(query + items_text) → sufficiency → LLM(query + resources_text)
```

Оба режима — **иерархический поиск с ранним выходом**. Разница только в механизме ранжирования: числовое косинусное расстояние vs. LLM-понимание текста.

---

## Сводная таблица ключевых промптов

| Промпт | Файл | Input | Output |
|---|---|---|---|
| Сегментация диалога | `preprocess/conversation.py` | Индексированный диалог | JSON `{"segments": [{start, end}]}` |
| Сжатие документа | `preprocess/document.py` | Текст документа | XML `<processed_content>` + `<caption>` |
| Видео/изображение | `preprocess/video.py`, `image.py` | Медиафайл (Vision API) | XML `<detailed_description>` + `<caption>` |
| Извлечение profile | `memory_type/profile.py` | Диалог + категории | XML `<item><memory><content>...</content><categories>...</categories></memory></item>` |
| Извлечение event | `memory_type/event.py` | Диалог + категории | XML аналогично |
| Извлечение knowledge | `memory_type/knowledge.py` | Диалог + категории | XML аналогично |
| Обновление category summary | `category_summary/category.py` | Старый summary + новые items | Markdown |
| Sufficiency check | `retrieve/pre_retrieval_decision.py` | Query + history + retrieved | XML `<decision>` + `<rewritten_query>` |
| LLM category ranker | `retrieve/llm_category_ranker.py` | Query + категории | JSON `{"categories": [ids]}` |
| LLM item ranker | `retrieve/llm_item_ranker.py` | Query + items | JSON `{"items": [ids]}` |
| LLM resource ranker | `retrieve/llm_resource_ranker.py` | Query + resources | JSON `{"resources": [ids]}` |

---

## Полный путь данных — от сырого диалога до поиска

```
диалог.json
  → нормализация в индексированный текст
  → LLM чанкинг (границы сегментов)
  → для каждого сегмента: LLM × N типов памяти параллельно
  → SHA256 дедупликация
  → факты + связи в БД, embedding каждого факта
  → LLM обновляет Markdown-summary каждой затронутой категории
  → Resource (исходник) тоже в БД с embedding caption'а

поиск:
  embed(query) → cosine по category.summary → [достаточно?]
              → cosine по item.embedding → [достаточно?]
              → cosine по resource.caption
```

---

## Детали, которые не очевидны

### Что такое нормализация и индексированный текст

Сырой диалог — JSON: `[{"role": "user", "content": "...", "created_at": "..."}]`.

Нормализация преобразует каждое сообщение в строку с порядковым индексом:

```
[0] 2024-01-15 [user]: Привет
[1] 2024-01-15 [assistant]: Добрый день
[2] 2024-01-15 [user]: Я переехал в Берлин
```

Цель: LLM при чанкинге возвращает `{"start": 0, "end": 42}` — и система механически нарезает строки по этим номерам. Без индексов LLM не смог бы указать точные границы.

### Что значит «параллельно для N типов памяти»

Один и тот же сегмент диалога содержит разные вещи одновременно. Например:

```
[user]: Я переехал в Берлин на прошлой неделе, работаю в Яндексе,
        обожаю пасту, вчера был на концерте
```

Из этого можно извлечь:
- **profile**: «User works at Yandex», «User loves pasta»
- **event**: «User moved to Berlin last week», «User attended a concert yesterday»

Для каждого типа — отдельный промпт с отдельной инструкцией. Они отправляются одновременно через `asyncio.gather`. Промпт для `profile` запрещает события, промпт для `event` запрещает паттерны поведения — типы взаимно исключают чужой контент правилами.

### Различие промптов profile vs event

**profile** (`PROMPT_BLOCK_RULES`):
```
- Each memory item must be < 30 words worth of length
- A single memory item must NOT contain timestamps.
Special rules: Any event-related item is forbidden in User Information.
```

**event** (`PROMPT_BLOCK_RULES`):
```
- Each memory item must be < 50 words (keep it concise but include relevant details).
- Include relevant details such as time, location, and participants where available.
Special rules: Behavioral patterns, habits, preferences, or factual knowledge
               are forbidden in Event Information.
```

Тот же диалог → два разных ответа. `profile` вытащит «работает в Яндексе», `event` вытащит «планирует поездку на следующей неделе».

### Как обновляется summary категории

После каждой операции `memorize`, на шаге `persist_index`, для каждой затронутой категории вызывается LLM с промптом из `prompts/category_summary/category.py`. Входные данные:

```
Topic: Work Life

Original content:
<content>
## Basic Information
- The user is 28 years old
</content>

New memory items:
<item>
- The user is 30 years old
- The user works at Yandex
</item>
```

LLM делает merge:
- конфликт → берёт более новое («30 лет» побеждает «28 лет»)
- добавляет новое (Yandex)
- выбрасывает one-off события

Результат — Markdown-документ, который записывается обратно в `MemoryCategory.summary`. Именно этот текст потом эмбеддируется на лету при RAG-поиске.

### Что видит LLM при поиске — весь диалог, не только последнее сообщение

API `retrieve()` принимает весь диалог:

```python
async def retrieve(self, queries: list[dict[str, Any]], ...) -> dict[str, Any]:
    original_query = self._extract_query_text(queries[-1])   # последнее сообщение
    context_queries_objs = queries[:-1] if len(queries) > 1 else []  # всё остальное
```

В sufficiency check (и в LLM-ranker'ах) передаётся и то, и другое. LLM видит:

```
Query Context:
- [user]: Привет, как дела
- [assistant]: Хорошо!
- [user]: А помнишь, мы говорили про мою поездку?

Current Query:
Когда именно я планировал лететь?

Retrieved Content:
Category: Travel
Summary: User is planning a trip next weekend...
```

Благодаря этому query rewriting работает правильно — LLM уточняет запрос используя контекст из предыдущих сообщений.

### Sufficiency check — слабое место архитектуры

Sufficiency check на уровне категорий — это слепая ставка: LLM видит только summary категории и решает «достаточно ли?». Summary — это синтез, детали могут быть потеряны. Факт за summary может всё перевернуть.

Логика авторов: если summary категории уже прямо отвечает на запрос — нет смысла копать факты. Для разговорных запросов это работает. Для точных запросов («когда именно я был в Берлине?») sufficiency вернёт `RETRIEVE` и пойдёт глубже.

Это эвристика, не гарантия. Для уменьшения промахов — LLM-режим, где LLM видит полный текст категорий при принятии решения.

### Salience Score — оценка важности памяти

```python
salience = similarity * log(reinforcement_count + 1) * exp(-0.693 * days / half_life)
```

- **similarity**: косинусное сходство с запросом
- **reinforcement factor**: `log(n+1)` — логарифмическое масштабирование, предотвращает доминирование часто повторяемых фактов
- **recency factor**: экспоненциальное затухание с периодом полураспада `recency_decay_days` (по умолчанию 30 дней)

### Item References в суммари категорий

Если включено `enable_item_references`, в суммари категорий вставляются цитаты `[ref:xxxxxx]`, где `xxxxxx` — первые 6 символов UUID элемента. При LLM-поиске с `use_category_references=True` вместо всех items загружаются только цитируемые — без повторного векторного поиска.

### Векторный поиск с argpartition

Оптимизация top-k: вместо полной сортировки `O(n log n)` используется `np.argpartition` — `O(n)` для нахождения k наибольших, затем сортируются только они.


