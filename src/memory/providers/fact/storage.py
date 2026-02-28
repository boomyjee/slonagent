"""storage.py — инициализация SQLite и LanceDB для FactProvider.

SQLite хранит метаданные, граф сущностей и связи между фактами.
LanceDB хранит только векторы (ссылка на факт через fact_id / model_id).

Схема SQLite:
  facts                — основная таблица фактов (world | experience | observation)
  entities             — уникальные сущности (люди, места, организации)
  entity_aliases       — fuzzy-resolved псевдонимы → canonical entity_id
  fact_entities        — M:N связь факт ↔ сущность
  fact_links           — рёбра графа (temporal / semantic / causal / entity)
  observation_evidence — ссылки observation → source_facts с цитатами
  mental_models        — user-created/auto curated summaries (highest priority)
"""
import logging
import os
import sqlite3

import lancedb
import numpy as np
import pyarrow as pa

log = logging.getLogger(__name__)

# ── SQLite DDL ─────────────────────────────────────────────────────────────────

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
    fact_type       TEXT NOT NULL DEFAULT 'world',   -- world | experience | observation
    occurred_start  TEXT,
    occurred_end    TEXT,
    mentioned_at    TEXT,
    document_id     TEXT,
    chunk_id        TEXT REFERENCES chunks(chunk_id) ON DELETE SET NULL,
    source_fact_ids TEXT,   -- JSON array, только для fact_type='observation'
    consolidated    INTEGER NOT NULL DEFAULT 0,  -- 1 = включён в observation
    tags            TEXT NOT NULL DEFAULT '[]'   -- JSON array of strings
);

CREATE INDEX IF NOT EXISTS idx_facts_mentioned_at    ON facts(mentioned_at);
CREATE INDEX IF NOT EXISTS idx_facts_occurred_start  ON facts(occurred_start);
CREATE INDEX IF NOT EXISTS idx_facts_document_id     ON facts(document_id);
CREATE INDEX IF NOT EXISTS idx_facts_consolidated    ON facts(consolidated);
CREATE INDEX IF NOT EXISTS idx_facts_fact_type       ON facts(fact_type);

