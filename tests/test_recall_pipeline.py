"""
Отладочный скрипт для recall-пайплайна.
Запускать из корня проекта:
  venv/Scripts/python tests/test_recall_pipeline.py

Показывает промежуточные результаты каждого шага:
  1. Semantic search
  2. BM25
  3. Graph link expansion
  4. Temporal search
  5. RRF merge
  6. Сравнение реранкеров: RRF / FlashRank / HuggingFace CrossEncoder
"""
import json
import os
import re
import sys

# Корень проекта — на уровень выше tests/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# ── Env / paths ────────────────────────────────────────────────────────────────

with open(os.path.join(ROOT, ".config.json")) as f:
    cfg = json.load(f)

for k, v in cfg.get("env", {}).items():
    os.environ.setdefault(k, v)

for k, v in list(os.environ.items()):
    if isinstance(v, str) and v.startswith("$"):
        os.environ[k] = os.environ.get(v[1:], v)

fact_cfg = next(
    p for p in cfg["agent"]["memory_providers"]
    if "FactProvider" in p["__class__"]
)
embedding_model_cfg = fact_cfg.get("embedding_model", {})
if isinstance(embedding_model_cfg, dict) and embedding_model_cfg.get("api_key", "").startswith("$"):
    embedding_model_cfg = dict(embedding_model_cfg)
    embedding_model_cfg["api_key"] = os.environ.get(
        embedding_model_cfg["api_key"][1:], embedding_model_cfg["api_key"]
    )

MEMORY_DIR   = os.path.join(ROOT, "memory")
SQLITE_PATH  = os.path.join(MEMORY_DIR, "fact", "facts.db")
LANCEDB_PATH = os.path.join(MEMORY_DIR, "fact", "lancedb")

# ── Init Storage ───────────────────────────────────────────────────────────────

from src.memory.providers.fact.storage import Storage
from src.memory.providers.fact.recall import (
    _semantic_search,
    _bm25_search,
    _graph_link_expansion,
    _extract_temporal_constraint,
    _temporal_search,
    _temporal_spread,
    _rrf_merge,
    _rerank,
    _apply_token_budget,
    _row_to_result,
    TOP_K, _BUDGET_FACTOR, RERANK_CANDIDATES,
)

print("Инициализация Storage...", flush=True)
storage = Storage(SQLITE_PATH, LANCEDB_PATH, embedding_model_cfg)
print("Storage готов.\n", flush=True)

# ── Параметры запроса ──────────────────────────────────────────────────────────

QUERY      = "диалог Тома и Длинного в спальне или жилом блоке в сценарии ТУПИК"
BUDGET     = "mid"
MAX_TOKENS = 4096
TYPES      = None
TAGS       = None

print(f"Запрос: {QUERY!r}")
print(f"Budget: {BUDGET}\n")
print("=" * 70)

factor = _BUDGET_FACTOR.get(BUDGET, 2)
limit  = TOP_K * factor

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_fact_text(fact_id: str) -> str:
    row = storage.conn.execute(
        "SELECT fact, fact_type FROM facts WHERE fact_id = ?", (fact_id,)
    ).fetchone()
    if not row:
        return f"[не найден: {fact_id}]"
    return f"[{row['fact_type']}] {row['fact']}"


def print_section(title: str):
    print(f"\n{'-' * 70}")
    print(f"  {title}")
    print(f"{'-' * 70}")


def print_ids(ids_with_score, label="score"):
    if not ids_with_score:
        print("  (пусто)")
        return
    for item in ids_with_score:
        fid, score = (item[0], item[1]) if isinstance(item, tuple) else (item, None)
        text = get_fact_text(fid)
        score_str = f"  [{label}={score:.3f}]" if score is not None else ""
        print(f"  {fid[:8]}...{score_str}")
        print(f"    {text[:120]}")


def print_plain_ids(ids):
    if not ids:
        print("  (пусто)")
        return
    for fid in ids:
        print(f"  {fid[:8]}...")
        print(f"    {get_fact_text(fid)[:120]}")


# ── Шаг 1: Semantic ────────────────────────────────────────────────────────────

print_section(f"1. SEMANTIC SEARCH (limit={limit * 2}, threshold=0.3)")
query_vec    = storage.encode_query(QUERY)
semantic_hits = _semantic_search(query_vec, storage, limit * 2)
semantic_ids  = [fid for fid, _ in semantic_hits]
print(f"  Найдено: {len(semantic_hits)}")
print_ids(semantic_hits, label="sim")

# ── Шаг 2: BM25 ────────────────────────────────────────────────────────────────

print_section(f"2. BM25 SEARCH (limit={limit * 2})")
tokens = [t for t in re.sub(r"[^\w\s]", " ", QUERY.lower()).split() if t]
print(f"  Токены: {tokens}")
print(f"  FTS-запрос: {' OR '.join(tokens)!r}")
bm25_ids = _bm25_search(QUERY, storage, limit * 2, tags=TAGS, types=TYPES)
print(f"  Найдено: {len(bm25_ids)}")
print_plain_ids(bm25_ids)

# ── Шаг 3: Graph ────────────────────────────────────────────────────────────────

print_section(f"3. GRAPH LINK EXPANSION (seeds={len(semantic_ids[:limit])})")
print(f"  Seeds (semantic top-{limit}):")
for fid in semantic_ids[:limit]:
    print(f"    {fid[:8]}... {get_fact_text(fid)[:80]}")
