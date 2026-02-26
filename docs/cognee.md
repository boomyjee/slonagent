# Cognee: разбор архитектуры памяти

> Репозиторий: https://github.com/topoteretes/cognee  
> Исходники анализировались из `lib/cognee/`

---

## Быстрый ответ на три ключевых вопроса

### 1. Когда данные попадают в память?

Два ручных вызова, никакого автоматизма:

```python
await cognee.add(data, dataset_name="main_dataset")  # сохранить сырые данные
await cognee.cognify()                                # обработать в граф знаний
```

Cognee — это **библиотека для построения базы знаний**, не агентный цикл.  
Ты сам решаешь когда обрабатывать данные.

### 2. Как данные обрабатываются и что на выходе?

```
документ / текст / код / CSV
    ↓
classify_documents()      — определить тип (текст, аудио, код, таблица...)
    ↓
extract_chunks_from_documents()  — разбить на чанки
    ↓
для каждого чанка параллельно:
    LLM(chunk.text + generate_graph_prompt.txt)
    → {"nodes": [{"id": "Max", "type": "Dog", "name": "Max", "description": "..."}],
       "edges": [{"source_node_id": "Max", "target_node_id": "User", "relationship_name": "belongs_to"}]}
    ↓
integrate_chunk_graphs()  — дедупликация узлов и рёбер
    ↓
summarize_text()          — создать суммаризации
    ↓
add_data_points()         → запись в три хранилища
```

**На выходе:** граф знаний — узлы (сущности) и рёбра (связи между ними).  
Не строки текста — а структурированные отношения: `"Max -[belongs_to]→ User"`.

### 3. Как память извлекается?

```python
results = await cognee.search("расскажи про собаку", query_type=SearchType.GRAPH_COMPLETION)
```

Гибридный поиск: вектор находит **какие узлы релевантны**, граф достаёт **всё их окружение**.

---

## Три хранилища одновременно

| БД | Что хранит | По умолчанию |
|---|---|---|
| Реляционная | метаданные документов, датасеты, пользователи | SQLite |
| Граф БД | узлы (сущности) и рёбра (связи) | KuzuDB (локальный файл) |
| Векторная БД | эмбеддинги для семантического поиска | LanceDB |

**Важно:** векторная БД — это **не индекс графа**. Это отдельный слой.  
Граф хранит структуру ("кто с кем связан"), вектор хранит смысловые расстояния ("что семантически близко к запросу"). При поиске оба слоя используются вместе.

---

## Промпт для извлечения графа

**Файл:** `lib/cognee/cognee/infrastructure/llm/prompts/generate_graph_prompt.txt`

```
You are a top-tier algorithm designed for extracting information in structured formats 
to build a knowledge graph.

Nodes represent entities and concepts. They're akin to Wikipedia nodes.
Edges represent relationships between concepts. They're akin to Wikipedia links.

# 1. Labeling Nodes
Consistency: Use basic or elementary types for node labels.
  - Person (not "Mathematician" or "Scientist")
  - Date, Location, Organization
  - Avoid too generic terms like "Entity"
Node IDs: Never use integers. Use human-readable names found in the text.

# 2. Handling Numerical Data and Dates
  - Dates as type "Date", format "YYYY-MM-DD"
  - Properties in key-value format, snake_case for relationship names

# 3. Coreference Resolution
  - If an entity is mentioned by different names/pronouns — always use
    the most complete identifier throughout the knowledge graph.

# 4. Strict Compliance
Adhere to the rules strictly.
```

**LLM возвращает** (structured output / json_schema):
```json
{
  "nodes": [
    {"id": "Max", "name": "Max", "type": "Dog", "description": "User's pet dog"}
  ],
  "edges": [
    {"source_node_id": "Max", "target_node_id": "User", "relationship_name": "belongs_to"}
  ]
}
```

Схема (`data_models.py`):
```python
class Node(BaseModel):
    id: str           # человекочитаемое имя ("Max", "User")
    name: str
    type: str         # "Dog", "Person", "Date", "Location"...
    description: str

class Edge(BaseModel):
    source_node_id: str
    target_node_id: str
    relationship_name: str   # snake_case: "belongs_to", "acted_in"

class KnowledgeGraph(BaseModel):
    nodes: List[Node]
    edges: List[Edge]
```

---

## Как данные попадают в хранилища

**Файл:** `lib/cognee/cognee/tasks/storage/add_data_points.py`

