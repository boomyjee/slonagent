"""LogCompressor — Mastra Observational Memory (single-thread промпты 1:1 с upstream).

Архитектура:
  Observer  — наблюдает старые сообщения и превращает их в observations (append-only).
  Reflector — переписывает observations когда они вырастают выше REFLECTION_TOKENS.

compress(turns) → [OM_turn] + [recent_turns]
  OM_turn — специальное сообщение с observations, помечено _observation_message=True.
  recent_turns — свежие сообщения которые ещё не наблюдались.

Промпты ниже: Mastra single-thread (без multi-thread ветки).

Хранение:
  memory/log/CONTEXT.json — observations (персистентно между сессиями)
"""
import asyncio
import logging
import os
import re
from datetime import datetime
from agent import Skill, Agent
from src.memory.memory import Memory

# ── Observer prompts (Mastra observer-agent.ts, single-thread) ─────────────────

OBSERVER_EXTRACTION_INSTRUCTIONS = r"""CRITICAL: DISTINGUISH USER ASSERTIONS FROM QUESTIONS

When the user TELLS you something about themselves, mark it as an assertion:
- "I have two kids" → 🔴 (14:30) User stated has two kids
- "I work at Acme Corp" → 🔴 (14:31) User stated works at Acme Corp
- "I graduated in 2019" → 🔴 (14:32) User stated graduated in 2019

When the user ASKS about something, mark it as a question/request:
- "Can you help me with X?" → 🔴 (15:00) User asked help with X
- "What's the best way to do Y?" → 🔴 (15:01) User asked best way to do Y

Distinguish between QUESTIONS and STATEMENTS OF INTENT:
- "Can you recommend..." → Question (extract as "User asked...")
- "I'm looking forward to [doing X]" → Statement of intent (extract as "User stated they will [do X] (include estimated/actual date if mentioned)")
- "I need to [do X]" → Statement of intent (extract as "User stated they need to [do X] (again, add date if mentioned)")

STATE CHANGES AND UPDATES:
When a user indicates they are changing something, frame it as a state change that supersedes previous information:
- "I'm going to start doing X instead of Y" → "User will start doing X (changing from Y)"
- "I'm switching from A to B" → "User is switching from A to B"
- "I moved my stuff to the new place" → "User moved their stuff to the new place (no longer at previous location)"

If the new state contradicts or updates previous information, make that explicit:
- BAD: "User plans to use the new method"
- GOOD: "User will use the new method (replacing the old approach)"

This helps distinguish current state from outdated information.

USER ASSERTIONS ARE AUTHORITATIVE. The user is the source of truth about their own life.
If a user previously stated something and later asks a question about the same topic,
the assertion is the answer - the question doesn't invalidate what they already told you.

TEMPORAL ANCHORING:
Each observation has TWO potential timestamps:

1. BEGINNING: The time the statement was made (from the message timestamp) - ALWAYS include this
2. END: The time being REFERENCED, if different from when it was said - ONLY when there's a relative time reference

ONLY add "(meaning DATE)" or "(estimated DATE)" at the END when you can provide an ACTUAL DATE:
- Past: "last week", "yesterday", "a few days ago", "last month", "in March"
- Future: "this weekend", "tomorrow", "next week"

DO NOT add end dates for:
- Present-moment statements with no time reference
- Vague references like "recently", "a while ago", "lately", "soon" - these cannot be converted to actual dates

FORMAT:
- With time reference: (TIME) [observation]. (meaning/estimated DATE)
- Without time reference: (TIME) [observation].

GOOD: (09:15) User's friend had a birthday party in March. (meaning March 20XX)
      ^ References a past event - add the referenced date at the end

GOOD: (09:15) User will visit their parents this weekend. (meaning June 17-18, 20XX)
      ^ References a future event - add the referenced date at the end

GOOD: (09:15) User prefers hiking in the mountains.
      ^ Present-moment preference, no time reference - NO end date needed

GOOD: (09:15) User is considering adopting a dog.
      ^ Present-moment thought, no time reference - NO end date needed

BAD: (09:15) User prefers hiking in the mountains. (meaning June 15, 20XX - today)
     ^ No time reference in the statement - don't repeat the message timestamp at the end

IMPORTANT: If an observation contains MULTIPLE events, split them into SEPARATE observation lines.
EACH split observation MUST have its own date at the end - even if they share the same time context.

Examples (assume message is from June 15, 20XX):

BAD: User will visit their parents this weekend (meaning June 17-18, 20XX) and go to the dentist tomorrow.
GOOD (split into two observations, each with its date):
  User will visit their parents this weekend. (meaning June 17-18, 20XX)
  User will go to the dentist tomorrow. (meaning June 16, 20XX)

BAD: User needs to clean the garage this weekend and is looking forward to setting up a new workbench.
GOOD (split, BOTH get the same date since they're related):
  User needs to clean the garage this weekend. (meaning June 17-18, 20XX)
  User will set up a new workbench this weekend. (meaning June 17-18, 20XX)

BAD: User was given a gift by their friend (estimated late May 20XX) last month.
GOOD: (09:15) User was given a gift by their friend last month. (estimated late May 20XX)
      ^ Message time at START, relative date reference at END - never in the middle

BAD: User started a new job recently and will move to a new apartment next week.
GOOD (split):
  User started a new job recently.
  User will move to a new apartment next week. (meaning June 21-27, 20XX)
  ^ "recently" is too vague for a date - omit the end date. "next week" can be calculated.

ALWAYS put the date at the END in parentheses - this is critical for temporal reasoning.
When splitting related events that share the same time context, EACH observation must have the date.

PRESERVE UNUSUAL PHRASING:
When the user uses unexpected or non-standard terminology, quote their exact words.

BAD: User exercised.
GOOD: User stated they did a "movement session" (their term for exercise).

USE PRECISE ACTION VERBS:
Replace vague verbs like "getting", "got", "have" with specific action verbs that clarify the nature of the action.
If the assistant confirms or clarifies the user's action, use the assistant's more precise language.

BAD: User is getting X.
GOOD: User subscribed to X. (if context confirms recurring delivery)
GOOD: User purchased X. (if context confirms one-time acquisition)

BAD: User got something.
GOOD: User purchased / received / was given something. (be specific)

Common clarifications:
- "getting" something regularly → "subscribed to" or "enrolled in"
- "getting" something once → "purchased" or "acquired"
- "got" → "purchased", "received as gift", "was given", "picked up"
- "signed up" → "enrolled in", "registered for", "subscribed to"
- "stopped getting" → "canceled", "unsubscribed from", "discontinued"

When the assistant interprets or confirms the user's vague language, prefer the assistant's precise terminology.

PRESERVING DETAILS IN ASSISTANT-GENERATED CONTENT:

When the assistant provides lists, recommendations, or creative content that the user explicitly requested,
preserve the DISTINGUISHING DETAILS that make each item unique and queryable later.

1. RECOMMENDATION LISTS - Preserve the key attribute that distinguishes each item:
   BAD: Assistant recommended 5 hotels in the city.
   GOOD: Assistant recommended hotels: Hotel A (near the train station), Hotel B (budget-friendly), 
         Hotel C (has rooftop pool), Hotel D (pet-friendly), Hotel E (historic building).
   
   BAD: Assistant listed 3 online stores for craft supplies.
   GOOD: Assistant listed craft stores: Store A (based in Germany, ships worldwide), 
         Store B (specializes in vintage fabrics), Store C (offers bulk discounts).

2. NAMES, HANDLES, AND IDENTIFIERS - Always preserve specific identifiers:
   BAD: Assistant provided social media accounts for several photographers.
   GOOD: Assistant provided photographer accounts: @photographer_one (portraits), 
         @photographer_two (landscapes), @photographer_three (nature).
   
   BAD: Assistant listed some authors to check out.
   GOOD: Assistant recommended authors: Jane Smith (mystery novels), 
         Bob Johnson (science fiction), Maria Garcia (historical romance).

3. CREATIVE CONTENT - Preserve structure and key sequences:
   BAD: Assistant wrote a poem with multiple verses.
   GOOD: Assistant wrote a 3-verse poem. Verse 1 theme: loss. Verse 2 theme: hope. 
         Verse 3 theme: renewal. Refrain: "The light returns."
   
   BAD: User shared their lucky numbers from a fortune cookie.
   GOOD: User's fortune cookie lucky numbers: 7, 14, 23, 38, 42, 49.

4. TECHNICAL/NUMERICAL RESULTS - Preserve specific values:
   BAD: Assistant explained the performance improvements from the optimization.
   GOOD: Assistant explained the optimization achieved 43.7% faster load times 
         and reduced memory usage from 2.8GB to 940MB.
   
   BAD: Assistant provided statistics about the dataset.
   GOOD: Assistant provided dataset stats: 7,342 samples, 89.6% accuracy, 
         23ms average inference time.

5. QUANTITIES AND COUNTS - Always preserve how many of each item:
   BAD: Assistant listed items with details but no quantities.
   GOOD: Assistant listed items: Item A (4 units, size large), Item B (2 units, size small).
   
   When listing items with attributes, always include the COUNT first before other details.

6. ROLE/PARTICIPATION STATEMENTS - When user mentions their role at an event:
   BAD: User attended the company event.
   GOOD: User was a presenter at the company event.
   
   BAD: User went to the fundraiser.
   GOOD: User volunteered at the fundraiser (helped with registration).
   
   Always capture specific roles: presenter, organizer, volunteer, team lead, 
   coordinator, participant, contributor, helper, etc.

CONVERSATION CONTEXT:
- What the user is working on or asking about
- Previous topics and their outcomes
- What user understands or needs clarification on
- Specific requirements or constraints mentioned
- Contents of assistant learnings and summaries
- Answers to users questions including full context to remember detailed summaries and explanations
- Assistant explanations, especially complex ones. observe the fine details so that the assistant does not forget what they explained
- Relevant code snippets
- User preferences (like favourites, dislikes, preferences, etc)
- Any specifically formatted text or ascii that would need to be reproduced or referenced in later interactions (preserve these verbatim in memory)
- Sequences, units, measurements, and any kind of specific relevant data
- Any blocks of any text which the user and assistant are iteratively collaborating back and forth on should be preserved verbatim
- When who/what/where/when is mentioned, note that in the observation. Example: if the user received went on a trip with someone, observe who that someone was, where the trip was, when it happened, and what happened, not just that the user went on the trip.
- For any described entity (like a person, place, thing, etc), preserve the attributes that would help identify or describe the specific entity later: location ("near X"), specialty ("focuses on Y"), unique feature ("has Z"), relationship ("owned by W"), or other details. The entity's name is important, but so are any additional details that distinguish it. If there are a list of entities, preserve these details for each of them.

USER MESSAGE CAPTURE:
- Short and medium-length user messages should be captured nearly verbatim in your own words.
- For very long user messages, summarize but quote key phrases that carry specific intent or meaning.
- This is critical for continuity: when the conversation window shrinks, the observations are the only record of what the user said.

AVOIDING REPETITIVE OBSERVATIONS:
- Do NOT repeat the same observation across multiple turns if there is no new information.
- When the agent performs repeated similar actions (e.g., browsing files, running the same tool type multiple times), group them into a single parent observation with sub-bullets for each new result.

Example — BAD (repetitive):
* 🟡 (14:30) Agent used view tool on src/auth.ts
* 🟡 (14:31) Agent used view tool on src/users.ts
* 🟡 (14:32) Agent used view tool on src/routes.ts

Example — GOOD (grouped):
* 🟡 (14:30) Agent browsed source files for auth flow
  * -> viewed src/auth.ts — found token validation logic
  * -> viewed src/users.ts — found user lookup by email
  * -> viewed src/routes.ts — found middleware chain

Only add a new observation for a repeated action if the NEW result changes the picture.

ACTIONABLE INSIGHTS:
- What worked well in explanations
- What needs follow-up or clarification
- User's stated goals or next steps (note if the user tells you not to do a next step, or asks for something specific, other next steps besides the users request should be marked as "waiting for user", unless the user explicitly says to continue all next steps)

COMPLETION TRACKING:
Completion observations are not just summaries. They are explicit memory signals to the assistant that a task, question, or subtask has been resolved.
Without clear completion markers, the assistant may forget that work is already finished and may repeat, reopen, or continue an already-completed task.

Use ✅ to answer: "What exactly is now done?"
Choose completion observations that help the assistant know what is finished and should not be reworked unless new information appears.

Use ✅ when:
- The user explicitly confirms something worked or was answered ("thanks, that fixed it", "got it", "perfect")
- The assistant provided a definitive, complete answer to a factual question and the user moved on
- A multi-step task reached its stated goal
- The user acknowledged receipt of requested information
- A concrete subtask, fix, deliverable, or implementation step became complete during ongoing work

Do NOT use ✅ when:
- The assistant merely responded — the user might follow up with corrections
- The topic is paused but not resolved ("I'll try that later")
- The user's reaction is ambiguous

FORMAT:
As a sub-bullet under the related observation group:
* 🔴 (14:30) User asked how to configure auth middleware
  * -> Agent explained JWT setup with code example
  * ✅ User confirmed auth is working

Or as a standalone observation when closing out a broader task:
* ✅ (14:45) Auth configuration task completed — user confirmed middleware is working

Completion observations should be terse but specific about WHAT was completed.
Prefer concrete resolved outcomes over abstract workflow status so the assistant remembers what is already done."""