graph_hits = _graph_link_expansion(semantic_ids[:limit], storage, budget=limit * 2)
graph_ids  = [fid for fid, _ in graph_hits]
print(f"\n  Найдено через граф: {len(graph_hits)}")
print_ids(graph_hits, label="graph_score")

# ── Шаг 4: Temporal ────────────────────────────────────────────────────────────

print_section("4. TEMPORAL SEARCH")
temporal_range = _extract_temporal_constraint(QUERY, reference_date=None)
if temporal_range:
    t_start, t_end = temporal_range
    print(f"  Диапазон: {t_start} -- {t_end}")
    entry_hits    = _temporal_search(t_start, t_end, storage, limit, tags=TAGS, types=TYPES)
    entry_scores  = {fid: score for fid, score in entry_hits}
    spread_scores = _temporal_spread(entry_scores, t_start, t_end, storage, budget=limit * 2)
    temporal_ids  = sorted(spread_scores, key=lambda x: spread_scores[x], reverse=True)
    print(f"  Найдено (+ spreading): {len(temporal_ids)}")
    print_ids([(fid, spread_scores[fid]) for fid in temporal_ids], label="temporal_score")
else:
    temporal_ids = []
    print("  Временных выражений не найдено -- шаг пропущен.")

# ── Шаг 5: RRF ─────────────────────────────────────────────────────────────────

print_section(f"5. RRF MERGE -> top-{RERANK_CANDIDATES}")
lists   = [semantic_ids, bm25_ids, graph_ids]
sources = ["semantic", "bm25", "graph"]
if temporal_ids:
    lists.append(temporal_ids)
    sources.append("temporal")

merged = _rrf_merge(lists, sources)[:RERANK_CANDIDATES]
print(f"  Всего уникальных кандидатов до обрезки: {len(_rrf_merge(lists, sources))}")
print(f"  После обрезки до RERANK_CANDIDATES={RERANK_CANDIDATES}: {len(merged)}")
for fid, score, srcs in merged:
    print(f"  {fid[:8]}... [rrf={score:.4f}] sources={srcs}")
    print(f"    {get_fact_text(fid)[:120]}")

# ── Подготовка RecallResult ────────────────────────────────────────────────────

merged_ids  = [fid for fid, _, _ in merged]
rrf_scores  = {fid: score for fid, score, _ in merged}
rrf_sources = {fid: srcs for fid, _, srcs in merged}

placeholders = ",".join("?" * len(merged_ids))
rows = storage.conn.execute(
    f"SELECT * FROM facts f WHERE fact_id IN ({placeholders})", merged_ids,
).fetchall()
row_map = {r["fact_id"]: r for r in rows}

results = []
for fid in merged_ids:
    if fid not in row_map:
        continue
    r = _row_to_result(row_map[fid], score=rrf_scores[fid])
    r.sources = rrf_sources[fid]
    results.append(r)

# ── Шаг 6: Сравнение реранкеров ────────────────────────────────────────────────

TOP_N = 10

def show_top(label, ranked, n=TOP_N):
    print(f"\n  -- {label} --")
    for i, r in enumerate(ranked[:n], 1):
        print(f"  [{i:2}] score={r.score:.4f}  {r.fact[:100]}")


print_section("6. СРАВНЕНИЕ ВАРИАНТОВ РАНЖИРОВАНИЯ (top-10)")

# A: RRF only
show_top("A: RRF only (без реранкинга)", list(results))

# B: FlashRank MultiBERT (если установлен)
print()
try:
    from flashrank import Ranker, RerankRequest
    passages = [{"id": r.fact_id, "text": r.fact} for r in results]
    ranker = Ranker(model_name="ms-marco-MultiBERT-L-12", cache_dir=".cache/flashrank")
    reranked_data = ranker.rerank(RerankRequest(query=QUERY, passages=passages))
    import math
    id_to_score = {item["id"]: 1.0 / (1.0 + math.exp(-item["score"])) for item in reranked_data}
    variant_fr = list(results)
    for r in variant_fr:
        r.score = id_to_score.get(r.fact_id, r.score)
    variant_fr.sort(key=lambda r: r.score, reverse=True)
    show_top("B: FlashRank ms-marco-MultiBERT-L-12", variant_fr)
except Exception as e:
    print(f"  FlashRank недоступен: {e}")

# C: HuggingFace SentenceTransformers CrossEncoder mmarco multilingual
print()
try:
    from sentence_transformers import CrossEncoder
    print("  [HuggingFace] Загрузка cross-encoder/mmarco-mMiniLMv2-L12-H384-v1...")
    hf_model = CrossEncoder("cross-encoder/mmarco-mMiniLMv2-L12-H384-v1")
    variant_hf = list(results)
    scores = hf_model.predict([(QUERY, r.fact) for r in variant_hf])
    for r, s in zip(variant_hf, scores):
        r.score = float(s)
    variant_hf.sort(key=lambda r: r.score, reverse=True)
    show_top("C: HuggingFace cross-encoder/mmarco-mMiniLMv2-L12-H384-v1", variant_hf)
except Exception as e:
    print(f"  ОШИБКА HuggingFace: {e}")

print("\n" + "=" * 70)
print("Готово.")
