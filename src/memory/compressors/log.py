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
from openai import AsyncOpenAI

from agent import Skill
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

Short and medium-length user messages should be captured nearly verbatim.

SHARED CONTENT — DOCUMENTS, SCRIPTS, CODE, STORIES:
When the user shares a document, script, story, code file, or any artifact, extract its key content as structured facts — not just "user shared X":
- For narratives (scripts, stories): characters + roles, key events in order, setting, tone, open threads
- For code/technical docs: purpose, key components, notable decisions or problems
- For data/lists: the actual items, not just "user shared a list"

Use sub-bullets to preserve structure without verbatim copying:

BAD:  🔴 (07:07) User shared a screenplay about a woman and her ex-boyfriend.
GOOD: 🔴 (07:07) User shared screenplay «Title» (dark comedy, holiday setting):
  * -> Characters: Anna (protagonist), her ex Mark (arrives unexpectedly)
  * -> Inciting event: chance encounter turns into confrontation
  * -> Rising action: situation escalates, Anna takes drastic action
  * -> Resolution: Anna disposes of evidence, returns to mundane routine
  * -> Cliffhanger: ambiguous final message implies Mark may not be gone
  * -> Subtext: holiday symbolism used ironically throughout

The goal: someone reading only the observations should be able to reconstruct the artifact's substance."""

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




def _format_relative_time(date: datetime, now: datetime) -> str:
    diff_days = (now.date() - date.date()).days
    if diff_days == 0: return "today"
    if diff_days == 1: return "yesterday"
    if diff_days < 0: return f"in {-diff_days} day{'s' if -diff_days > 1 else ''}"
    if diff_days < 7: return f"{diff_days} days ago"
    if diff_days < 14: return "1 week ago"
    if diff_days < 30: return f"{diff_days // 7} weeks ago"
    if diff_days < 60: return "1 month ago"
    if diff_days < 365: return f"{diff_days // 30} months ago"
    years = diff_days // 365
    return f"{years} year{'s' if years > 1 else ''} ago"


def _format_gap(prev: datetime, curr: datetime) -> str | None:
    diff_days = (curr.date() - prev.date()).days
    if diff_days <= 1: return None
    if diff_days < 7: return f"[{diff_days} days later]"
    if diff_days < 14: return "[1 week later]"
    if diff_days < 30: return f"[{diff_days // 7} weeks later]"
    if diff_days < 60: return "[1 month later]"
    return f"[{diff_days // 30} months later]"


def _add_relative_time(observations: str, now: datetime) -> str:
    """Добавляет относительные метки времени к датам в наблюдениях (порт Mastra addRelativeTimeToObservations)."""
    date_header_re = re.compile(r"^(Date:\s*)([A-Z][a-z]+ \d{1,2},? \d{4})$", re.MULTILINE)

    dates = []
    for m in date_header_re.finditer(observations):
        try:
            date_str = m.group(2).replace(",", "")
            parsed = datetime.strptime(date_str, "%b %d %Y")
            dates.append({"index": m.start(), "end": m.end(), "date": parsed,
                          "prefix": m.group(1), "date_str": m.group(2)})
        except ValueError:
            pass

    if not dates:
        return observations

    result = ""
    last_index = 0
    for i, curr in enumerate(dates):
        result += observations[last_index:curr["index"]]
        if i > 0:
            gap = _format_gap(dates[i - 1]["date"], curr["date"])
            if gap:
                result += f"\n{gap}\n\n"
        relative = _format_relative_time(curr["date"], now)
        result += f"{curr['prefix']}{curr['date_str']} ({relative})"
        last_index = curr["end"]
    result += observations[last_index:]
    return result


def _optimize_for_context(observations: str) -> str:
    """Убирает 🟡/🟢, -> и лишние пробелы для передачи агенту (аналог Mastra optimizeObservationsForContext)."""
    obs = re.sub(r"🟡\s*", "", observations)
    obs = re.sub(r"🟢\s*", "", obs)
    obs = re.sub(r"\s*->\s*", " ", obs)
    obs = re.sub(r" +", " ", obs)
    obs = re.sub(r"\n{3,}", "\n\n", obs)
    return obs.strip()


