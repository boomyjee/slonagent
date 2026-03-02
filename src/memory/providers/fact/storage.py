"""storage.py — Storage: фасад над SQLite + LanceDB для FactProvider.

Схема SQLite:
  facts                — основная таблица фактов (world | experience | observation)
  entities             — уникальные сущности (люди, места, организации)
  entity_aliases       — fuzzy-resolved псевдонимы → canonical entity_id
  entity_cooccurrences — счётчики совместной встречаемости сущностей
  fact_entities        — M:N связь факт ↔ сущность
  fact_links           — рёбра графа (temporal / semantic / causal / entity)
  observation_evidence — ссылки observation → source_facts с цитатами
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
    consolidated    INTEGER NOT NULL DEFAULT 0,
    tags            TEXT NOT NULL DEFAULT '[]'
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

CREATE TABLE IF NOT EXISTS observation_evidence (
    observation_id TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    source_fact_id TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    quote          TEXT NOT NULL,
    relevance      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (observation_id, source_fact_id)
);
CREATE INDEX IF NOT EXISTS idx_obs_evidence_source ON observation_evidence(source_fact_id);

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




# ── Storage class ──────────────────────────────────────────────────────────────

EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
EMBEDDING_DIM   = 1024   # размерность Qwen3-Embedding-0.6B


class Storage:
    """
    Фасад над SQLite + LanceDB.

    Хранит:
      conn     — SQLite соединение
      table    — LanceDB таблица векторов фактов
      mm_table — LanceDB таблица векторов mental models

    Статические методы для эмбеддинга (lazy singleton):
      encode_query(text)  — вектор поискового запроса (с query prompt)
      encode_texts(texts) — батч векторов для фактов/документов

    Модель фиксирована константой EMBEDDING_MODEL / EMBEDDING_DIM.
    """

    _embed_model = None

    # ── Embedding (lazy singleton) ───────────────────────────────────────────────

    @staticmethod
    def _get_embed_model():
        if Storage._embed_model is None:
            from sentence_transformers import SentenceTransformer
            from huggingface_hub import try_to_load_from_cache
            cached = try_to_load_from_cache(EMBEDDING_MODEL, "config.json") is not None
            Storage._embed_model = SentenceTransformer(EMBEDDING_MODEL, local_files_only=cached)
            log.info(
                "[storage] embedding model loaded: %s, dim=%d",
                EMBEDDING_MODEL,
                Storage._embed_model.get_sentence_embedding_dimension(),
            )
        return Storage._embed_model

    @staticmethod
    def encode_query(text: str) -> list:
        """Вектор поискового запроса (с query prompt если поддерживается)."""
        model = Storage._get_embed_model()
        if hasattr(model, "prompts") and "query" in (model.prompts or {}):
            return model.encode(text, prompt_name="query", normalize_embeddings=True).tolist()
        return model.encode(text, normalize_embeddings=True).tolist()

    @staticmethod
    def encode_texts(texts) -> list:
        """Батч-кодирование фактов/наблюдений/описаний (str или list[str])."""
        return Storage._get_embed_model().encode(texts, normalize_embeddings=True).tolist()

    # ── Init ─────────────────────────────────────────────────────────────────────

    def __init__(self, sqlite_path: str, lancedb_path: str):
        self.conn = self._open_sqlite(sqlite_path)
        self.table, self.mm_table = self._open_lancedb(lancedb_path)

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

    @staticmethod
    def _open_lancedb(db_path: str):
        os.makedirs(db_path, exist_ok=True)
        db = lancedb.connect(db_path)
        existing = db.table_names()

        if _LANCEDB_TABLE in existing:
            table = db.open_table(_LANCEDB_TABLE)
        else:
            table = db.create_table(_LANCEDB_TABLE, schema=pa.schema([
                pa.field("fact_id",      pa.string()),
                pa.field("mentioned_at", pa.string()),
                pa.field("vector",       pa.list_(pa.float32(), EMBEDDING_DIM)),
            ]))

        if _LANCEDB_MM_TABLE in existing:
            mm_table = db.open_table(_LANCEDB_MM_TABLE)
        else:
            mm_table = db.create_table(_LANCEDB_MM_TABLE, schema=pa.schema([
                pa.field("model_id", pa.string()),
                pa.field("vector",   pa.list_(pa.float32(), EMBEDDING_DIM)),
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
                 document_id, chunk_id, source_fact_ids, context, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (f.fact_id, f.fact, f.fact_type,
                 f.occurred_start, f.occurred_end, f.mentioned_at, f.document_id,
                 getattr(f, "chunk_id", None),
                 json.dumps(getattr(f, "source_fact_ids", None) or None),
                 getattr(f, "context", None) or None,
                 json.dumps(getattr(f, "tags", None) or []))
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
            SELECT fact_id FROM facts
            WHERE mentioned_at BETWEEN ? AND ? AND fact_id != ?
            LIMIT ?
            """,
            (window_start, window_end, exclude_fact_id, limit),
        ).fetchall()

    def insert_observation_evidence(self, observation_id: str, evidence: list[dict]) -> None:
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO observation_evidence
                (observation_id, source_fact_id, quote, relevance)
            VALUES (?, ?, ?, ?)
            """,
            [(observation_id, e["fact_id"], e["quote"], e.get("relevance", "")) for e in evidence],
        )
        self.conn.commit()

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
            log.warning("[storage] search_vectors failed: %s", e)
            return []

    def insert_vectors(self, rows: list[dict]) -> None:
        if rows:
            self.table.add(rows)

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
        vec = Storage.encode_texts([mm.description])[0]
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

