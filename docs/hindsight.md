# Hindsight: разбор архитектуры памяти

> Репозиторий: https://github.com/vectorize-io/hindsight  
> Исходники анализировались из `lib/hindsight/`

---

## Запуск сервера (Podman / Docker)

Hindsight поставляется как Docker-образ. Podman полностью совместим — команды идентичны.

### Переменные окружения

| Переменная | Описание |
|---|---|
| `HINDSIGHT_API_LLM_PROVIDER` | Провайдер LLM: `gemini`, `openai`, `anthropic`, `groq`, `ollama`, `lmstudio` |
| `HINDSIGHT_API_LLM_API_KEY` | API-ключ выбранного провайдера |
| `HINDSIGHT_API_LLM_MODEL` | Модель (опционально, иначе берётся дефолтная для провайдера) |

### Запуск (встроенная PostgreSQL, всё в одном контейнере)

```powershell
podman run --rm -d `
  -p 8888:8888 -p 9999:9999 `
  -e HINDSIGHT_API_LLM_PROVIDER=gemini `
  -e HINDSIGHT_API_LLM_API_KEY=<GEMINI_API_KEY> `
  -v "$env:USERPROFILE\.hindsight:/home/hindsight/.pg0" `
  ghcr.io/vectorize-io/hindsight:latest
```

- API: http://localhost:8888  
- UI: http://localhost:9999

### Остановка

```powershell
podman stop $(podman ps -q --filter ancestor=ghcr.io/vectorize-io/hindsight:latest)
```

### Проверка работоспособности

```powershell
Invoke-RestMethod http://localhost:8888/health
```

---

---

## Быстрый ответ на три ключевых вопроса

### 1. Когда данные попадают в память?

Явный вызов `retain(text)` — не автоматический. Это плюс, не минус: ты сам контролируешь момент. Удобная стратегия — вызывать по лимиту токенов: пока история влезает в контекст — всё и так видно, как только начинает обрезаться — архивируем обрезаемый кусок.

### 2. Как обрабатывается?

```
текст (диалог, событие, заметка)
    ↓
LLM: extract_facts() → [{what, when, where, who, why, entities, causal_relations}, ...]
    ↓
embed каждый факт → вектор
    ↓
дедупликация (24-часовое окно + семантика)
    ↓
PostgreSQL: сохранить факты
    ↓
создать связи:
    temporal  — факты близкие по времени
    semantic  — семантически похожие факты
    causal    — причинно-следственные (LLM извлекает при сохранении)
    entity    — связать факты через общие сущности
```

### 3. Как извлекается?

4 параллельных стратегии → Reciprocal Rank Fusion:
- **Semantic** — косинусное сходство векторов
- **BM25** — полнотекстовый поиск по ключевым словам
- **Graph** — обход графа связей (MPFP/BFS/LinkExpansion)
- **Temporal** — поиск по временным диапазонам

Затем `reflect()` — агент с тулами который ищет иерархически: mental models → observations → raw facts.

---

## Как обрабатываются данные: подробно

### Шаг 1: Извлечение фактов (LLM)

**Файл:** `hindsight-api/hindsight_api/engine/retain/fact_extraction.py`

Промпт:
```
Extract SIGNIFICANT facts from text. Be SELECTIVE — only extract facts worth remembering long-term.

LANGUAGE: Detect the language and produce ALL output in that EXACT same language.

FACT FORMAT:
- what:  Core fact (1-2 sentences max)
- when:  Temporal info. "N/A" if none.
- where: Location if relevant. "N/A" if none.
- who:   People involved with relationships. "N/A" if general.
- why:   Context/significance ONLY if important. "N/A" if obvious.