CREATE TABLE IF NOT EXISTS entities (
    entity_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE   -- canonical: case-insensitive
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias       TEXT PRIMARY KEY,  -- нижний регистр
    entity_id   TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_fact_entities_entity ON fact_entities(entity_id);

CREATE TABLE IF NOT EXISTS fact_links (
    source_id   TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    target_id   TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    link_type   TEXT NOT NULL,   -- temporal | semantic | causal | entity
    strength    REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (source_id, target_id, link_type)
);

CREATE INDEX IF NOT EXISTS idx_fact_links_source ON fact_links(source_id);
CREATE INDEX IF NOT EXISTS idx_fact_links_target ON fact_links(target_id);

CREATE TABLE IF NOT EXISTS observation_evidence (
    observation_id  TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    source_fact_id  TEXT NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    quote           TEXT NOT NULL,
    relevance       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (observation_id, source_fact_id)
);

CREATE INDEX IF NOT EXISTS idx_obs_evidence_source ON observation_evidence(source_fact_id);

CREATE TABLE IF NOT EXISTS entity_cooccurrences (
    entity_id_1 TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    entity_id_2 TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    count       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (entity_id_1, entity_id_2),
    CHECK (entity_id_1 < entity_id_2)   -- нормализация: всегда id1 < id2
);

CREATE INDEX IF NOT EXISTS idx_cooc_1 ON entity_cooccurrences(entity_id_1);
CREATE INDEX IF NOT EXISTS idx_cooc_2 ON entity_cooccurrences(entity_id_2);

CREATE TABLE IF NOT EXISTS mental_models (
    model_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL,  -- one-liner для быстрого поиска
    summary         TEXT,           -- полный синтезированный текст
    source_fact_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array
    tags            TEXT NOT NULL DEFAULT '[]',  -- JSON array
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mental_models_name ON mental_models(name);
"""

# ── LanceDB table schemas ──────────────────────────────────────────────────────

LANCEDB_TABLE        = "fact_vectors"
LANCEDB_MM_TABLE     = "mental_model_vectors"


def _vector_schema(dim: int) -> pa.Schema:
    return pa.schema([
        pa.field("fact_id",      pa.string()),
        pa.field("mentioned_at", pa.string()),  # нужен для dedup time-window check
        pa.field("vector",       pa.list_(pa.float32(), dim)),
    ])


def _mm_vector_schema(dim: int) -> pa.Schema:
    return pa.schema([
        pa.field("model_id",  pa.string()),
        pa.field("vector",    pa.list_(pa.float32(), dim)),
    ])


# ── FTS5 DDL ───────────────────────────────────────────────────────────────────

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
    INSERT INTO facts_fts(facts_fts, rowid, fact_id, fact) VALUES ('delete', old.rowid, old.fact_id, old.fact);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, fact_id, fact) VALUES ('delete', old.rowid, old.fact_id, old.fact);
    INSERT INTO facts_fts(rowid, fact_id, fact) VALUES (new.rowid, new.fact_id, new.fact);
END;
"""


def ensure_fts(conn: sqlite3.Connection) -> None:
    conn.executescript(_FTS_DDL)
    conn.commit()


# ── Init ───────────────────────────────────────────────────────────────────────

def init_sqlite(db_path: str) -> sqlite3.Connection:
    """Открывает (или создаёт) SQLite базу, применяет DDL и FTS5 триггеры."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()
    ensure_fts(conn)
    log.info("[storage] SQLite ready: %s", db_path)
    return conn


def init_lancedb(db_path: str, dim: int):
    """Открывает (или создаёт) LanceDB таблицы под векторы."""
    os.makedirs(db_path, exist_ok=True)
    db = lancedb.connect(db_path)

    if LANCEDB_TABLE in db.table_names():
        table = db.open_table(LANCEDB_TABLE)
    else:
        table = db.create_table(LANCEDB_TABLE, schema=_vector_schema(dim))
        log.info("[storage] LanceDB fact_vectors created, dim=%d", dim)

    if LANCEDB_MM_TABLE in db.table_names():
        mm_table = db.open_table(LANCEDB_MM_TABLE)
    else:
        mm_table = db.create_table(LANCEDB_MM_TABLE, schema=_mm_vector_schema(dim))
        log.info("[storage] LanceDB mental_model_vectors created, dim=%d", dim)

    return table, mm_table


# ── Chunks ─────────────────────────────────────────────────────────────────────

def insert_chunks(
    conn: sqlite3.Connection,
    document_id: str,
    chunks: list[tuple[int, str]],  # [(chunk_index, chunk_text), ...]
) -> dict[int, str]:
    """Вставляет чанки документа. Возвращает {chunk_index: chunk_id}."""
    chunk_id_map = {}
    for chunk_index, chunk_text in chunks:
        chunk_id = f"{document_id}_{chunk_index}"
        conn.execute(
            "INSERT OR IGNORE INTO chunks (chunk_id, document_id, chunk_index, chunk_text) VALUES (?, ?, ?, ?)",
            (chunk_id, document_id, chunk_index, chunk_text),
        )
        chunk_id_map[chunk_index] = chunk_id
    conn.commit()
    return chunk_id_map


# ── Facts ──────────────────────────────────────────────────────────────────────

def insert_facts(conn: sqlite3.Connection, facts) -> None:
    """Вставляет факты в SQLite (игнорирует дубли по fact_id)."""
    import json as _json
    conn.executemany(
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
             _json.dumps(getattr(f, "source_fact_ids", None) or None),
             _json.dumps(getattr(f, "tags", None) or []))
            for f in facts
        ],
    )
    conn.commit()


def mark_consolidated(conn: sqlite3.Connection, fact_ids: list[str]) -> None:
    """Помечает world/experience факты как включённые в наблюдения."""
    if not fact_ids:
        return
    placeholders = ",".join("?" * len(fact_ids))
    conn.execute(
        f"UPDATE facts SET consolidated = 1 WHERE fact_id IN ({placeholders})",
        fact_ids,
    )
    conn.commit()


def get_pending_consolidation_count(conn: sqlite3.Connection) -> int:
    """Количество world/experience фактов, ещё не включённых в observations."""
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM facts WHERE consolidated = 0 AND fact_type IN ('world', 'experience')"
    ).fetchone()
    return row["cnt"] if row else 0


def get_unconsolidated_facts(
    conn: sqlite3.Connection,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Возвращает world/experience факты, ещё не включённые ни в одно наблюдение."""
    return conn.execute(
        """
        SELECT * FROM facts
        WHERE consolidated = 0
          AND fact_type IN ('world', 'experience')
        ORDER BY mentioned_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def insert_observation_evidence(
    conn: sqlite3.Connection,
    observation_id: str,
    evidence: list[dict],  # [{"fact_id", "quote", "relevance"}]
) -> None:
    conn.executemany(
        """
        INSERT OR IGNORE INTO observation_evidence
            (observation_id, source_fact_id, quote, relevance)
        VALUES (?, ?, ?, ?)
        """,
        [(observation_id, e["fact_id"], e["quote"], e.get("relevance", "")) for e in evidence],
    )
    conn.commit()


def get_facts_by_ids(conn: sqlite3.Connection, fact_ids: list[str]) -> list[sqlite3.Row]:
    """Возвращает факты по списку fact_id."""
    if not fact_ids:
        return []
    placeholders = ",".join("?" * len(fact_ids))
    return conn.execute(
        f"SELECT * FROM facts WHERE fact_id IN ({placeholders})", fact_ids
    ).fetchall()


# ── Entities ───────────────────────────────────────────────────────────────────

def upsert_entities(conn: sqlite3.Connection, facts) -> None:
    """Вставляет сущности и связи факт↔сущность.

    Canonical resolution через EntityResolver:
      1. COLLATE NOCASE (регистронезависимый exact match)
      2. Alias cache (ранее fuzzy-resolved)
      3. Substring match
      4. SequenceMatcher fuzzy ≥ 0.82
    """
    from src.memory.providers.fact.entity_resolver import EntityResolver

    resolver = EntityResolver(conn)
    for fact in facts:
        names = [n.strip() for n in fact.entities if n.strip()]
        if not names:
            continue
        # resolve_batch передаёт каждому имени остальных как nearby → co-occurrence disambiguation
        resolved = resolver.resolve_batch(names)
        for entity_id in resolved.values():
            conn.execute(
                "INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)",
                (fact.fact_id, entity_id),
            )
    conn.commit()
    update_cooccurrences(conn, facts)


def update_cooccurrences(conn: sqlite3.Connection, facts) -> None:
    """
    Обновляет счётчики совместной встречаемости сущностей.

    Для каждого факта берём все его entity_id и инкрементируем
    entity_cooccurrences для каждой пары (id1 < id2).
    Аналог Hindsight entity_cooccurrences таблицы.
    """
    fact_ids = [f.fact_id for f in facts]
    if not fact_ids:
        return

    placeholders = ",".join("?" * len(fact_ids))
    rows = conn.execute(
        f"SELECT fact_id, entity_id FROM fact_entities WHERE fact_id IN ({placeholders})",
        fact_ids,
    ).fetchall()

    # Группируем по факту
    fact_entity_map: dict[str, list[str]] = {}
    for r in rows:
        fact_entity_map.setdefault(r["fact_id"], []).append(r["entity_id"])

    pairs: list[tuple[str, str]] = []
    for eids in fact_entity_map.values():
        eids_sorted = sorted(set(eids))
        for i, a in enumerate(eids_sorted):
            for b in eids_sorted[i + 1:]:
                pairs.append((a, b))  # a < b гарантировано сортировкой

    if not pairs:
        return

    conn.executemany(
        """
        INSERT INTO entity_cooccurrences (entity_id_1, entity_id_2, count)
        VALUES (?, ?, 1)
        ON CONFLICT(entity_id_1, entity_id_2) DO UPDATE SET count = count + 1
        """,
        pairs,
    )
    conn.commit()


def get_cooccurrence_map(
    conn: sqlite3.Connection,
    entity_ids: list[str],
) -> dict[str, set[str]]:
    """
    Возвращает {entity_id: set of co-occurring entity_ids} для переданных entity_ids.
    Используется в EntityResolver для контекстного disambiguation.
    """
    if not entity_ids:
        return {}

    placeholders = ",".join("?" * len(entity_ids))
    rows = conn.execute(
        f"""
        SELECT entity_id_1, entity_id_2 FROM entity_cooccurrences
        WHERE entity_id_1 IN ({placeholders}) OR entity_id_2 IN ({placeholders})
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


def build_entity_links(conn: sqlite3.Connection, facts, max_links_per_entity: int = 50) -> int:
    """
    Создаёт entity links между фактами, разделяющими одну сущность.
    Аналог Hindsight step [6.3]: факты об одном entity связываются bidirectionally.
    """
    if not facts:
        return 0

    fact_ids = [f.fact_id for f in facts]
    placeholders = ",".join("?" * len(fact_ids))

    # Все entity_id для новых фактов
    entity_rows = conn.execute(
        f"SELECT DISTINCT entity_id FROM fact_entities WHERE fact_id IN ({placeholders})",
        fact_ids,
    ).fetchall()

    links = []
    new_fact_set = set(fact_ids)

    for entity_row in entity_rows:
        entity_id = entity_row["entity_id"]

        # Все факты с этой сущностью (включая уже существующие)
        all_with_entity = conn.execute(
            "SELECT fact_id FROM fact_entities WHERE entity_id = ?",
            (entity_id,),
        ).fetchall()

        all_ids = [r["fact_id"] for r in all_with_entity]
        new_ids = [fid for fid in all_ids if fid in new_fact_set]
        old_ids = [fid for fid in all_ids if fid not in new_fact_set][-max_links_per_entity:]

        # Новые ↔ новые
        for i, a in enumerate(new_ids):
            for b in new_ids[i + 1:]:
                links.extend([(a, b, "entity", 1.0), (b, a, "entity", 1.0)])

        # Новые ↔ старые
        for new_id in new_ids:
            for old_id in old_ids:
                links.extend([(new_id, old_id, "entity", 1.0), (old_id, new_id, "entity", 1.0)])

    if links:
        conn.executemany(
            "INSERT OR IGNORE INTO fact_links (source_id, target_id, link_type, strength) VALUES (?, ?, ?, ?)",
            links,
        )
        conn.commit()

    return len(links)


def get_facts_by_entity(conn: sqlite3.Connection, entity_name: str, limit: int = 20) -> list[sqlite3.Row]:
    """Все факты, связанные с данной сущностью."""
    return conn.execute(
        """
        SELECT f.*
        FROM facts f
        JOIN fact_entities fe ON fe.fact_id = f.fact_id
        JOIN entities e ON e.entity_id = fe.entity_id
        WHERE e.name = ?
        ORDER BY f.mentioned_at DESC
        LIMIT ?
        """,
        (entity_name, limit),
    ).fetchall()


# ── Links ──────────────────────────────────────────────────────────────────────

def insert_links(
    conn: sqlite3.Connection,
    links: list[tuple[str, str, str, float]],
) -> None:
    """Вставляет рёбра графа. Каждый элемент: (source_id, target_id, link_type, strength)."""
    if not links:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO fact_links (source_id, target_id, link_type, strength) VALUES (?, ?, ?, ?)",
        links,
    )
    conn.commit()


def get_facts_in_time_window(
    conn: sqlite3.Connection,
    mentioned_at: str,
    window_start: str,
    window_end: str,
    exclude_fact_id: str,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Факты в заданном временном окне (для построения temporal links)."""
    return conn.execute(
        """
        SELECT fact_id FROM facts
        WHERE mentioned_at BETWEEN ? AND ?
          AND fact_id != ?
        LIMIT ?
        """,
        (window_start, window_end, exclude_fact_id, limit),
    ).fetchall()


def get_linked_facts(
    conn: sqlite3.Connection,
    fact_ids: list[str],
    link_types: tuple[str, ...] = ("semantic", "temporal"),
    limit: int = 20,
) -> list[sqlite3.Row]:
    """Факты, смежные с переданными fact_ids по графу (для расширения при recall)."""
    if not fact_ids:
        return []

    placeholders      = ",".join("?" * len(fact_ids))
    type_placeholders = ",".join("?" * len(link_types))

    return conn.execute(
        f"""
        SELECT DISTINCT f.*
        FROM facts f
        JOIN fact_links fl ON fl.target_id = f.fact_id OR fl.source_id = f.fact_id
        WHERE (fl.source_id IN ({placeholders}) OR fl.target_id IN ({placeholders}))
          AND fl.link_type IN ({type_placeholders})
          AND f.fact_id NOT IN ({placeholders})
        ORDER BY fl.strength DESC
        LIMIT ?
        """,
        [*fact_ids, *fact_ids, *link_types, *fact_ids, limit],
    ).fetchall()
