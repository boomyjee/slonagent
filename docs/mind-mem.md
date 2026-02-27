# mind-mem — Анализ системы памяти

**Источник:** `lib/mind-mem` (github.com/star-ga/mind-mem)  
**Версия:** 1.7.1  
**Дата анализа:** 2026-02-27

---

## Концепция

mind-mem — **"Memory OS"** для AI-агентов: структурированная, управляемая, самопроверяющаяся база знаний с фокусом на governance (управление изменениями, аудит, детекция противоречий). Ключевой тезис: *не просто хранить и искать, а обнаруживать, когда память стала неправильной*.

**По умолчанию ориентирован на структурированные знания** (решения, задачи, сущности), но архитектурно поддерживает conversational memory через дневные логи (`memory/YYYY-MM-DD.md`) с теми же механизмами хранения и recall. Для диалогов на русском языке regex-based capture/extraction не работает (только английские паттерны), но LLM-слой (`llm_extractor.py`) может заменить regex-экстракцию при наличии локальной модели.

---

## Зависимости (реальные)

Заявлено "zero core dependencies" — и это правда для BM25 режима. Но:

```
[project.optional-dependencies]
mcp        = ["fastmcp==2.14.5"]           # MCP сервер
embeddings = ["onnxruntime==1.24.2",       # векторный backend (ONNX)
              "tokenizers==0.22.2"]
cross-encoder = ["sentence-transformers==5.2.3"]  # переранжирование
```

**Где физически хранятся векторы:**

- **Локальный режим (default):** SQLite база `.mind-mem-index/recall.db` — таблица `embedding_cache` (BLOB-поле `embedding`, float32 packed через `struct.pack`). Модель по умолчанию — `all-MiniLM-L6-v2` (384 измерения), загружается через `sentence-transformers` или ONNX.
- **Qdrant:** `qdrant-client` + `http://localhost:6333`, коллекция `mind-mem`
- **Pinecone:** `pinecone-client` + cloud API

**Где хранится BM25 индекс:**

`.mind-mem-index/recall.db` — SQLite FTS5 virtual table. Поля: `statement`, `title`, `name`, `description`, `tags`, `context`, `all_text`. Инкрементальная пересборка через `file_state` (mtime + hash).

**Где хранятся основные данные:**

Markdown-файлы (source of truth — человекочитаемые, редактируются напрямую):
```
decisions/DECISIONS.md          # решения [D-YYYYMMDD-###]
tasks/TASKS.md                  # задачи [T-YYYYMMDD-###]
entities/projects.md            # [PRJ-###]
entities/people.md              # [PER-###]
entities/tools.md               # [TOOL-###]
entities/incidents.md           # [INC-###]
memory/YYYY-MM-DD.md            # дневные логи (append-only)
intelligence/SIGNALS.md         # автозахваченные сигналы
intelligence/CONTRADICTIONS.md  # обнаруженные противоречия
intelligence/DRIFT.md           # дрейф
.mind-mem-index/recall.db       # SQLite: FTS5 + эмбеддинги + метаданные
.mind-mem/block_meta.db         # A-MEM метаданные блоков
.mind-mem-wal/                  # Write-Ahead Log (crash recovery)
```

**Где хранятся извлечённые факты:**

Факты **не пишутся в `.md` файлы**. Они существуют только в SQLite как производные записи, создаваемые при индексировании (`sqlite_index.py build`). ID факта строится из ID родительского блока: `D-20240315-001::F1`, `D-20240315-001::F2` и т.д.

```
.mind-mem-index/recall.db
├── таблица blocks
│   ├── "D-20240315-001"        ← оригинальный блок (отражение из .md)
│   ├── "D-20240315-001::F1"    ← факт №1, извлечённый из блока (только в SQLite)
│   └── "D-20240315-001::F2"    ← факт №2
├── таблица blocks_fts (FTS5)
│   ├── "D-20240315-001"        ← блок в полнотекстовом поиске
│   └── "D-20240315-001::F1"    ← факт в поиске (Statement = extracted content)
└── таблица block_vectors       ← эмбеддинги блоков и фактов (если включены)
```

При recall работает **small-to-big retrieval**: поиск идёт по маленьким атомарным фактам (точнее матчат запрос), а в контекст возвращается большой родительский блок (содержит полный контекст).

