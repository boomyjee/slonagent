"""SemanticProvider — семантическая долгосрочная память на основе векторного поиска.

Реимплементация ядра SimpleMem в одном файле без внешней зависимости.

Pipeline (write):
  pending turns → _prepare_contents (embed timestamps) → Gemini extracts MemoryEntry list → Qwen3-Embedding → LanceDB

Pipeline (read):
  query → Qwen3-Embedding → LanceDB vector search → список фактов

Хранит данные в memory/semantic/lancedb, отладочный дамп последней консолидации — в last_entries.json.
"""
import asyncio, json, logging, os, re, uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

import httpx
import lancedb
import numpy as np
import pyarrow as pa
from google import genai

from agent import tool
from src.memory.providers.base import BaseProvider
from src.memory.memory import Memory

log = logging.getLogger(__name__)

# ── Extraction prompt (from SimpleMem) ────────────────────────────────────────

EXTRACT_PROMPT = """\
Your task is to extract all valuable information from the conversation above and convert them into structured memory entries.

{context}

[Requirements]
1. **Complete Coverage**: Generate enough memory entries to ensure ALL information in the dialogues is captured
2. **Force Disambiguation**: Absolutely PROHIBIT using pronouns (he, she, it, they, this, that) and relative time (yesterday, today, last week, tomorrow)
3. **Lossless Information**: Each entry's lossless_restatement must be a complete, independent, understandable sentence
4. **Precise Extraction**:
   - keywords: Core keywords (names, places, entities, topic words)
   - timestamp: Absolute time in ISO 8601 format (if explicit time mentioned in dialogue)
   - location: Specific location name (if mentioned)
   - persons: All person names mentioned
   - entities: Companies, products, organizations, etc.
   - topic: The topic of this information

[Output Format]
Return a JSON array, each element is a memory entry:

```json
[
  {{
    "lossless_restatement": "Complete unambiguous restatement (must include all subjects, objects, time, location, etc.)",
    "keywords": ["keyword1", "keyword2", ...],
    "timestamp": "YYYY-MM-DDTHH:MM:SS or null",
    "location": "location name or null",
    "persons": ["name1", "name2", ...],
    "entities": ["entity1", "entity2", ...],
    "topic": "topic phrase"
  }},
  ...
]
```

[Example]
Dialogues:
[2025-11-15T14:30:00] Alice: Bob, let's meet at Starbucks tomorrow at 2pm to discuss the new product
[2025-11-15T14:31:00] Bob: Okay, I'll prepare the materials

Output:
```json
[
  {{
    "lossless_restatement": "Alice suggested at 2025-11-15T14:30:00 to meet with Bob at Starbucks on 2025-11-16T14:00:00 to discuss the new product.",
    "keywords": ["Alice", "Bob", "Starbucks", "new product", "meeting"],
    "timestamp": "2025-11-16T14:00:00",
    "location": "Starbucks",
    "persons": ["Alice", "Bob"],
    "entities": ["new product"],
    "topic": "Product discussion meeting arrangement"
  }},
  {{
    "lossless_restatement": "Bob agreed to attend the meeting and committed to prepare relevant materials.",
    "keywords": ["Bob", "prepare materials", "agree"],
    "timestamp": null,
    "location": null,
    "persons": ["Bob"],
    "entities": [],
    "topic": "Meeting preparation confirmation"
  }}
]
```

Now process the conversation above. Return ONLY the JSON array, no other explanations.\
"""

# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class MemoryEntry:
    lossless_restatement: str
    keywords: list = field(default_factory=list)
    timestamp: Optional[str] = None
    location: Optional[str] = None
    persons: list = field(default_factory=list)
    entities: list = field(default_factory=list)
    topic: Optional[str] = None
    document_id: Optional[str] = None
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))


# ── Provider ───────────────────────────────────────────────────────────────────

