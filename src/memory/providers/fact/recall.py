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
  - tags       — OR-match по тегам (как у Hindsight по умолчанию)
  - fact_types — фильтр по fact_type ('world', 'experience', 'observation')

Staleness API:
  - RecallResponse.pending_consolidation — кол-во неконсолидированных фактов
  - RecallResponse.is_stale — True если pending_consolidation > 0
"""
import asyncio
import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

TOP_K = 10
SEMANTIC_THRESHOLD = 0.3
RRF_K = 60
MAX_ENTITY_FREQUENCY = 500   # пропускаем слишком частые сущности (как у Hindsight)
CAUSAL_WEIGHT_THRESHOLD = 0.3
RERANK_CANDIDATES = 30       # pre-filter перед reranking (как у Hindsight)

# Lazy singleton — загружается один раз при первом вызове
_ranker = None


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
    tags: list[str] = field(default_factory=list)
    score: float = 0.0
    sources: list[str] = field(default_factory=list)


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
    tags_raw = row["tags"] if "tags" in row.keys() else "[]"
    try:
        tags = _json.loads(tags_raw or "[]")
    except (ValueError, TypeError):
        tags = []
    return RecallResult(
        fact_id=row["fact_id"],
        fact=row["fact"],
        fact_type=row["fact_type"],
        occurred_start=row["occurred_start"] or None,
        occurred_end=row["occurred_end"] or None,
        mentioned_at=row["mentioned_at"] or None,
        document_id=row["document_id"] or None,
        tags=tags,
        score=score,
        sources=[source] if source else [],
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

def _tags_where(tags: Optional[list[str]], param_offset: int) -> tuple[str, list]:
    """OR-match по тегам (как Hindsight tags_match='any')."""
    if not tags:
        return "", []
    # SQLite JSON: json_each(tags) — проверяем вхождение любого тега
    conditions = " OR ".join(
        f"EXISTS (SELECT 1 FROM json_each(f.tags) WHERE value = ?)" for _ in tags
    )
    return f"AND ({conditions})", list(tags)


def _fact_types_where(fact_types: Optional[list[str]]) -> tuple[str, list]:
    if not fact_types:
        return "", []
    placeholders = ",".join("?" * len(fact_types))
    return f"AND fact_type IN ({placeholders})", list(fact_types)


# ── 2. BM25 search ─────────────────────────────────────────────────────────────

def _bm25_search(
    query: str,
    storage,
    limit: int,
    tags: Optional[list[str]] = None,
    fact_types: Optional[list[str]] = None,
) -> list[str]:
    """SQLite FTS5 + опциональный post-filter по тегам и fact_type."""
    tokens = [t for t in re.sub(r"[^\w\s]", " ", query.lower()).split() if t]
    if not tokens:
        return []
    fts_query = " OR ".join(tokens)
    try:
        tags_clause, tags_params = _tags_where(tags, 3)
        ft_clause,   ft_params   = _fact_types_where(fact_types)
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


def _graph_link_expansion(
    seed_ids: list[str],
    storage,
    budget: int,
) -> list[tuple[str, float]]:
    """
    LinkExpansion: 3 запроса, аналог Hindsight.

    Priority: entity co-occurrence > causal links > fallback (semantic/temporal/entity).
    Fallback score умножается на 0.5x как у Hindsight.
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