```
Запрос: "PostgreSQL"

BM25 находит:  D-20240315-001::F1  score=4.2  ("Alice is using PostgreSQL")
               D-20240315-001::F2  score=1.1  ("Alice prefers PostgreSQL over MySQL")

_aggregate_facts_to_parents():
  → факты сворачиваются в родителя D-20240315-001
  → score родителя = max(4.2, 1.1) * 0.8 + parent_score * 0.2
  → возвращается D-20240315-001 с полным Statement, Rationale, Tags
```

**Почему нетривиально:** если бы индекс хранил только целые блоки, запрос "PostgreSQL" матчил бы блок целиком — и длинный многостраничный блок получал бы низкий BM25 score (длина нормализует tf). Атомарные факты короткие → высокий tf → лучший score при точном совпадении. После матча фактов агрегация возвращает полный контекст родителя. Это стандартная техника из RAG-литературы, здесь реализованная аккуратно.

---

## Как данные превращаются в факты

### 1. Структура блока (source of truth)

Данные хранятся не как сырой текст, а как **типизированные блоки** в Markdown:

```markdown
[D-20260213-001]
Date: 2026-02-13
Status: active
Statement: Use PostgreSQL for the user database
Rationale: Better JSON support than MySQL
Tags: database, infrastructure
ConstraintSignatures:
- id: CS-db-engine
  domain: infrastructure
  subject: database
  predicate: engine
  object: postgresql
  modality: must
  priority: 9
  scope: {projects: [PRJ-myapp]}
  evidence: Benchmarked JSON performance
  axis:
    key: database.engine
```

Парсер (`block_parser.py`) — zero-dependency Markdown-парсер, разбирает `[ID]`-заголовки и `Key: Value`-поля в структурированные dict.

### 2. Пути попадания данных в систему

**Путь A — вручную:** Пользователь/агент пишет блок прямо в Markdown-файл.

**Путь B — через `propose_update` (MCP tool):** Агент предлагает → сигнал попадает в `SIGNALS.md` → только `/apply` продвигает в `DECISIONS.md`. Никогда не пишет напрямую в source of truth.

**Путь C — `capture.py` (auto-capture):** Сканирует дневной лог (`memory/YYYY-MM-DD.md`) по 26 regex-паттернам, определяет решения и задачи:

```python
# High confidence decision patterns
(r"\bwe(?:'ll| will| decided| agreed| chose| went with)\b", "decision", "high"),
(r"\bdecided to\b", "decision", "high"),
(r"\bfrom now on\b", "decision", "high"),
(r"\bno longer\b", "decision", "high"),
# High confidence task patterns
(r"\baction item\b", "task", "high"),
(r"\bdeadline\b", "task", "high"),
(r"\bblocked on\b", "task", "high"),
# Low confidence task patterns
(r"\bwould be nice\b", "task", "low"),
(r"\bsomeday\b", "task", "low"),
```

**Пайплайн capture.py:**
1. Читает `memory/YYYY-MM-DD.md`
2. Каждая строка проверяется по 26 паттернам → тип (`decision`/`task`) + confidence (`high/medium/low`)
3. `extract_structure()` — эвристически вытаскивает `subject`, `predicate`, `object`, `tags` (regex, не NLP)
4. SHA256 хэш нормализованного текста для дедупликации
5. Пишет в `SIGNALS.md` с полными метаданными

**Путь D — `transcript_capture.py`:** Сканирует JSONL-транскрипты (Claude Code, OpenClaw) на entity mentions и конвенции.

### 3. Что НЕ является автоматическим

Попадание в `DECISIONS.md` или `TASKS.md` **никогда не происходит автоматически**. Все сигналы из `capture.py` требуют ручного `/apply`. Apply engine делает: snapshot → mutation → post-validate → commit/rollback. Каждая операция логируется в `AUDIT.md` с diff.

### 4. Индексирование → atomization фактов (extractor.py)

Это не "путь" добавления данных, а **шаг самого индексирования** (`sqlite_index.py build`). При каждом индексировании блока с непустым полем `Statement` (> 15 символов) вызывается `extractor.py → extract_facts()`:

