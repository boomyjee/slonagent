"""retain.py — извлечение фактов из текста и сохранение в LanceDB.

Промпты и chunk_text — 1 в 1 с Hindsight.

Pipeline:
  текст → chunk_text → [LLM параллельно] → Fact list → embeddings → LanceDB
"""
import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

log = logging.getLogger(__name__)

CHUNK_SIZE               = 3_000   # символов на чанк (DEFAULT_RETAIN_CHUNK_SIZE)
RETAIN_MAX_OUTPUT_TOKENS = 64_000  # лимит вывода LLM на чанк (DEFAULT_RETAIN_MAX_COMPLETION_TOKENS)
SECONDS_PER_FACT         = 0.01    # офсет между фактами для сохранения порядка
FACT_WORKERS             = 8       # макс. параллельных LLM-запросов при извлечении фактов
OBSERVATION_WORKERS      = 4       # макс. параллельных LLM-запросов при создании observations


# ── Prompts (verbatim from Hindsight) ─────────────────────────────────────────

_BASE_FACT_EXTRACTION_PROMPT = """Extract SIGNIFICANT facts from text. Be SELECTIVE - only extract facts worth remembering long-term.

LANGUAGE: MANDATORY — Detect the language of the input text and produce ALL output in that EXACT same language. You are STRICTLY FORBIDDEN from translating or switching to any other language. Every single word of your output must be in the same language as the input. Do NOT output in a different language under any circumstance.

{retain_mission_section}{extraction_guidelines}

══════════════════════════════════════════════════════════════════════════
FACT FORMAT - BE CONCISE
══════════════════════════════════════════════════════════════════════════

1. **what**: Core fact - concise but complete (1-2 sentences max)
2. **when**: Temporal info if mentioned. "N/A" if none. Use day name when known.
3. **where**: Location if relevant. "N/A" if none.
4. **who**: People involved with relationships. "N/A" if just general info.
5. **why**: Context/significance ONLY if important. "N/A" if obvious.

CONCISENESS: Capture the essence, not every word. One good sentence beats three mediocre ones.

══════════════════════════════════════════════════════════════════════════
COREFERENCE RESOLUTION
══════════════════════════════════════════════════════════════════════════

Link generic references to names when both appear:
- "my roommate" + "Emily" → use "Emily (user's roommate)"
- "the manager" + "Sarah" → use "Sarah (the manager)"

══════════════════════════════════════════════════════════════════════════
CLASSIFICATION
══════════════════════════════════════════════════════════════════════════

fact_kind:
- "event": Specific datable occurrence (set occurred_start/end)
- "conversation": Ongoing state, preference, trait (no dates)

fact_type:
- "world": About user's life, other people, external events
- "assistant": Interactions with assistant (requests, recommendations)

══════════════════════════════════════════════════════════════════════════
TEMPORAL HANDLING
══════════════════════════════════════════════════════════════════════════

Use "Event Date" from input as reference for relative dates.
- CRITICAL: Convert ALL relative temporal expressions to absolute dates in the fact text itself.
  "yesterday" → write the resolved date (e.g. "on November 12, 2024"), NOT the word "yesterday"
  "last night", "this morning", "today", "tonight" → convert to the resolved absolute date
- For events: set occurred_start AND occurred_end (same for point events)
- For conversation facts: NO occurred dates

══════════════════════════════════════════════════════════════════════════
ENTITIES
══════════════════════════════════════════════════════════════════════════

Include: people names, organizations, places, key objects, abstract concepts (career, friendship, etc.)
Always include "user" when fact is about the user.{examples}"""

_CONCISE_GUIDELINES = """══════════════════════════════════════════════════════════════════════════
SELECTIVITY - CRITICAL (Reduces 90% of unnecessary output)
══════════════════════════════════════════════════════════════════════════

ONLY extract facts that are:
✅ Personal info: names, relationships, roles, background
✅ Preferences: likes, dislikes, habits, interests (e.g., "Alice likes coffee")
✅ Significant events: milestones, decisions, achievements, changes
✅ Plans/goals: future intentions, deadlines, commitments
✅ Expertise: skills, knowledge, certifications, experience
✅ Important context: projects, problems, constraints
✅ Sensory/emotional details: feelings, sensations, perceptions that provide context
✅ Observations: descriptions of people, places, things with specific details

DO NOT extract:
❌ Generic greetings: "how are you", "hello", pleasantries without substance
❌ Pure filler: "thanks", "sounds good", "ok", "got it", "sure"
❌ Process chatter: "let me check", "one moment", "I'll look into it"
❌ Repeated info: if already stated, don't extract again

CONSOLIDATE related statements into ONE fact when possible."""

