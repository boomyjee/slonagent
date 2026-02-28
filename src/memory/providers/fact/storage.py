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
from typing import Callable, Optional

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

class Storage:
    """
    Фасад над SQLite + LanceDB.

    Хранит:
      conn      — SQLite соединение
      table     — LanceDB таблица векторов фактов
      mm_table  — LanceDB таблица векторов mental models
      embed_fn  — функция получения эмбеддинга: str → list[float]
    """

    def __init__(
        self,
        sqlite_path: str,
        lancedb_path: str,
        embed_fn: Callable[[str], list],
        dim: int,
    ):
        self.embed_fn = embed_fn
        self.conn     = self._open_sqlite(sqlite_path)
        self.table, self.mm_table = self._open_lancedb(lancedb_path, dim)

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
    def _open_lancedb(db_path: str, dim: int):
        os.makedirs(db_path, exist_ok=True)
        db = lancedb.connect(db_path)

        fact_schema = pa.schema([
            pa.field("fact_id",      pa.string()),
            pa.field("mentioned_at", pa.string()),
            pa.field("vector",       pa.list_(pa.float32(), dim)),
        ])
        mm_schema = pa.schema([
            pa.field("model_id", pa.string()),
            pa.field("vector",   pa.list_(pa.float32(), dim)),
        ])

        table = (
            db.open_table(_LANCEDB_TABLE)
            if _LANCEDB_TABLE in db.table_names()
            else db.create_table(_LANCEDB_TABLE, schema=fact_schema)
        )
        mm_table = (
            db.open_table(_LANCEDB_MM_TABLE)
            if _LANCEDB_MM_TABLE in db.table_names()
            else db.create_table(_LANCEDB_MM_TABLE, schema=mm_schema)
        )
        log.info("[storage] LanceDB ready: %s (dim=%d)", db_path, dim)
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
                 document_id, chunk_id, source_fact_ids, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (f.fact_id, f.fact, f.fact_type,
                 f.occurred_start, f.occurred_end, f.mentioned_at, f.document_id,
                 getattr(f, "chunk_id", None),
                 json.dumps(getattr(f, "source_fact_ids", None) or None),
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
        return self.table.search(query_vec).limit(limit).to_list()

    def insert_vectors(self, rows: list[dict]) -> None:
        if rows:
            self.table.add(rows)

    # ── Vectors (mental_model_vectors) ───────────────────────────────────────────

    def search_mm_vectors(self, query_vec, limit: int) -> list:
        try:
            if self.mm_table.count_rows() == 0:
                return []
        except Exception:
            return []
        return self.mm_table.search(query_vec).limit(limit).to_list()

    def insert_mm_vector(self, model_id: str, vec) -> None:
        self.mm_table.add([{"model_id": model_id, "vector": vec}])

    def delete_mm_vector(self, model_id: str) -> None:
        try:
            self.mm_table.delete(f"model_id = '{model_id}'")
        except Exception:
            pass

    # ── Mental models ────────────────────────────────────────────────────────────

    def get_mental_model_rows(self, model_ids: list[str]) -> list[sqlite3.Row]:
        if not model_ids:
            return []
        ph = ",".join("?" * len(model_ids))
        return self.conn.execute(
            f"SELECT * FROM mental_models WHERE model_id IN ({ph})", model_ids
        ).fetchall()

    def insert_mental_model(self, row: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO mental_models
                (model_id, name, description, summary, source_fact_ids, tags, created_at, updated_at)
            VALUES (:model_id, :name, :description, :summary,
                    :source_fact_ids, :tags, :created_at, :updated_at)
            """,
            row,
        )
        self.conn.commit()

    def update_mental_model_row(self, model_id: str, row: dict) -> None:
        self.conn.execute(
            """
            UPDATE mental_models
            SET description = :description, summary = :summary,
                source_fact_ids = :source_fact_ids, tags = :tags, updated_at = :updated_at
            WHERE model_id = :model_id
            """,
            {**row, "model_id": model_id},
        )
        self.conn.commit()

    def delete_mental_model_row(self, model_id: str) -> None:
        self.conn.execute("DELETE FROM mental_models WHERE model_id = ?", (model_id,))
        self.conn.commit()

    def get_all_mental_model_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM mental_models ORDER BY updated_at DESC"
        ).fetchall()
