# A-mem: разбор архитектуры памяти

> Репозиторий: https://github.com/agiresearch/A-mem  
> Статья: https://arxiv.org/pdf/2502.12110  
> Исходники анализировались из `lib/A-mem/`

---

## Быстрый ответ на три ключевых вопроса

### 1. Когда диалог попадает в память?

**Только явно** — через вызов `add_note(content)`. Никакого автоматизма нет.

Это библиотека, не агент. Она не знает о диалоге — ты сам решаешь что и когда передавать.

### 2. Как обрабатывается и что на выходе?

```
add_note(content)
    ↓
ChromaDB: найти 5 ближайших по эмбеддингам
    ↓
(если база не пустая)
LLM: "вот новое воспоминание, вот соседи — нужно ли связать?"
    ↓
JSON: {"should_evolve": true, "actions": ["strengthen", "update_neighbor"], ...}
    ↓
"strengthen"  → добавить ID соседей в note.links, обновить теги новой ноты
"update_neighbor" → обновить теги и context у соседних нот в памяти
    ↓
ChromaDB: сохранить новую ноту с эмбеддингом
```

### 3. Как память извлекается?

**Только явно** — через `search_agentic(query, k=5)`.

1. ChromaDB семантический поиск → топ-k нот
2. Для каждой найденной ноты: подтянуть все связанные через `links`
3. Вернуть объединённый список, связанные помечены `is_neighbor: True`

Никакой автовставки в контекст нет — это библиотека.

---

## Хранилище

**Двойное хранение:**
- Python dict `self.memories: Dict[str, MemoryNote]` — основное хранилище в RAM
- ChromaDB — векторные эмбеддинги для семантического поиска

**Эмбеддинги:** `all-MiniLM-L6-v2` (SentenceTransformer, локально, без API).

**Схема `MemoryNote`:**

```python
# lib/A-mem/agentic_memory/memory_system.py:24
class MemoryNote:
    content: str              # текст воспоминания
    id: str                   # UUID
    keywords: List[str]       # ключевые слова
    context: str              # контекст/домен ("General" по умолчанию)
    category: str             # категория ("Uncategorized" по умолчанию)
    tags: List[str]           # теги для классификации и поиска
    links: List[str]          # ID связанных воспоминаний (граф связей)
    timestamp: str            # YYYYMMDDHHMM — время создания
    last_accessed: str        # YYYYMMDDHHMM — последнее обращение
    retrieval_count: int      # счётчик обращений
    evolution_history: List   # история изменений (поле есть, но не заполняется)
```

**ChromaDB:** хранит тот же текст + все поля как метаданные. Списки сериализуются в JSON-строки при записи и десериализуются обратно при чтении:

```python
# lib/A-mem/agentic_memory/retrievers.py:63
def add_document(self, document: str, metadata: Dict, doc_id: str):
    processed_metadata = {}
    for key, value in metadata.items():
        if isinstance(value, list):
            processed_metadata[key] = json.dumps(value)   # список → строка
        ...
    self.collection.add(documents=[document], metadatas=[processed_metadata], ids=[doc_id])
```

---

## Эволюция памяти — как строятся связи

Это главная инновация A-mem. При добавлении каждой новой ноты LLM решает как связать её с уже существующими.

### Шаг 1: Поиск соседей

```python
# lib/A-mem/agentic_memory/memory_system.py:590
def process_memory(self, note: MemoryNote) -> Tuple[bool, MemoryNote]:
    if not self.memories:
        return False, note  # первая нота — без эволюции
    
    neighbors_text, indices = self.find_related_memories(note.content, k=5)
```

`find_related_memories` возвращает форматированную строку для промпта:
```
memory index:0  talk start time:202503021500  memory content: User's dog is Max
                memory context: Personal  memory keywords: ['dog', 'Max']
                memory tags: ['pet', 'family']
memory index:1  talk start time:...
```

### Шаг 2: LLM решает что делать

**Полный системный промпт** (`_evolution_system_prompt`):