def _temporal_search(
    start: datetime,
    end: datetime,
    storage,
    limit: int,
    tags: Optional[list[str]] = None,
    fact_types: Optional[list[str]] = None,
) -> list[str]:
    """Факты в временном диапазоне + опциональные фильтры."""
    tags_clause, tags_params = _tags_where(tags, 8)
    ft_clause,   ft_params   = _fact_types_where(fact_types)
    rows = storage.conn.execute(
        f"""
        SELECT fact_id FROM facts f
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
    return [r["fact_id"] for r in rows]


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


# ── Cross-encoder reranking (FlashRank) ───────────────────────────────────────

def _get_ranker():
    """Lazy singleton FlashRank ranker."""
    global _ranker
    if _ranker is None:
        from flashrank import Ranker
        _ranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2", cache_dir=".cache/flashrank")
        log.info("[recall] FlashRank ranker loaded")
    return _ranker


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _rerank(query: str, results: list["RecallResult"]) -> list["RecallResult"]:
    """
    Cross-encoder reranking аналогично Hindsight.

    Формат документа (как у Hindsight):
      [Date: June 5, 2022 (2022-06-05)] fact_text
    """
    if not results:
        return results

    from flashrank import RerankRequest

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
        passages.append({"id": r.fact_id, "text": doc_text})

    try:
        ranker = _get_ranker()
        request = RerankRequest(query=query, passages=passages)
        reranked = ranker.rerank(request)

        id_to_score = {item["id"]: _sigmoid(item["score"]) for item in reranked}
        for r in results:
            r.score = id_to_score.get(r.fact_id, r.score)

        results.sort(key=lambda r: r.score, reverse=True)
    except Exception as e:
        log.warning("[recall] reranking failed, keeping RRF order: %s", e)

    return results


# ── Main recall ────────────────────────────────────────────────────────────────

def recall(
    query: str,
    query_vec,
    storage,
    limit: int = TOP_K,
    reference_date: Optional[datetime] = None,
    tags: Optional[list[str]] = None,
    fact_types: Optional[list[str]] = None,
) -> RecallResponse:
    """
    4-way поиск + RRF + cross-encoder reranking.
    Синхронный — вызывать через asyncio.to_thread.

    1. Semantic  — LanceDB cosine ≥ 0.3
    2. BM25      — SQLite FTS5 (+ tags/fact_types filter)
    3. Graph     — LinkExpansion от semantic seeds (entity > causal > fallback*0.5)
    4. Temporal  — dateparser + SQL date range (+ tags/fact_types filter)
    5. Reranking — FlashRank cross-encoder поверх топ-RERANK_CANDIDATES

    Возвращает RecallResponse с полями:
      .results              — список RecallResult
      .pending_consolidation — кол-во неконсолидированных фактов (Staleness API)
      .is_stale             — True если pending_consolidation > 0
      .freshness            — "up_to_date" | "slightly_stale" | "stale"
    """
    # 1. Semantic
    semantic_hits = _semantic_search(query_vec, storage, limit * 2)
    semantic_ids = [fid for fid, _ in semantic_hits]

    # 2. BM25
    bm25_ids = _bm25_search(query, storage, limit * 2, tags=tags, fact_types=fact_types)

    # 3. Graph — LinkExpansion от semantic seeds (граф не фильтруем по тегам — это постфильтр)
    graph_hits = _graph_link_expansion(semantic_ids[:limit], storage, budget=limit * 2)
    graph_ids = [fid for fid, _ in graph_hits]

    # 4. Temporal
    temporal_range = _extract_temporal_constraint(query, reference_date)
    temporal_ids = (
        _temporal_search(temporal_range[0], temporal_range[1], storage, limit,
                         tags=tags, fact_types=fact_types)
        if temporal_range else []
    )

    lists = [semantic_ids, bm25_ids, graph_ids]
    sources = ["semantic", "bm25", "graph"]
    if temporal_ids:
        lists.append(temporal_ids)
        sources.append("temporal")

    # RRF — берём RERANK_CANDIDATES кандидатов для reranker, потом обрезаем до limit
    merged = _rrf_merge(lists, sources)[:RERANK_CANDIDATES]
    if not merged:
        return RecallResponse(results=[], pending_consolidation=storage.get_pending_consolidation_count())

    merged_ids = [fid for fid, _, _ in merged]
    rrf_scores = {fid: score for fid, score, _ in merged}
    rrf_sources = {fid: srcs for fid, _, srcs in merged}

    # Post-фильтр по tags и fact_types для semantic/graph результатов
    tags_clause, tags_params = _tags_where(tags, 1)
    ft_clause,   ft_params   = _fact_types_where(fact_types)
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
        results.append(r)

    # 5. Cross-encoder reranking → обрезаем до limit
    results = _rerank(query, results)[:limit]

    pending = storage.get_pending_consolidation_count()
    log.info(
        "[recall] %r → %d results (sem=%d bm25=%d graph=%d temporal=%d pending=%d)",
        query[:60], len(results),
        len(semantic_ids), len(bm25_ids), len(graph_ids), len(temporal_ids), pending,
    )
    return RecallResponse(results=results, pending_consolidation=pending)


async def recall_async(
    query: str,
    query_vec,
    storage,
    limit: int = TOP_K,
    reference_date: Optional[datetime] = None,
    tags: Optional[list[str]] = None,
    fact_types: Optional[list[str]] = None,
) -> RecallResponse:
    return await asyncio.to_thread(
        recall, query, query_vec, storage, limit, reference_date, tags, fact_types
    )