_CONCISE_EXAMPLES = """

══════════════════════════════════════════════════════════════════════════
EXAMPLES (shown in English for illustration; for non-English input, ALL output values MUST be in the input language)
══════════════════════════════════════════════════════════════════════════

Example 1 - Selective extraction (Event Date: June 10, 2024):
Input: "Hey! How's it going? Good morning! So I'm planning my wedding - want a small outdoor ceremony. Just got back from Emily's wedding, she married Sarah at a rooftop garden. It was nice weather. I grabbed a coffee on the way."

Output: ONLY 2 facts (skip greetings, weather, coffee):
1. what="User planning wedding, wants small outdoor ceremony", who="user", why="N/A", entities=["user", "wedding"]
2. what="Emily married Sarah at rooftop garden", who="Emily (user's friend), Sarah", occurred_start="2024-06-09", entities=["Emily", "Sarah", "wedding"]

Example 2 - Professional context:
Input: "Alice has 5 years of Kubernetes experience and holds CKA certification. She's been leading the infrastructure team since March. By the way, she prefers dark roast coffee."

Output: ONLY 2 facts (skip coffee preference - too trivial):
1. what="Alice has 5 years Kubernetes experience, CKA certified", who="Alice", entities=["Alice", "Kubernetes", "CKA"]
2. what="Alice leads infrastructure team since March", who="Alice", entities=["Alice", "infrastructure"]

══════════════════════════════════════════════════════════════════════════
QUALITY OVER QUANTITY
══════════════════════════════════════════════════════════════════════════

Ask: "Would this be useful to recall in 6 months?" If no, skip it.

IMPORTANT: Sensory/emotional details and observations that provide meaningful context
about experiences ARE important to remember, even if they seem small (e.g., how food
tasted, how someone looked, how loud music was). Extract these if they characterize
an experience or person."""

_CAUSAL_RELATIONSHIPS_SECTION = """

══════════════════════════════════════════════════════════════════════════
CAUSAL RELATIONSHIPS
══════════════════════════════════════════════════════════════════════════

Link facts with causal_relations (max 2 per fact). target_index must be < this fact's index.
Type: "caused_by" (this fact was caused by the target fact)

Example: "Lost job → couldn't pay rent → moved apartment"
- Fact 0: Lost job, causal_relations: null
- Fact 1: Couldn't pay rent, causal_relations: [{"target_index": 0, "relation_type": "caused_by", "strength": 1.0}]
- Fact 2: Moved apartment, causal_relations: [{"target_index": 1, "relation_type": "caused_by", "strength": 1.0}]"""

_OUTPUT_FORMAT_SECTION = """

══════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT
══════════════════════════════════════════════════════════════════════════

ALWAYS return valid JSON in this exact structure:
{"facts": [...]}

If no significant facts found, return: {"facts": []}
NEVER return plain text. NEVER omit the JSON wrapper."""

CONCISE_FACT_EXTRACTION_PROMPT = _BASE_FACT_EXTRACTION_PROMPT.format(
    retain_mission_section="",
    extraction_guidelines=_CONCISE_GUIDELINES,
    examples=_CONCISE_EXAMPLES,
) + _CAUSAL_RELATIONSHIPS_SECTION + _OUTPUT_FORMAT_SECTION


def _build_extraction_prompt(retain_mission: str = "", custom_instructions: str = "") -> str:
    """Строит промпт извлечения фактов с опциональными mission и custom_instructions."""
    if retain_mission:
        mission_section = f"## RETAIN MISSION\n{retain_mission}\n\n"
    else:
        mission_section = ""

    if custom_instructions:
        guidelines = custom_instructions
    else:
        guidelines = _CONCISE_GUIDELINES

    return (
        _BASE_FACT_EXTRACTION_PROMPT.format(
            retain_mission_section=mission_section,
            extraction_guidelines=guidelines,
            examples=_CONCISE_EXAMPLES,
        )
        + _CAUSAL_RELATIONSHIPS_SECTION
        + _OUTPUT_FORMAT_SECTION
    )


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class RetainItem:
    content: str
    context: str = ""
    event_date: Optional[datetime] = field(default_factory=datetime.utcnow)
    document_id: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    retain_mission: str = ""
    custom_instructions: str = ""
    metadata: Optional[dict] = None
    entities: Optional[list[str]] = None


@dataclass
class Fact:
    fact_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    fact: str = ""
    fact_type: str = "world"
    occurred_start: Optional[str] = None
    occurred_end: Optional[str] = None
    mentioned_at: Optional[str] = None
    entities: list[str] = field(default_factory=list)
    document_id: Optional[str] = None
    chunk_id: Optional[str] = None
    context: str = ""
    tags: list[str] = field(default_factory=list)
    # causal_relations: [(target_fact_id, strength), ...] — заполняется после gather
    causal_relations: list[tuple[str, float]] = field(default_factory=list)


# ── Text chunking (verbatim from Hindsight) ────────────────────────────────────

def chunk_text(text: str, max_chars: int = CHUNK_SIZE) -> list[str]:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    if len(text) <= max_chars:
        return [text]

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=max_chars,
        chunk_overlap=0,
        length_function=len,
        is_separator_regex=False,
        separators=[
            "\n\n",
            "\n",
            ". ",
            "! ",
            "? ",
            "; ",
            ", ",
            " ",
            "",
        ],
    )
    return splitter.split_text(text)


# ── Temporal helpers (verbatim from Hindsight) ────────────────────────────────

def _infer_temporal_date(fact_text: str, event_date: datetime) -> Optional[str]:
    fact_lower = fact_text.lower()
    temporal_patterns = {
        r"\blast night\b": -1,
        r"\byesterday\b": -1,
        r"\btoday\b": 0,
        r"\bthis morning\b": 0,
        r"\bthis afternoon\b": 0,
        r"\bthis evening\b": 0,
        r"\btonigh?t\b": 0,
        r"\btomorrow\b": 1,
        r"\blast week\b": -7,
        r"\bthis week\b": 0,
        r"\bnext week\b": 7,
        r"\blast month\b": -30,
        r"\bthis month\b": 0,
        r"\bnext month\b": 30,
    }
    for pattern, offset_days in temporal_patterns.items():
        if re.search(pattern, fact_lower):
            target = event_date + timedelta(days=offset_days)
            return target.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return None


