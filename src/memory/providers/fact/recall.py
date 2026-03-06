"""recall.py — 4-way параллельный поиск фактов.

1:1 с Hindsight (LinkExpansion graph strategy + dateparser temporal + RRF +
cross-encoder reranking via FlashRank).

Методы:
  1. Semantic  — векторный поиск в LanceDB
  2. BM25      — SQLite FTS5
  3. Graph     — LinkExpansion: entity co-occurrence + causal + fallback links
  4. Temporal  — dateparser temporal constraint + spreading
  5. Reranking — FlashRank cross-encoder (ms-marco-MiniLM-L-12-v2)

Параметры фильтрации:
  - types      — фильтр по fact_type ('world', 'experience', 'observation')
  - tags       — фильтр по тегам
  - tags_match — "any"|"all"|"any_strict"|"all_strict" (как у Hindsight)
  - budget     — "low"|"mid"|"high" (глубина поиска)
  - max_tokens — мягкий лимит выходных токенов

Staleness API:
  - RecallResponse.pending_consolidation — кол-во неконсолидированных фактов
  - RecallResponse.is_stale — True если pending_consolidation > 0
"""
import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Optional

log = logging.getLogger(__name__)

TOP_K = 10
SEMANTIC_THRESHOLD = 0.3
RRF_K = 60
MAX_ENTITY_FREQUENCY = 500   # пропускаем слишком частые сущности (как у Hindsight)
CAUSAL_WEIGHT_THRESHOLD = 0.3
RERANK_CANDIDATES = 30       # pre-filter перед reranking (как у Hindsight)

# budget → множитель кандидатов (как у Hindsight: low < mid < high)
_BUDGET_FACTOR = {"low": 1, "mid": 2, "high": 4}

RERANK_MODEL_DEFAULT = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"

# Lazy singletons по имени модели
_rankers: dict[str, object] = {}


# ── Result models ──────────────────────────────────────────────────────────────

@dataclass
class RecallResult:
    fact_id: str
    fact: str
    fact_type: str
    occurred_start: Optional[str] = None
    occurred_end: Optional[str] = None
    mentioned_at: Optional[str] = None
    document_id: Optional[str] = None
    context: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    score: float = 0.0
    temporal_score: float = 0.0
    sources: list[str] = field(default_factory=list)
    is_real_document: bool = False


@dataclass
class RecallResponse:
    """Обёртка над результатами — содержит staleness информацию."""
    results: list[RecallResult]
    pending_consolidation: int = 0

    @property
    def is_stale(self) -> bool:
        return self.pending_consolidation > 0

    @property
    def freshness(self) -> str:
        if self.pending_consolidation == 0:
            return "up_to_date"
        if self.pending_consolidation < 10:
            return "slightly_stale"
        return "stale"


def _row_to_result(row: sqlite3.Row, score: float = 0.0, source: str = "") -> RecallResult:
    import json as _json
    keys = row.keys()
    tags_raw = row["tags"] if "tags" in keys else "[]"
    try:
        tags = _json.loads(tags_raw or "[]")
    except (ValueError, TypeError):
        tags = []
    context = (row["context"] or None) if "context" in keys else None
    return RecallResult(
        fact_id=row["fact_id"],
        fact=row["fact"],
        fact_type=row["fact_type"],
        occurred_start=row["occurred_start"] or None,
        occurred_end=row["occurred_end"] or None,
        mentioned_at=row["mentioned_at"] or None,
        document_id=row["document_id"] or None,
        context=context,
        tags=tags,
        score=score,
        sources=[source] if source else [],
        is_real_document=bool(row["is_real_document"]),
    )




# ── 1. Semantic search ─────────────────────────────────────────────────────────

def _semantic_search(query_vec, storage, limit: int) -> list[tuple[str, float]]:
    """LanceDB vector search через Storage. Возвращает [(fact_id, similarity)]."""
    results = storage.search_vectors(query_vec, limit)
    return [
        (r["fact_id"], 1.0 - r.get("_distance", 1.0))
        for r in results
        if (1.0 - r.get("_distance", 1.0)) >= SEMANTIC_THRESHOLD
    ]


# ── Tag / fact_type WHERE helpers ─────────────────────────────────────────────

