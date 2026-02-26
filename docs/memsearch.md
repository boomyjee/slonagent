# memsearch: разбор архитектуры памяти

> Репозиторий: https://github.com/zilliztech/memsearch  
> Исходники анализировались из `lib/memsearch/`

---

## Быстрый ответ на три ключевых вопроса

### 1. Когда данные попадают в память?

Явно. Агент пишет текст в `.md` файл, затем вызывает `await mem.index()` или запускает `mem.watch()`. Никакой автоматической LLM-обработки нет — данные попадают в Milvus как есть (после чанкинга и эмбеддинга).

### 2. Как происходит поиск?

Гибридный поиск: **dense vector (cosine) + BM25 sparse**, слияние через RRF (k=60). Оба индекса хранятся в одной Milvus-коллекции, запрос идёт одновременно по обоим.

### 3. Что делает библиотека автоматически?

- **Дедупликация**: SHA-256 хэш содержимого чанка — неизменившийся чанк никогда не переэмбеддируется
- **Watch**: фоновый поток следит за .md файлами, при изменении переиндексирует
- **Stale cleanup**: при `index()` удаляет чанки файлов, которые больше не существуют
- **Compact**: LLM-суммаризация всех чанков → запись обратно в daily .md → переиндексация

---

## Архитектура

```
  .md файлы  ──▶  Chunker  ──▶  SHA-256 dedup  ──▶  Embed  ──▶  Milvus Lite
(единственный         │          (пропустить         (только       (local .db,
  источник          heading-     уже известные)      новые)         dense +
   правды)          based                                           BM25)
                      │
                    watch
                 (background
                   thread)
```

---

## Чанкинг: `chunker.py`

Разбивает markdown по **заголовкам** (`#`, `##`, ...). Каждый раздел (от заголовка до следующего) — один чанк. Если раздел превышает `max_chunk_size=1500` символов — дополнительно делится по **границам параграфов** с `overlap_lines=2` строк overlap.

Chunk ID = SHA256(`markdown:{source}:{start_line}:{end_line}:{content_hash}:{model}`)[:16]

Одинаковый контент с разными провайдерами → разные ID (модель в составе ключа).

---

## Хранилище: `store.py` → Milvus

Схема коллекции:

| Поле | Тип | Описание |
|---|---|---|
| `chunk_hash` | VARCHAR (PK) | SHA-256 ID чанка |
| `embedding` | FLOAT_VECTOR | Dense вектор |
| `sparse_vector` | SPARSE_FLOAT_VECTOR | BM25, автогенерируется из `content` |
| `content` | VARCHAR | Текст чанка |
| `source` | VARCHAR | Путь к файлу |
| `heading` | VARCHAR | Ближайший заголовок |
| `heading_level` | INT64 | Уровень заголовка (0 = преамбула) |
| `start_line`, `end_line` | INT64 | Позиция в файле |

**Hybrid search** (из `store.py`):
```python
dense_req  = AnnSearchRequest(data=[embedding], anns_field="embedding",  metric="COSINE", limit=top_k)
bm25_req   = AnnSearchRequest(data=[query_text], anns_field="sparse_vector", metric="BM25", limit=top_k)
results    = client.hybrid_search(reqs=[dense_req, bm25_req], ranker=RRFRanker(k=60), ...)
```

BM25 — встроенная функция Milvus, вычисляется автоматически из поля `content`. Не нужен внешний tokenizer.

### Режимы Milvus

| Режим | `milvus_uri` | Особенности |
|---|---|---|
| Milvus Lite (default) | `~/.memsearch/milvus.db` | Локальный файл, ноль конфига |
| Milvus Server | `http://localhost:19530` | Для multi-agent / команды |
| Zilliz Cloud | `https://in03-xxx.zillizcloud.com` | Managed production |

---

## Провайдеры эмбеддингов

| Провайдер | Модель по умолчанию | Установка |
|---|---|---|
| `openai` | `text-embedding-3-small` | `pip install memsearch` |
| `google` | `gemini-embedding-001` | `pip install "memsearch[google]"` |
| `voyage` | `voyage-3-lite` | `pip install "memsearch[voyage]"` |
| `ollama` | `nomic-embed-text` | `pip install "memsearch[ollama]"` |
| `local` | `all-MiniLM-L6-v2` | `pip install "memsearch[local]"` |

