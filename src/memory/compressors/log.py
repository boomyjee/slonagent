"""LogCompressor — Mastra Observational Memory, реализация 1:1.

Архитектура:
  Observer  — наблюдает старые сообщения и превращает их в observations (append-only).
  Reflector — переписывает observations когда они вырастают выше REFLECTION_TOKENS.

compress(turns) → [OM_turn] + [recent_turns]
  OM_turn — специальное сообщение с observations, помечено _observation_message=True.
  recent_turns — свежие сообщения которые ещё не наблюдались.

Хранение:
  memory/log/CONTEXT.json — observations (персистентно между сессиями)
"""
import asyncio
import logging
import os
import re
from datetime import datetime, timezone
import httpx
from google import genai
from google.genai import types

from src.memory.memory import Memory

log = logging.getLogger(__name__)


# ── Observer prompts (verbatim from Mastra observer-agent.ts) ─────────────────

_OBSERVER_EXTRACTION_INSTRUCTIONS = """\
CRITICAL: DISTINGUISH USER ASSERTIONS FROM QUESTIONS

When the user TELLS you something about themselves, mark it as an assertion:
- "I have two kids" → 🔴 (14:30) User stated has two kids
- "I work at Acme Corp" → 🔴 (14:31) User stated works at Acme Corp

When the user ASKS about something, mark it as a question/request:
- "Can you help me with X?" → 🔴 (15:00) User asked help with X

Distinguish between QUESTIONS and STATEMENTS OF INTENT:
- "Can you recommend..." → Question (extract as "User asked...")
- "I'm looking forward to [doing X]" → Statement of intent (extract as "User stated they will [do X]")
- "I need to [do X]" → Statement of intent

STATE CHANGES AND UPDATES:
When a user indicates they are changing something, frame it as a state change:
- "I'm going to start doing X instead of Y" → "User will start doing X (changing from Y)"
- "I moved my stuff to the new place" → "User moved their stuff to the new place (no longer at previous location)"

If the new state contradicts previous information, make that explicit:
- BAD: "User plans to use the new method"
- GOOD: "User will use the new method (replacing the old approach)"

USER ASSERTIONS ARE AUTHORITATIVE. The user is the source of truth about their own life.

TEMPORAL ANCHORING:
Each observation has TWO potential timestamps:
1. BEGINNING: The time the statement was made (from the message timestamp) — ALWAYS include
2. END: The time being REFERENCED, if different — ONLY when there's an actual date to compute

ONLY add "(meaning DATE)" at the END for:
- Past: "last week", "yesterday", "in March"
- Future: "this weekend", "tomorrow", "next week"

DO NOT add end dates for vague references like "recently", "soon", "lately".

FORMAT:
- With time reference: (TIME) [observation]. (meaning DATE)
- Without time reference: (TIME) [observation].

GOOD: (09:15) User will visit their parents this weekend. (meaning June 17-18, 2025)
GOOD: (09:15) User prefers hiking in the mountains.
BAD:  (09:15) User prefers hiking in the mountains. (meaning June 15, 2025)

IMPORTANT: If an observation contains MULTIPLE events, split into SEPARATE lines, each with its own timestamp.

PRESERVE UNUSUAL PHRASING: Quote the user's exact words when they use non-standard terminology.

USE PRECISE ACTION VERBS:
- "getting" something regularly → "subscribed to"
- "getting" something once → "purchased"
- "stopped getting" → "canceled"

PRESERVING DETAILS IN ASSISTANT-GENERATED CONTENT:
When the assistant provides recommendations or lists, preserve distinguishing details:
- BAD: Assistant recommended 5 hotels.
- GOOD: Assistant recommended hotels: Hotel A (near station), Hotel B (budget), Hotel C (rooftop pool).

AVOIDING REPETITIVE OBSERVATIONS:
Group repeated tool calls / file browsing into one parent observation with sub-bullets.

BAD:
* 🟡 (14:30) Agent used view tool on src/auth.ts
* 🟡 (14:31) Agent used view tool on src/users.ts

GOOD:
* 🟡 (14:30) Agent browsed source files for auth flow
  * -> viewed src/auth.ts — found token validation logic
  * -> viewed src/users.ts — found user lookup by email

Short and medium-length user messages should be captured nearly verbatim."""