class SemanticProvider(BaseProvider):
    EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
    TABLE_NAME = "memory_entries"
    TOP_K = 10

    def __init__(self, model_name: str, api_key: str, consolidate_tokens: int = 3_000,
                 auto_recall: bool = True):
        super().__init__(consolidate_tokens=consolidate_tokens)
        self.model_name = model_name
        self._auto_recall = auto_recall

        db_path = os.path.join(Memory.memory_dir, "semantic", "lancedb")
        os.makedirs(db_path, exist_ok=True)
        self._db = lancedb.connect(db_path)
        self._table = None  # lazy: initialized on first write/read
        self._embed = None  # lazy: loaded on first use

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        http_options = {"httpx_client": http_client, "api_version": "v1alpha"} if http_client else {"api_version": "v1alpha"}
        self._client = genai.Client(api_key=api_key, http_options=http_options)

    # ── embedding ──────────────────────────────────────────────────────────────

    def _get_embed(self):
        if self._embed is None:
            from sentence_transformers import SentenceTransformer
            from huggingface_hub import try_to_load_from_cache
            cached = try_to_load_from_cache(self.EMBEDDING_MODEL, "config.json") is not None
            self._embed = SentenceTransformer(self.EMBEDDING_MODEL, local_files_only=cached)
            self._dim = self._embed.get_sentence_embedding_dimension()
            log.info("[SemanticProvider] embedding model loaded, dim=%d", self._dim)
        return self._embed

    def _encode_docs(self, texts: list[str]) -> np.ndarray:
        return self._get_embed().encode(texts, normalize_embeddings=True)

    def _encode_query(self, text: str) -> np.ndarray:
        embed = self._get_embed()
        if hasattr(embed, "prompts") and "query" in (embed.prompts or {}):
            return embed.encode(text, prompt_name="query", normalize_embeddings=True)
        return embed.encode(text, normalize_embeddings=True)

    # ── LanceDB table ──────────────────────────────────────────────────────────

    def _get_table(self):
        if self._table is not None:
            return self._table
        dim = self._get_embed().get_sentence_embedding_dimension()
        schema = pa.schema([
            pa.field("entry_id",             pa.string()),
            pa.field("lossless_restatement", pa.string()),
            pa.field("keywords",             pa.list_(pa.string())),
            pa.field("timestamp",            pa.string()),
            pa.field("location",             pa.string()),
            pa.field("persons",              pa.list_(pa.string())),
            pa.field("entities",             pa.list_(pa.string())),
            pa.field("topic",                pa.string()),
            pa.field("document_id",          pa.string()),
            pa.field("vector",               pa.list_(pa.float32(), dim)),
        ])
        if self.TABLE_NAME in self._db.table_names():
            self._table = self._db.open_table(self.TABLE_NAME)
        else:
            self._table = self._db.create_table(self.TABLE_NAME, schema=schema)
            log.info("[SemanticProvider] created LanceDB table")
        return self._table

    def _add_entries(self, entries: list[MemoryEntry]):
        if not entries:
            return
        texts = [e.lossless_restatement for e in entries]
        vectors = self._encode_docs(texts)
        rows = [
            {
                "entry_id":             e.entry_id,
                "lossless_restatement": e.lossless_restatement,
                "keywords":             e.keywords or [],
                "timestamp":            e.timestamp or "",
                "location":             e.location or "",
                "persons":              e.persons or [],
                "entities":             e.entities or [],
                "topic":                e.topic or "",
                "document_id":          e.document_id or "",
                "vector":               v,
            }
            for e, v in zip(entries, vectors)
        ]
        self._get_table().add(rows)
        log.info("[SemanticProvider] added %d entries", len(entries))

    def _search(self, query: str) -> list[MemoryEntry]:
        table = self._get_table()
        try:
            if table.count_rows() == 0:
                return []
        except Exception:
            return []
        vec = self._encode_query(query)
        results = table.search(vec).limit(self.TOP_K).to_list()
        return [
            MemoryEntry(
                entry_id=r["entry_id"],
                lossless_restatement=r["lossless_restatement"],
                keywords=r.get("keywords") or [],
                timestamp=r.get("timestamp") or None,
                location=r.get("location") or None,
                persons=r.get("persons") or [],
                entities=r.get("entities") or [],
                topic=r.get("topic") or None,
                document_id=r.get("document_id") or None,
            )
            for r in results
        ]

    # ── LLM extraction ─────────────────────────────────────────────────────────

    @staticmethod
    def _prepare_contents(turns: list) -> list:
        """Strip private fields, embed _timestamp as text prefix to preserve temporal context."""
        result = []
        for turn in turns:
            if not isinstance(turn, dict) or turn.get("role") not in ("user", "model"):
                continue
            ts = (turn.get("_timestamp") or "")[:19]
            parts = []
            for i, part in enumerate(turn.get("parts", [])):
                if not isinstance(part, dict):
                    continue
                clean = {k: v for k, v in part.items() if not k.startswith("_")}
                if i == 0 and ts and "text" in clean:
                    clean["text"] = f"[{ts}] {clean['text']}"
                parts.append(clean)
            if parts:
                result.append({"role": turn["role"], "parts": parts})
        return result

    def _build_context(self) -> str:
        try:
            entries = self._search("recent conversation facts")[:3]
            if not entries:
                return ""
            lines = ["[Existing memory entries for context (avoid duplication):]"]
            for e in entries:
                lines.append(f"- {e.lossless_restatement}")
            return "\n".join(lines)
        except Exception:
            return ""

    async def _extract_entries(self, contents: list,
                               document_id: Optional[str] = None) -> list[MemoryEntry]:
        context = await asyncio.to_thread(self._build_context)
        instruction = EXTRACT_PROMPT.format(context=context)
        for attempt in range(3):
            try:
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=self.model_name,
                    contents=[*contents, {"role": "user", "parts": [{"text": instruction}]}],
                )
                raw = response.text or ""
                m = re.search(r"\[.*\]", raw, re.DOTALL)
                if not m:
                    log.warning("[SemanticProvider] no JSON array in LLM response (attempt %d)", attempt + 1)
                    continue
                data = json.loads(m.group(0))
                entries = [
                    MemoryEntry(
                        entry_id=str(uuid.uuid4()),
                        lossless_restatement=item["lossless_restatement"],
                        keywords=item.get("keywords") or [],
                        timestamp=item.get("timestamp"),
                        location=item.get("location"),
                        persons=item.get("persons") or [],
                        entities=item.get("entities") or [],
                        topic=item.get("topic"),
                        document_id=document_id,
                    )
                    for item in data
                    if item.get("lossless_restatement")
                ]
                return entries
            except Exception as e:
                log.warning("[SemanticProvider] extraction attempt %d failed: %s", attempt + 1, e)
        return []

    # ── consolidation & context ────────────────────────────────────────────────

    def _save_last_entries(self, entries: list[MemoryEntry]):
        path = os.path.join(Memory.memory_dir, "semantic", "last_entries.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump([asdict(e) for e in entries], f, ensure_ascii=False, indent=2)
        except Exception as ex:
            log.warning("[SemanticProvider] save last_entries failed: %s", ex)

    async def _consolidate(self, pending: list):
        all_entries: list[MemoryEntry] = []

        # Диалоговые turn'ы → одним запросом
        contents = self._prepare_contents(pending)
        if contents:
            entries = await self._extract_entries(contents)
            all_entries.extend(entries)

        # Документы → отдельный запрос для каждого
        docs: dict[str, str] = {}
        for turn in pending:
            if not isinstance(turn, dict):
                continue
            for part in turn.get("parts", []):
                if isinstance(part, dict) and "text" in part and part.get("_document_id"):
                    doc_id = part["_document_id"]
                    docs.setdefault(doc_id, part["text"])

        for doc_id, doc_text in docs.items():
            doc_contents = [{"role": "user", "parts": [{"text": doc_text}]}]
            entries = await self._extract_entries(doc_contents, document_id=doc_id)
            all_entries.extend(entries)
            log.info("[SemanticProvider] document %s → %d entries", doc_id, len(entries))

        if all_entries:
            await asyncio.to_thread(self._add_entries, all_entries)
            self._save_last_entries(all_entries)
        log.info("[SemanticProvider] consolidated %d turns → %d entries", len(pending), len(all_entries))

    async def get_context_prompt(self, user_text: str = "") -> str:
        """Автоматический поиск по тексту пользователя → системный промпт."""
        if not self._auto_recall or not user_text:
            return ""
        try:
            entries = await asyncio.to_thread(self._search, user_text[:1500])
        except Exception as e:
            log.warning("[SemanticProvider] recall for context failed: %s", e)
            return ""
        if not entries:
            return ""

        conv_lines, doc_lines = [], []
        for e in entries:
            if e.document_id:
                doc_lines.append(f"  - {e.lossless_restatement}")
            else:
                conv_lines.append(f"- {e.lossless_restatement}")

        parts = []
        if conv_lines:
            parts.append("Из разговоров:\n" + "\n".join(conv_lines))
        if doc_lines:
            parts.append("Из документов:\n" + "\n".join(doc_lines))

        return (
            "<semantic_memories>\n"
            + "\n\n".join(parts) + "\n"
            "</semantic_memories>\n\n"
            "Управляй долгосрочной памятью: search_memory"
        )

    @tool("Семантический поиск по долгосрочной памяти. Используй, когда нужно вспомнить факты, события или детали из прошлых разговоров. Перефразируй запрос пользователя в виде конкретного поискового вопроса.")
    async def search_memory(self, query: str) -> str:
        try:
            entries = await asyncio.to_thread(self._search, query)
        except Exception as e:
            log.warning("[SemanticProvider] search failed: %s", e)
            return "Ошибка поиска."
        if not entries:
            return "Ничего не найдено."
        lines = []
        for e in entries:
            prefix = f"[doc:{e.document_id}] " if e.document_id else ""
            lines.append(f"- {prefix}{e.lossless_restatement}")
        return "\n".join(lines)