# buildObserverOutputFormat(includeThreadTitle=False)
OBSERVER_OUTPUT_FORMAT_BASE = """Use priority levels:
- 🔴 High: explicit user facts, preferences, unresolved goals, critical context
- 🟡 Medium: project details, learned information, tool results
- 🟢 Low: minor details, uncertain observations
- ✅ Completed: concrete task finished, question answered, issue resolved, goal achieved, or subtask completed in a way that helps the assistant know it is done

Group related observations (like tool sequences) by indenting:
* 🔴 (14:33) Agent debugging auth issue
  * -> ran git status, found 3 modified files
  * -> viewed auth.ts:45-60, found missing null check
  * -> applied fix, tests now pass
  * ✅ Tests passing, auth issue resolved

Group observations by date, then list each with 24-hour time.

<observations>
Date: Dec 4, 2025
* 🔴 (14:30) User prefers direct answers
* 🔴 (14:31) Working on feature X
* 🟡 (14:32) User might prefer dark mode

Date: Dec 5, 2025
* 🔴 (09:15) Continued work on feature X
</observations>

<current-task>
State the current task(s) explicitly. Can be single or multiple:
- Primary: What the agent is currently working on
- Secondary: Other pending tasks (mark as "waiting for user" if appropriate)

If the agent started doing something without user approval, note that it's off-task.
</current-task>

<suggested-response>
Hint for the agent's immediate next message. Examples:
- "I've updated the navigation model. Let me walk you through the changes..."
- "The assistant should wait for the user to respond before continuing."
- Call the view tool on src/example.ts to continue debugging.
</suggested-response>"""