```python
# sqlite_index.py, строки 473-523
facts = extract_facts(statement, speaker=speaker, date=block_date, source_id=bid)
for i, card in enumerate(facts):
    fact_id = f"{bid}::F{i + 1}"        # например "D-20240315-001::F1"
    # → INSERT INTO blocks (id=fact_id, parent_id=bid, ...)
    # → INSERT INTO blocks_fts (block_id=fact_id, statement=card["content"], ...)
```

`extractor.py` использует regex-паттерны для извлечения атомарных утверждений из `Statement`. Типы фактов: `FACT`, `EVENT`, `PREFERENCE`, `DECISION`, `EMOTION`, `GOAL`, `PLAN`. Факты записываются **только в SQLite**, Markdown-файлы не трогаются.

Для диалоговых данных (`memory/YYYY-MM-DD.md`) это работает: если записать реплику как Statement в блок дневного лога, `extractor.py` вытащит из неё факты при следующем `build`. Ограничение — regex паттерны только для английского.

### 5. Путь E — llm_extractor.py (опциональный LLM-слой)

Отдельный модуль с LLM-извлечением фактов и entities. Активируется если:
1. В `mind-mem.json` прописано `"extraction": {"enabled": true}`
2. Доступен один из backend-ов: ollama (`localhost:11434`) или `llama-cpp-python`

**Когда работает:** при индексировании блоков (обогащает их дополнительными извлечёнными фактами) и при recall, если включён LLM reranking (`llm_rerank: true`).

**Что извлекает:**
- **entities** — persons, places, dates, organizations, decisions, tools, projects
- **facts** — фактические утверждения с confidence и category (identity, event, preference, relation, negation, plan, state)

Модель по умолчанию: `phi3:mini` через ollama. **Может быть любой локальной моделью** — включая русскоязычные (`llama3`, `mistral`, `gemma` и т.д.). Это снимает ограничение regex-capture на английский: для main flow диалогов можно настроить LLM-based extraction вместо regex-based capture.

---

## Пайплайн Recall (поиск)

`recall.py` — фасад над 8 субмодулями. Основной путь — BM25F, опциональный — гибридный BM25+Vector+RRF.

### Шаг 1: Tokenize

```python
# _recall_tokenization.py
# Porter stemming + irregular lemma lookup + stopword filter
tokenize("authentication decisions") 
# → ["authent", "decis"]  (стеммированные)
```

### Шаг 2: Intent Classification

```python
# intent_router.py (опционально, fallback → detect_query_type)
# 9 типов: WHY, WHEN, ENTITY, WHAT, HOW, LIST, VERIFY, COMPARE, TRACE
intent_result = router.classify("why did we choose PostgreSQL?")
# → intent=WHY, params={limit: 5, graph_boost: True, temporal_filter: False}
```

Каждый intent-тип имеет свои параметры поиска (лимит, включение graph boost, temporal filter).

### Шаг 3: Query Expansion (RM3)

```python
# _recall_expansion.py
# RM3 pseudo-relevance feedback — JM-smoothed language model
# Берёт топ-N начальных результатов, вытаскивает топ-M терминов,
# добавляет к запросу с весом RM3_BLEND_WEIGHT=0.4
expanded_query = rm3_expand(workspace, query_tokens, initial_results)
```

Статические synonyms как fallback (`_QUERY_EXPANSIONS` словарь: `"auth" → ["authentication", "login", "oauth"]` и т.д.).

### Шаг 4: Multi-hop decomposition

Для multi-hop запросов ("what decisions affected the auth module after the migration?") — разбивает на sub-запросы, запускает recall для каждого, мёржит по max score.

### Шаг 5: BM25F Scoring

Обычный BM25 не различает поля документа — совпадение в заголовке и в хвосте примечаний даёт одинаковый вес. BM25F решает это field weights: каждое поле умножается на свой коэффициент перед суммированием.

```python
FIELD_WEIGHTS = {
    "Statement":   3.0,   # основное утверждение — максимальный вес
    "Title":       2.5,
    "Name":        2.0,
    "Summary":     1.5,
    "Description": 1.2,
    "Tags":        0.8,   # теги важны, но не как само содержание
    "Context":     0.5,   # контекст — вспомогательный сигнал
    "Rationale":   0.5,
    "History":     0.3,   # история изменений почти не влияет
}
```