def _sanitize_text(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    text = text.replace("\x00", "")
    return re.sub(r"[\ud800-\udfff]", "", text)


# ── User message (matches Hindsight format) ────────────────────────────────────

def _build_user_message(
    chunk: str,
    chunk_index: int,
    total_chunks: int,
    event_date: Optional[datetime],
    context: str = "",
    metadata: Optional[dict] = None,
) -> str:
    sanitized_chunk = _sanitize_text(chunk)
    sanitized_context = _sanitize_text(context) if context else "none"

    # event_date=None означает документ без явной даты — не вставляем Event Date,
    # чтобы LLM не привязывал факты к дате загрузки.
    event_date_line = (
        f"Event Date: {event_date.strftime('%A, %B %d, %Y')} ({event_date.isoformat()})\n"
        if event_date else ""
    )

    metadata_line = ""
    if metadata:
        meta_str = ", ".join(f"{k}={v}" for k, v in metadata.items())
        metadata_line = f"Metadata: {meta_str}\n"

    return f"""Extract facts from the following text chunk.

Chunk: {chunk_index + 1}/{total_chunks}
{event_date_line}{metadata_line}Context: {sanitized_context}

Text:
{sanitized_chunk}"""


# ── LLM extraction ─────────────────────────────────────────────────────────────

def _parse_facts_from_json(
    raw_json: dict, event_date: datetime, mentioned_at: str
) -> tuple[list[Fact], bool]:
    """Возвращает (facts, has_malformed)."""
    facts = []
    raw_facts = raw_json.get("facts", [])
    has_malformed = False

    for i, item in enumerate(raw_facts):
        if not isinstance(item, dict):
            has_malformed = True
            continue

        def get_value(field_name):
            value = item.get(field_name)
            if value and value != "" and value != [] and str(value).upper() != "N/A":
                return value
            return None

        what = get_value("what")
        if not what:
            continue

        when = get_value("when")
        who  = get_value("who")
        why  = get_value("why")

        combined_parts = [what]
        if when:
            combined_parts.append(f"When: {when}")
        if who:
            combined_parts.append(f"Involving: {who}")
        if why:
            combined_parts.append(why)
        combined_text = " | ".join(combined_parts)

        fact_type_raw = item.get("fact_type", "world")
        fact_type: Literal["world", "experience"] = (
            "experience" if fact_type_raw == "assistant" else "world"
        )
        if fact_type not in ("world", "experience"):
            fact_type = "world"

        fact_kind = item.get("fact_kind", "conversation")
        if fact_kind not in ("conversation", "event"):
            fact_kind = "conversation"

        occurred_start = None
        occurred_end = None
        if fact_kind == "event":
            occurred_start = get_value("occurred_start")
            if not occurred_start:
                occurred_start = _infer_temporal_date(combined_text, event_date)
            occurred_end = get_value("occurred_end") or occurred_start

        raw_entities = get_value("entities") or []
        entities = []
        for ent in raw_entities:
            if isinstance(ent, str):
                entities.append(ent)
            elif isinstance(ent, dict) and "text" in ent:
                entities.append(ent["text"])

        raw_causal = item.get("causal_relations") or []
        # Сохраняем как локальные индексы внутри чанка — резолвим в fact_id после gather
        local_causal = []
        for rel in raw_causal:
            if not isinstance(rel, dict):
                continue
            target_idx = rel.get("target_index")
            relation_type = rel.get("relation_type")
            strength = float(rel.get("strength", 1.0))
            if target_idx is None or relation_type != "caused_by":
                continue
            if not isinstance(target_idx, int) or target_idx < 0 or target_idx >= i:
                continue
            local_causal.append((target_idx, strength))

        try:
            f = Fact(
                fact=combined_text,
                fact_type=fact_type,
                occurred_start=occurred_start,
                occurred_end=occurred_end,
                mentioned_at=mentioned_at,
                entities=entities,
            )
        except Exception as e:
            log.error("[retain] failed to create Fact at index %d: %s", i, e, exc_info=True)
            has_malformed = True
            continue
        f._local_causal = local_causal  # временный атрибут, удалим после резолва
        facts.append(f)

    return facts, has_malformed


class _OutputTooLongError(Exception):
    """LLM обрезал вывод — чанк нужно разделить."""


async def _extract_from_chunk(
    chunk: str,
    chunk_index: int,
    total_chunks: int,
    event_date: datetime,
    context: str,
    client,
    model_name: str,
    retain_mission: str = "",
    custom_instructions: str = "",
    metadata: Optional[dict] = None,
) -> list[Fact]:
    """Один LLM-запрос. Бросает _OutputTooLongError если ответ был обрезан."""
    effective_date = event_date or datetime.now(timezone.utc)
    user_message = _build_user_message(chunk, chunk_index, total_chunks, event_date, context, metadata)
    mentioned_at = effective_date.isoformat()
    system_prompt = _build_extraction_prompt(retain_mission, custom_instructions)

    for attempt in range(3):
        try:
            from google.genai import types as genai_types
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=model_name,
                contents=[
                    {"role": "user", "parts": [{"text": system_prompt}]},
                    {"role": "model", "parts": [{"text": "Understood. I will extract significant facts from the text you provide."}]},
                    {"role": "user", "parts": [{"text": user_message}]},
                ],
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=RETAIN_MAX_OUTPUT_TOKENS,
                    response_mime_type="application/json",
                ),
            )

            # Проверяем, был ли вывод обрезан по лимиту токенов
            candidate = response.candidates[0] if response.candidates else None
            if candidate is not None:
                reason = str(getattr(candidate, "finish_reason", "")).upper()
                if "MAX_TOKEN" in reason or reason == "2":
                    raise _OutputTooLongError(f"finish_reason={reason}")

            raw = response.text or ""
            m = re.search(r"\{", raw)
            if not m:
                log.warning("[retain] no JSON object in response (attempt %d): %r", attempt + 1, raw[:300])
                continue

            data, _ = json.JSONDecoder().raw_decode(raw, m.start())
            facts, has_malformed = _parse_facts_from_json(data, effective_date, mentioned_at)
            raw_count = len(data.get("facts", []))
            if has_malformed and raw_count > 0 and len(facts) < raw_count * 0.8 and attempt < 2:
                log.warning(
                    "[retain] chunk %d: %d/%d facts valid on attempt %d, retrying...",
                    chunk_index, len(facts), raw_count, attempt + 1,
                )
                continue
            log.info("[retain] chunk %d/%d: extracted %d facts", chunk_index + 1, total_chunks, len(facts))
            return facts

        except _OutputTooLongError:
            raise  # пробрасываем наверх для auto-split
        except Exception as e:
            log.warning("[retain] chunk %d extraction attempt %d failed: %s", chunk_index, attempt + 1, e)

    return []