CLASSIFICATION:
fact_kind: "event" (datable occurrence) or "conversation" (ongoing state)
fact_type: "world" (about user's life) or "assistant" (AI interactions)

TEMPORAL HANDLING:
- Convert ALL relative expressions to absolute dates ("last night" → ISO timestamp)
- For events: set occurred_start AND occurred_end
- For conversations: NO occurred dates

ENTITIES: people, organizations, places, key objects, abstract concepts

CAUSAL RELATIONS: link this fact to previous facts if one caused the other
  - target_index: index of the previous fact (MUST be < this fact's position)
  - relation_type: "caused_by"
  - strength: 0.0-1.0
```

**LLM возвращает массив:**
```json
[
  {
    "what": "Пользователь выиграл хакатон",
    "when": "2024-03-15",
    "where": "Москва",
    "who": "Пользователь, команда из 3 человек",
    "why": "Первый хакатон, важное достижение",
    "fact_kind": "event",
    "fact_type": "world",
    "occurred_start": "2024-03-15T00:00:00+00:00",
    "occurred_end": "2024-03-15T23:59:59+00:00",
    "entities": [{"text": "Москва"}],
    "causal_relations": null
  },
  {
    "what": "После победы пользователь решил продолжить программировать",
    "when": "N/A",
    "where": "N/A",
    "who": "N/A",
    "why": "Победа мотивировала",
    "fact_kind": "conversation",
    "fact_type": "world",
    "occurred_start": null,
    "causal_relations": [{"target_index": 0, "relation_type": "caused_by", "strength": 0.9}]
  }
]
```

Текст факта собирается из полей:
```python
def build_fact_text(self) -> str:
    parts = [self.what]
    if self.who and self.who.upper() != "N/A":
        parts.append(f"Involving: {self.who}")
    if self.why and self.why.upper() != "N/A":
        parts.append(self.why)
    return " | ".join(parts)
```

### Шаг 2: Эмбеддинги

Перед эмбеддингом текст **аугментируется датой**:
```python
# embedding_processing.py
augmented_texts = augment_texts_with_dates(extracted_facts, format_date_fn)
# → "2024-03-15: Пользователь выиграл хакатон | Involving: команда"
```

Это помогает семантическому поиску учитывать временной контекст.

### Шаг 3: Дедупликация

**Файл:** `hindsight-api/hindsight_api/engine/retain/deduplication.py`

```python
# Группировка по 12-часовым бакетам
bucket_key = fact_date.replace(hour=(fact_date.hour // 12) * 12, ...)

# Для каждого бакета: проверить семантическое сходство в 24-часовом окне
dup_flags = await duplicate_checker_fn(conn, bank_id, texts, embeddings, bucket_date, time_window_hours=24)
```

Дедупликация по двум критериям вместе: **время** (24 ч) + **семантика**. Один без другого не считается дублем — можно написать одно и то же в разные дни.

### Шаг 4: Связи

**Файл:** `hindsight-api/hindsight_api/engine/retain/link_creation.py`

Три типа создаются автоматически:

| Тип | Как создаётся |
|---|---|
| `temporal` | SQL: факты близкие по `occurred_start` / `mentioned_at` |
| `semantic` | Косинусное сходство между новыми эмбеддингами и существующими в базе |
| `causal` | Из `causal_relations` которые LLM извлекла при сохранении |

Четвёртый тип — `entity` — создаётся через таблицу `unit_entities`: все факты где упоминается одна и та же сущность ("Москва", "Иван") связываются через неё.

---

## Как хранится: схема базы

**PostgreSQL** обязателен — используется:
- `vector` тип (pgvector) — для хранения эмбеддингов
- `tsvector` / BM25 — для полнотекстового поиска
- JSONB — для метаданных
- Транзакции — для атомарного сохранения факта + связей + сущностей

Альтернативные векторные движки через env var: `pgvector` (дефолт), `vchord`, `pgvectorscale/DiskANN`.

**Основные таблицы:**

```sql
-- Факты
memory_units (
    id UUID,
    bank_id VARCHAR,
    text TEXT,              -- собранный текст: "what | who | why"
    embedding VECTOR,       -- для семантического поиска
    search_vector TSVECTOR, -- для BM25
    occurred_start TIMESTAMPTZ,
    occurred_end TIMESTAMPTZ,
    mentioned_at TIMESTAMPTZ,
    fact_type VARCHAR,      -- 'world', 'experience', 'opinion'
    document_id UUID,
    chunk_id UUID,
    tags VARCHAR[],
    metadata JSONB
)

-- Граф связей
memory_links (
    from_unit_id UUID,
    to_unit_id UUID,
    link_type VARCHAR,  -- 'temporal', 'semantic', 'causes', 'caused_by', 'entity'
    weight FLOAT
)

-- Сущности (люди, места, концепции)
entities (
    id UUID,
    bank_id VARCHAR,
    canonical_name VARCHAR,
    entity_type VARCHAR,
    summary TEXT           -- консолидированные наблюдения о сущности
)

-- Связь факты ↔ сущности
unit_entities (unit_id UUID, entity_id UUID)

-- Банки памяти (изолированные пространства)
banks (
    bank_id VARCHAR,
    disposition JSONB,  -- skepticism, literalism, empathy
    mission TEXT
)

-- Высокоуровневые выводы (mental models / observations)
mental_models / observations (отдельные таблицы)
```

---

## Как извлекается: поиск

**Файл:** `hindsight-api/hindsight_api/engine/search/retrieval.py`

### 4 параллельных стратегии

```python
async def retrieve_all_fact_types_parallel(...):
    # 1+2. Semantic + BM25 в одном SQL-запросе (один round-trip)
    semantic_bm25 = await retrieve_semantic_bm25_combined(
        conn, query_emb_str, query_text, bank_id, fact_types, limit
    )

    # 3. Graph: обход связей от найденных узлов
    graph_results = await graph_retriever.retrieve(...)
    # Варианты: MPFP (по умолчанию), BFS, LinkExpansion

    # 4. Temporal: если в запросе есть временное ограничение
    if temporal_constraint:
        temporal_results = await retrieve_temporal_combined(...)
```

**Semantic + BM25 Combined SQL:**
```sql
WITH semantic_ranked AS (
    SELECT id, text, 1 - (embedding <=> $1::vector) AS similarity,
           ROW_NUMBER() OVER (PARTITION BY fact_type ORDER BY embedding <=> $1) AS rn
    FROM memory_units
    WHERE bank_id = $2 AND fact_type = ANY($3)
      AND (1 - (embedding <=> $1::vector)) >= 0.3
),
bm25_ranked AS (
    SELECT id, text, ts_rank(search_vector, query) AS bm25_score, ...
)
```

### RRF Fusion

**Файл:** `hindsight-api/hindsight_api/engine/search/fusion.py`

```python
def reciprocal_rank_fusion(result_lists, k=60):
    # score(d) = sum(1 / (k + rank(d))) по всем спискам
    for source_idx, results in enumerate(result_lists):
        for rank, retrieval in enumerate(results, start=1):
            rrf_scores[doc_id] += 1.0 / (k + rank)
    return sorted by rrf_score desc
```

Результат: единый ранжированный список где факт получает высокий балл если он высоко во **многих** стратегиях одновременно.

---

## Reflect: иерархический агент

**Файл:** `hindsight-api/hindsight_api/engine/reflect/agent.py`  
**Промпты:** `hindsight-api/hindsight_api/engine/reflect/prompts.py`

`reflect(query)` — не просто поиск, а агент с тулами который может делать несколько запросов.

**Иерархия поиска:**
```
1. search_mental_models()  ← высокоуровневые обобщения о пользователе
2. search_observations()   ← консолидированные наблюдения о сущностях
3. recall()                ← сырые факты (ground truth)
4. expand()                ← расширить контекст от найденного факта
```

**Системный промпт агента:**
```
CRITICAL: You MUST ONLY use information from retrieved tool results.
NEVER make up names, people, events, or entities.

HIERARCHICAL RETRIEVAL STRATEGY:
1. MENTAL MODELS — try first (highest quality user-curated summaries)
2. OBSERVATIONS — second priority (consolidated knowledge)
3. RAW FACTS — ground truth fallback

CRITICAL RULES:
- ONLY use information from tool results — no external knowledge
- You SHOULD synthesize, infer, and reason from retrieved memories
- You MUST search before saying you don't have information

How to Reason:
- If memories mention someone did an activity, infer they likely enjoyed it
- Synthesize a coherent narrative from related memories
- When exact answer isn't stated, use what IS stated to give the best answer
```

---

## Зачем PostgreSQL?

Не SQLite, потому что нужно:
1. **`vector` тип** — pgvector, хранение и `<=>` оператор для cosine similarity
2. **`tsvector` + BM25** — нативный полнотекстовый поиск
3. **Граф-запросы** — CTE и рекурсивные запросы для обхода `memory_links`
4. **Параллельные соединения** — 4 стратегии поиска параллельно через connection pool

Альтернативные векторные индексы через env var: pgvector (HNSW), vchord, DiskANN (Azure). Это не просто хранилище — это вычислительный движок.

**Можно ли без Postgres?** Принципиально — нет. Вся архитектура завязана на pgvector и pg fulltext. Переписать на LanceDB + SQLite было бы отдельным проектом.

---

## Оценка

**Что хорошо:**
- Структурированные факты (what/when/where/who/why) — факты атомарные и читаемые
- Каузальные связи — уникально, ни у кого больше нет из коробки
- 4 стратегии поиска + RRF — robust, одна стратегия не провалит весь поиск
- `retain()` вызывается явно — полный контроль когда архивировать
- Иерархия mental models → observations → facts — хорошо структурированная абстракция
- Multi-language: LLM сохраняет на языке входного текста

**Что сложно:**
- PostgreSQL обязателен — тяжелее развернуть чем SQLite/LanceDB
- API-first архитектура — встроенный режим (`hindsight-embed`) есть, но это обёртка над сервером
- Нет автоматической интеграции в агентный цикл — надо самому вызывать `retain()` и `reflect()`

**Сравнение с нашим LettaProvider:**

| | Hindsight | LettaProvider |
|---|---|---|
| Core memory (всегда в контексте) | Нет | Есть (блоки) |
| Archival memory (семантический поиск) | Есть (4 стратегии) | Есть (LanceDB) |
| Recall memory (история диалогов) | Есть (факты из диалогов) | Есть (JSONL) |
| Каузальные связи | Есть | Нет |
| Граф связей | Есть (3 типа) | Нет |
| Иерархия абстракций | Есть (3 уровня) | Нет |
| БД | PostgreSQL | LanceDB + файлы |
| Сложность развёртывания | Высокая | Низкая |