Почему это правильно: запрос "use PostgreSQL" должен лучше матчить блок где это написано в `Statement` ("We decided to use PostgreSQL"), чем блок где это упомянуто в `Tags: postgresql`. Без field weights оба блока получат одинаковый BM25 score при совпадении одного слова.

Плюс:
- **Bigram phrase matching:** "+25% за каждый matching bigram" — "authentication decisions" как фраза весит больше, чем два отдельных слова
- **Sentence chunking:** overlapping 3-sentence windows с 1-sentence overlap — блок может скорить по лучшему chunk

### Шаг 6: Graph Boost (опционально)

```python
# build_xref_graph() — строит граф перекрёстных ссылок из [D-ID] в текстах
# 1-hop neighbor: +40% от score referencing block
# 2-hop neighbor: +40% от 40% = +16%
# Ограничение: MAX_GRAPH_NEIGHBORS_PER_HOP=50
```

### Шаг 7: Deterministic Reranking (4 сигнала)

```python
# _recall_reranking.py
# Сигнал 1: negation_penalty — если блок противоречит запросу (negation detection)
# Сигнал 2: date_proximity — Gaussian decay по дате
# Сигнал 3: category_boost — 20-категориальная таксономия
# Сигнал 4: status/priority boost — active=1.2x, P0/P1=1.1x
```

### Шаг 8: A-MEM Importance Boost (опционально)

```python
# block_metadata.py
# Каждый блок имеет: access_count, importance_score [0.8, 1.5]
# Exponential decay by recency
# Frequently retrieved blocks получают importance boost при следующих запросах
```

### Шаг 9: Hard Negative Mining

```python
# retrieval_graph.py
# Логирует "hard negatives" — блоки с высоким BM25 но низким cross-encoder score
# (т.е. ключевые слова есть, но семантически не то)
# В будущих запросах HARD_NEGATIVE_PENALTY=0.7 применяется к ним
```

### Шаг 10: Adaptive Knee Cutoff

Fixed top-K — это произвол: при `limit=10` в контекст попадают и блок со score 4.2, и блок со score 0.3, хотя последний — шум. Knee cutoff ищет точку где score резко падает и обрезает там.

```python
def knee_cutoff(results, *, min_results=1, min_score=0.0, max_drop_ratio=0.5):
    scores = [r["score"] for r in results]
    best_cut = len(results)
    max_drop = 0.0
    for i in range(len(scores) - 1):
        drop = scores[i] - scores[i + 1]
        relative_drop = drop / scores[0]          # относительно лучшего
        if relative_drop > max_drop_ratio and drop > max_drop:
            max_drop = drop
            best_cut = i + 1                      # режем здесь
    return results[:max(min_results, best_cut)]
```

Пример: scores = [4.2, 3.8, 3.5, 0.9, 0.7, 0.3]  
Падение между 3 и 4 позицией: `(3.5 - 0.9) / 4.2 = 62%` → > 50% порога → обрезаем до 3 результатов.

Результат: вместо "всегда top-10" — 3-15 блоков адаптивно, зависит от реального распределения scores. LLM-судья получает меньше шума в контексте.

### Шаг 11 (гибридный режим): Vector Search + RRF Fusion

```python
# hybrid_recall.py
# BM25 results (rank 1..N) + Vector results (rank 1..N)
# RRF score = 1/(k + rank_bm25) + 1/(k + rank_vector), k=60
# bm25_weight=1.0, vector_weight=1.0 (configurable)
```

Векторный backend при включении:
- Модель по умолчанию: `all-MiniLM-L6-v2` (384d) через sentence-transformers или ONNX
- Альтернатива: `llama.cpp` embedding server (`http://localhost:8090`) — без Python-зависимостей
- Эмбеддинги кэшируются в SQLite `embedding_cache` по `(block_id, model_name)` + `content_hash` для инвалидации

---

## Опциональные шаги recall — кто контролирует

Три компонента помечены как опциональные через `try/except ImportError` в `_recall_core.py`:

```python
# A-MEM block metadata (optional)
try:
    from .block_metadata import BlockMetadataManager
    _HAS_BLOCK_META = True
except ImportError:
    _HAS_BLOCK_META = False

# Intent Router (optional — falls back to detect_query_type)
try:
    from .intent_router import get_router as _get_intent_router
    _HAS_INTENT_ROUTER = True
except ImportError:
    _HAS_INTENT_ROUTER = False

# LLM Extractor (optional — config-gated)
try:
    from .llm_extractor import enrich_results as _llm_enrich_results
    _HAS_LLM_EXTRACTOR = True
except ImportError:
    _HAS_LLM_EXTRACTOR = False
```