```python
# 1. Граф БД — структура
await graph_engine.add_nodes(nodes)
await graph_engine.add_edges(edges)

# 2. Векторная БД — эмбеддинги узлов
await index_data_points(nodes)
# → создаёт коллекции вида "Entity_name", "Entity_description"
# → каждый узел индексируется по полям из его metadata["index_fields"]

# 3. Опционально: триплеты
if embed_triplets:
    triplets = _create_triplets_from_graph(nodes, edges)
    # triplet.text = "Max -› belongs_to-›User"
    await index_data_points(triplets)
    # → коллекция "Triplet_text"
```

**Дедупликация:**
- узлы — по `node.id`
- рёбра — по ключу `str(source) + relationship + str(target)`

**Что индексируется в векторную:**
- поля узлов: `Entity_name`, `Entity_description`
- рёбра: `EdgeType_relationship_name`
- чанки: `DocumentChunk_text`
- суммаризации: `TextSummary_text`
- (опционально) триплеты: `Triplet_text`

---

## Как работает поиск GRAPH_COMPLETION

Это самое интересное — двухэтапный гибридный поиск.

**Файл:** `lib/cognee/cognee/modules/retrieval/graph_completion_retriever.py`  
**Файл:** `lib/cognee/cognee/modules/retrieval/utils/brute_force_triplet_search.py`

### Этап 1: Векторный поиск — "какие узлы релевантны?"

```python
# lib/cognee/cognee/modules/retrieval/utils/node_edge_vector_search.py

# Параллельный поиск по 5+ векторным коллекциям
collections = [
    "Entity_name",              # имена сущностей
    "TextSummary_text",         # тексты суммаризаций
    "EntityType_name",          # типы сущностей
    "DocumentChunk_text",       # тексты чанков
    "EdgeType_relationship_name" # названия связей
]

# Для каждой коллекции: embed(query) → cosine search → [(node_id, distance), ...]
# Результат: node_distances = {"Entity_name": [(id1, 0.92), (id2, 0.87), ...], ...}
#            edge_distances  = [(edge_id, 0.78), ...]
```

### Этап 2: Загрузка подграфа и скоринг триплетов

```python
# wide_search_top_k=100 — берём топ-100 узлов из векторного поиска
relevant_node_ids = vector_search.extract_relevant_node_ids()

# Загружаем из KuzuDB подграф только с этими узлами
memory_fragment = await get_memory_fragment(
    relevant_ids_to_filter=relevant_node_ids,
    triplet_distance_penalty=3.5,  # штраф за длину пути в графе
)

# Каждому узлу и ребру в подграфе присваиваем векторное расстояние
await memory_fragment.map_vector_distances_to_graph_nodes(node_distances)
await memory_fragment.map_vector_distances_to_graph_edges(edge_distances)

# Скоринг триплетов: score(triplet) = f(distance_node1, distance_edge, distance_node2, path_length)
# Возвращаем топ-k триплетов
triplets = await memory_fragment.calculate_top_triplet_importances(k=5)
```

**Смысл:** вектор находит кандидатов, граф достаёт их полное окружение.  
Если нашли "Max" — автоматически получаем все его связи из графа, даже если они семантически не похожи на запрос.

### Этап 3: Контекст → LLM

```python
# Триплеты → читаемый текст
context = """
Node1: {name: Max, type: Dog, description: User's pet dog}
Edge: {relationship_name: belongs_to}
Node2: {name: User, type: Person, description: ...}
---
Node1: {name: Max, type: Dog}
Edge: {relationship_name: age_is}
Node2: {name: 3 years, type: Age}
"""

# Системный промпт: "Answer the question using the provided context. Be as brief as possible."
# Пользовательский промпт:
# "The question is: `{{ question }}`
#  and here is the context provided with a set of relationships from a knowledge graph: `{{ context }}`"
completion = await generate_completion(query, context)
```

---

## Типы поиска

| Тип | Что делает |
|---|---|
| `GRAPH_COMPLETION` | вектор → подграф → scoring → LLM с контекстом |
| `CHUNKS` | чисто векторный поиск по `DocumentChunk_text` |
| `SUMMARIES` | векторный поиск по `TextSummary_text` |
| `TRIPLETS` | семантический поиск по триплетам без LLM |
| `RAG_COMPLETION` | LLM + чанки без графа (классический RAG) |
| `CYPHER` | прямой запрос к графу на языке Cypher |
| `FEELING_LUCKY` | автоматический выбор типа |

---

## Что такое memify

**Файл:** `lib/cognee/cognee/modules/memify/memify.py`

`memify` — это пайплайн **обогащения уже построенного графа**. Если `cognify` строит граф из документов, то `memify` берёт существующий граф и прогоняет через кастомные задачи.

```python
await cognee.memify(
    extraction_tasks=[...],   # достать данные из графа
    enrichment_tasks=[...],   # что-то сделать с ними
    node_type=NodeSet,        # работать только с этим типом узлов
)
```