```
You are an AI memory evolution agent responsible for managing and evolving a knowledge base.
Analyze the new memory note according to keywords and context, 
also with their several nearest neighbors memory.
Make decisions about its evolution.

The new memory context:
{context}
content: {content}
keywords: {keywords}

The nearest neighbors memories:
{nearest_neighbors_memories}

Based on this information, determine:
1. Should this memory be evolved? Consider its relationships with other memories.
2. What specific actions should be taken (strengthen, update_neighbor)?
   2.1 If choose to strengthen the connection, which memory should it be connected to?
       Can you give the updated tags of this memory?
   2.2 If choose to update_neighbor, you can update the context and tags of these memories
       based on the understanding of these memories. If the context and the tags are not
       updated, the new context and tags should be the same as the original ones.
       Generate the new context and tags in the sequential order of the input neighbors.
Tags should be determined by the content and characteristics of these memories,
which can be used to retrieve them later and categorize them.
Note that the length of new_tags_neighborhood must equal the number of input neighbors,
and the length of new_context_neighborhood must equal the number of input neighbors.
The number of neighbors is {neighbor_number}.

Return your decision in JSON format:
{
    "should_evolve": True or False,
    "actions": ["strengthen", "update_neighbor"],
    "suggested_connections": ["neighbor_memory_ids"],
    "tags_to_update": ["tag_1", ..., "tag_n"],
    "new_context_neighborhood": ["new context", ..., "new context"],
    "new_tags_neighborhood": [["tag_1", ..., "tag_n"], ..., ["tag_1", ..., "tag_n"]]
}
```

LLM отвечает JSON со structured outputs (json_schema), что снижает риск битого парсинга.

### Шаг 3: Применение решения

```python
# lib/A-mem/agentic_memory/memory_system.py:679
if should_evolve:
    for action in actions:
        if action == "strengthen":
            # Добавить связи к новой ноте
            note.links.extend(suggested_connections)   # ← ID соседей
            note.tags = new_tags                        # ← обновить теги

        elif action == "update_neighbor":
            # Обновить соседей в обратную сторону
            for i, neighbor_idx in enumerate(indices):
                neighbor = noteslist[neighbor_idx]
                neighbor.tags = new_tags_neighborhood[i]
                neighbor.context = new_context_neighborhood[i]
                self.memories[notes_id[neighbor_idx]] = neighbor
```

**Итог:** связи **однонаправленные** — новая нота знает о соседях (через `links`), но соседи не знают о новой ноте. Граф несимметричный.

### Два действия эволюции

| Действие | Что меняется | Кто меняется |
|---|---|---|
| `strengthen` | `links` и `tags` | новая нота |
| `update_neighbor` | `context` и `tags` | существующие соседи |

Могут применяться оба одновременно.

---

## Как связи используются при извлечении

```python
# lib/A-mem/agentic_memory/memory_system.py:509
def search_agentic(self, query: str, k: int = 5) -> List[Dict]:
    # Шаг 1: семантический поиск в ChromaDB
    results = self.retriever.search(query, k)
    
    memories = []
    seen_ids = set()
    
    # Шаг 2: добавить прямые результаты
    for doc_id in results['ids'][0][:k]:
        memory_dict = {
            'id': doc_id,
            'content': ...,
            'is_neighbor': False,   # ← прямое попадание
            ...
        }
        memories.append(memory_dict)
        seen_ids.add(doc_id)
    
    # Шаг 3: подтянуть связанные через links
    neighbor_count = 0
    for memory in list(memories):
        if neighbor_count >= k:
            break
        links = memory.get('links', [])
        for link_id in links:
            if link_id not in seen_ids:
                neighbor = self.memories.get(link_id)
                if neighbor:
                    memories.append({
                        'id': link_id,
                        'content': neighbor.content,
                        'is_neighbor': True,    # ← связанное, не прямое
                        ...
                    })
                    seen_ids.add(link_id)
                    neighbor_count += 1
    
    return memories[:k]   # ← итого не больше k
```

**Пример:** запрос "что я рассказывал про собаку?"