**Механизм:** просто проверяется наличие модуля при импорте. Никакого конфиг-флага не нужно — если модуль есть, он используется, если нет — graceful fallback. При старте логируется что отключено:

```
optional_subsystem_unavailable subsystem=block_metadata impact="A-MEM importance boost disabled"
optional_subsystem_unavailable subsystem=intent_router impact="falling back to detect_query_type()"
```

**Фактически:**
- `block_metadata.py` — часть самого пакета, всегда есть → A-MEM включён всегда
- `intent_router.py` — тоже часть пакета → intent classification всегда работает  
- `llm_extractor.py` — есть в пакете, но активируется только если в `mind-mem.json` прописан `llm_rerank: true` + URL модели

**Контроль через конфиг** (те шаги, которые реально опциональны через `mind-mem.json`):

| Параметр | Что включает |
|---|---|
| `recall.backend: "hybrid"` | Vector search + RRF fusion |
| `recall.vector_enabled: true` | Эмбеддинги (нужен `sentence-transformers` или ONNX) |
| `recall.onnx_backend: true` | ONNX вместо `sentence-transformers` |
| `llm_rerank: true` + `llm_rerank_url` | LLM-assisted reranking через внешний сервер |
| `graph_boost: true` в запросе | Cross-reference graph boosting |

---

## Как данные попадают в файлы — полный пайплайн

### Физические файлы: что куда

```
memory/YYYY-MM-DD.md        ← сырой дневной лог (пишется вручную или агентом)
intelligence/SIGNALS.md     ← захваченные сигналы (capture.py, propose_update)
intelligence/proposed/      ← staged proposals (intel_scan.py)
decisions/DECISIONS.md      ← source of truth решений (только через apply_engine.py)
tasks/TASKS.md              ← source of truth задач (только через apply_engine.py)
entities/*.md               ← проекты, люди, инструменты, инциденты
.mind-mem-index/recall.db   ← SQLite индекс (строится из всех файлов выше)
```

### Путь 1: Прямая запись (вручную/агент)

Агент или пользователь пишет блок прямо в `DECISIONS.md`/`TASKS.md`/`entities/`. Это единственный путь где source of truth меняется без `/apply`. Допустимо если governance mode не enforce.

### Путь 2: capture.py → SIGNALS.md

```
memory/2026-02-27.md   →   capture.py   →   SIGNALS.md
(дневной лог)               (26 regex)        (сигналы)
```

`capture.py` сканирует дневной лог построчно. На каждую строку применяет 26 regex-паттернов, определяет тип (decision/task) и confidence. Затем `extract_structure()` эвристически вытаскивает subject/predicate/object (regex, не NLP, только английский).

Результат в `SIGNALS.md`:
```markdown
[SIG-20260227-001]
Date: 2026-02-27
Type: decision
Status: pending
Excerpt: We decided to use PostgreSQL
Priority: P1
Pattern: decided_to
Confidence: high
Structure: {"subject": "we", "tags": ["database"]}
Hash: a3f2b1c9d4e5f6a7
```

**Важно:** `capture.py` пишет ТОЛЬКО в `SIGNALS.md`. Никогда в `DECISIONS.md`.

### Путь 3: intel_scan.py → proposed/ → apply_engine.py → DECISIONS.md

```
SIGNALS.md + DECISIONS.md  →  intel_scan.py  →  proposed/DECISIONS_PROPOSED.md
                                                          ↓
                                              /apply → apply_engine.py
                                                          ↓
                                              snapshot → mutate → validate → commit
                                                          ↓
                                              decisions/DECISIONS.md (source of truth)
```

`apply_engine.py` поддерживает 7 операций над файлами:
```python
VALID_OPS = {
    "append_block",        # добавить новый блок в конец файла
    "insert_after_block",  # вставить после конкретного блока
    "update_field",        # обновить конкретное поле блока
    "append_list_item",    # добавить элемент в список (ConstraintSignatures и т.п.)
    "replace_range",       # заменить диапазон строк
    "set_status",          # изменить Status поля
    "supersede_decision",  # пометить старое решение superseded + записать новое
}
```