def _tags_where(
    tags: Optional[list[str]],
    tags_match: str = "any",
) -> tuple[str, list]:
    """
    Строит WHERE-клаузу для фильтрации по тегам.
    Поведение 1:1 с Hindsight:
      any        — OR-match, включает записи без тегов
      all        — AND-match, включает записи без тегов
      any_strict — OR-match, исключает записи без тегов
      all_strict — AND-match, исключает записи без тегов
    """
    if not tags:
        return "", []

    has_tags  = "json_array_length(f.tags) > 0"
    or_match  = " OR ".join(
        "EXISTS (SELECT 1 FROM json_each(f.tags) WHERE value = ?)" for _ in tags
    )
    and_match = " AND ".join(
        "EXISTS (SELECT 1 FROM json_each(f.tags) WHERE value = ?)" for _ in tags
    )
    params = list(tags)

    if tags_match == "any":
        return f"AND ({or_match} OR NOT ({has_tags}))", params
    if tags_match == "all":
        return f"AND ({and_match} OR NOT ({has_tags}))", params
    if tags_match == "any_strict":
        return f"AND ({or_match})", params
    if tags_match == "all_strict":
        return f"AND ({and_match})", params
    return f"AND ({or_match} OR NOT ({has_tags}))", params  # fallback → any


def _fact_types_where(types: Optional[list[str]]) -> tuple[str, list]:
    if not types:
        return "", []
    placeholders = ",".join("?" * len(types))
    return f"AND fact_type IN ({placeholders})", list(types)


