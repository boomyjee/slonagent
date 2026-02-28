"""reflect.py — консолидация фактов в наблюдения (observations).

Аналог Hindsight reflect / consolidation pipeline.

Пайплайн:
  1. Взять unconsolidated world/experience факты из БД
  2. Сгруппировать по общим сущностям (entity clusters)
  3. Для каждого кластера ≥ MIN_FACTS_PER_CLUSTER — запустить LLM
  4. LLM возвращает list[Observation] с цитатами и trend
  5. Observation сохраняется в facts (fact_type='observation')
     + evidence в observation_evidence
  6. Исходные факты помечаются как consolidated=1

Trend вычисляется по временным меткам evidence (как в Hindsight):
  new          — весь evidence < 7 дней
  strengthening — >60% evidence в последние 30 дней
  weakening    — <20% evidence в последние 30 дней, но есть старый
  stale        — нет evidence в последние 30 днях
  stable       — иначе
"""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

MIN_FACTS_PER_CLUSTER = 2     # минимальный размер кластера для консолидации
MIN_NEW_FACTS = 5             # не запускаем, если накоплено < N новых фактов
MAX_FACTS_PER_CLUSTER = 30    # ограничение на кластер, чтобы не перегружать LLM
MAX_CLUSTERS_PER_RUN = 10     # кол-во кластеров за один проход


# ── Prompts ────────────────────────────────────────────────────────────────────

CONSOLIDATION_PROMPT = """\
You are analyzing a set of facts to derive higher-level observations and patterns.

Facts (JSON):
{facts_json}

Identify meaningful PATTERNS or BELIEFS that emerge from MULTIPLE facts above.
Return ONLY valid JSON matching this schema exactly:

{{
  "observations": [
    {{
      "text": "Concise, precise pattern statement (1-2 sentences)",
      "trend": "stable|strengthening|weakening|new|stale",
      "evidence": [
        {{
          "fact_id": "<fact_id from the list above>",
          "quote": "Exact or near-exact text from the fact",
          "relevance": "One sentence: why this fact supports the observation"
        }}
      ]
    }}
  ]
}}

Rules:
- Each observation MUST cite ≥ 2 different facts as evidence
- Observations describe PATTERNS, not individual facts
- Do NOT repeat a single fact as a standalone observation
- Trend definitions:
    "new"          — ALL evidence is < 7 days old
    "strengthening" — > 60% of evidence occurred in the last 30 days (but some is older)
    "weakening"    — < 20% of evidence is recent (< 30 days), rest is older
    "stale"        — NO evidence in the last 30 days
    "stable"       — evidence distributed evenly across time
- Return {{"observations": []}} if no meaningful patterns exist
"""


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class ObservationEvidence:
    fact_id: str
    quote: str
    relevance: str = ""


@dataclass
class Observation:
    observation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    text: str = ""
    trend: str = "stable"
    evidence: list[ObservationEvidence] = field(default_factory=list)
    source_fact_ids: list[str] = field(default_factory=list)
    mentioned_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ── Trend computation ──────────────────────────────────────────────────────────

def _compute_trend(evidence_timestamps: list[datetime]) -> str:
    """
    Вычисляет trend по временным меткам evidence (аналог Hindsight Trend enum).
    """
    if not evidence_timestamps:
        return "stable"

    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=30)
    new_cutoff    = now - timedelta(days=7)

    # ensure tz-aware
    stamps = []
    for ts in evidence_timestamps:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        stamps.append(ts)

    if all(ts >= new_cutoff for ts in stamps):
        return "new"
    if all(ts < recent_cutoff for ts in stamps):
        return "stale"

    recent_count = sum(1 for ts in stamps if ts >= recent_cutoff)
    ratio = recent_count / len(stamps)
    if ratio > 0.6:
        return "strengthening"
    if ratio < 0.2:
        return "weakening"
    return "stable"


# ── Entity clustering ──────────────────────────────────────────────────────────

def _cluster_facts_by_entity(fact_rows, storage) -> list[list]:
    """
    Группирует факты по общим сущностям.
    Факты без сущностей попадают в отдельный кластер "mixed".
    """
    fact_ids = [r["fact_id"] for r in fact_rows]
    if not fact_ids:
        return []

    placeholders = ",".join("?" * len(fact_ids))

    # entity_id → [fact_id, ...]
    rows = storage.conn.execute(
        f"""
        SELECT fe.fact_id, fe.entity_id
        FROM fact_entities fe
        WHERE fe.fact_id IN ({placeholders})
        """,
        fact_ids,
    ).fetchall()

    entity_to_facts: dict[str, list[str]] = {}
    fact_to_entities: dict[str, list[str]] = {}
    for r in rows:
        entity_to_facts.setdefault(r["entity_id"], []).append(r["fact_id"])
        fact_to_entities.setdefault(r["fact_id"], []).append(r["entity_id"])

    row_map = {r["fact_id"]: r for r in fact_rows}

    visited: set[str] = set()
    clusters: list[list[sqlite3.Row]] = []

    # BFS-like expansion: факты, связанные общей сущностью, идут в один кластер
    for fid in fact_ids:
        if fid in visited:
            continue
        cluster_ids: set[str] = {fid}
        queue = [fid]
        while queue:
            current = queue.pop()
            for eid in fact_to_entities.get(current, []):
                for neighbor in entity_to_facts.get(eid, []):
                    if neighbor not in cluster_ids:
                        cluster_ids.add(neighbor)
                        queue.append(neighbor)
        visited.update(cluster_ids)
        cluster = [row_map[cid] for cid in cluster_ids if cid in row_map]
        clusters.append(cluster[:MAX_FACTS_PER_CLUSTER])

    # Факты без сущностей — в один общий кластер
    no_entity = [row_map[fid] for fid in fact_ids if fid not in fact_to_entities]
    if no_entity:
        clusters.append(no_entity[:MAX_FACTS_PER_CLUSTER])

    return [c for c in clusters if len(c) >= MIN_FACTS_PER_CLUSTER]