### Путь 4: extractor.py → SQLite fact cards (при индексировании)

Это самая скрытая часть. При запуске `sqlite_index.py build` каждый блок из всех файлов проходит через `extractor.extract_facts()`:

```python
cards = extract_facts(
    text="[Caroline] I went to a LGBTQ support group yesterday",
    speaker="Caroline",
    date="2023-05-07",
    source_id="DIA-D1-3",
)
# → [{"type": "EVENT", "content": "Caroline went to a LGBTQ support group",
#      "date": "2023-05-07", "source_id": "DIA-D1-3", "confidence": 0.8}]
```

**5 типов fact card:**

| Тип | Паттерн | Пример |
|---|---|---|
| `FACT` | `I am/I'm a X`, `my X is Y` | "Caroline is a nurse" |
| `EVENT` | `I went/visited/attended/...` (70+ глаголов) | "Caroline went to LGBTQ support group" |
| `PREFERENCE` | `I love/like/enjoy X`, `favorite X is Y` | "Caroline likes hiking" |
| `RELATION` | `X is my Y`, `I met/know/work with X` | "Tim is Caroline's brother" |
| `NEGATION` | `I never/didn't/don't X` | "Caroline never drinks alcohol" |
| `PLAN` | `I want to/plan to/will X` | "Caroline plans to adopt" |

Fact cards хранятся в SQLite с полем `parent_id` → ссылка на родительский блок. При recall происходит **small-to-big retrieval**: находим факт-карту → поднимаем до родительского блока → блендируем score:

```python
# CHUNK_BLEND_BEST=0.6, CHUNK_BLEND_FULL=0.4
final_score = 0.6 * best_chunk_score + 0.4 * full_block_score
```

**Важно:** `extractor.py` тоже только английский — те же проблемы что и `capture.py` для русского языка.

### SQLite: полная схема индекса

`.mind-mem-index/recall.db`:

```sql
-- Основные блоки (все данные из markdown-файлов)
blocks(id PK, type, file, line, status, date, speaker, tags, dia_id, parent_id, json_blob)

-- FTS5 full-text search (BM25 через SQLite porter tokenizer)
blocks_fts(block_id, statement, title, name, description, tags, context, all_text)
-- tokenize='porter unicode61' — встроенный Porter stemming в SQLite

-- Граф перекрёстных ссылок (для graph boost)
xref_edges(src, dst)

-- Отслеживание изменений файлов для инкрементальной пересборки
file_state(path PK, mtime, size, hash)

-- Векторные эмбеддинги (опционально, когда включён vector backend)
block_vectors(id PK, embedding BLOB, model TEXT)
-- embedding = struct.pack('384f', *floats) — float32 binary

-- A-MEM метаданные
block_meta(id PK, importance REAL, access_count INT, last_accessed, keywords, connections)
```

Инкрементальная пересборка: `file_state` хранит mtime + SHA256 первых 64KB файла. При изменении файла переиндексируются только изменившиеся файлы.

---

## Governance Pipeline

### Contradiction Detection

```python
# intel_scan.py
# Для каждой пары активных решений с ConstraintSignatures проверяет:
# 1. sig_a.axis.key == sig_b.axis.key
# 2. Оба Status == "active"
# 3. Scopes пересекаются
# 4. Ни одна не указывает другую в composes_with
# 5. modality conflict: must vs must_not → CRITICAL
#    same predicate, different objects, both must → CRITICAL
#    both should, different objects → WARNING
```

### Proposal Apply (ACID-like)

1. **Pre-check:** валидация формата, проверка что target block существует, budget limits
2. **Snapshot:** сохранить все затронутые файлы в `intelligence/applied/<timestamp>/`
3. **Execute:** мутация файла
4. **Post-check:** `validate.sh` (74 проверки) + scan на новые противоречия
5. **Commit/Rollback:** если post-check упал — restore from snapshot

---

## Как это использовать как провайдер памяти (наш случай)