def _split_chunk(chunk: str) -> tuple[str, str]:
    """Делит чанк пополам по ближайшей границе предложения."""
    mid = len(chunk) // 2
    search_range = int(len(chunk) * 0.2)
    search_start = max(0, mid - search_range)
    search_end   = min(len(chunk), mid + search_range)

    best_split = mid
    for ending in ("\n\n", ". ", "! ", "? "):
        pos = chunk.rfind(ending, search_start, search_end)
        if pos != -1:
            best_split = pos + len(ending)
            break

    return chunk[:best_split].strip(), chunk[best_split:].strip()


async def _extract_from_chunk_auto_split(
    chunk: str,
    chunk_index: int,
    total_chunks: int,
    event_date: datetime,
    context: str,
    client,
    model_name: str,
    retain_mission: str = "",
    custom_instructions: str = "",
    metadata: Optional[dict] = None,
) -> list[Fact]:
    """Обёртка с рекурсивным авто-сплитом при OutputTooLong."""
    try:
        return await _extract_from_chunk(
            chunk, chunk_index, total_chunks, event_date, context, client, model_name,
            retain_mission, custom_instructions, metadata,
        )
    except _OutputTooLongError:
        first, second = _split_chunk(chunk)
        if not first or not second:
            log.warning("[retain] chunk %d: cannot split further, skipping", chunk_index)
            return []

        log.info(
            "[retain] chunk %d too long (%d chars) → split into %d + %d chars",
            chunk_index, len(chunk), len(first), len(second),
        )
        sub_results = await asyncio.gather(
            _extract_from_chunk_auto_split(
                first,  chunk_index, total_chunks, event_date, context, client, model_name,
                retain_mission, custom_instructions, metadata,
            ),
            _extract_from_chunk_auto_split(
                second, chunk_index, total_chunks, event_date, context, client, model_name,
                retain_mission, custom_instructions, metadata,
            ),
        )
        return sub_results[0] + sub_results[1]


async def extract_facts(
    items: list[RetainItem],
    client,
    model_name: str,
) -> tuple[list[Fact], list[tuple[str, int, str]]]:
    """
    Извлекает факты из списка items, все чанки всех items обрабатываются параллельно.
    Возвращает (facts, chunk_meta) где chunk_meta: [(document_id, chunk_index, chunk_text), ...]
    только для items с document_id.
    """
    if not items:
        return [], []

    tasks = []
    task_meta = []  # (item_idx, chunk_idx, chunk_text)
    sem = asyncio.Semaphore(FACT_WORKERS)

    async def _limited(coro):
        async with sem:
            return await coro

    for item_idx, item in enumerate(items):
        chunks = chunk_text(item.content)
        for chunk_idx, chunk in enumerate(chunks):
            tasks.append(_limited(_extract_from_chunk_auto_split(
                chunk, chunk_idx, len(chunks), item.event_date, item.context, client, model_name,
                item.retain_mission, item.custom_instructions, item.metadata,
            )))
            task_meta.append((item_idx, chunk_idx, chunk))

    results = await asyncio.gather(*tasks)

    # Собираем факты и резолвим causal local indices → fact_ids
    all_facts = []
    chunk_meta_out = []  # [(document_id, chunk_index, chunk_text)]

    for (item_idx, chunk_idx, chunk_text_str), chunk_facts in zip(task_meta, results):
        item = items[item_idx]
        doc_id = item.document_id

        # Трекинг чанков для документов
        if doc_id:
            chunk_meta_out.append((doc_id, chunk_idx, chunk_text_str))

        # Chunk_id для фактов из документов
        chunk_id = f"{doc_id}_{chunk_idx}" if doc_id else None

        # Резолвим causal: local index → fact_id внутри этого чанка
        for local_pos, fact in enumerate(chunk_facts):
            fact.document_id = doc_id
            fact.chunk_id = chunk_id
            fact.context = item.context
            fact.tags = list(item.tags)
            # Мёрдж user-provided entities (R6)
            if item.entities:
                existing = set(fact.entities)
                for ent in item.entities:
                    if ent not in existing:
                        fact.entities.append(ent)
                        existing.add(ent)
            local_causal = getattr(fact, "_local_causal", [])
            fact.causal_relations = [
                (chunk_facts[target_idx].fact_id, strength)
                for target_idx, strength in local_causal
                if target_idx < len(chunk_facts)
            ]
            if hasattr(fact, "_local_causal"):
                del fact._local_causal

        all_facts.extend(chunk_facts)

    # Временные офсеты для сохранения порядка (как у Hindsight)
    for i, fact in enumerate(all_facts):
        offset = timedelta(seconds=i * SECONDS_PER_FACT)
        if fact.occurred_start:
            try:
                fact.occurred_start = (datetime.fromisoformat(fact.occurred_start) + offset).isoformat()
            except ValueError:
                pass
        if fact.occurred_end:
            try:
                fact.occurred_end = (datetime.fromisoformat(fact.occurred_end) + offset).isoformat()
            except ValueError:
                pass
        if fact.mentioned_at:
            try:
                fact.mentioned_at = (datetime.fromisoformat(fact.mentioned_at) + offset).isoformat()
            except ValueError:
                pass

    log.info("[retain] extracted %d facts from %d items", len(all_facts), len(items))
    return all_facts, chunk_meta_out


