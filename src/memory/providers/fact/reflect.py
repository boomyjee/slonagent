"""reflect.py — агентный цикл для fact_reflect.

Аналог Hindsight run_reflect_agent, адаптированный для:
  - Gemini tool calling (google-genai SDK)
  - Локальных компонентов (SQLite + LanceDB вместо PostgreSQL)
  - Без bank_profile / disposition / directives

Иерархия инструментов (та же, что у Hindsight):
  Если есть mental models:  iter0 → search_mental_models
                            iter1 → search_observations
                            iter2 → recall
                            iter3+ → auto
  Иначе:                   iter0 → search_observations
                            iter1 → recall
                            iter2+ → auto

Последняя итерация — forced text (без инструментов).
"""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

MAX_ITERATIONS = 10


@dataclass
class MentalModel:
    model_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""       # one-liner — индексируется как вектор
    summary: Optional[str] = None
    source_fact_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    relevance: float = 0.0      # заполняется при поиске


# ── System prompt ──────────────────────────────────────────────────────────────

def _build_system_prompt(has_mental_models: bool) -> str:
    parts = [
        "CRITICAL: You MUST ONLY use information from retrieved tool results."
        " NEVER make up names, people, events, or entities.",
        "",
        "You are a reflection agent that answers questions by reasoning over retrieved memories.",
        "",
        "## LANGUAGE RULE",
        "- Detect the language of the user's question and respond in that SAME language.",
        "",
        "## CRITICAL RULES",
        "- ONLY use information from tool results — no external knowledge or guessing",
        "- You SHOULD synthesize, infer, and reason from the retrieved memories",
        "- You MUST search before saying you don't have information",
        "",
        "## How to Reason",
        "- If memories mention someone did an activity, you can infer they likely enjoyed it",
        "- Synthesize a coherent narrative from related memories",
        "- Be a thoughtful interpreter, not just a literal repeater",
        "- When the exact answer isn't stated, use what IS stated to give the best answer",
        "",
        "## HIERARCHICAL RETRIEVAL STRATEGY",
        "",
    ]

    if has_mental_models:
        parts += [
            "You have access to THREE levels of knowledge. Use them in this order:",
            "",
            "### 1. MENTAL MODELS (search_mental_models) — Try First",
            "- User-curated summaries about specific topics",
            "- HIGHEST quality — manually created and maintained",
            "- If a relevant mental model exists and is FRESH, it may fully answer the question",
            "- Check `is_stale` field — if stale, also verify with lower levels",
            "",
            "### 2. OBSERVATIONS (search_observations) — Second Priority",
            "- Auto-consolidated knowledge from memories",
            "- Check `is_stale` field — if stale, ALSO use recall() to verify",
            "- Good for understanding patterns and summaries",
            "",
            "### 3. RAW FACTS (recall) — Ground Truth",
            "- Individual memories (world facts and experiences)",
            "- Use when: no mental models/observations exist, they're stale, or you need specific details",
            "- MANDATORY: If search_mental_models and search_observations both return 0 results, "
              "you MUST call recall() before giving up",
            "- This is the source of truth that other levels are built from",
            "",
        ]
    else:
        parts += [
            "You have access to TWO levels of knowledge. Use them in this order:",
            "",
            "### 1. OBSERVATIONS (search_observations) — Try First",
            "- Auto-consolidated knowledge from memories",
            "- Check `is_stale` field — if stale, ALSO use recall() to verify",
            "- Good for understanding patterns and summaries",
            "",
            "### 2. RAW FACTS (recall) — Ground Truth",
            "- Individual memories (world facts and experiences)",
            "- Use when: no observations exist, they're stale, or you need specific details",
            "- MANDATORY: If search_observations returns 0 results or count=0, "
              "you MUST call recall() before giving up",
            "- This is the source of truth that observations are built from",
            "",
        ]

    parts += [
        "## Query Strategy",
        "recall() uses semantic search. NEVER just echo the user's question — "
        "decompose it into targeted searches:",
        "",
        "BAD:  User asks 'recurring lesson themes' → recall('recurring lesson themes')",
        "GOOD: Break it down:",
        "  1. recall('lessons') — find all lesson-related memories",
        "  2. recall('teaching sessions') — alternative phrasing",
        "",
        "Think: What ENTITIES and CONCEPTS does this question involve? Search for each separately.",
        "",
        "## Workflow",
    ]

    if has_mental_models:
        parts += [
            "1. First, try search_mental_models() — check if a curated summary exists",
            "2. If no mental model or it's stale, try search_observations() for consolidated knowledge",
            "3. If observations are stale OR you need specific details, use recall() for raw facts",
            "4. Use expand() if you need more context on specific memories",
            "5. When ready, call done() with your answer and supporting IDs",
        ]
    else:
        parts += [
            "1. First, try search_observations() — check for consolidated knowledge",
            "2. If search_observations returns 0 results OR observations are stale, "
              "you MUST call recall() for raw facts",
            "3. Use expand() if you need more context on specific memories",
            "4. When ready, call done() with your answer and supporting IDs",
        ]

    parts += [
        "",
        "## Final Reminder",
        "- NEVER answer from prior knowledge — only from tool results",
        "- If no relevant information found after searching, say so explicitly",
        "- Citations go in the ID arrays of done(), NOT in the answer text",
    ]

    return "\n".join(parts)