# ── LLM call ───────────────────────────────────────────────────────────────────

async def _extract_observations(
    cluster: list,
    client,
    model_name: str,
) -> list[Observation]:
    """Запрашивает LLM и парсит наблюдения из кластера фактов."""
    facts_data = [
        {
            "fact_id": r["fact_id"],
            "fact": r["fact"],
            "fact_type": r["fact_type"],
            "occurred_start": r["occurred_start"],
            "mentioned_at": r["mentioned_at"],
        }
        for r in cluster
    ]
    prompt = CONSOLIDATION_PROMPT.format(facts_json=json.dumps(facts_data, ensure_ascii=False, indent=2))

    try:
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            config={"response_mime_type": "application/json"},
        )
        raw = response.text.strip()
    except Exception as e:
        log.error("[reflect] LLM call failed: %s", e)
        return []

    try:
        data = json.loads(raw)
        obs_list = data.get("observations", [])
    except json.JSONDecodeError as e:
        log.warning("[reflect] JSON parse failed: %s | raw=%r", e, raw[:200])
        return []

    valid_fact_ids = {r["fact_id"] for r in cluster}
    fact_timestamps: dict[str, Optional[datetime]] = {}
    for r in cluster:
        ts_str = r["mentioned_at"] or r["occurred_start"]
        if ts_str:
            try:
                fact_timestamps[r["fact_id"]] = datetime.fromisoformat(ts_str)
            except ValueError:
                fact_timestamps[r["fact_id"]] = None

    observations: list[Observation] = []
    for item in obs_list:
        if not isinstance(item, dict):
            continue
        text = item.get("text", "").strip()
        if not text:
            continue

        raw_evidence = item.get("evidence", [])
        evidence = []
        for ev in raw_evidence:
            if not isinstance(ev, dict):
                continue
            fid = ev.get("fact_id", "")
            if fid not in valid_fact_ids:
                continue
            evidence.append(ObservationEvidence(
                fact_id=fid,
                quote=ev.get("quote", ""),
                relevance=ev.get("relevance", ""),
            ))

        if len(evidence) < MIN_FACTS_PER_CLUSTER:
            continue

        # Вычисляем trend по меткам evidence
        ev_timestamps = [
            fact_timestamps[e.fact_id]
            for e in evidence
            if e.fact_id in fact_timestamps and fact_timestamps[e.fact_id]
        ]
        trend = item.get("trend") or _compute_trend(ev_timestamps)

        obs = Observation(
            text=text,
            trend=trend,
            evidence=evidence,
            source_fact_ids=[e.fact_id for e in evidence],
        )
        observations.append(obs)

    return observations


# ── Storage helpers ────────────────────────────────────────────────────────────

def _store_observation(obs: Observation, storage) -> None:
    """Сохраняет одно наблюдение в SQLite и LanceDB через Storage."""
    now = datetime.now(timezone.utc).isoformat()

    storage.conn.execute(
        """
        INSERT OR IGNORE INTO facts
            (fact_id, fact, fact_type, mentioned_at, source_fact_ids)
        VALUES (?, ?, 'observation', ?, ?)
        """,
        (obs.observation_id, obs.text, now, json.dumps(obs.source_fact_ids)),
    )
    storage.conn.commit()

    storage.insert_observation_evidence(
        obs.observation_id,
        [{"fact_id": e.fact_id, "quote": e.quote, "relevance": e.relevance} for e in obs.evidence],
    )
    storage.mark_consolidated(obs.source_fact_ids)

    try:
        vec = storage.embed_fn(obs.text)
        storage.insert_vectors([{"fact_id": obs.observation_id, "mentioned_at": now, "vector": vec}])
    except Exception as e:
        log.warning("[reflect] LanceDB write failed for %s: %s", obs.observation_id, e)


# ── Public API ─────────────────────────────────────────────────────────────────

async def consolidate(
    storage,
    client,
    model_name: str,
    min_new_facts: int = MIN_NEW_FACTS,
) -> list[Observation]:
    """
    Консолидирует unconsolidated факты в observations.

    Возвращает список созданных observations (может быть пустым,
    если фактов ещё недостаточно).
    """
    fact_rows = await asyncio.to_thread(storage.get_unconsolidated_facts, 200)

    if len(fact_rows) < min_new_facts:
        log.debug(
            "[reflect] skip consolidation: %d facts < min_new_facts=%d",
            len(fact_rows), min_new_facts,
        )
        return []

    clusters = await asyncio.to_thread(_cluster_facts_by_entity, fact_rows, storage)
    clusters = clusters[:MAX_CLUSTERS_PER_RUN]

    log.info("[reflect] consolidating %d facts in %d clusters", len(fact_rows), len(clusters))

    all_observations: list[Observation] = []

    for cluster in clusters:
        obs_list = await _extract_observations(cluster, client, model_name)
        for obs in obs_list:
            await asyncio.to_thread(_store_observation, obs, storage)
            all_observations.append(obs)
            log.info(
                "[reflect] observation created: %r (trend=%s, evidence=%d)",
                obs.text[:80], obs.trend, len(obs.evidence),
            )

    log.info("[reflect] consolidation done: %d observations created", len(all_observations))
    return all_observations