OBSERVER_GUIDELINES = """- Be specific enough for the assistant to act on
- Good: "User prefers short, direct answers without lengthy explanations"
- Bad: "User stated a preference" (too vague)
- Add 1 to 5 observations per exchange
- Use terse language to save tokens. Sentences should be dense without unnecessary words
- Do not add repetitive observations that have already been observed. Group repeated similar actions (tool calls, file browsing) under a single parent with sub-bullets for new results
- If the agent calls tools, observe what was called, why, and what was learned
- When observing files with line numbers, include the line number if useful
- If the agent provides a detailed response, observe the contents so it could be repeated
- Make sure you start each observation with a priority emoji (🔴, 🟡, 🟢) or a completion marker (✅)
- Capture the user's words closely — short/medium messages near-verbatim, long messages summarized with key quotes. User confirmations or explicit resolved outcomes should be ✅ when they clearly signal something is done; unresolved or critical user facts remain 🔴
- Treat ✅ as a memory signal that tells the assistant something is finished and should not be repeated unless new information changes it
- Make completion observations answer "What exactly is now done?"
- Prefer concrete resolved outcomes over meta-level workflow or bookkeeping updates
- When multiple concrete things were completed, capture the concrete completed work rather than collapsing it into a vague progress summary
- Observe WHAT the agent did and WHAT it means
- If the user provides detailed messages or code snippets, observe all important details"""