def _count_tokens(text: str) -> int:
    """Оценка числа токенов (tiktoken cl100k_base если доступен, иначе len//4)."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def _apply_token_budget(results: list, max_tokens: int) -> list:
    """Обрезает список результатов по суммарному бюджету токенов."""
    total = 0
    out = []
    for r in results:
        t = _count_tokens(r.fact)
        if out and total + t > max_tokens:
            break
        total += t
        out.append(r)
    return out


# ── 2. BM25 search ─────────────────────────────────────────────────────────────

def _bm25_search(
    query: str,
    storage,
    limit: int,
    tags: Optional[list[str]] = None,
    tags_match: str = "any",
    types: Optional[list[str]] = None,
) -> list[str]:
    """SQLite FTS5 + опциональный post-filter по тегам и fact_type."""
    tokens = [t for t in re.sub(r"[^\w\s]", " ", query.lower()).split() if t]
    if not tokens:
        return []
    fts_query = " OR ".join(tokens)
    try:
        tags_clause, tags_params = _tags_where(tags, tags_match)
        ft_clause,   ft_params   = _fact_types_where(types)
        rows = storage.conn.execute(
            f"""
            SELECT fts.fact_id FROM facts_fts fts
            JOIN facts f ON f.fact_id = fts.fact_id
            WHERE facts_fts MATCH ?
              {tags_clause} {ft_clause}
            ORDER BY rank
            LIMIT ?
            """,
            [fts_query, *tags_params, *ft_params, limit],
        ).fetchall()
        return [r["fact_id"] for r in rows]
    except Exception as e:
        log.warning("[recall] FTS5 failed: %s", e)
        return []


# ── 3. Graph search (LinkExpansion) ────────────────────────────────────────────

def _entity_frequency(storage, entity_id: str) -> int:
    """Количество фактов с данной сущностью."""
    row = storage.conn.execute(
        "SELECT COUNT(*) AS cnt FROM fact_entities WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    return row["cnt"] if row else 0


def _observation_graph_expansion(
    seed_ids: list[str],
    storage,
    budget: int,
) -> list[tuple[str, float]]:
    """
    S3: Observation graph expansion (аналог Hindsight link_expansion_retrieval.py).

    Для observations: observation → source_fact_ids → entities →
    все world-факты с теми же сущностями → их observations (кроме seeds).
    """
    if not seed_ids:
        return []

    placeholders = ",".join("?" * len(seed_ids))

    # Шаг 1: собираем source_fact_ids из observation-seeds
    obs_rows = storage.conn.execute(
        f"""
        SELECT fact_id, source_fact_ids FROM facts
        WHERE fact_id IN ({placeholders})
          AND fact_type = 'observation'
        """,
        seed_ids,
    ).fetchall()

    source_ids: list[str] = []
    for row in obs_rows:
        try:
            import json as _json
            ids = _json.loads(row["source_fact_ids"] or "[]")
            source_ids.extend(ids)
        except Exception:
            pass

    if not source_ids:
        return []

    src_placeholders = ",".join("?" * len(source_ids))

    # Шаг 2: entities источников (с фильтром по частоте)
    entity_rows = storage.conn.execute(
        f"""
        SELECT DISTINCT fe.entity_id
        FROM fact_entities fe
        WHERE fe.fact_id IN ({src_placeholders})
          AND (SELECT COUNT(*) FROM fact_entities WHERE entity_id = fe.entity_id) < ?
        """,
        [*source_ids, MAX_ENTITY_FREQUENCY],
    ).fetchall()

    entity_ids = [r["entity_id"] for r in entity_rows]
    if not entity_ids:
        return []

    ent_placeholders = ",".join("?" * len(entity_ids))

    # Шаг 3: all world-facts with those entities → their observations (excluding seeds)
    result_rows = storage.conn.execute(
        f"""
        WITH connected_sources AS (
            SELECT DISTINCT fe.fact_id AS source_id
            FROM fact_entities fe
            WHERE fe.entity_id IN ({ent_placeholders})
        ),
        obs_matches AS (
            SELECT f.fact_id, jsfid.value AS matched_source
            FROM facts f, json_each(f.source_fact_ids) jsfid
            WHERE f.fact_type = 'observation'
              AND f.fact_id NOT IN ({placeholders})
              AND jsfid.value IN (SELECT source_id FROM connected_sources)
        )
        SELECT fact_id, COUNT(DISTINCT matched_source) AS score
        FROM obs_matches
        GROUP BY fact_id
        ORDER BY score DESC
        LIMIT ?
        """,
        [*entity_ids, *seed_ids, budget],
    ).fetchall()

    return [(r["fact_id"], float(r["score"])) for r in result_rows]


def _graph_link_expansion(
    seed_ids: list[str],
    storage,
    budget: int,
) -> list[tuple[str, float]]:
    """
    LinkExpansion: 3 запроса, аналог Hindsight.

    Priority: entity co-occurrence > causal links > fallback (semantic/temporal/entity).
    Fallback score умножается на 0.5x как у Hindsight.

    Для seeds типа 'observation' дополнительно выполняется observation graph expansion (S3).
    """
    if not seed_ids:
        return []

    placeholders = ",".join("?" * len(seed_ids))
    score_map: dict[str, float] = {}

    # --- Query 1: entity co-occurrence (как unit_entities JOIN в Hindsight) ---
    # Находим сущности seeds → находим все факты с теми же сущностями
    # пропуская высокочастотные сущности (MAX_ENTITY_FREQUENCY)
    entity_rows = storage.conn.execute(
        f"""
        SELECT fe2.fact_id, COUNT(*) AS score
        FROM fact_entities fe1
        JOIN entities e ON fe1.entity_id = e.entity_id
        JOIN fact_entities fe2 ON fe1.entity_id = fe2.entity_id
        WHERE fe1.fact_id IN ({placeholders})
          AND fe2.fact_id NOT IN ({placeholders})
          AND (SELECT COUNT(*) FROM fact_entities WHERE entity_id = e.entity_id) < ?
        GROUP BY fe2.fact_id
        ORDER BY score DESC
        LIMIT ?
        """,
        [*seed_ids, *seed_ids, MAX_ENTITY_FREQUENCY, budget],
    ).fetchall()

    for row in entity_rows:
        fid = row["fact_id"]
        score_map[fid] = max(score_map.get(fid, 0), float(row["score"]))

    # --- Query 2: causal links (caused_by) ---
    causal_rows = storage.conn.execute(
        f"""
        SELECT fl.target_id AS fact_id, (fl.strength + 1.0) AS score
        FROM fact_links fl
        WHERE fl.source_id IN ({placeholders})
          AND fl.link_type = 'caused_by'
          AND fl.strength >= ?
          AND fl.target_id NOT IN ({placeholders})
        ORDER BY fl.strength DESC
        LIMIT ?
        """,
        [*seed_ids, CAUSAL_WEIGHT_THRESHOLD, *seed_ids, budget],
    ).fetchall()

    for row in causal_rows:
        fid = row["fact_id"]
        score_map[fid] = max(score_map.get(fid, 0), float(row["score"]))

    # --- Query 3: fallback — semantic/temporal/entity links, оба направления ---
    fallback_rows = storage.conn.execute(
        f"""
        SELECT fact_id, MAX(weight) * 0.5 AS score FROM (
            SELECT fl.target_id AS fact_id, fl.strength AS weight
            FROM fact_links fl
            WHERE fl.source_id IN ({placeholders})
              AND fl.link_type IN ('semantic', 'temporal', 'entity')
              AND fl.strength >= ?
              AND fl.target_id NOT IN ({placeholders})
            UNION ALL
            SELECT fl.source_id AS fact_id, fl.strength AS weight
            FROM fact_links fl
            WHERE fl.target_id IN ({placeholders})
              AND fl.link_type IN ('semantic', 'temporal', 'entity')
              AND fl.strength >= ?
              AND fl.source_id NOT IN ({placeholders})
        )
        GROUP BY fact_id
        ORDER BY score DESC
        LIMIT ?
        """,
        [
            *seed_ids, CAUSAL_WEIGHT_THRESHOLD, *seed_ids,
            *seed_ids, CAUSAL_WEIGHT_THRESHOLD, *seed_ids,
            budget,
        ],
    ).fetchall()

    for row in fallback_rows:
        fid = row["fact_id"]
        if fid not in score_map:  # entity/causal уже приоритетнее
            score_map[fid] = float(row["score"])

    # --- Query 4: observation graph expansion (S3) ---
    obs_expanded = _observation_graph_expansion(seed_ids, storage, budget)
    for fid, score in obs_expanded:
        if fid not in score_map:
            score_map[fid] = score

    sorted_ids = sorted(score_map, key=lambda x: score_map[x], reverse=True)[:budget]
    return [(fid, score_map[fid]) for fid in sorted_ids]


# ── 4. Temporal search ─────────────────────────────────────────────────────────

def _extract_temporal_constraint(
    query: str,
    reference_date: Optional[datetime] = None,
) -> Optional[tuple[datetime, datetime]]:
    """
    Извлекает временной диапазон из запроса через dateparser (как у Hindsight).
    Fallback на regex при отсутствии библиотеки.
    """
    try:
        import dateparser.search as dps

        ref = reference_date or datetime.utcnow()
        settings = {"RELATIVE_BASE": ref, "RETURN_AS_TIMEZONE_AWARE": False}
        dates = dps.search_dates(query, settings=settings)
        if dates:
            parsed_dates = sorted(d for _, d in dates)
            start = parsed_dates[0].replace(hour=0, minute=0, second=0, microsecond=0)
            end = parsed_dates[-1].replace(hour=23, minute=59, second=59, microsecond=0)
            if start == end:
                # Одна дата — берём ±1 день как окно
                end = start + timedelta(days=1)
            return start, end
    except ImportError:
        pass

    # Regex fallback
    now = reference_date or datetime.utcnow()
    q = query.lower()
    patterns = [
        (r"\byesterday\b",  now - timedelta(days=1),  now - timedelta(days=1)),
        (r"\btoday\b",      now,                       now),
        (r"\blast week\b",  now - timedelta(days=7),   now),
        (r"\blast month\b", now - timedelta(days=30),  now),
        (r"\blast year\b",  now - timedelta(days=365), now),
        (r"\brecently\b",   now - timedelta(days=14),  now),
    ]
    for pattern, start, end in patterns:
        if re.search(pattern, q):
            return (
                start.replace(hour=0, minute=0, second=0, microsecond=0),
                end.replace(hour=23, minute=59, second=59, microsecond=0),
            )
    return None


def _temporal_proximity(
    row: sqlite3.Row,
    mid_date: datetime,
    total_days: float,
) -> float:
    """Вычисляет temporal proximity score для одного факта (1:1 с Hindsight)."""
    best_date: Optional[datetime] = None
    try:
        if row["occurred_start"] and row["occurred_end"]:
            s = datetime.fromisoformat(row["occurred_start"]).replace(tzinfo=None)
            e = datetime.fromisoformat(row["occurred_end"]).replace(tzinfo=None)
            best_date = s + (e - s) / 2
        elif row["occurred_start"]:
            best_date = datetime.fromisoformat(row["occurred_start"]).replace(tzinfo=None)
        elif row["mentioned_at"]:
            best_date = datetime.fromisoformat(row["mentioned_at"]).replace(tzinfo=None)
    except (ValueError, TypeError):
        pass

    if best_date is None:
        return 0.5

    days_from_mid = abs((best_date - mid_date).total_seconds() / 86400)
    return 1.0 - min(days_from_mid / max(total_days / 2, 1.0), 1.0) if total_days > 0 else 1.0


def _temporal_search(
    start: datetime,
    end: datetime,
    storage,
    limit: int,
    tags: Optional[list[str]] = None,
    tags_match: str = "any",
    types: Optional[list[str]] = None,
) -> list[tuple[str, float]]:
    """
    Факты в временном диапазоне + опциональные фильтры.
    Возвращает [(fact_id, temporal_proximity_score)], отсортированные по proximity DESC.
    """
    total_days = (end - start).total_seconds() / 86400
    mid_date = start + (end - start) / 2

    tags_clause, tags_params = _tags_where(tags, tags_match)
    ft_clause,   ft_params   = _fact_types_where(types)
    rows = storage.conn.execute(
        f"""
        SELECT fact_id, occurred_start, occurred_end, mentioned_at FROM facts f
        WHERE (
            (occurred_start IS NOT NULL AND occurred_start <= ? AND (occurred_end IS NULL OR occurred_end >= ?))
            OR (mentioned_at IS NOT NULL AND mentioned_at BETWEEN ? AND ?)
            OR (occurred_start IS NOT NULL AND occurred_start BETWEEN ? AND ?)
        )
        {tags_clause} {ft_clause}
        ORDER BY COALESCE(occurred_start, mentioned_at) DESC
        LIMIT ?
        """,
        (
            end.isoformat(), start.isoformat(),
            start.isoformat(), end.isoformat(),
            start.isoformat(), end.isoformat(),
            *tags_params, *ft_params,
            limit,
        ),
    ).fetchall()

    results = []
    for r in rows:
        score = _temporal_proximity(r, mid_date, total_days)
        results.append((r["fact_id"], score))
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def _temporal_spread(
    entry_scores: dict[str, float],
    start: datetime,
    end: datetime,
    storage,
    budget: int,
) -> dict[str, float]:
    """
    BFS spreading через temporal/causal links от temporal entry points (S1).

    Аналог Hindsight retrieval.py spreading loop:
      propagated = parent_score * link.weight * causal_boost * 0.7
      combined   = max(neighbor_proximity, propagated)
    """
    if not entry_scores:
        return {}

    total_days = (end - start).total_seconds() / 86400
    mid_date = start + (end - start) / 2

    results: dict[str, float] = dict(entry_scores)
    frontier = list(entry_scores.keys())
    visited: set[str] = set(frontier)
    budget_remaining = budget - len(frontier)
    batch_size = 20

    while frontier and budget_remaining > 0:
        batch = frontier[:batch_size]
        frontier = frontier[batch_size:]
        placeholders = ",".join("?" * len(batch))

        rows = storage.conn.execute(
            f"""
            SELECT fl.source_id, fl.target_id AS neighbor_id,
                   fl.strength AS weight, fl.link_type,
                   f.occurred_start, f.occurred_end, f.mentioned_at
            FROM fact_links fl
            JOIN facts f ON f.fact_id = fl.target_id
            WHERE fl.source_id IN ({placeholders})
              AND fl.link_type IN ('temporal', 'caused_by')
              AND fl.strength >= 0.1
              AND fl.target_id NOT IN ({placeholders})
            ORDER BY fl.strength DESC
            LIMIT ?
            """,
            [*batch, *batch, batch_size * 10],
        ).fetchall()

        for row in rows:
            neighbor_id = row["neighbor_id"]
            if neighbor_id in visited:
                continue

            parent_score = results.get(row["source_id"], 0.5)
            causal_boost = 2.0 if row["link_type"] == "caused_by" else 1.0
            propagated = parent_score * row["weight"] * causal_boost * 0.7
            neighbor_proximity = _temporal_proximity(row, mid_date, total_days)
            combined = max(neighbor_proximity, propagated)

            if combined > 0.2:
                results[neighbor_id] = combined
                visited.add(neighbor_id)
                if budget_remaining > 0:
                    frontier.append(neighbor_id)
                    budget_remaining -= 1

    return results


# ── RRF ────────────────────────────────────────────────────────────────────────

def _rrf_merge(
    ranked_lists: list[list[str]],
    sources: list[str],
    k: int = RRF_K,
) -> list[tuple[str, float, list[str]]]:
    """Reciprocal Rank Fusion. Возвращает [(fact_id, score, [sources])]."""
    scores: dict[str, float] = {}
    fact_sources: dict[str, list[str]] = {}

    for ranked, source in zip(ranked_lists, sources):
        for rank, fact_id in enumerate(ranked, start=1):
            scores[fact_id] = scores.get(fact_id, 0.0) + 1.0 / (k + rank)
            fact_sources.setdefault(fact_id, [])
            if source not in fact_sources[fact_id]:
                fact_sources[fact_id].append(source)

    return [
        (fid, score, fact_sources[fid])
        for fid, score in sorted(scores.items(), key=lambda x: x[1], reverse=True)
    ]


# ── Cross-encoder reranking (SentenceTransformers) ────────────────────────────

def _get_ranker(model_name: str = ""):
    """Lazy singleton SentenceTransformers CrossEncoder по имени модели."""
    model_name = model_name or RERANK_MODEL_DEFAULT
    if model_name not in _rankers:
        from sentence_transformers import CrossEncoder
        _rankers[model_name] = CrossEncoder(model_name)
        log.info("[recall] CrossEncoder loaded: %s", model_name)
    return _rankers[model_name]


def _rerank(query: str, results: list["RecallResult"], rerank_model: str = "") -> list["RecallResult"]:
    """
    Cross-encoder reranking через SentenceTransformers CrossEncoder.

    Формат документа (как у Hindsight):
      [Date: June 5, 2022 (2022-06-05)] fact_text
    """
    if not results:
        return results

    passages = []
    for r in results:
        doc_text = r.fact
        if r.occurred_start:
            try:
                dt = datetime.fromisoformat(r.occurred_start)
                date_iso = dt.strftime("%Y-%m-%d")
                date_readable = dt.strftime("%B %d, %Y")
                doc_text = f"[Date: {date_readable} ({date_iso})] {doc_text}"
            except ValueError:
                pass
        if r.context:
            doc_text = f"{r.context}: {doc_text}"
        passages.append(doc_text)

    try:
        ranker = _get_ranker(rerank_model)
        pairs = [(query, doc) for doc in passages]
        scores = ranker.predict(pairs)

        for r, score in zip(results, scores):
            r.score = float(score)

        results.sort(key=lambda r: r.score, reverse=True)
    except Exception as e:
        log.warning("[recall] reranking failed, keeping RRF order: %s", e, exc_info=True)

    return results


# ── Main recall ────────────────────────────────────────────────────────────────

def recall(
    query: str,
    query_vec,
    storage,
    types: Optional[list[str]] = None,
    max_tokens: int = 4096,
    budget: str = "mid",
    query_timestamp: Optional[str] = None,
    tags: Optional[list[str]] = None,
    tags_match: Literal["any", "all", "any_strict", "all_strict"] = "any",
    rerank_model: str = "",
) -> RecallResponse:
    """
    4-way поиск + RRF + cross-encoder reranking.
    Синхронный — вызывать через asyncio.to_thread.

    Сигнатура 1:1 с Hindsight arecall (без bank_id/trace/include_*).

    1. Semantic  — LanceDB cosine ≥ 0.3
    2. BM25      — SQLite FTS5 (+ tags/types filter)
    3. Graph     — LinkExpansion от semantic seeds (entity > causal > fallback*0.5)
    4. Temporal  — dateparser + SQL date range (+ tags/types filter)
    5. Reranking — FlashRank cross-encoder поверх топ-RERANK_CANDIDATES
    6. Token budget — обрезаем по max_tokens

    Возвращает RecallResponse с полями:
      .results               — список RecallResult
      .pending_consolidation — кол-во неконсолидированных фактов (Staleness API)
      .is_stale              — True если pending_consolidation > 0
      .freshness             — "up_to_date" | "slightly_stale" | "stale"
    """
    factor = _BUDGET_FACTOR.get(budget, 2)
    limit  = TOP_K * factor

    # Temporal reference date
    ref_date: Optional[datetime] = None
    if query_timestamp:
        try:
            ref_date = datetime.fromisoformat(query_timestamp)
        except ValueError:
            pass

    # 1. Semantic
    semantic_hits = _semantic_search(query_vec, storage, limit * 2)
    semantic_ids = [fid for fid, _ in semantic_hits]

    # 2. BM25
    bm25_ids = _bm25_search(
        query, storage, limit * 2, tags=tags, tags_match=tags_match, types=types
    )

    # 3. Graph — LinkExpansion от semantic seeds (граф не фильтруем по тегам — это постфильтр)
    graph_hits = _graph_link_expansion(semantic_ids[:limit], storage, budget=limit * 2)
    graph_ids = [fid for fid, _ in graph_hits]

    # 4. Temporal + spreading (S1/S2)
    temporal_range = _extract_temporal_constraint(query, ref_date)
    temporal_ids: list[str] = []
    temporal_scores: dict[str, float] = {}
    if temporal_range:
        t_start, t_end = temporal_range
        entry_hits = _temporal_search(
            t_start, t_end, storage, limit,
            tags=tags, tags_match=tags_match, types=types,
        )
        entry_scores = {fid: score for fid, score in entry_hits}
        spread_scores = _temporal_spread(entry_scores, t_start, t_end, storage, budget=limit * 2)
        temporal_scores = spread_scores
        temporal_ids = sorted(spread_scores, key=lambda x: spread_scores[x], reverse=True)

    lists = [semantic_ids, bm25_ids, graph_ids]
    sources = ["semantic", "bm25", "graph"]
    if temporal_ids:
        lists.append(temporal_ids)
        sources.append("temporal")

    # RRF — берём RERANK_CANDIDATES кандидатов для reranker
    merged = _rrf_merge(lists, sources)[:RERANK_CANDIDATES]
    if not merged:
        return RecallResponse(results=[], pending_consolidation=storage.get_pending_consolidation_count())

    merged_ids = [fid for fid, _, _ in merged]
    rrf_scores = {fid: score for fid, score, _ in merged}
    rrf_sources = {fid: srcs for fid, _, srcs in merged}

    # Post-фильтр по tags и types для semantic/graph результатов
    tags_clause, tags_params = _tags_where(tags, tags_match)
    ft_clause,   ft_params   = _fact_types_where(types)
    placeholders = ",".join("?" * len(merged_ids))
    rows = storage.conn.execute(
        f"SELECT * FROM facts f WHERE fact_id IN ({placeholders}) {tags_clause} {ft_clause}",
        [*merged_ids, *tags_params, *ft_params],
    ).fetchall()
    row_map = {r["fact_id"]: r for r in rows}

    results = []
    for fid in merged_ids:
        if fid not in row_map:
            continue
        r = _row_to_result(row_map[fid], score=rrf_scores[fid])
        r.sources = rrf_sources[fid]
        r.temporal_score = temporal_scores.get(fid, 0.0)
        results.append(r)

    # 5. Cross-encoder reranking
    results = _rerank(query, results, rerank_model)

    # 6. Token budget
    results = _apply_token_budget(results, max_tokens)

    pending = storage.get_pending_consolidation_count()
    log.info(
        "[recall] %r → %d results (sem=%d bm25=%d graph=%d temporal=%d budget=%s pending=%d)",
        query[:60], len(results),
        len(semantic_ids), len(bm25_ids), len(graph_ids), len(temporal_ids), budget, pending,
    )
    return RecallResponse(results=results, pending_consolidation=pending)


async def recall_async(
    query: str,
    query_vec,
    storage,
    types: Optional[list[str]] = None,
    max_tokens: int = 4096,
    budget: str = "mid",
    query_timestamp: Optional[str] = None,
    tags: Optional[list[str]] = None,
    tags_match: Literal["any", "all", "any_strict", "all_strict"] = "any",
    rerank_model: str = "",
) -> RecallResponse:
    return await asyncio.to_thread(
        recall, query, query_vec, storage,
        types, max_tokens, budget, query_timestamp, tags, tags_match, rerank_model,
    )