```
ChromaDB находит: "User's dog is Max" (прямое попадание, score=0.92)
links у этой ноты: ["id-abc123", "id-def456"]
    → подтягивается: "Max болел в январе" (is_neighbor=True)
    → подтягивается: "Max любит гулять в парке" (is_neighbor=True)

Итого возвращается 3 ноты вместо 1.
```

**Ограничение:** суммарный лимит `k` делится между прямыми и связанными. При `k=5` и 3 прямых попаданиях остаётся место только для 2 связанных.

---

## Консолидация

Вызывается каждые `evo_threshold` эволюций (по умолчанию 100):

```python
# lib/A-mem/agentic_memory/memory_system.py:266
def consolidate_memories(self):
    # Сброс ChromaDB коллекции — старая удаляется
    self.retriever = ChromaRetriever(collection_name="memories", model_name=self.model_name)
    
    # Переиндексация всех нот с актуальными метаданными
    for memory in self.memories.values():
        self.retriever.add_document(memory.content, metadata, memory.id)
```

**Что делает:** синхронизирует изменённые теги/контекст (из `update_neighbor`) обратно в ChromaDB. Без этого поиск по тегам и контексту работал бы на устаревших метаданных.

**Чего не делает:** не удаляет дубликаты, не мёрджит похожие ноты.

---

## Варианты хранилища

Три реализации `ChromaRetriever`:

| Класс | Хранение | Когда использовать |
|---|---|---|
| `ChromaRetriever` | In-memory, теряется при перезапуске | Тесты, разработка |
| `PersistentChromaRetriever` | Файлы на диске (`~/.chromadb`) | Продакшн, постоянная память |
| `CopiedChromaRetriever` | Копия существующей коллекции во временной директории | Изолированный инстанс от общей базы |

```python
# lib/A-mem/agentic_memory/retrievers.py:152
class PersistentChromaRetriever(ChromaRetriever):
    def __init__(self, directory=None, collection_name="memories", extend=False):
        self.client = chromadb.PersistentClient(path=str(directory))
        # extend=False → выбросит ошибку если коллекция уже существует
        # extend=True → загрузит существующую коллекцию
```

---

## Оценка

**Инновация:** граф связей между нотами. Вместо того чтобы каждый раз искать только по косинусному расстоянию, поиск подтягивает ассоциативно связанные факты — как работает человеческая ассоциативная память.

**Проблемы:**

- **LLM на каждое `add_note`** — при большом потоке данных дорого. Нет батчинга.
- **Связи однонаправленные** — нота A знает о ноте B, но B не знает об A.
- **`evolution_history` не заполняется** — поле есть в схеме, код сохраняет его в ChromaDB, но нигде не пишет историю изменений.
- **Дедупликации нет** — дубликаты просто связываются между собой через `strengthen`.
- **Лимит `k` делится** между прямыми и связанными результатами — при большом числе связей прямые результаты вытесняются.
- **Консолидация = полная переиндексация** каждые 100 эволюций — дорого на большой базе.

---

## Сводная схема

```
add_note(content)
    ↓
MemoryNote(content, id=UUID, links=[], tags=[], ...)
    ↓
find_related_memories(content, k=5)
    → ChromaDB.query() → топ-5 по эмбеддингам
    ↓
LLM(_evolution_system_prompt) → JSON
    ↓
    ├─ should_evolve=False → просто сохранить
    └─ should_evolve=True
          ├─ "strengthen"      → note.links.extend(ids), note.tags = new_tags
          └─ "update_neighbor" → neighbor.tags = ..., neighbor.context = ...
    ↓
self.memories[note.id] = note            (Python dict)
self.retriever.add_document(note.content, metadata, note.id)  (ChromaDB)
    ↓
если evo_cnt % 100 == 0 → consolidate_memories() → переиндексация ChromaDB


search_agentic(query, k=5)
    ↓
ChromaDB.query(query, k=5) → топ-k по косинусному расстоянию
    ↓
для каждого результата: подтянуть note.links → соседи (is_neighbor=True)
    ↓
вернуть прямые + связанные, суммарно не больше k
```