_OBSERVER_OUTPUT_FORMAT = """\
Use priority levels:
- 🔴 High: explicit user facts, preferences, goals achieved, critical context
- 🟡 Medium: project details, learned information, tool results
- 🟢 Low: minor details, uncertain observations

Group observations by date, then list each with 24-hour time:

Date: Dec 4, 2025
* 🔴 (14:30) User prefers direct answers
* 🔴 (14:31) Working on feature X
* 🟡 (14:32) Agent browsed auth files
  * -> viewed auth.ts — found missing null check

Date: Dec 5, 2025
* 🔴 (09:15) Continued work on feature X"""

_OBSERVER_GUIDELINES = """\
- Be specific enough for the assistant to act on
- Good: "User prefers short, direct answers without lengthy explanations"
- Bad: "User stated a preference" (too vague)
- Add 1 to 5 observations per exchange
- Use terse language. Sentences should be dense without unnecessary words
- Do not add repetitive observations. Group repeated similar actions under a single parent
- If the agent calls tools, observe what was called, why, and what was learned
- Make sure you start each observation with a priority emoji (🔴, 🟡, 🟢)
- User messages are always 🔴 priority. Capture the user's words closely
- Observe WHAT the agent did and WHAT it means"""

OBSERVER_SYSTEM_PROMPT = f"""\
You are the memory consciousness of an AI assistant. \
Your observations will be the ONLY information the assistant has about past interactions with this user.

Extract observations that will help the assistant remember:

{_OBSERVER_EXTRACTION_INSTRUCTIONS}

=== OUTPUT FORMAT ===

Your output MUST use XML tags:

<observations>
{_OBSERVER_OUTPUT_FORMAT}
</observations>

<current-task>
State the current task(s) explicitly.
</current-task>

<suggested-response>
Hint for the agent's immediate next message.
</suggested-response>

=== GUIDELINES ===

{_OBSERVER_GUIDELINES}

Remember: These observations are the assistant's ONLY memory. Make them count.
User messages are extremely important — capture them clearly."""


# ── Reflector prompts (verbatim from Mastra reflector-agent.ts) ───────────────

REFLECTOR_SYSTEM_PROMPT = f"""\
You are the memory consciousness of an AI assistant. \
Your memory observation reflections will be the ONLY information the assistant has about past interactions.

The following instructions were given to the observer part of your psyche to create memories.
Use this to understand how observational memories were created.

<observational-memory-instruction>
{_OBSERVER_EXTRACTION_INSTRUCTIONS}

=== OUTPUT FORMAT ===

{_OBSERVER_OUTPUT_FORMAT}

=== GUIDELINES ===

{_OBSERVER_GUIDELINES}
</observational-memory-instruction>

You are the observation reflector — a broader part of the same psyche.
Your role is to reflect on all observations, re-organize and streamline them, \
draw connections and conclusions about what happened.

Think about what the observed goal at hand is. Did we get off track? If so, why and how to get back.

IMPORTANT: your reflections are THE ENTIRETY of the assistant's memory. \
Any information you do not include will be immediately forgotten. \
Do not leave anything out.

When consolidating observations:
- Preserve dates/times (temporal context is critical)
- Combine related items (e.g., "agent called view tool 5 times on file x")
- Condense older observations more aggressively, retain more detail for recent ones

CRITICAL: USER ASSERTIONS vs QUESTIONS
- "User stated: X" = authoritative assertion
- "User asked: X" = question/request
USER ASSERTIONS TAKE PRECEDENCE over questions about the same topic.

=== OUTPUT FORMAT ===

<observations>
All consolidated observations here using date-grouped format with priority emojis.
</observations>

<current-task>
State the current task(s) explicitly.
</current-task>

<suggested-response>
Hint for the agent's immediate next message.
</suggested-response>

User messages are extremely important. If the user asks a question or gives a new task, \
make it clear in <current-task> that this is the priority."""