**Запись (конец сессии/сообщения):**
```python
# 1. Дописать диалог в daily log
with open(f"{workspace}/memory/{today}.md", "a") as f:
    f.write(f"\n## {timestamp}\n**User:** {user_text}\n**Agent:** {response}\n")

# 2. Запустить capture.py (async, non-blocking)
subprocess.Popen(["python3", "scripts/capture.py", workspace])
```

**Чтение (начало обработки сообщения):**
```python
from mind_mem.recall import recall

results = recall(workspace, query=user_text, limit=10)
context = "\n\n".join(r["excerpt"] for r in results)
```

**Или через MCP:**
```python
# recall tool → JSON array с ranked blocks
# prefetch tool → pre-assemble context по сигналам из разговора
```

---

## Архитектурная суть: RAG + три оси расширения контекста

mind-mem — это RAG. Retrieval → контекст → LLM. Но "просто RAG" недооценка: retrieval расширяется тремя независимыми осями одновременно.

**Ось 1: вертикальная (small-to-big через parent_id)**

Поиск идёт по коротким атомарным фактам — у них высокий BM25 score из-за длины. После матча факт сворачивается в родительский блок с полным контекстом.

```
Поиск: "PostgreSQL"
  ↓ матчит: D-001::F1  "Alice is using PostgreSQL"   score=4.2
  ↓ матчит: D-001::F2  "Alice prefers PostgreSQL"    score=1.1
  ↓ агрегация: → D-001 (полный блок: Statement + Rationale + Tags)
```

Глубина иерархии ровно 2 уровня. Не дерево — просто parent reference.

**Ось 2: горизонтальная (citation graph)**

Граф строится из упоминаний `[D-ID]` в тексте блоков. BFS до 2 хопов, score decay.

```
D-001 ("Use PostgreSQL", Context: "See D-002")
  → D-002 ("Migration plan")    score * 0.40   (1-hop)
    → D-003 ("Schema changes")  score * 0.16   (2-hop)
```

Не knowledge graph — нет типизированных отношений. Это citation index: если упомянул ID — связаны.

**Ось 3: временная (dialog adjacency)**

При матче реплики-вопроса автоматически добавляются следующие 1-2 реплики той же сессии.

```
DIA-D1-1 "[Alice] What's the deployment process?"  ← матч
  → DIA-D1-2 "[Bob] We use blue-green strategy."   ← via_adjacency
  → DIA-D1-3 "[Alice] That makes sense."            ← via_adjacency
```

Срабатывает только для вопросов (детекция `?` + question words), не для утверждений.

---

**Три оси вместе:** запрос одновременно тянет факты → родителя, родителя → соседей по цитатам, диалоговую реплику → соседей по времени. Именно это даёт LongMemEval 88 — не алгоритмическая новизна, а аккуратная комбинация известных техник.

---

## Бенчмарки (LongMemEval)

LongMemEval — тест на долгосрочную conversational memory, более сложный чем LoCoMo (многосессионные диалоги, вопросы через несколько сессий, цепочки фактов).

| Система | LongMemEval |
|---|---|
| mind-mem | 88 |
| Hindsight | 91 |

Разрыв в 3 балла при том, что Hindsight — облачный сервис с нейронными моделями, а mind-mem — локальный BM25. Это хороший результат для подхода без эмбеддингов.

---

## Оценка для нашего проекта

| Критерий | Оценка |
|---|---|
| Локальная работа (Windows) | ✅ zero core deps, SQLite |
| Автоматическое запоминание разговора | ⚠️ только через daily log + capture (regex, не семантика) |
| Поиск по разговорам | ✅ BM25F быстрый, 2.1ms p50 |
| Семантический поиск | ⚠️ опционально, нужен sentence-transformers |
| Governance / аудит | ✅ уникальная фича |
| Интеграция сложность | ⚠️ нужно писать диалоги в daily log в определённом формате |
| Заточен под разговорный ассистент | ⚠️ по умолчанию для coding agents, но dialog adjacency и fact atomization работают и для чата |

**Вывод:** Репа свежая (1.5 недели), 3 звезды, один автор — не production-ready библиотека. Но идеи правильные и тесты хорошие. Стоит брать как reference implementation: смотреть как устроен recall pipeline, брать конкретные идеи (small-to-big, BM25F field weights, knee cutoff), реализовывать под свои нужды. Hindsight при 91 LongMemEval — более зрелый выбор для conversational memory прямо сейчас.