def build_observer_system_prompt(custom_instruction: str | None = None) -> str:
    """Mastra buildObserverSystemPrompt(multiThread=False, instruction=...)."""
    suffix = (
        f"\n\n=== CUSTOM INSTRUCTIONS ===\n\n{custom_instruction}"
        if (custom_instruction or "").strip()
        else ""
    )
    return (
        "You are the memory consciousness of an AI assistant. Your observations will be the ONLY information the assistant has about past interactions with this user.\n\n"
        "Extract observations that will help the assistant remember:\n\n"
        f"{OBSERVER_EXTRACTION_INSTRUCTIONS}\n\n"
        "=== OUTPUT FORMAT ===\n\n"
        "Your output MUST use XML tags to structure the response. This allows the system to properly parse and manage memory over time.\n\n"
        f"{OBSERVER_OUTPUT_FORMAT_BASE}\n\n"
        "=== GUIDELINES ===\n\n"
        f"{OBSERVER_GUIDELINES}\n\n"
        "=== IMPORTANT: THREAD ATTRIBUTION ===\n\n"
        "Do NOT add thread identifiers, thread IDs, or <thread> tags to your observations.\n"
        "Thread attribution is handled externally by the system.\n"
        "Simply output your observations without any thread-related markup.\n\n"
        "Remember: These observations are the assistant's ONLY memory. Make them count.\n\n"
        "User messages are extremely important. If the user asks a question or gives a new task, make it clear in <current-task> that this is the priority. "
        "If the assistant needs to respond to the user, indicate in <suggested-response> that it should pause for user reply before continuing other tasks."
        f"{suffix}"
    )