# ── Provider ──────────────────────────────────────────────────────────────────

class LogCompressor(Skill):
    """
    Компрессор истории на основе Mastra Observational Memory.

    compress(turns) → [OM_turn] + [recent_turns]
      OM_turn       — сообщение с observations, помечено _observation_message=True.
      recent_turns  — свежие сообщения которые ещё не вошли в observations.

    OM_turn персистируется в истории через memory.py как обычный turn.
    """

    def __init__(self, model_name: str, api_key: str, base_url: str,
                 recent_tokens: int        = 6_000,
                 min_recent_turns: int     = 10,
                 compress_after_tokens: int = 30_000,
                 reflect_after_tokens: int  = 40_000):
        super().__init__()
        self._compress_after_tokens = compress_after_tokens
        self._reflect_after_tokens  = reflect_after_tokens
        self._recent_tokens         = recent_tokens
        self._min_recent_turns      = min_recent_turns
        self._model_name = model_name

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.AsyncClient(proxy=proxy_url, timeout=120.0)
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http_client, max_retries=0)

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

        existing_observations = om_turn.get("_raw_observations", "") if om_turn else ""

        # Решаем что наблюдать: оставляем хвост recent_turns нетронутым
        to_observe, recent = self._split_recent(rest)
        if not to_observe:
            return turns

        new_obs = await self._run_observer(to_observe, existing_observations)
        if not new_obs:
            log.warning("[LogCompressor] Observer returned empty")
            return turns

        updated = (existing_observations + "\n\n" + new_obs).strip() if existing_observations else new_obs

        obs_tokens = Memory.count_tokens([{"role": "user", "content": updated}])
        if obs_tokens >= self._reflect_after_tokens:
            updated = await self._run_reflector(updated) or updated

        obs_text = (
            "The following observations block contains your memory of past conversations with this user.\n\n"
            f"<observations>\n{_optimize_for_context(_add_relative_time(updated, datetime.now()))}\n</observations>\n\n"
            "IMPORTANT: When responding, reference specific details from these observations. "
            "Do not give generic advice — personalize your response based on what you know about this user. "
            "For conflicting information, prefer the MOST RECENT observation (check dates)."
        )
        new_om = {"role": "user", "content": obs_text, "_observation_message": True, "_raw_observations": updated}
        self._write_log(updated)  # debug only
        log.info("[LogCompressor] %d → 1 OM + %d recent turns", len(to_observe), len(recent))
        return [new_om] + recent

    # ── Internal ──────────────────────────────────────────────────────────────

    async def start(self):
        self._migrate_v1_log_file()

    def _migrate_v1_log_file(self):
        """v1 → v2: observations хранились в memory/log/LOG.md, теперь — в CONTEXT.json как _raw_observations."""
        old_log = os.path.join(self.agent.memory.memory_dir, "log", "LOG.md")
        if not os.path.exists(old_log):
            return
        for turn in self.agent.memory._turns:
            if isinstance(turn, dict) and turn.get("_observation_message") and not turn.get("_raw_observations"):
                with open(old_log, encoding="utf-8") as f:
                    turn["_raw_observations"] = f.read()
                from src.memory.memory import save_turns_json
                save_turns_json(self.agent.memory._state_file, self.agent.memory._turns)
                log.info("[LogCompressor] migrated LOG.md → _raw_observations")
                break
        import shutil
        shutil.rmtree(os.path.dirname(old_log), ignore_errors=True)

    def _write_log(self, observations: str):
        path = os.path.join(self.agent.memory.memory_dir, "LOG.md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(observations)
        except Exception as e:
            log.warning("[LogCompressor] write LOG.md failed: %s", e, exc_info=True)

    def _split_recent(self, turns: list) -> tuple[list, list]:
        """Отделяет recent_turns (последние до recent_tokens) от turns для наблюдения.

        Гарантирует не менее min_recent_turns шагов в recent, даже если они превышают бюджет токенов.
        Никогда не разрывает пару assistant(tool_calls) + tool: если граница попадает внутрь пары,
        tool-туры сдвигаются в to_observe вместе с вызовом.
        """
        recent_budget = self._recent_tokens
        recent, tokens = [], 0
        for turn in reversed(turns):
            t = Memory.count_tokens([turn])
            if tokens + t > recent_budget and len(recent) >= self._min_recent_turns:
                break
            recent.append(turn)
            tokens += t
        recent.reverse()
        to_observe = turns[:len(turns) - len(recent)]

        # Не допускаем, чтобы recent начинался с tool-тура без парного assistant перед ним.
        while recent and isinstance(recent[0], dict) and recent[0].get("role") == "tool":
            to_observe.append(recent.pop(0))

        return to_observe, recent

    async def _generate(self, label: str, system: str, messages: list, **kwargs) -> str:
        max_retries, delay = 5, 0.5
        for attempt in range(max_retries):
            try:
                response = await self._client.chat.completions.create(
                    model=self._model_name,
                    messages=[{"role": "system", "content": system}, *messages],
                    **kwargs,
                )
                return (response.choices[0].message.content or "").strip()
            except Exception as e:
                messages = self.agent.apply_error_restriction(self._model_name, e, messages)
                if attempt + 1 == max_retries:
                    log.error("[LogCompressor] %s LLM failed: %s", label, e, exc_info=True)
                    return ""
                wait = delay * 2 ** attempt
                log.warning("[LogCompressor] %s error, retry %d/%d in %ds: %s", label, attempt + 1, max_retries, wait, e)
                await asyncio.sleep(wait)

    async def _run_observer(self, turns: list, existing_observations: str) -> str:
        messages = []
        if existing_observations:
            messages.append({"role": "user", "content": f"## Previous Observations\n\n{existing_observations}\n\nDo not repeat existing observations. Append new ones only."})
            messages.append({"role": "assistant", "content": "Understood. I will only append new observations."})
        messages.extend(self.agent.strip_contents_private(turns, self._model_name))
        messages.append({"role": "user", "content": "Extract observations from the conversation above."})

        response = await self._generate(
            "Observer", OBSERVER_SYSTEM_PROMPT, messages, temperature=0.3, max_tokens=100_000,
        )
        return _parse_observations(response)

    async def _run_reflector(self, observations: str, compression_level: int = 0) -> str:
        user_prompt = (
            f"## OBSERVATIONS TO REFLECT ON\n\n{observations}\n\n---\n\n"
            "Please analyze these observations and produce a refined, condensed version "
            "that will become the assistant's entire memory going forward."
        )
        if compression_level > 0:
            user_prompt += f"\n\n{_COMPRESSION_GUIDANCE[compression_level]}"

        reflected = _parse_observations(await self._generate(
            "Reflector", REFLECTOR_SYSTEM_PROMPT, [{"role": "user", "content": user_prompt}],
            temperature=0.0, max_tokens=100_000,
        ))

        if not reflected:
            log.warning("[LogCompressor] Reflector returned empty")
            return ""

        orig_tokens = Memory.count_tokens([{"role": "user", "content": observations}])
        refl_tokens = Memory.count_tokens([{"role": "user", "content": reflected}])

        if refl_tokens >= orig_tokens and compression_level < 3:
            log.warning("[LogCompressor] Reflection didn't compress (level %d → %d)", compression_level, compression_level + 1)
            return await self._run_reflector(observations, compression_level + 1)

        log.info("[LogCompressor] Reflector: %d → %d tokens (%.0f%%)",
                 orig_tokens, refl_tokens, (1 - refl_tokens / max(orig_tokens, 1)) * 100)
        return reflected