# ── Embedding augmentation (matches Hindsight) ────────────────────────────────

def _format_date(iso_str: Optional[str]) -> str:
    """Форматирует ISO дату в читаемый вид для augmentation."""
    if not iso_str:
        return "unknown date"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%B %Y")
    except ValueError:
        return iso_str


def augment_text_for_embedding(fact: Fact) -> str:
    """
    Добавляет дату к тексту факта перед embedding.
    Hindsight: f"{fact_text} (happened in {readable_date})"
    """
    date_str = _format_date(fact.occurred_start or fact.mentioned_at)
    return f"{fact.fact} (happened in {date_str})"


# ── Deduplication (simplified Hindsight logic for LanceDB) ────────────────────

DEDUP_SIMILARITY_THRESHOLD = 0.95
DEDUP_TIME_WINDOW_HOURS = 24


def _is_within_time_window(iso_a: Optional[str], iso_b: Optional[str], hours: int) -> bool:
    if not iso_a or not iso_b:
        return True  # нет временной инфы — считаем потенциальным дублем
    try:
        a = datetime.fromisoformat(iso_a)
        b = datetime.fromisoformat(iso_b)
        return abs((a - b).total_seconds()) <= hours * 3600
    except ValueError:
        return True


def deduplicate(
    facts: list[Fact],
    storage,
) -> tuple[list[Fact], list]:
    """
    Фильтрует дубли по cosine similarity + time window.
    Возвращает (new_facts, vectors) — векторы уже посчитаны, переиспользуем в store_facts.
    """
    if not facts:
        return [], []

    augmented = [augment_text_for_embedding(f) for f in facts]
    vectors = storage.encode_texts(augmented)

    try:
        if storage.table.count_rows() == 0:
            return facts, vectors
    except Exception:
        return facts, vectors

    new_facts = []
    new_vectors = []
    for fact, vec in zip(facts, vectors):
        try:
            results = storage.table.search(vec).limit(1).to_list()
        except Exception as e:
            log.warning("[retain] deduplicate search failed: %s", e, exc_info=True)
            new_facts.append(fact)
            new_vectors.append(vec)
            continue
        if not results:
            new_facts.append(fact)
            new_vectors.append(vec)
            continue

        top = results[0]
        similarity = 1.0 - top.get("_distance", 1.0)
        if similarity >= DEDUP_SIMILARITY_THRESHOLD and _is_within_time_window(
            fact.mentioned_at, top.get("mentioned_at"), DEDUP_TIME_WINDOW_HOURS
        ):
            log.debug("[retain] dedup skip: similarity=%.3f fact=%s", similarity, fact.fact[:60])
        else:
            new_facts.append(fact)
            new_vectors.append(vec)

    skipped = len(facts) - len(new_facts)
    if skipped:
        log.info("[retain] deduplication: skipped %d duplicates, keeping %d", skipped, len(new_facts))
    return new_facts, new_vectors


# ── Graph link building ────────────────────────────────────────────────────────

SEMANTIC_LINK_THRESHOLD = 0.85
TEMPORAL_LINK_WINDOW_HOURS = 24


def build_semantic_links(facts: list[Fact], vectors: list, storage) -> int:
    """Ищет похожие факты в LanceDB и вставляет semantic links в SQLite."""
    if not facts:
        return 0
    links = []
    for fact, vec in zip(facts, vectors):
        results = storage.search_vectors(vec, limit=5)
        for r in results:
            if r["fact_id"] == fact.fact_id:
                continue
            similarity = 1.0 - r.get("_distance", 1.0)
            if similarity >= SEMANTIC_LINK_THRESHOLD:
                links.append((fact.fact_id, r["fact_id"], "semantic", float(similarity)))
    storage.insert_links(links)
    return len(links)


def build_causal_links(facts: list[Fact], storage) -> int:
    """Вставляет causal links из fact.causal_relations."""
    links = [
        (fact.fact_id, target_id, "caused_by", strength)
        for fact in facts
        for target_id, strength in fact.causal_relations
        if target_id != fact.fact_id
    ]
    storage.insert_links(links)
    return len(links)