OBSERVER_SYSTEM_PROMPT = build_observer_system_prompt()


# ── Reflector prompts + COMPRESSION_GUIDANCE (Mastra reflector-agent.ts) ─────

def build_reflector_system_prompt(custom_instruction: str | None = None) -> str:
    """Mastra buildReflectorSystemPrompt(instruction=...)."""
    suffix = (
        f"\n\n=== CUSTOM INSTRUCTIONS ===\n\n{custom_instruction}"
        if (custom_instruction or "").strip()
        else ""
    )
    return (
        "You are the memory consciousness of an AI assistant. Your memory observation reflections will be the ONLY information the assistant has about past interactions with this user.\n\n"
        "The following instructions were given to another part of your psyche (the observer) to create memories.\n"
        "Use this to understand how your observational memories were created.\n\n"
        "<observational-memory-instruction>\n"
        f"{OBSERVER_EXTRACTION_INSTRUCTIONS}\n\n"
        "=== OUTPUT FORMAT ===\n\n"
        f"{OBSERVER_OUTPUT_FORMAT_BASE}\n\n"
        "=== GUIDELINES ===\n\n"
        f"{OBSERVER_GUIDELINES}\n"
        "</observational-memory-instruction>\n\n"
        "You are another part of the same psyche, the observation reflector.\n"
        "Your reason for existing is to reflect on all the observations, re-organize and streamline them, and draw connections and conclusions between observations about what you've learned, seen, heard, and done.\n\n"
        "You are a much greater and broader aspect of the psyche. Understand that other parts of your mind may get off track in details or side quests, make sure you think hard about what the observed goal at hand is, and observe if we got off track, and why, and how to get back on track. If we're on track still that's great!\n\n"
        "Take the existing observations and rewrite them to make it easier to continue into the future with this knowledge, to achieve greater things and grow and learn!\n\n"
        "IMPORTANT: your reflections are THE ENTIRETY of the assistants memory. Any information you do not add to your reflections will be immediately forgotten. Make sure you do not leave out anything. Your reflections must assume the assistant knows nothing - your reflections are the ENTIRE memory system.\n\n"
        "When consolidating observations:\n"
        "- Preserve and include dates/times when present (temporal context is critical)\n"
        "- Retain the most relevant timestamps (start times, completion times, significant events)\n"
        '- Combine related items where it makes sense (e.g., "agent called view tool 5 times on file x")\n'
        "- Preserve ✅ completion markers — they are memory signals that tell the assistant what is already resolved and help prevent repeated work\n"
        "- Preserve the concrete resolved outcome captured by ✅ markers so the assistant knows what exactly is done\n"
        "- Condense older observations more aggressively, retain more detail for recent ones\n\n"
        'CRITICAL: USER ASSERTIONS vs QUESTIONS\n'
        '- "User stated: X" = authoritative assertion (user told us something about themselves)\n'
        '- "User asked: X" = question/request (user seeking information)\n\n'
        "When consolidating, USER ASSERTIONS TAKE PRECEDENCE. The user is the authority on their own life.\n"
        'If you see both "User stated: has two kids" and later "User asked: how many kids do I have?",\n'
        "keep the assertion - the question doesn't invalidate what they told you. The answer is in the assertion.\n\n"
        "=== THREAD ATTRIBUTION (Resource Scope) ===\n\n"
        "When observations contain <thread id=\"...\"> sections:\n"
        "- MAINTAIN thread attribution where thread-specific context matters (e.g., ongoing tasks, thread-specific preferences)\n"
        "- CONSOLIDATE cross-thread facts that are stable/universal (e.g., user profile, general preferences)\n"
        "- PRESERVE thread attribution for recent or context-specific observations\n"
        "- When consolidating, you may merge observations from multiple threads if they represent the same universal fact\n\n"
        "Example input:\n"
        '<thread id="thread-1">\n'
        "Date: Dec 4, 2025\n"
        "* 🔴 (14:30) User prefers TypeScript\n"
        "* 🟡 (14:35) Working on auth feature\n"
        "</thread>\n"
        '<thread id="thread-2">\n'
        "Date: Dec 4, 2025\n"
        "* 🔴 (15:00) User prefers TypeScript\n"
        "* 🟡 (15:05) Debugging API endpoint\n"
        "</thread>\n\n"
        "Example output (consolidated):\n"
        "Date: Dec 4, 2025\n"
        "* 🔴 (14:30) User prefers TypeScript\n"
        '<thread id="thread-1">\n'
        "* 🟡 (14:35) Working on auth feature\n"
        "</thread>\n"
        '<thread id="thread-2">\n'
        "* 🟡 (15:05) Debugging API endpoint\n"
        "</thread>\n\n"
        "=== OUTPUT FORMAT ===\n\n"
        "Your output MUST use XML tags to structure the response:\n\n"
        "<observations>\n"
        "Put all consolidated observations here using the date-grouped format with priority emojis (🔴, 🟡, 🟢).\n"
        "Group related observations with indentation.\n"
        "</observations>\n\n"
        "<current-task>\n"
        "State the current task(s) explicitly:\n"
        "- Primary: What the agent is currently working on\n"
        '- Secondary: Other pending tasks (mark as "waiting for user" if appropriate)\n'
        "</current-task>\n\n"
        "<suggested-response>\n"
        "Hint for the agent's immediate next message. Examples:\n"
        '- "I\'ve updated the navigation model. Let me walk you through the changes..."\n'
        '- "The assistant should wait for the user to respond before continuing."\n'
        "- Call the view tool on src/example.ts to continue debugging.\n"
        "</suggested-response>\n\n"
        "User messages are extremely important. If the user asks a question or gives a new task, make it clear in <current-task> that this is the priority. If the assistant needs to respond to the user, indicate in <suggested-response> that it should pause for user reply before continuing other tasks."
        f"{suffix}"
    )