По умолчанию (без параметров): достаёт весь граф → запускает `add_rule_associations` — поиск паттернов для coding agent. Фактически это extension-точка для пользовательской логики поверх графа.

---

## Схема полного пайплайна

```
await cognee.add(data)
    ↓
SQLite: metadata (filename, hash, mime_type, dataset_id)
    ↓ дедупликация по content_hash

await cognee.cognify()
    ↓
classify_documents()
    ↓
extract_chunks_from_documents(max_chunk_size=512..8192 tokens)
    ↓
для каждого чанка параллельно:
    extract_content_graph(chunk.text, KnowledgeGraph) → LLM → nodes + edges
    ↓
integrate_chunk_graphs():
    - онтология (опционально)
    - дедупликация по node.id и source+rel+target
    ↓
add_data_points():
    ├─ KuzuDB.add_nodes(nodes)
    ├─ LanceDB.index(nodes)       # эмбеддинги по index_fields
    ├─ KuzuDB.add_edges(edges)
    ├─ LanceDB.index(edges)       # эмбеддинги relationship_name
    └─ (если embed_triplets) LanceDB.index(triplets) # "Max -› belongs_to -› User"
    ↓
summarize_text()
    → LanceDB.index(summaries)

await cognee.search(query, SearchType.GRAPH_COMPLETION)
    ↓
embed(query) → параллельный поиск по 5 коллекциям LanceDB
    → node_distances, edge_distances
    ↓
get_memory_fragment() — загрузить подграф из KuzuDB
    (топ-100 узлов из векторного поиска)
    ↓
map distances → calculate_top_triplet_importances(k=5)
    → топ-5 триплетов
    ↓
resolve_edges_to_text() → context string
    ↓
LLM(query + context) → ответ
```

---

## Оценка

**Преимущества:**
- Граф позволяет traversal-запросы: "кто связан с X?" — обход по рёбрам, не по косинусу
- Дедупликация узлов из разных чанков: "Max" из разных частей документа → один узел
- Суммаризации как отдельный векторный слой — дополнительная точность поиска
- Три провайдера под каждую БД, можно заменить без изменения логики

**Проблемы:**
- LLM извлекает граф из текста — качество зависит от модели, ошибки тихие
- Три разные базы — тяжёлая инфраструктура для простого агента
- Весь пайплайн ручной, нет интеграции с агентным циклом
- `memify` без параметров запускает coding agent логику — странный дефолт
- Для коротких диалогов граф избыточен — "User: привет" не стоит превращать в граф сущностей

**Где это оправдано:**  
Большие документы, корпоративные базы знаний, данные где важны связи между сущностями. Не для личного ассистента — слишком тяжело.

---

## Чанкинг: как обрабатываются большие документы

Если передать cognee большой текст (например, "Война и Мир") — он разобьёт его на куски и обработает каждый отдельно.

**Размер чанка:** `min(embedding_max_tokens, llm_max_tokens // 2)` — обычно 512–8192 токенов в зависимости от модели.  
**Стратегия по умолчанию:** `TextChunker` — режет по абзацам, не обрывая на полуслове.

**Что происходит с каждым чанком:**

```
Глава 1: "Ну что, мой принц. Генуя и Лукка..."  [512 токенов]
    ↓ LLM
    nodes: [Анна Шерер, Болконский, Наполеон]
    edges: [Анна знает Болконского, Болконский критикует Наполеона]

Глава 1 продолжение: "— Нет, я вас не отпущу..."  [512 токенов]
    ↓ LLM
    nodes: [Болконский, Пьер, Лиза]
    edges: [Болконский женат на Лизе, Пьер друг Болконского]

... × 1000 чанков
```

**Дедупликация узлов:** Болконский встречается в тысяче чанков — в графе он один узел, потому что `node.id = "Болконский"`. Каждый новый чанк добавляет ему новые рёбра, не создаёт копии.

**Что теряется:** контекст на границах чанков. Если в чанке 47 написано "он сказал" — LLM может не знать кто этот "он", потому что имя было в чанке 46. Часть связей теряется.

**Итог для большой книги:** несколько тысяч узлов (персонажи, места, события, даты) и десятки тысяч рёбер. Запрос "расскажи про отношения Наташи и Андрея" найдёт все триплеты где оба упоминаются — из разных частей книги, связанных в единый граф.

**Настройка размера чанка важна** (подтверждено исследованием авторов Cognee на бенчмарках HotPotQA/TwoWikiMultiHop): слишком маленькие чанки → фрагментированный граф, слишком большие → LLM теряет детали. Оптимум подбирается под конкретную задачу. ([arxiv 2505.24478](https://arxiv.org/html/2505.24478v1))