def build_temporal_links(facts: list[Fact], storage) -> int:
    """Ищет факты в том же временном окне и вставляет temporal links.

    Вес = max(0.3, 1.0 - time_diff / window_seconds) — убывает с расстоянием по времени.
    """
    links = []
    window_secs = TEMPORAL_LINK_WINDOW_HOURS * 3600
    for fact in facts:
        if not fact.mentioned_at:
            continue
        try:
            ts = datetime.fromisoformat(fact.mentioned_at)
        except ValueError:
            continue
        window_start = (ts - timedelta(hours=TEMPORAL_LINK_WINDOW_HOURS)).isoformat()
        window_end   = (ts + timedelta(hours=TEMPORAL_LINK_WINDOW_HOURS)).isoformat()
        for n in storage.get_facts_in_time_window(window_start, window_end, fact.fact_id):
            try:
                n_ts = datetime.fromisoformat(n["mentioned_at"])
                time_diff = abs((ts - n_ts).total_seconds())
                weight = max(0.3, 1.0 - time_diff / window_secs)
            except (ValueError, TypeError):
                weight = 1.0
            links.append((fact.fact_id, n["fact_id"], "temporal", weight))
    storage.insert_links(links)
    return len(links)


# ── Storage ────────────────────────────────────────────────────────────────────

def store_facts(
    facts: list[Fact],
    storage,
    chunk_meta: list[tuple[str, int, str]] | None = None,
) -> tuple[list[Fact], list]:
    """
    Дедуплицирует и сохраняет факты через Storage.
    Возвращает (new_facts, vectors) для построения графа.
    """
    if not facts:
        return [], []

    facts, vectors = deduplicate(facts, storage)
    if not facts:
        return [], []

    if chunk_meta:
        chunks_by_doc: dict[str, list[tuple[int, str]]] = {}
        for doc_id, chunk_idx, chunk_text in chunk_meta:
            chunks_by_doc.setdefault(doc_id, []).append((chunk_idx, chunk_text))
        for doc_id, doc_chunks in chunks_by_doc.items():
            storage.insert_chunks(doc_id, doc_chunks)

    storage.insert_facts(facts)
    storage.upsert_entities(facts)
    storage.build_entity_links(facts)

    storage.insert_vectors([
        {"fact_id": f.fact_id, "mentioned_at": f.mentioned_at or "", "vector": v}
        for f, v in zip(facts, vectors)
    ])

    log.info("[retain] stored %d facts", len(facts))
    return facts, vectors


# ── Consolidation (observation pipeline) ──────────────────────────────────────

CONSOLIDATION_LLM_BATCH_SIZE = 8   # фактов на один LLM-вызов (1:1 с Hindsight)

_CONSOLIDATION_PROMPT = """\
You are a memory consolidation system. Synthesize facts into observations \
and merge with existing observations when appropriate.

## MISSION
Track every detail: names, numbers, dates, places, and relationships. \
Prefer specifics over abstractions, never generalise.

Processing rules (always apply):
- REDUNDANT: same info worded differently → UPDATE the existing observation.
- CONTRADICTION/UPDATE: capture both states with temporal markers ("used to X, now Y").
- RESOLVE REFERENCES: when a new fact provides a concrete value resolving a vague \
placeholder in an existing observation, UPDATE the observation to embed the resolved \
value explicitly.
- NEVER merge observations about different people or unrelated topics.

NEW FACTS:
{facts_text}

EXISTING OBSERVATIONS (JSON array, pooled from recalls across all facts above):
{observations_text}

Each observation includes:
- id: unique identifier for updating
- text: the observation content

Compare the facts against existing observations:
- Same topic as an existing observation → UPDATE it (observation_id + source_fact_ids)
- New topic with durable knowledge → CREATE a new observation (source_fact_ids)
- Cross-reference facts within the batch: a later fact may resolve a vague reference in an earlier one
- Purely ephemeral facts → omit them (no create/update needed)

Output a JSON object with three arrays.

Example:
{{"creates": [{{"text": "Alice lives in Berlin", "source_fact_ids": ["uuid1", "uuid2"]}}],
  "updates": [{{"text": "Alice works at Acme Corp as senior engineer", "observation_id": "uuid3", "source_fact_ids": ["uuid4"]}}],
  "deletes": [{{"observation_id": "uuid5"}}]}}

Rules:
- "source_fact_ids": copy the EXACT UUID strings shown in brackets [uuid] from NEW FACTS.
- "observation_id": copy the EXACT "id" string from EXISTING OBSERVATIONS.
- "deletes": only when an observation is directly superseded or contradicted by new facts.
- Return {{"creates": [], "updates": [], "deletes": []}} if nothing durable is found.\
"""


@dataclass
class _CreateAction:
    text: str
    source_fact_ids: list[str]


@dataclass
class _UpdateAction:
    text: str
    observation_id: str
    source_fact_ids: list[str]


@dataclass
class _DeleteAction:
    observation_id: str


@dataclass
class _BatchResponse:
    creates: list[_CreateAction] = field(default_factory=list)
    updates: list[_UpdateAction] = field(default_factory=list)
    deletes: list[_DeleteAction] = field(default_factory=list)


async def _find_related_observations(fact_text: str, storage) -> list:
    """Async recall existing observations related to fact_text."""
    from src.memory.providers.fact.recall import recall_async
    q_vec = await asyncio.to_thread(storage.encode_query, fact_text[:1000])
    resp = await recall_async(fact_text, q_vec, storage, types=["observation"], max_tokens=512, budget="low")
    return resp.results