_COMPRESSION_GUIDANCE = {
    1: """\
## COMPRESSION REQUIRED

Your previous reflection was the same size or larger than the original observations.

Please re-process with slightly more compression:
- Towards the beginning, condense more observations into higher-level reflections
- Closer to the end, retain more fine details (recent context matters more)
- Combine related items more aggressively but do not lose important specific details
- Your current detail level was a 10/10, aim for 8/10""",

    2: """\
## AGGRESSIVE COMPRESSION REQUIRED

Your previous reflection was still too large after compression guidance.

Please re-process with much more aggressive compression:
- Towards the beginning, heavily condense into high-level summaries
- Remove redundant information and merge overlapping observations
- Your current detail level was a 10/10, aim for 6/10""",

    3: """\
## CRITICAL COMPRESSION REQUIRED

Please re-process with maximum compression:
- Summarize the oldest observations (first 50-70%) into brief high-level paragraphs
- For the most recent observations (last 30-50%), retain important details but use condensed style
- Drop procedural details (tool calls, retries, intermediate steps) — keep only final outcomes
- Preserve: names, dates, decisions, errors, user preferences, architectural choices
- Your current detail level was a 10/10, aim for 4/10""",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_xml_tag(text: str, tag: str) -> str:
    """Извлекает содержимое первого XML-тега."""
    m = re.search(rf"<{tag}>([\s\S]*?)</{tag}>", text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _parse_observations(text: str) -> str:
    """Извлекает observations из XML, fallback — list items."""
    obs = _parse_xml_tag(text, "observations")
    if obs:
        return obs
    lines = [l for l in text.splitlines() if re.match(r"^\s*[-*]\s", l)]
    return "\n".join(lines).strip() or text.strip()


def _format_turns(pending: list) -> str:
    """Форматирует turns для Observer: Role (timestamp): content."""
    lines = []
    for turn in pending:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role", "unknown")
        label = "User" if role == "user" else "Assistant"
        ts_raw = turn.get("_timestamp")
        if ts_raw:
            try:
                dt = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else ts_raw
                ts_str = dt.strftime("%b %d, %Y %H:%M")
            except Exception:
                ts_str = str(ts_raw)
        else:
            ts_str = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M")

        parts_text = " ".join(
            p.get("text", "") for p in turn.get("parts", [])
            if isinstance(p, dict) and "text" in p
        ).strip()
        if parts_text:
            lines.append(f"**{label} ({ts_str}):**\n{parts_text}")

    return "\n\n---\n\n".join(lines)


def _optimize_for_context(observations: str) -> str:
    """Убирает 🟡/🟢, -> и лишние пробелы для передачи агенту (аналог Mastra optimizeObservationsForContext)."""
    obs = re.sub(r"🟡\s*", "", observations)
    obs = re.sub(r"🟢\s*", "", obs)
    obs = re.sub(r"\s*->\s*", " ", obs)
    obs = re.sub(r" +", " ", obs)
    obs = re.sub(r"\n{3,}", "\n\n", obs)
    return obs.strip()


# ── Provider ──────────────────────────────────────────────────────────────────

class LogCompressor:
    """
    Компрессор истории на основе Mastra Observational Memory.

    compress(turns) → [OM_turn] + [recent_turns]
      OM_turn       — сообщение с observations, помечено _observation_message=True.
      recent_turns  — свежие сообщения которые ещё не вошли в observations.

    OM_turn персистируется в истории через memory.py как обычный turn.
    """

    def __init__(self, model_name: str, api_key: str,
                 recent_tokens: int   = 6_000,
                 compress_after_tokens: int = 30_000,
                 reflect_after_tokens: int  = 40_000):
        self._compress_after_tokens = compress_after_tokens
        self._reflect_after_tokens  = reflect_after_tokens
        self._recent_tokens      = recent_tokens
        self._model_name = model_name

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        http_opts = {"httpx_client": http_client, "api_version": "v1alpha"} if http_client else {"api_version": "v1alpha"}
        self._client = genai.Client(api_key=api_key, http_options=http_opts)

    # ── Public ────────────────────────────────────────────────────────────────

    async def compress(self, turns: list) -> list:
        if Memory.count_tokens(turns) < self._compress_after_tokens:
            return turns

        # Разделяем: OM_turn (если есть) + остальные
        om_turn, rest = None, []
        for t in turns:
            if isinstance(t, dict) and t.get("_observation_message"):
                om_turn = t
            else:
                rest.append(t)

        existing_observations = (
            om_turn["parts"][0]["text"] if om_turn else ""
        )

        # Решаем что наблюдать: оставляем хвост recent_turns нетронутым
        to_observe, recent = self._split_recent(rest)
        if not to_observe:
            return turns

        formatted = _format_turns(to_observe)
        new_obs = await self._run_observer(formatted, existing_observations)
        if not new_obs:
            log.warning("[LogCompressor] Observer returned empty")
            return turns

        updated = (existing_observations + "\n\n" + new_obs).strip() if existing_observations else new_obs

        obs_tokens = Memory.count_tokens([{"role": "user", "parts": [{"text": updated}]}])
        if obs_tokens >= self._reflect_after_tokens:
            updated = await self._run_reflector(updated) or updated

        obs_text = (
            "The following observations block contains your memory of past conversations with this user.\n\n"
            f"<observations>\n{_optimize_for_context(updated)}\n</observations>\n\n"
            "IMPORTANT: When responding, reference specific details from these observations. "
            "Do not give generic advice — personalize your response based on what you know about this user. "
            "For conflicting information, prefer the MOST RECENT observation (check dates)."
        )
        new_om = {"role": "user", "parts": [{"text": obs_text}], "_observation_message": True}
        log.info("[LogCompressor] %d → 1 OM + %d recent turns", len(to_observe), len(recent))
        return [new_om] + recent

    # ── Internal ──────────────────────────────────────────────────────────────

    def _split_recent(self, turns: list) -> tuple[list, list]:
        """Отделяет recent_turns (последние до recent_tokens) от turns для наблюдения."""
        recent_budget = self._recent_tokens
        recent, tokens = [], 0
        for turn in reversed(turns):
            t = Memory.count_tokens([turn])
            if tokens + t > recent_budget:
                break
            recent.append(turn)
            tokens += t
        recent.reverse()
        to_observe = turns[:len(turns) - len(recent)]
        return to_observe, recent

    async def _run_observer(self, formatted_turns: str, existing_observations: str) -> str:
        user_prompt = ""
        if existing_observations:
            user_prompt += f"## Previous Observations\n\n{existing_observations}\n\n"
            user_prompt += "Do not repeat existing observations. Append new ones only.\n\n---\n\n"
        user_prompt += f"## New Messages to Observe\n\n{formatted_turns}\n\n---\n\n"
        user_prompt += "Extract observations from the new messages above."

        try:
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self._model_name,
                contents=[{"role": "user", "parts": [{"text": user_prompt}]}],
                config=types.GenerateContentConfig(
                    system_instruction=OBSERVER_SYSTEM_PROMPT,
                    temperature=0.3,
                    max_output_tokens=100_000,
                ),
            )
            return _parse_observations(response.text.strip())
        except Exception as e:
            log.error("[LogCompressor] Observer LLM failed: %s", e)
            return ""

    async def _run_reflector(self, observations: str, compression_level: int = 0) -> str:
        user_prompt = (
            f"## OBSERVATIONS TO REFLECT ON\n\n{observations}\n\n---\n\n"
            "Please analyze these observations and produce a refined, condensed version "
            "that will become the assistant's entire memory going forward."
        )
        if compression_level > 0:
            user_prompt += f"\n\n{_COMPRESSION_GUIDANCE[compression_level]}"

        try:
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self._model_name,
                contents=[{"role": "user", "parts": [{"text": user_prompt}]}],
                config=types.GenerateContentConfig(
                    system_instruction=REFLECTOR_SYSTEM_PROMPT,
                    temperature=0.0,
                    max_output_tokens=100_000,
                ),
            )
            reflected = _parse_observations(response.text.strip())
        except Exception as e:
            log.error("[LogCompressor] Reflector LLM failed: %s", e)
            return ""

        if not reflected:
            log.warning("[LogCompressor] Reflector returned empty")
            return ""

        orig_tokens = Memory.count_tokens([{"role": "user", "parts": [{"text": observations}]}])
        refl_tokens = Memory.count_tokens([{"role": "user", "parts": [{"text": reflected}]}])

        if refl_tokens >= orig_tokens and compression_level < 3:
            log.warning("[LogCompressor] Reflection didn't compress (level %d → %d)", compression_level, compression_level + 1)
            return await self._run_reflector(observations, compression_level + 1)

        log.info("[LogCompressor] Reflector: %d → %d tokens (%.0f%%)",
                 orig_tokens, refl_tokens, (1 - refl_tokens / max(orig_tokens, 1)) * 100)
        return reflected