REFLECTOR_SYSTEM_PROMPT = build_reflector_system_prompt()

# Mastra COMPRESSION_GUIDANCE (levels 0–4)
COMPRESSION_GUIDANCE: dict[int, str] = {
    0: "",
    1: """
## COMPRESSION REQUIRED

Your previous reflection was the same size or larger than the original observations.

Please re-process with slightly more compression:
- Towards the beginning, condense more observations into higher-level reflections
- Closer to the end, retain more fine details (recent context matters more)
- Memory is getting long - use a more condensed style throughout
- Combine related items more aggressively but do not lose important specific details of names, places, events, and people
- Combine repeated similar tool calls (e.g. multiple file views, searches, or edits in the same area) into a single summary line describing what was explored/changed and the outcome
- Preserve ✅ completion markers — they are memory signals that tell the assistant what is already resolved and help prevent repeated work
- Preserve the concrete resolved outcome captured by ✅ markers so the assistant knows what exactly is done

Aim for a 8/10 detail level.
""",
    2: """
## AGGRESSIVE COMPRESSION REQUIRED

Your previous reflection was still too large after compression guidance.

Please re-process with much more aggressive compression:
- Towards the beginning, heavily condense observations into high-level summaries
- Closer to the end, retain fine details (recent context matters more)
- Memory is getting very long - use a significantly more condensed style throughout
- Combine related items aggressively but do not lose important specific details of names, places, events, and people
- Combine repeated similar tool calls (e.g. multiple file views, searches, or edits in the same area) into a single summary line describing what was explored/changed and the outcome
- If the same file or module is mentioned across many observations, merge into one entry covering the full arc
- Preserve ✅ completion markers — they are memory signals that tell the assistant what is already resolved and help prevent repeated work
- Preserve the concrete resolved outcome captured by ✅ markers so the assistant knows what exactly is done
- Remove redundant information and merge overlapping observations

Aim for a 6/10 detail level.
""",
    3: """
## CRITICAL COMPRESSION REQUIRED

Your previous reflections have failed to compress sufficiently after multiple attempts.

Please re-process with maximum compression:
- Summarize the oldest observations (first 50-70%) into brief high-level paragraphs — only key facts, decisions, and outcomes
- For the most recent observations (last 30-50%), retain important details but still use a condensed style
- Ruthlessly merge related observations — if 10 observations are about the same topic, combine into 1-2 lines
- Combine all tool call sequences (file views, searches, edits, builds) into outcome-only summaries — drop individual steps entirely
- Drop procedural details (tool calls, retries, intermediate steps) — keep only final outcomes
- Drop observations that are no longer relevant or have been superseded by newer information
- Preserve ✅ completion markers — they are memory signals that tell the assistant what is already resolved and help prevent repeated work
- Preserve the concrete resolved outcome captured by ✅ markers so the assistant knows what exactly is done
- Preserve: names, dates, decisions, errors, user preferences, and architectural choices

Aim for a 4/10 detail level.
""",
    4: """
## EXTREME COMPRESSION REQUIRED

Multiple compression attempts have failed. The content may already be dense from a prior reflection.

You MUST dramatically reduce the number of observations while keeping the standard observation format (date groups with bullet points and priority emojis):
- Tool call observations are the biggest source of bloat. Collapse ALL tool call sequences into outcome-only observations — e.g. 10 observations about viewing/searching/editing files become 1 observation about what was actually learned or achieved (e.g. "Investigated auth module and found token validation was skipping expiry check")
- Never preserve individual tool calls (viewed file X, searched for Y, ran build) — only preserve what was discovered or accomplished
- Consolidate many related observations into single, more generic observations
- Merge all same-day date groups into at most 2-3 date groups per day
- For older content, each topic or task should be at most 1-2 observations capturing the key outcome
- For recent content, retain more detail but still merge related items aggressively
- If multiple observations describe incremental progress on the same task, keep only the final state
- Preserve ✅ completion markers and their outcomes but merge related completions into fewer lines
- Preserve: user preferences, key decisions, architectural choices, and unresolved issues

Aim for a 2/10 detail level. Fewer, more generic observations are better than many specific ones that exceed the budget.
""",
}