---

## Compact: LLM-суммаризация

`compact()` — единственная LLM-операция в memsearch:

1. Забирает все чанки из Milvus
2. Склеивает их через `---` разделитель
3. Отправляет в LLM с промптом "сожми, сохраняя факты"
4. Результат дописывает в `memory/YYYY-MM-DD.md`
5. Немедленно переиндексирует файл

Поддерживаемые LLM: `openai`, `anthropic`, `gemini`.

Это ручная операция — compact не запускается автоматически.

---

## Claude Code Plugin: паттерн использования

Самый интересный пример применения. Демонстрирует, как memsearch встраивается в workflow агента:

### Хуки

| Хук | Действие |
|---|---|
| `SessionStart` | Запускает `memsearch watch` в фоне + инжектирует 2 последних daily log (последние 30 строк каждого) как холодный старт |
| `UserPromptSubmit` | Добавляет `[memsearch] Memory available` в системный промпт — намёк для агента |
| `SessionEnd` | Останавливает watch |

### Skill `memory-recall`

Pull-based поиск — агент вызывает сам когда нужно:

1. `memsearch search "<query>" --top-k 5` → получает chunk hashes
2. `memsearch expand <chunk_hash>` → полный раздел с контекстом
3. (опционально) `memsearch transcript <path> --turn <uuid>` → оригинальные реплики диалога

### Паттерн записи памяти

Каждая сессия — раздел в daily файле `memory/YYYY-MM-DD.md`:
```markdown
## Session 14:30

Пользователь спросил про Redis, решили использовать для кэширования.
Alice — frontend lead.
```

Markdown читаем человеком, версионируется в git, не требует миграций БД.

---

## Оценка

### Главная идея

**Markdown как единственный источник правды.** Milvus — только производный индекс, который можно дропнуть и пересобрать. Это принципиально отличает memsearch от всех остальных систем.

### Сильные стороны

| Свойство | Почему важно |
|---|---|
| SHA-256 dedup | Переиндексация при изменении файла не пересчитывает неизменённые чанки |
| Milvus Lite | Никакого сервера — просто файл `.db` |
| Гибридный поиск | BM25 встроен в Milvus, не нужен отдельный движок |
| Heading-based chunking | Смысловые границы, а не фиксированный размер |
| File watch | Живая синхронизация без ручных вызовов index() |
| Gemini embeddings | `memsearch[google]` — наш провайдер уже поддерживается |

### Слабые стороны

| Проблема | Описание |
|---|---|
| Нет LLM-экстракции фактов | Агент должен сам формулировать что писать в `.md` |
| Нет автоматической консолидации | Compact — ручная операция |
| Нет графа связей | Только векторный поиск + BM25, нет каузальных/темпоральных ссылок |
| Нет иерархии абстракций | Всё хранится как плоские чанки, нет mental models |
| Качество памяти = качество записей | Мусор на входе → мусор при поиске |

### Сравнение с остальными библиотеками

| Критерий | memsearch | A-mem | cognee | hindsight |
|---|---|---|---|---|
| Сложность | Минимальная | Средняя | Высокая | Высокая |
| Серверные требования | Нет (Milvus Lite) | Нет (ChromaDB) | Нет (LanceDB) | Да (PostgreSQL) |
| Автоматическая LLM-обработка | Нет | Есть (evolve) | Есть (graph extract) | Есть (fact extract) |
| Граф связей | Нет | Есть (links) | Есть (KuzuDB) | Есть (3 типа) |
| Hybrid search | Да (dense+BM25) | Нет | Нет (только dense) | Да (4 стратегии) |
| Markdown читаем | Да | Нет | Нет | Нет |
| Windows | Да | Да | Да | Только через Docker |

### Почему интересен для нашего проекта

Если не нужна сложность Hindsight:
- **Нет зависимости от внешнего сервера** — Milvus Lite это просто `.db` файл
- **Gemini embeddings** поддерживаются из коробки (`memsearch[google]`)
- **Hybrid search** даёт лучше чем чистый векторный поиск без сложности
- **Прозрачность** — память всегда можно открыть и прочитать как обычный текст

Подходит как **основной провайдер** для сценария "агент сам решает что записывать" через инструменты `memsearch_write` + автоматический поиск в `get_context_prompt`.
