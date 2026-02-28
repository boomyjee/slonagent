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
    storage,
    name: str,
    description: str,
    summary: Optional[str] = None,
    source_fact_ids: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
) -> MentalModel:
    """Создаёт новый mental model. description индексируется как вектор."""
    mm = MentalModel(
        name=name,
        description=description,
        summary=summary,
        source_fact_ids=source_fact_ids or [],
        tags=tags or [],
    )
    storage.insert_mental_model({
        "model_id": mm.model_id,
        "name": mm.name,
        "description": mm.description,
        "summary": mm.summary,
        "source_fact_ids": json.dumps(mm.source_fact_ids),
        "tags": json.dumps(mm.tags),
        "created_at": mm.created_at,
        "updated_at": mm.updated_at,
    })
    try:
        vec = storage.embed_fn(description)
        storage.insert_mm_vector(mm.model_id, vec)
    except Exception as e:
        log.warning("[mental_models] LanceDB write failed: %s", e)

    log.info("[mental_models] created %r (%s)", name, mm.model_id)
    return mm


def update_mental_model(
    storage,
    model_id: str,
    description: Optional[str] = None,
    summary: Optional[str] = None,
    source_fact_ids: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
) -> bool:
    """Обновляет существующий mental model. Возвращает True если найден."""
    rows = storage.get_mental_model_rows([model_id])
    if not rows:
        return False
    row = rows[0]

    now              = datetime.now(timezone.utc).isoformat()
    new_description  = description     if description     is not None else row["description"]
    new_summary      = summary         if summary         is not None else row["summary"]
    new_source_ids   = source_fact_ids if source_fact_ids is not None else json.loads(row["source_fact_ids"] or "[]")
    new_tags         = tags            if tags            is not None else json.loads(row["tags"] or "[]")

    storage.update_mental_model_row(model_id, {
        "description":    new_description,
        "summary":        new_summary,
        "source_fact_ids": json.dumps(new_source_ids),
        "tags":           json.dumps(new_tags),
        "updated_at":     now,
    })

    if description is not None:
        try:
            vec = storage.embed_fn(new_description)
            storage.delete_mm_vector(model_id)
            storage.insert_mm_vector(model_id, vec)
        except Exception as e:
            log.warning("[mental_models] LanceDB update failed: %s", e)

    return True


def delete_mental_model(storage, model_id: str) -> bool:
    rows = storage.get_mental_model_rows([model_id])
    if not rows:
        return False
    storage.delete_mental_model_row(model_id)
    storage.delete_mm_vector(model_id)
    return True


def get_all_mental_models(storage) -> list[MentalModel]:
    return [_row_to_mm(r) for r in storage.get_all_mental_model_rows()]


def _row_to_mm(row) -> MentalModel:
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
    storage,
    limit: int = 5,
    tags: Optional[list[str]] = None,
    pending_consolidation: int = 0,
) -> list[MentalModelResult]:
    """
    Векторный поиск по description mental models.
    Аналог Hindsight tool_search_mental_models.

    Возвращает результаты с is_stale=True если pending_consolidation > 0.
    """
    hits = storage.search_mm_vectors(query_vec, limit=limit * 2)
    if not hits:
        return []

    model_ids  = [h["model_id"] for h in hits]
    similarity = {h["model_id"]: 1.0 - h.get("_distance", 1.0) for h in hits}
    model_ids  = [mid for mid in model_ids if similarity[mid] >= MENTAL_MODEL_SIMILARITY_THRESHOLD]
    if not model_ids:
        return []

    rows    = storage.get_mental_model_rows(model_ids)
    row_map = {r["model_id"]: r for r in rows}
    is_stale = pending_consolidation > 0

    results = []
    for mid in model_ids:
        if mid not in row_map:
            continue
        row     = row_map[mid]
        mm_tags = json.loads(row["tags"] or "[]")
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