# ── Tool schemas ───────────────────────────────────────────────────────────────

_SCHEMA_SEARCH_MENTAL_MODELS = {
    "name": "search_mental_models",
    "description": (
        "Search user-curated mental models (manually created summaries). "
        "These are the highest-quality knowledge. Use FIRST when the question might be covered."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why you're making this search"},
            "query":  {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Max results to return (default 5)"},
        },
        "required": ["reason", "query"],
    },
}

_SCHEMA_SEARCH_OBSERVATIONS = {
    "name": "search_observations",
    "description": (
        "Search consolidated observations (auto-generated knowledge). "
        "Returns freshness info (is_stale). If stale, ALSO use recall() to verify. "
        "If search_mental_models is available, call it FIRST."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why you're making this search"},
            "query":  {"type": "string", "description": "Search query"},
        },
        "required": ["reason", "query"],
    },
}

_SCHEMA_RECALL = {
    "name": "recall",
    "description": (
        "Search raw memories (facts and experiences) — the ground truth. "
        "Use when observations/mental models don't exist, are stale, or you need specific details."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why you're making this search"},
            "query":  {"type": "string", "description": "Search query"},
        },
        "required": ["reason", "query"],
    },
}

_SCHEMA_EXPAND = {
    "name": "expand",
    "description": "Get surrounding chunk or full document context for specific memories.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why you need more context"},
            "memory_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Memory IDs from recall results",
            },
            "depth": {
                "type": "string",
                "enum": ["chunk", "document"],
                "description": "chunk: surrounding text chunk, document: full source document",
            },
        },
        "required": ["reason", "memory_ids", "depth"],
    },
}

_SCHEMA_DONE = {
    "name": "done",
    "description": "Signal completion. Use when you have gathered enough information to answer.",
    "parameters": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": (
                    "Your response as well-formatted markdown. "
                    "NEVER include memory IDs or UUIDs in this text — put IDs only in the arrays. "
                    "Write in the SAME language as the user's question."
                ),
            },
            "memory_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Memory IDs that support your answer",
            },
            "mental_model_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Mental model IDs that support your answer",
            },
            "observation_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Observation IDs that support your answer",
            },
        },
        "required": ["answer"],
    },
}


def _tool_schemas(has_mental_models: bool) -> list[dict]:
    schemas = []
    if has_mental_models:
        schemas.append(_SCHEMA_SEARCH_MENTAL_MODELS)
    schemas += [_SCHEMA_SEARCH_OBSERVATIONS, _SCHEMA_RECALL, _SCHEMA_EXPAND, _SCHEMA_DONE]
    return schemas


# ── Tool callbacks ─────────────────────────────────────────────────────────────

async def _exec_recall(query: str, storage, rerank_model: str) -> dict[str, Any]:
    from src.memory.providers.fact.recall import recall_async
    vec  = await asyncio.to_thread(storage.encode_query, query)
    resp = await recall_async(query, vec, storage, types=["world", "experience"], rerank_model=rerank_model)
    memories = [
        {
            "id":            r.fact_id,
            "text":          r.fact,
            "fact_type":     r.fact_type,
            "occurred_start": r.occurred_start,
            "document_id":   r.document_id,
            "score":         round(r.score, 3),
        }
        for r in resp.results
    ]
    return {"query": query, "memories": memories, "count": len(memories)}