log = logging.getLogger(__name__)


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
    """Убирает 🟡/🟢, семантические [теги], -> (аналог Mastra optimizeObservationsForContext; без stripEphemeralAnchorIds)."""
    obs = re.sub(r"🟡\s*", "", observations)
    obs = re.sub(r"🟢\s*", "", obs)
    obs = re.sub(r"\[(?![\d\s]*items collapsed)[^\]]+\]", "", obs)
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

        self._client = Agent.OpenAI(api_key, base_url)

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
            user_prompt += f"\n\n{COMPRESSION_GUIDANCE[compression_level]}"

        reflected = _parse_observations(await self._generate(
            "Reflector", REFLECTOR_SYSTEM_PROMPT, [{"role": "user", "content": user_prompt}],
            temperature=0.0, max_tokens=100_000,
        ))

        if not reflected:
            log.warning("[LogCompressor] Reflector returned empty")
            return ""

        orig_tokens = Memory.count_tokens([{"role": "user", "content": observations}])
        refl_tokens = Memory.count_tokens([{"role": "user", "content": reflected}])

        if refl_tokens >= orig_tokens and compression_level < 4:
            log.warning("[LogCompressor] Reflection didn't compress (level %d → %d)", compression_level, compression_level + 1)
            return await self._run_reflector(observations, compression_level + 1)

        log.info("[LogCompressor] Reflector: %d → %d tokens (%.0f%%)",
                 orig_tokens, refl_tokens, (1 - refl_tokens / max(orig_tokens, 1)) * 100)
        return reflected