def _build_observations_for_prompt(union_obs: list, storage) -> list[dict]:
    """Строит обогащённый список observations для промпта: proof_count + source_memories."""
    obs_list = []
    for o in union_obs:
        row = storage.conn.execute(
            "SELECT source_fact_ids FROM facts WHERE fact_id = ?", (o.fact_id,)
        ).fetchone()
        source_ids: list[str] = json.loads(row["source_fact_ids"] or "[]") if row else []
        source_rows = storage.get_facts_by_ids(source_ids) if source_ids else []

        obs_data: dict = {
            "id": o.fact_id,
            "text": o.fact,
            "proof_count": len(source_ids) or 1,
        }
        if o.occurred_start:
            obs_data["occurred_start"] = o.occurred_start
        if o.mentioned_at:
            obs_data["mentioned_at"] = o.mentioned_at
        if source_rows:
            obs_data["source_memories"] = [
                {k: v for k, v in {"text": r["fact"], "mentioned_at": r["mentioned_at"]}.items() if v}
                for r in source_rows
            ]
        obs_list.append(obs_data)
    return obs_list


async def _consolidate_llm_batch(batch: list, union_obs: list, storage, client, model_name: str) -> _BatchResponse:
    """Single LLM call: batch of facts + recalled observations → creates/updates/deletes."""
    facts_text = "\n".join(
        " | ".join(filter(None, [
            f"[{r['fact_id']}] {r['fact']}",
            f"occurred_start={r['occurred_start']}" if r["occurred_start"] else None,
            f"occurred_end={r['occurred_end']}" if r["occurred_end"] else None,
            f"mentioned_at={r['mentioned_at']}" if r["mentioned_at"] else None,
        ]))
        for r in batch
    )
    obs_list = await asyncio.to_thread(_build_observations_for_prompt, union_obs, storage)
    observations_text = json.dumps(obs_list, ensure_ascii=False, indent=2)
    prompt = _CONSOLIDATION_PROMPT.format(facts_text=facts_text, observations_text=observations_text)

    for attempt in range(3):
        try:
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=[{"role": "user", "parts": [{"text": prompt}]}],
                config={"response_mime_type": "application/json"},
            )
            if not response.text:
                raise ValueError("empty response from LLM")
            data = json.loads(response.text.strip())
            result = _BatchResponse(
                creates=[
                    _CreateAction(text=c["text"], source_fact_ids=c.get("source_fact_ids", []))
                    for c in data.get("creates", []) if isinstance(c, dict) and c.get("text")
                ],
                updates=[
                    _UpdateAction(text=u["text"], observation_id=u["observation_id"], source_fact_ids=u.get("source_fact_ids", []))
                    for u in data.get("updates", []) if isinstance(u, dict) and u.get("text") and u.get("observation_id")
                ],
                deletes=[
                    _DeleteAction(observation_id=d["observation_id"])
                    for d in data.get("deletes", []) if isinstance(d, dict) and d.get("observation_id")
                ],
            )
            log.info(
                "[consolidate] LLM decision: +%d create / %d update / %d delete",
                len(result.creates), len(result.updates), len(result.deletes),
            )
            return result
        except Exception as e:
            log.warning("[consolidate] LLM call failed (attempt %d/3): %s", attempt + 1, e)
    return _BatchResponse()


def _min_str_date(dates) -> Optional[str]:
    """Минимальная дата из итерируемого (строки ISO или None)."""
    valid = [d for d in dates if d]
    return min(valid) if valid else None


def _max_str_date(dates) -> Optional[str]:
    """Максимальная дата из итерируемого (строки ISO или None)."""
    valid = [d for d in dates if d]
    return max(valid) if valid else None


def _store_new_observation(text: str, source_fact_ids: list[str], storage) -> None:
    now = datetime.now(timezone.utc).isoformat()
    obs_id = str(uuid.uuid4())

    source_rows = storage.get_facts_by_ids(source_fact_ids)
    occurred_start = _min_str_date(r["occurred_start"] for r in source_rows)
    occurred_end   = _max_str_date(r["occurred_end"]   for r in source_rows)
    mentioned_at   = _max_str_date(r["mentioned_at"]   for r in source_rows) or now

    storage.conn.execute(
        """INSERT OR IGNORE INTO facts
           (fact_id, fact, fact_type, occurred_start, occurred_end, mentioned_at, source_fact_ids)
           VALUES (?, ?, 'observation', ?, ?, ?, ?)""",
        (obs_id, text, occurred_start, occurred_end, mentioned_at, json.dumps(source_fact_ids)),
    )
    storage.conn.commit()
    try:
        vec = storage.encode_texts([text])[0]
        storage.insert_vectors([{"fact_id": obs_id, "mentioned_at": mentioned_at, "vector": vec}])
    except Exception as e:
        log.warning("[consolidate] LanceDB write failed for %s: %s", obs_id, e, exc_info=True)


def _update_observation(observation_id: str, new_text: str, source_fact_ids: list[str], storage) -> None:
    row = storage.conn.execute(
        "SELECT source_fact_ids FROM facts WHERE fact_id = ? AND fact_type = 'observation'",
        (observation_id,),
    ).fetchone()
    if not row:
        return
    existing_ids = json.loads(row["source_fact_ids"] or "[]")
    merged_ids = list(dict.fromkeys(existing_ids + source_fact_ids))

    source_rows = storage.get_facts_by_ids(source_fact_ids)
    new_occurred_start = _min_str_date(r["occurred_start"] for r in source_rows)
    new_occurred_end   = _max_str_date(r["occurred_end"]   for r in source_rows)
    new_mentioned_at   = _max_str_date(r["mentioned_at"]   for r in source_rows)

    storage.conn.execute(
        """UPDATE facts SET
               fact = ?,
               source_fact_ids = ?,
               occurred_start = CASE WHEN occurred_start IS NULL OR (? IS NOT NULL AND ? < occurred_start) THEN ? ELSE occurred_start END,
               occurred_end   = CASE WHEN occurred_end   IS NULL OR (? IS NOT NULL AND ? > occurred_end)   THEN ? ELSE occurred_end   END,
               mentioned_at   = CASE WHEN mentioned_at   IS NULL OR (? IS NOT NULL AND ? > mentioned_at)   THEN ? ELSE mentioned_at   END
           WHERE fact_id = ? AND fact_type = 'observation'""",
        (
            new_text, json.dumps(merged_ids),
            new_occurred_start, new_occurred_start, new_occurred_start,
            new_occurred_end,   new_occurred_end,   new_occurred_end,
            new_mentioned_at,   new_mentioned_at,   new_mentioned_at,
            observation_id,
        ),
    )
    storage.conn.commit()
    try:
        storage.table.delete(f"fact_id = '{observation_id}'")
        now = datetime.now(timezone.utc).isoformat()
        vec = storage.encode_texts([new_text])[0]
        storage.insert_vectors([{"fact_id": observation_id, "mentioned_at": now, "vector": vec}])
    except Exception as e:
        log.warning("[consolidate] LanceDB update failed for %s: %s", observation_id, e, exc_info=True)