async def _exec_search_observations(query: str, storage, rerank_model: str) -> dict[str, Any]:
    from src.memory.providers.fact.recall import recall_async
    pending = await asyncio.to_thread(storage.get_pending_consolidation_count)
    vec     = await asyncio.to_thread(storage.encode_query, query)
    resp    = await recall_async(query, vec, storage, types=["observation"], rerank_model=rerank_model)
    is_stale = pending > 0
    freshness = "up_to_date" if pending == 0 else "slightly_stale" if pending < 10 else "stale"
    observations = [
        {
            "id":            r.fact_id,
            "text":          r.fact,
            "occurred_start": r.occurred_start,
            "score":         round(r.score, 3),
            "is_stale":      is_stale,
        }
        for r in resp.results
    ]
    return {
        "query":        query,
        "count":        len(observations),
        "observations": observations,
        "is_stale":     is_stale,
        "freshness":    freshness,
    }


async def _exec_search_mental_models(
    query: str, storage, max_results: int = 5
) -> dict[str, Any]:
    pending  = await asyncio.to_thread(storage.get_pending_consolidation_count)
    is_stale = pending > 0
    vec      = await asyncio.to_thread(storage.encode_query, query)
    results  = await asyncio.to_thread(storage.search_mental_models, vec, max_results)
    mental_models = [
        {
            "id":        r.model_id,
            "name":      r.name,
            "content":   r.summary or r.description,
            "is_stale":  is_stale,
            "relevance": r.relevance,
        }
        for r in results
    ]
    return {"query": query, "count": len(mental_models), "mental_models": mental_models}


def _exec_expand_sync(memory_ids: list[str], depth: str, storage) -> dict[str, Any]:
    results = []
    for mid in memory_ids:
        row = storage.conn.execute(
            "SELECT fact_id, fact, chunk_id, document_id FROM facts WHERE fact_id = ?", (mid,)
        ).fetchone()
        if not row:
            results.append({"memory_id": mid, "error": "not found"})
            continue

        if depth == "chunk" and row["chunk_id"]:
            chunk = storage.conn.execute(
                "SELECT chunk_text, chunk_index FROM chunks WHERE chunk_id = ?",
                (row["chunk_id"],),
            ).fetchone()
            if chunk:
                results.append({
                    "memory_id":   mid,
                    "chunk_text":  chunk["chunk_text"],
                    "chunk_index": chunk["chunk_index"],
                })
                continue

        if depth == "document" and row["document_id"]:
            chunks = storage.conn.execute(
                "SELECT chunk_text FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                (row["document_id"],),
            ).fetchall()
            if chunks:
                results.append({
                    "memory_id":     mid,
                    "document_id":   row["document_id"],
                    "document_text": "\n".join(c["chunk_text"] for c in chunks),
                })
                continue

        results.append({
            "memory_id": mid,
            "fact_text": row["fact"],
            "note":      "no chunk/document context available",
        })

    return {"results": results}


async def _execute_tool(name: str, args: dict, storage, rerank_model: str) -> dict[str, Any]:
    if name == "recall":
        return await _exec_recall(args.get("query", ""), storage, rerank_model)
    if name == "search_observations":
        return await _exec_search_observations(args.get("query", ""), storage, rerank_model)
    if name == "search_mental_models":
        return await _exec_search_mental_models(
            args.get("query", ""), storage, int(args.get("max_results", 5))
        )
    if name == "expand":
        return await asyncio.to_thread(
            _exec_expand_sync, args.get("memory_ids", []), args.get("depth", "chunk"), storage
        )
    return {"error": f"Unknown tool: {name}"}


def _safe_json(obj: Any) -> Any:
    """Рекурсивно приводит объект к JSON-совместимому виду."""
    return json.loads(json.dumps(obj, default=str))


# ── Agentic loop ───────────────────────────────────────────────────────────────

