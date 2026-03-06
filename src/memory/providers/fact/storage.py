"""storage.py — Storage: фасад над SQLite + LanceDB для FactProvider.

Схема SQLite:
  facts                — основная таблица фактов (world | experience | observation)
  entities             — уникальные сущности (люди, места, организации)
  entity_aliases       — fuzzy-resolved псевдонимы → canonical entity_id
  entity_cooccurrences — счётчики совместной встречаемости сущностей
  fact_entities        — M:N связь факт ↔ сущность
  fact_links           — рёбра графа (temporal / semantic / causal / entity)
  mental_models        — user-created/auto curated summaries (highest priority)
  chunks               — raw text chunks документов
"""
import json
import logging
import os
import sqlite3

import lancedb
import pyarrow as pa

log = logging.getLogger(__name__)

# ── DDL ────────────────────────────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_text  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);

CREATE TABLE IF NOT EXISTS facts (
    fact_id         TEXT PRIMARY KEY,
    fact            TEXT NOT NULL,
    fact_type       TEXT NOT NULL DEFAULT 'world',
    occurred_start  TEXT,
    occurred_end    TEXT,
    mentioned_at    TEXT,
    document_id     TEXT,
    chunk_id        TEXT REFERENCES chunks(chunk_id) ON DELETE SET NULL,
    source_fact_ids TEXT,
    context         TEXT,
    consolidated     INTEGER NOT NULL DEFAULT 0,
    is_real_document INTEGER NOT NULL DEFAULT 0,
    tags             TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_facts_mentioned_at   ON facts(mentioned_at);
CREATE INDEX IF NOT EXISTS idx_facts_occurred_start ON facts(occurred_start);
CREATE INDEX IF NOT EXISTS idx_facts_document_id    ON facts(document_id);
CREATE INDEX IF NOT EXISTS idx_facts_consolidated   ON facts(consolidated);
CREATE INDEX IF NOT EXISTS idx_facts_fact_type      ON facts(fact_type);

CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias     TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS entity_cooccurrences (
    entity_id_1 TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    entity_id_2 TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    count       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (entity_id_1, entity_id_2),
    CHECK (entity_id_1 < entity_id_2)
);
CREATE INDEX IF NOT EXISTS idx_cooc_1 ON entity_cooccurrences(entity_id_1);
CREATE INDEX IF NOT EXISTS idx_cooc_2 ON entity_cooccurrences(entity_id_2);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    PRIMARY KEY (fact_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_fact_entities_entity ON fact_entities(entity_id);

CREATE TABLE IF NOT EXISTS fact_links (
    source_id TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    link_type TEXT NOT NULL,
    strength  REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (source_id, target_id, link_type)
);
CREATE INDEX IF NOT EXISTS idx_fact_links_source ON fact_links(source_id);
CREATE INDEX IF NOT EXISTS idx_fact_links_target ON fact_links(target_id);

CREATE TABLE IF NOT EXISTS mental_models (
    model_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL,
    summary         TEXT,
    source_fact_ids TEXT NOT NULL DEFAULT '[]',
    tags            TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mental_models_name ON mental_models(name);
"""

_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    fact_id UNINDEXED,
    fact,
    content='facts',
    content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, fact_id, fact) VALUES (new.rowid, new.fact_id, new.fact);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, fact_id, fact)
    VALUES ('delete', old.rowid, old.fact_id, old.fact);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, fact_id, fact)
    VALUES ('delete', old.rowid, old.fact_id, old.fact);
    INSERT INTO facts_fts(rowid, fact_id, fact) VALUES (new.rowid, new.fact_id, new.fact);
END;
"""

_LANCEDB_TABLE    = "fact_vectors"
_LANCEDB_MM_TABLE = "mental_model_vectors"


def ensure_fts(conn: sqlite3.Connection) -> None:
    conn.executescript(_FTS_DDL)
    conn.commit()




# ── Embedders ──────────────────────────────────────────────────────────────────

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class LocalEmbedder:
    """Локальный embedder через sentence-transformers."""

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer
        from huggingface_hub import try_to_load_from_cache
        fully_cached = (
            try_to_load_from_cache(model_name, "model.safetensors") is not None
            or try_to_load_from_cache(model_name, "pytorch_model.bin") is not None
        )
        self._model = SentenceTransformer(model_name, local_files_only=fully_cached)
        self.dimension = self._model.get_sentence_embedding_dimension()

    def encode_query(self, text: str) -> list:
        if hasattr(self._model, "prompts") and "query" in (self._model.prompts or {}):
            return self._model.encode(text, prompt_name="query", normalize_embeddings=True).tolist()
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def encode_texts(self, texts) -> list:
        return self._model.encode(texts, normalize_embeddings=True).tolist()


class GoogleEmbedder:
    """Google Gemini embedder (text-embedding-004) с task_type для асимметричного retrieval."""

    def __init__(self, model: str, api_key: str):
        import httpx
        from google import genai
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        http_options = {"api_version": "v1alpha"}
        if http_client:
            http_options["httpx_client"] = http_client
        self._client = genai.Client(api_key=api_key, http_options=http_options)
        self._model = model
        # Определяем размерность через тестовый запрос
        result = self._client.models.embed_content(model=model, contents="test")
        self.dimension = len(result.embeddings[0].values)

    def encode_query(self, text: str) -> list:
        result = self._client.models.embed_content(
            model=self._model,
            contents=text,
            config={"task_type": "RETRIEVAL_QUERY"},
        )
        return list(result.embeddings[0].values)

    def encode_texts(self, texts) -> list:
        if isinstance(texts, str):
            texts = [texts]
        batch_size = 100
        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            result = self._client.models.embed_content(
                model=self._model,
                contents=batch,
                config={"task_type": "RETRIEVAL_DOCUMENT"},
            )
            all_embeddings.extend(list(e.values) for e in result.embeddings)
        return all_embeddings


def _make_embedder(embedding_model) -> "LocalEmbedder | GoogleEmbedder":
    """Создаёт embedder по конфигу: строка → LocalEmbedder, dict → по provider."""
    if isinstance(embedding_model, str):
        return LocalEmbedder(embedding_model or DEFAULT_EMBEDDING_MODEL)
    provider = embedding_model.get("provider", "local")
    if provider == "google":
        return GoogleEmbedder(
            model=embedding_model.get("model", "models/text-embedding-004"),
            api_key=embedding_model["api_key"],
        )
    return LocalEmbedder(embedding_model.get("model", DEFAULT_EMBEDDING_MODEL))


# ── Storage class ──────────────────────────────────────────────────────────────


class Storage:
    """
    Фасад над SQLite + LanceDB.

    Хранит:
      conn       — SQLite соединение
      table      — LanceDB таблица векторов фактов
      mm_table   — LanceDB таблица векторов mental models
      embed_dim  — размерность эмбеддингов (определяется при загрузке модели)

    Методы эмбеддинга:
      encode_query(text)  — вектор поискового запроса (с query prompt)
      encode_texts(texts) — батч векторов для фактов/документов

    При смене embedding_model нужно пересоздать БД.
    """

    # ── Init ─────────────────────────────────────────────────────────────────────

    def __init__(self, sqlite_path: str, lancedb_path: str, embedding_model=None):
        self._embedder = _make_embedder(embedding_model or DEFAULT_EMBEDDING_MODEL)
        self._lancedb_path = lancedb_path
        self.embed_dim = self._embedder.dimension
        log.info("[storage] embedder loaded: %s, dim=%d", type(self._embedder).__name__, self.embed_dim)

        self.conn = self._open_sqlite(sqlite_path)
        self.table, self.mm_table = self._open_lancedb(lancedb_path)

    # ── Embedding ────────────────────────────────────────────────────────────────

    def encode_query(self, text: str) -> list:
        """Вектор поискового запроса (с query task_type/prompt если поддерживается)."""
        return self._embedder.encode_query(text)

    def encode_texts(self, texts) -> list:
        """Батч-кодирование фактов/наблюдений/описаний (str или list[str])."""
        return self._embedder.encode_texts(texts)

    # ── Init helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _open_sqlite(db_path: str) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_DDL)
        conn.commit()
        ensure_fts(conn)
        log.info("[storage] SQLite ready: %s", db_path)
        return conn

    def _open_lancedb(self, db_path: str):
        os.makedirs(db_path, exist_ok=True)
        db = lancedb.connect(db_path)
        existing = db.table_names()

        if _LANCEDB_TABLE in existing:
            table = db.open_table(_LANCEDB_TABLE)
        else:
            table = db.create_table(_LANCEDB_TABLE, schema=pa.schema([
                pa.field("fact_id",      pa.string()),
                pa.field("mentioned_at", pa.string()),
                pa.field("vector",       pa.list_(pa.float32(), self.embed_dim)),
            ]))

        if _LANCEDB_MM_TABLE in existing:
            mm_table = db.open_table(_LANCEDB_MM_TABLE)
        else:
            mm_table = db.create_table(_LANCEDB_MM_TABLE, schema=pa.schema([
                pa.field("model_id", pa.string()),
                pa.field("vector",   pa.list_(pa.float32(), self.embed_dim)),
            ]))

        log.info("[storage] LanceDB ready: %s", db_path)
        return table, mm_table

    # ── Chunks ──────────────────────────────────────────────────────────────────

    def insert_chunks(
        self,
        document_id: str,
        chunks: list[tuple[int, str]],
    ) -> dict[int, str]:
        """Вставляет чанки документа. Возвращает {chunk_index: chunk_id}."""
        chunk_id_map = {}
        for chunk_index, chunk_text in chunks:
            chunk_id = f"{document_id}_{chunk_index}"
            self.conn.execute(
                "INSERT OR IGNORE INTO chunks (chunk_id, document_id, chunk_index, chunk_text) VALUES (?, ?, ?, ?)",
                (chunk_id, document_id, chunk_index, chunk_text),
            )
            chunk_id_map[chunk_index] = chunk_id
        self.conn.commit()
        return chunk_id_map

    # ── Facts ───────────────────────────────────────────────────────────────────

    def insert_facts(self, facts) -> None:
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO facts
                (fact_id, fact, fact_type, occurred_start, occurred_end, mentioned_at,
                 document_id, chunk_id, source_fact_ids, context, tags, is_real_document)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (f.fact_id, f.fact, f.fact_type,
                 f.occurred_start, f.occurred_end, f.mentioned_at, f.document_id,
                 getattr(f, "chunk_id", None),
                 json.dumps(getattr(f, "source_fact_ids", None) or None),
                 getattr(f, "context", None) or None,
                 json.dumps(getattr(f, "tags", None) or []),
                 int(getattr(f, "is_real_document", False)))
                for f in facts
            ],
        )
        self.conn.commit()

    def mark_consolidated(self, fact_ids: list[str]) -> None:
        if not fact_ids:
            return
        ph = ",".join("?" * len(fact_ids))
        self.conn.execute(f"UPDATE facts SET consolidated = 1 WHERE fact_id IN ({ph})", fact_ids)
        self.conn.commit()

    def get_pending_consolidation_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM facts WHERE consolidated = 0 AND fact_type IN ('world', 'experience')"
        ).fetchone()
        return row["cnt"] if row else 0

    def get_unconsolidated_facts(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM facts
            WHERE consolidated = 0 AND fact_type IN ('world', 'experience')
            ORDER BY mentioned_at ASC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def get_facts_by_ids(self, fact_ids: list[str]) -> list[sqlite3.Row]:
        if not fact_ids:
            return []
        ph = ",".join("?" * len(fact_ids))
        return self.conn.execute(f"SELECT * FROM facts WHERE fact_id IN ({ph})", fact_ids).fetchall()

    def get_facts_by_entity(self, entity_name: str, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT f.* FROM facts f
            JOIN fact_entities fe ON fe.fact_id = f.fact_id
            JOIN entities e ON e.entity_id = fe.entity_id
            WHERE e.name = ? ORDER BY f.mentioned_at DESC LIMIT ?
            """,
            (entity_name, limit),
        ).fetchall()

    def get_facts_in_time_window(
        self,
        window_start: str,
        window_end: str,
        exclude_fact_id: str,
        limit: int = 10,
    ) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT fact_id, mentioned_at FROM facts
            WHERE mentioned_at BETWEEN ? AND ? AND fact_id != ?
            LIMIT ?
            """,
            (window_start, window_end, exclude_fact_id, limit),
        ).fetchall()

    # ── Entities ─────────────────────────────────────────────────────────────────

    def upsert_entities(self, facts) -> None:
        """Canonical resolution через EntityResolver (exact → alias → substring → fuzzy+cooc)."""
        from src.memory.providers.fact.entity_resolver import EntityResolver

        resolver = EntityResolver(self.conn)
        for fact in facts:
            names = [n.strip() for n in fact.entities if n.strip()]
            if not names:
                continue
            resolved = resolver.resolve_batch(names)
            for entity_id in resolved.values():
                self.conn.execute(
                    "INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)",
                    (fact.fact_id, entity_id),
                )
        self.conn.commit()
        self.update_cooccurrences(facts)

    def update_cooccurrences(self, facts) -> None:
        fact_ids = [f.fact_id for f in facts]
        if not fact_ids:
            return
        ph = ",".join("?" * len(fact_ids))
        rows = self.conn.execute(
            f"SELECT fact_id, entity_id FROM fact_entities WHERE fact_id IN ({ph})", fact_ids
        ).fetchall()

        fact_entity_map: dict[str, list[str]] = {}
        for r in rows:
            fact_entity_map.setdefault(r["fact_id"], []).append(r["entity_id"])

        pairs = []
        for eids in fact_entity_map.values():
            eids_sorted = sorted(set(eids))
            for i, a in enumerate(eids_sorted):
                for b in eids_sorted[i + 1:]:
                    pairs.append((a, b))

        if not pairs:
            return
        self.conn.executemany(
            """
            INSERT INTO entity_cooccurrences (entity_id_1, entity_id_2, count) VALUES (?, ?, 1)
            ON CONFLICT(entity_id_1, entity_id_2) DO UPDATE SET count = count + 1
            """,
            pairs,
        )
        self.conn.commit()

    def get_cooccurrence_map(self, entity_ids: list[str]) -> dict[str, set[str]]:
        if not entity_ids:
            return {}
        ph = ",".join("?" * len(entity_ids))
        rows = self.conn.execute(
            f"""
            SELECT entity_id_1, entity_id_2 FROM entity_cooccurrences
            WHERE entity_id_1 IN ({ph}) OR entity_id_2 IN ({ph})
            """,
            [*entity_ids, *entity_ids],
        ).fetchall()
        result: dict[str, set[str]] = {eid: set() for eid in entity_ids}
        for r in rows:
            e1, e2 = r["entity_id_1"], r["entity_id_2"]
            if e1 in result:
                result[e1].add(e2)
            if e2 in result:
                result[e2].add(e1)
        return result

    def build_entity_links(self, facts, max_links_per_entity: int = 50) -> int:
        if not facts:
            return 0
        fact_ids = [f.fact_id for f in facts]
        ph = ",".join("?" * len(fact_ids))
        entity_rows = self.conn.execute(
            f"SELECT DISTINCT entity_id FROM fact_entities WHERE fact_id IN ({ph})", fact_ids
        ).fetchall()

        links = []
        new_fact_set = set(fact_ids)
        for entity_row in entity_rows:
            all_with_entity = self.conn.execute(
                "SELECT fact_id FROM fact_entities WHERE entity_id = ?", (entity_row["entity_id"],)
            ).fetchall()
            all_ids = [r["fact_id"] for r in all_with_entity]
            new_ids = [fid for fid in all_ids if fid in new_fact_set]
            old_ids = [fid for fid in all_ids if fid not in new_fact_set][-max_links_per_entity:]
            for i, a in enumerate(new_ids):
                for b in new_ids[i + 1:]:
                    links.extend([(a, b, "entity", 1.0), (b, a, "entity", 1.0)])
            for new_id in new_ids:
                for old_id in old_ids:
                    links.extend([(new_id, old_id, "entity", 1.0), (old_id, new_id, "entity", 1.0)])

        if links:
            self.conn.executemany(
                "INSERT OR IGNORE INTO fact_links (source_id, target_id, link_type, strength) VALUES (?, ?, ?, ?)",
                links,
            )
            self.conn.commit()
        return len(links)

    # ── Links ────────────────────────────────────────────────────────────────────

    def insert_links(self, links: list[tuple[str, str, str, float]]) -> None:
        if not links:
            return
        self.conn.executemany(
            "INSERT OR IGNORE INTO fact_links (source_id, target_id, link_type, strength) VALUES (?, ?, ?, ?)",
            links,
        )
        self.conn.commit()

    def get_linked_facts(
        self,
        fact_ids: list[str],
        link_types: tuple[str, ...] = ("semantic", "temporal"),
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        if not fact_ids:
            return []
        ph  = ",".join("?" * len(fact_ids))
        tph = ",".join("?" * len(link_types))
        return self.conn.execute(
            f"""
            SELECT DISTINCT f.* FROM facts f
            JOIN fact_links fl ON fl.target_id = f.fact_id OR fl.source_id = f.fact_id
            WHERE (fl.source_id IN ({ph}) OR fl.target_id IN ({ph}))
              AND fl.link_type IN ({tph})
              AND f.fact_id NOT IN ({ph})
            ORDER BY fl.strength DESC LIMIT ?
            """,
            [*fact_ids, *fact_ids, *link_types, *fact_ids, limit],
        ).fetchall()

    # ── Vectors (fact_vectors) ───────────────────────────────────────────────────

    def search_vectors(self, query_vec, limit: int) -> list:
        try:
            if self.table.count_rows() == 0:
                return []
        except Exception:
            return []
        try:
            return self.table.search(query_vec).limit(limit).to_list()
        except Exception as e:
            log.warning("[storage] search_vectors failed: %s", e, exc_info=True)
            return []

    def insert_vectors(self, rows: list[dict]) -> None:
        if rows:
            try:
                self.table.add(rows)
            except Exception as e:
                log.warning("[storage] insert_vectors failed: %s", e, exc_info=True)

    # ── Mental models ────────────────────────────────────────────────────────────

    def upsert_mental_model(self, mm) -> None:
        """Создаёт или обновляет mental model (SQLite + LanceDB)."""
        row = {
            "model_id":        mm.model_id,
            "name":            mm.name,
            "description":     mm.description,
            "summary":         mm.summary,
            "source_fact_ids": json.dumps(mm.source_fact_ids),
            "tags":            json.dumps(mm.tags),
            "created_at":      mm.created_at,
            "updated_at":      mm.updated_at,
        }
        cols    = ", ".join(row)
        vals    = ", ".join(f":{k}" for k in row)
        updates = ", ".join(f"{k} = excluded.{k}" for k in row if k != "model_id")
        self.conn.execute(
            f"INSERT INTO mental_models ({cols}) VALUES ({vals}) "
            f"ON CONFLICT(model_id) DO UPDATE SET {updates}",
            row,
        )
        self.conn.commit()
        vec = self.encode_texts([mm.description])[0]
        try:
            self.mm_table.delete(f"model_id = '{mm.model_id}'")
        except Exception:
            pass
        self.mm_table.add([{"model_id": mm.model_id, "vector": vec}])

    def delete_mental_model(self, model_id: str) -> bool:
        """Удаляет mental model из SQLite и LanceDB. Возвращает True если был найден."""
        row = self.conn.execute(
            "SELECT model_id FROM mental_models WHERE model_id = ?", (model_id,)
        ).fetchone()
        if not row:
            return False
        self.conn.execute("DELETE FROM mental_models WHERE model_id = ?", (model_id,))
        self.conn.commit()
        try:
            self.mm_table.delete(f"model_id = '{model_id}'")
        except Exception:
            pass
        return True

    def get_all_mental_models(self) -> list:
        """Возвращает все mental models как list[MentalModel]."""
        from src.memory.providers.fact.reflect import MentalModel
        rows = self.conn.execute(
            "SELECT * FROM mental_models ORDER BY updated_at DESC"
        ).fetchall()
        return [self._mm_from_row(r) for r in rows]

    def search_mental_models(self, query_vec, limit: int = 5, tags=None,
                             threshold: float = 0.3) -> list:
        """Векторный поиск по description. Возвращает list[MentalModel] с relevance."""
        try:
            if self.mm_table.count_rows() == 0:
                return []
        except Exception:
            return []

        hits = self.mm_table.search(query_vec).limit(limit * 2).to_list()
        similarity = {h["model_id"]: 1.0 - h.get("_distance", 1.0) for h in hits}
        model_ids  = [h["model_id"] for h in hits if similarity[h["model_id"]] >= threshold]
        if not model_ids:
            return []

        ph   = ",".join("?" * len(model_ids))
        rows = self.conn.execute(
            f"SELECT * FROM mental_models WHERE model_id IN ({ph})", model_ids
        ).fetchall()
        row_map = {r["model_id"]: r for r in rows}

        results = []
        for mid in model_ids:
            if mid not in row_map:
                continue
            mm = self._mm_from_row(row_map[mid], relevance=round(similarity[mid], 4))
            if tags and not any(t in mm.tags for t in tags):
                continue
            results.append(mm)

        return sorted(results, key=lambda m: m.relevance, reverse=True)[:limit]

    # ── Reindex ──────────────────────────────────────────────────────────────────

    def reindex(self) -> None:
        """
        Пересчитывает все эмбеддинги в LanceDB с текущей моделью.
        Нужно вызвать после смены embedding_model — пересоздаёт таблицы с новой размерностью.
        """
        import shutil
        from src.memory.providers.fact.retain import augment_text_for_embedding, Fact

        log.info("[storage] reindex: dropping LanceDB tables at %s", self._lancedb_path)
        shutil.rmtree(self._lancedb_path, ignore_errors=True)
        self.table, self.mm_table = self._open_lancedb(self._lancedb_path)

        # факты
        rows = self.conn.execute(
            "SELECT fact_id, fact, fact_type, occurred_start, mentioned_at FROM facts"
        ).fetchall()
        if rows:
            facts = [
                Fact(
                    fact_id=r["fact_id"],
                    fact=r["fact"],
                    fact_type=r["fact_type"],
                    occurred_start=r["occurred_start"],
                    mentioned_at=r["mentioned_at"],
                )
                for r in rows
            ]
            augmented = [augment_text_for_embedding(f) for f in facts]
            vectors = self.encode_texts(augmented)
            self.table.add([
                {"fact_id": f.fact_id, "mentioned_at": f.mentioned_at, "vector": v}
                for f, v in zip(facts, vectors)
            ])
            log.info("[storage] reindex: indexed %d facts", len(facts))

        # mental models
        mm_rows = self.conn.execute("SELECT * FROM mental_models").fetchall()
        if mm_rows:
            mm_list = [self._mm_from_row(r) for r in mm_rows]
            mm_vecs = self.encode_texts([m.description for m in mm_list])
            self.mm_table.add([
                {"model_id": m.model_id, "vector": v}
                for m, v in zip(mm_list, mm_vecs)
            ])
            log.info("[storage] reindex: indexed %d mental models", len(mm_list))

        log.info("[storage] reindex done, model=%s dim=%d", self._embedder, self.embed_dim)

    def _mm_from_row(self, row, relevance: float = 0.0):
        from src.memory.providers.fact.reflect import MentalModel
        return MentalModel(
            model_id=row["model_id"],
            name=row["name"],
            description=row["description"],
            summary=row["summary"],
            source_fact_ids=json.loads(row["source_fact_ids"] or "[]"),
            tags=json.loads(row["tags"] or "[]"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            relevance=relevance,
        )

