"""mental_models.py — user-created / auto-generated curated summaries.

Аналог Hindsight MentalModel — наивысший приоритет при recall.

Mental model = name + description (one-liner для поиска) + summary (полный текст).
Хранится в SQLite (metadata) + LanceDB (вектор description для семантического поиска).

В recall иерархия:
  1. mental_models   — ищем первыми (highest quality)
  2. observations    — auto-consolidated patterns
  3. raw facts       — ground truth
"""
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

MENTAL_MODEL_SIMILARITY_THRESHOLD = 0.3


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class MentalModel:
    model_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""   # one-liner — индексируется как вектор
    summary: Optional[str] = None
    source_fact_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── CRUD ───────────────────────────────────────────────────────────────────────

def create_mental_model(
    conn: sqlite3.Connection,
    mm_table,
    embed_fn,
    name: str,
    description: str,
    summary: Optional[str] = None,
    source_fact_ids: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
) -> MentalModel:
    """
    Создаёт новый mental model. description индексируется как вектор.
    """
    mm = MentalModel(
        name=name,
        description=description,
        summary=summary,
        source_fact_ids=source_fact_ids or [],
        tags=tags or [],
    )

    conn.execute(
        """
        INSERT INTO mental_models
            (model_id, name, description, summary, source_fact_ids, tags, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (mm.model_id, mm.name, mm.description, mm.summary,
         json.dumps(mm.source_fact_ids), json.dumps(mm.tags),
         mm.created_at, mm.updated_at),
    )
    conn.commit()

    try:
        vec = embed_fn(description)
        mm_table.add([{"model_id": mm.model_id, "vector": vec}])
    except Exception as e:
        log.warning("[mental_models] LanceDB write failed: %s", e)

    log.info("[mental_models] created %r (%s)", name, mm.model_id)
    return mm


def update_mental_model(
    conn: sqlite3.Connection,
    mm_table,
    embed_fn,
    model_id: str,
    description: Optional[str] = None,
    summary: Optional[str] = None,
    source_fact_ids: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
) -> bool:
    """Обновляет существующий mental model. Возвращает True если найден."""
    row = conn.execute(
        "SELECT * FROM mental_models WHERE model_id = ?", (model_id,)
    ).fetchone()
    if not row:
        return False

    now = datetime.now(timezone.utc).isoformat()
    new_description  = description      if description      is not None else row["description"]
    new_summary      = summary          if summary          is not None else row["summary"]
    new_source_ids   = source_fact_ids  if source_fact_ids  is not None else json.loads(row["source_fact_ids"] or "[]")
    new_tags         = tags             if tags             is not None else json.loads(row["tags"] or "[]")

    conn.execute(
        """
        UPDATE mental_models
        SET description = ?, summary = ?, source_fact_ids = ?, tags = ?, updated_at = ?
        WHERE model_id = ?
        """,
        (new_description, new_summary, json.dumps(new_source_ids), json.dumps(new_tags), now, model_id),
    )
    conn.commit()

    if description is not None:
        try:
            vec = embed_fn(new_description)
            mm_table.delete(f"model_id = '{model_id}'")
            mm_table.add([{"model_id": model_id, "vector": vec}])
        except Exception as e:
            log.warning("[mental_models] LanceDB update failed: %s", e)

    return True


def delete_mental_model(conn: sqlite3.Connection, mm_table, model_id: str) -> bool:
    row = conn.execute(
        "SELECT model_id FROM mental_models WHERE model_id = ?", (model_id,)
    ).fetchone()
    if not row:
        return False
    conn.execute("DELETE FROM mental_models WHERE model_id = ?", (model_id,))
    conn.commit()
    try:
        mm_table.delete(f"model_id = '{model_id}'")
    except Exception:
        pass
    return True


def get_all_mental_models(conn: sqlite3.Connection) -> list[MentalModel]:
    rows = conn.execute("SELECT * FROM mental_models ORDER BY updated_at DESC").fetchall()
    return [_row_to_mm(r) for r in rows]


def _row_to_mm(row: sqlite3.Row) -> MentalModel:
    return MentalModel(
        model_id=row["model_id"],
        name=row["name"],
        description=row["description"],
        summary=row["summary"],
        source_fact_ids=json.loads(row["source_fact_ids"] or "[]"),
        tags=json.loads(row["tags"] or "[]"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Search ─────────────────────────────────────────────────────────────────────

@dataclass
class MentalModelResult:
    model_id: str
    name: str
    description: str
    summary: Optional[str]
    tags: list[str]
    relevance: float
    source_fact_ids: list[str]
    is_stale: bool = False
    pending_consolidation: int = 0


def search_mental_models(
    query_vec,
    conn: sqlite3.Connection,
    mm_table,
    limit: int = 5,
    tags: Optional[list[str]] = None,
    pending_consolidation: int = 0,
) -> list[MentalModelResult]:
    """
    Векторный поиск по description mental models.
    Аналог Hindsight tool_search_mental_models.

    Возвращает результаты с is_stale=True если pending_consolidation > 0
    (агент должен дополнительно вызвать recall для верификации).
    """
    try:
        if mm_table.count_rows() == 0:
            return []
    except Exception:
        return []

    hits = mm_table.search(query_vec).limit(limit * 2).to_list()
    if not hits:
        return []

    model_ids = [h["model_id"] for h in hits]
    similarity = {h["model_id"]: 1.0 - h.get("_distance", 1.0) for h in hits}

    # Фильтруем по порогу
    model_ids = [mid for mid in model_ids if similarity[mid] >= MENTAL_MODEL_SIMILARITY_THRESHOLD]
    if not model_ids:
        return []

    placeholders = ",".join("?" * len(model_ids))
    rows = conn.execute(
        f"SELECT * FROM mental_models WHERE model_id IN ({placeholders})",
        model_ids,
    ).fetchall()
    row_map = {r["model_id"]: r for r in rows}

    results = []
    is_stale = pending_consolidation > 0

    for mid in model_ids:
        if mid not in row_map:
            continue
        row = row_map[mid]
        mm_tags = json.loads(row["tags"] or "[]")

        # Tag filter (OR match, как у Hindsight по умолчанию)
        if tags and not any(t in mm_tags for t in tags):
            continue

        results.append(MentalModelResult(
            model_id=mid,
            name=row["name"],
            description=row["description"],
            summary=row["summary"],
            tags=mm_tags,
            relevance=round(similarity[mid], 4),
            source_fact_ids=json.loads(row["source_fact_ids"] or "[]"),
            is_stale=is_stale,
            pending_consolidation=pending_consolidation,
        ))

    return sorted(results, key=lambda r: r.relevance, reverse=True)[:limit]