def _delete_observation(observation_id: str, storage) -> None:
    storage.conn.execute(
        "DELETE FROM facts WHERE fact_id = ? AND fact_type = 'observation'",
        (observation_id,),
    )
    storage.conn.commit()
    try:
        storage.table.delete(f"fact_id = '{observation_id}'")
    except Exception as e:
        log.warning("[consolidate] LanceDB delete failed for %s: %s", observation_id, e, exc_info=True)


async def create_observations(storage, client, model_name: str) -> int:
    """Consolidation loop: обрабатывает все неконсолидированные факты батчами по CONSOLIDATION_LLM_BATCH_SIZE.

    1:1 с Hindsight: для каждого батча делает recall существующих observations,
    затем один LLM-вызов, который возвращает creates/updates/deletes.
    """
    total = await asyncio.to_thread(storage.get_pending_consolidation_count)
    if total == 0:
        log.debug("[consolidate] skip: no unconsolidated facts")
        return 0

    log.info("[consolidate] starting: %d unconsolidated facts", total)
    n_created = n_updated = n_deleted = n_processed = 0

    while True:
        batch = await asyncio.to_thread(storage.get_unconsolidated_facts, CONSOLIDATION_LLM_BATCH_SIZE)
        if not batch:
            break

        # recall существующих observations для каждого факта (последовательно — SQLite не thread-safe)
        per_fact_obs = []
        for r in batch:
            per_fact_obs.append(await _find_related_observations(r["fact"], storage))

        # Union observations (дедупликация) + per-fact mapping для security check
        seen_ids: set[str] = set()
        union_obs: list = []
        per_fact_obs_ids: dict[str, set[str]] = {}
        for fact_row, obs_list in zip(batch, per_fact_obs):
            fid = fact_row["fact_id"]
            per_fact_obs_ids[fid] = {o.fact_id for o in obs_list}
            for o in obs_list:
                if o.fact_id not in seen_ids:
                    seen_ids.add(o.fact_id)
                    union_obs.append(o)

        result = await _consolidate_llm_batch(batch, union_obs, storage, client, model_name)
        valid_fact_ids = {r["fact_id"] for r in batch}

        for create in result.creates:
            valid_sources = [fid for fid in create.source_fact_ids if fid in valid_fact_ids]
            if valid_sources:
                await asyncio.to_thread(_store_new_observation, create.text, valid_sources, storage)
                n_created += 1

        for update in result.updates:
            valid_sources = [fid for fid in update.source_fact_ids if fid in valid_fact_ids]
            if not valid_sources:
                continue
            if not any(update.observation_id in per_fact_obs_ids.get(fid, set()) for fid in valid_sources):
                log.debug("[consolidate] rejected update — obs %s not in recall for sources", update.observation_id)
                continue
            await asyncio.to_thread(_update_observation, update.observation_id, update.text, valid_sources, storage)
            n_updated += 1

        for delete in result.deletes:
            if delete.observation_id not in seen_ids:
                log.debug("[consolidate] rejected delete — obs %s not in recall", delete.observation_id)
                continue
            await asyncio.to_thread(_delete_observation, delete.observation_id, storage)
            n_deleted += 1

        await asyncio.to_thread(storage.mark_consolidated, [r["fact_id"] for r in batch])
        n_processed += len(batch)

    log.info("[consolidate] done: %d processed → %d created, %d updated, %d deleted",
             n_processed, n_created, n_updated, n_deleted)
    return n_created


# ── Public entry point ─────────────────────────────────────────────────────────

async def retain(
    items: list[RetainItem],
    client,
    model_name: str,
    storage,
    with_observations: bool = True,
) -> list[Fact]:
    """
    Полный retain pipeline: LLM → dedup → store → graph links → create_observations.
    """
    log.info("[retain] started: %d items", len(items))
    facts, chunk_meta = await extract_facts(items, client, model_name)
    if not facts:
        return []

    new_facts, vectors = store_facts(facts, storage, chunk_meta)
    if not new_facts:
        return []

    n_causal   = build_causal_links(new_facts, storage)
    n_temporal = build_temporal_links(new_facts, storage)
    n_semantic = build_semantic_links(new_facts, vectors, storage)

    log.info(
        "[retain] done: %d facts, %d causal / %d temporal / %d semantic links",
        len(new_facts), n_causal, n_temporal, n_semantic,
    )

    if with_observations:
        await create_observations(storage, client, model_name)

    return new_facts
