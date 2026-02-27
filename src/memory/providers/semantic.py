"""SemanticProvider — reimplementation of SimpleMem core in a single file.

Pipeline (write):
  pending turns → dialogue text → Gemini extracts MemoryEntry list → embed → LanceDB

Pipeline (read):
  user_text → embed → LanceDB vector search → context string
"""
import asyncio, json, logging, os, re, uuid
from dataclasses import dataclass, field
from typing import Optional

import httpx
import lancedb
import numpy as np
import pyarrow as pa
from google import genai

from src.memory.providers.base import BaseProvider
from src.memory.memory import Memory

log = logging.getLogger(__name__)

# ── Extraction prompt (from SimpleMem) ────────────────────────────────────────

EXTRACT_PROMPT = """\
Your task is to extract all valuable information from the following dialogues and convert them into structured memory entries.

{context}

[Current Dialogues]
{dialogue_text}

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
    "keywords": ["keyword1", "keyword2"],
    "timestamp": "YYYY-MM-DDTHH:MM:SS or null",
    "location": "location name or null",
    "persons": ["name1", "name2"],
    "entities": ["entity1", "entity2"],
    "topic": "topic phrase"
  }}
]
```

Return ONLY the JSON array, no other explanations.\
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
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))


# ── Provider ───────────────────────────────────────────────────────────────────

class SemanticProvider(BaseProvider):
    EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
    TABLE_NAME = "memory_entries"
    TOP_K = 10

    def __init__(self, model_name: str, api_key: str, consolidate_tokens: int = 3_000):
        super().__init__(consolidate_tokens=consolidate_tokens)
        self.model_name = model_name

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
            self._embed = SentenceTransformer(self.EMBEDDING_MODEL)
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
            )
            for r in results
        ]

    # ── LLM extraction ─────────────────────────────────────────────────────────

    @staticmethod
    def _turns_to_dialogue(turns: list) -> str:
        lines = []
        for turn in turns:
            if not isinstance(turn, dict) or turn.get("role") not in ("user", "model"):
                continue
            speaker = "User" if turn["role"] == "user" else "Assistant"
            ts = turn.get("_timestamp", "")[:19] if turn.get("_timestamp") else ""
            for part in turn.get("parts", []):
                if isinstance(part, dict) and (text := part.get("text")):
                    prefix = f"[{ts}] " if ts else ""
                    lines.append(f"{prefix}{speaker}: {text[:500]}")
        return "\n".join(lines)

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

    async def _extract_entries(self, dialogue_text: str) -> list[MemoryEntry]:
        context = await asyncio.to_thread(self._build_context)
        prompt = EXTRACT_PROMPT.format(
            context=context,
            dialogue_text=dialogue_text,
        )
        for attempt in range(3):
            try:
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=self.model_name,
                    contents=[{"role": "user", "parts": [{"text": prompt}]}],
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
                    )
                    for item in data
                    if item.get("lossless_restatement")
                ]
                return entries
            except Exception as e:
                log.warning("[SemanticProvider] extraction attempt %d failed: %s", attempt + 1, e)
        return []

    # ── consolidation & context ────────────────────────────────────────────────

    async def _consolidate(self, pending: list):
        dialogue_text = self._turns_to_dialogue(pending)
        if not dialogue_text.strip():
            return
        entries = await self._extract_entries(dialogue_text)
        if entries:
            await asyncio.to_thread(self._add_entries, entries)
        log.info("[SemanticProvider] consolidated %d turns → %d entries", len(pending), len(entries))

    async def get_context_prompt(self, user_text: str = "") -> str:
        if not user_text:
            return ""
        try:
            entries = await asyncio.to_thread(self._search, user_text)
        except Exception as e:
            log.warning("[SemanticProvider] search failed: %s", e)
            return ""
        if not entries:
            return ""
        lines = [f"- {e.lossless_restatement}" for e in entries]
        return "## Semantic memory\n" + "\n".join(lines)