async def run_reflect_agent(
    query: str,
    storage,
    llm_client,
    model_name: str,
    max_iterations: int = MAX_ITERATIONS,
    rerank_model: str = "",
) -> dict[str, Any]:
    """
    Агентный цикл для fact_reflect.

    Аналог Hindsight run_reflect_agent с иерархическим поиском:
      1. search_mental_models (если есть)
      2. search_observations
      3. recall

    Args:
        query:          Вопрос пользователя.
        storage:        Storage объект (SQLite + LanceDB).
        llm_client:     genai.Client.
        model_name:     Название Gemini-модели.
        max_iterations: Лимит итераций (default 10).

    Returns:
        {"answer": str, "iterations": int, "facts_used": int, ...}
    """
    from google.genai import types

    has_mental_models = await asyncio.to_thread(
        lambda: storage.conn.execute("SELECT COUNT(*) FROM mental_models").fetchone()[0] > 0
    )

    system_prompt = _build_system_prompt(has_mental_models)
    schemas       = _tool_schemas(has_mental_models)

    gemini_tools = [
        types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=s["name"],
                description=s["description"],
                parameters=s["parameters"],
            )
            for s in schemas
        ])
    ]

    def _forced_config(tool_name: str) -> "types.ToolConfig":
        return types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode="ANY",
                allowed_function_names=[tool_name],
            )
        )

    def _auto_config() -> "types.ToolConfig":
        return types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="AUTO")
        )

    def _tool_config_for(iteration: int) -> "types.ToolConfig":
        if has_mental_models:
            if iteration == 0: return _forced_config("search_mental_models")
            if iteration == 1: return _forced_config("search_observations")
            if iteration == 2: return _forced_config("recall")
        else:
            if iteration == 0: return _forced_config("search_observations")
            if iteration == 1: return _forced_config("recall")
        return _auto_config()

    contents: list = [
        types.Content(role="user", parts=[types.Part.from_text(text=query)])
    ]

    available_memory_ids:       set[str] = set()
    available_observation_ids:  set[str] = set()
    available_mental_model_ids: set[str] = set()

    for iteration in range(max_iterations):
        is_last = iteration == max_iterations - 1

        if is_last:
            response = await asyncio.to_thread(
                llm_client.models.generate_content,
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_prompt),
            )
            return {
                "answer":     (response.text or "").strip(),
                "iterations": iteration + 1,
                "facts_used": len(available_memory_ids),
            }

        try:
            response = await asyncio.to_thread(
                llm_client.models.generate_content,
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=gemini_tools,
                    tool_config=_tool_config_for(iteration),
                ),
            )
        except Exception as e:
            log.warning("[reflect_agent] LLM error iter %d: %s", iteration + 1, e, exc_info=True)
            has_evidence = bool(
                available_memory_ids or available_observation_ids or available_mental_model_ids
            )
            if not has_evidence:
                continue
            break

        function_calls = response.function_calls or []

        if not function_calls:
            answer = (response.text or "").strip()
            if answer:
                return {
                    "answer":     answer,
                    "iterations": iteration + 1,
                    "facts_used": len(available_memory_ids),
                }
            continue

        # Split done vs other tool calls
        done_calls  = [fc for fc in function_calls if fc.name == "done"]
        other_calls = [fc for fc in function_calls if fc.name != "done"]

        if done_calls:
            has_evidence = bool(
                available_memory_ids or available_observation_ids or available_mental_model_ids
            )
            if not has_evidence and iteration < max_iterations - 1:
                # Guardrail: требуем сначала собрать доказательства
                contents.append(response.candidates[0].content)
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(function_response=types.FunctionResponse(
                        name="done",
                        response={
                            "error": (
                                "You must search for information first. "
                                "Use search_observations() or recall() before providing your final answer."
                            )
                        },
                    ))]
                ))
                continue

            args = dict(done_calls[0].args)
            return {
                "answer":            args.get("answer", "").strip(),
                "iterations":        iteration + 1,
                "facts_used":        len(available_memory_ids),
                "observation_ids":   list(available_observation_ids),
                "mental_model_ids":  list(available_mental_model_ids),
            }

        if other_calls:
            contents.append(response.candidates[0].content)

            results = await asyncio.gather(
                *[_execute_tool(fc.name, dict(fc.args), storage, rerank_model) for fc in other_calls],
                return_exceptions=True,
            )

            for fc, result in zip(other_calls, results):
                if isinstance(result, Exception):
                    result = {"error": str(result)}

                # Отслеживаем доступные ID для валидации done()
                if fc.name == "recall" and isinstance(result, dict):
                    for m in result.get("memories", []):
                        if "id" in m:
                            available_memory_ids.add(m["id"])
                elif fc.name == "search_observations" and isinstance(result, dict):
                    for obs in result.get("observations", []):
                        if "id" in obs:
                            available_observation_ids.add(obs["id"])
                elif fc.name == "search_mental_models" and isinstance(result, dict):
                    for mm in result.get("mental_models", []):
                        if "id" in mm:
                            available_mental_model_ids.add(mm["id"])

                contents.append(types.Content(
                    role="user",
                    parts=[types.Part(function_response=types.FunctionResponse(
                        name=fc.name,
                        response=_safe_json(result),
                    ))]
                ))

    return {
        "answer":     "Не удалось сформулировать ответ в рамках заданного числа итераций.",
        "iterations": max_iterations,
        "facts_used": len(available_memory_ids),
    }
