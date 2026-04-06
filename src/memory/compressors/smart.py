"""Context compression for long conversation history.

Порт ReMe (Alibaba) MessageCompactOp + MessageCompressOp + MessageOffloadOp.

Использование:
    compressor = MemoryCompressor(client=gemini_client, model_name="gemini-2.0-flash")
    memory = Memory(compress_fn=compressor.compress)
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from agent import Skill, Agent
from src.memory.memory import Memory

log = logging.getLogger(__name__)


class WorkingMemoryMode(str, Enum):
    COMPACT = "compact"
    COMPRESS = "compress"
    AUTO = "auto"


COMPRESS_PROMPT = """\
Here is the conversation history that needs to be compressed:
\"\"\"
{messages_content}
\"\"\"

You are the component that summarizes internal chat history into a given structure.
When the conversation history grows too large, you will be invoked to distill the entire \
history into a concise, structured XML snapshot. This snapshot is CRITICAL, as it will \
become the agent's *only* memory of the past. The agent will resume its work based solely \
on this snapshot. All crucial details, plans, errors, and user directives MUST be preserved.

First, think through the entire history in a private <scratchpad>. Review the user's overall \
goal, the agent's actions, tool outputs, file modifications, and any unresolved questions. \
Identify every piece of information that is essential for future actions.

After your reasoning is complete, generate the final <state_snapshot> XML object. \
Be incredibly dense with information. Omit any irrelevant conversational filler.

The structure MUST be as follows:

<state_snapshot>
    <overall_goal>
        A single, concise sentence describing the user's high-level objective.
    </overall_goal>

    <key_knowledge>
        Crucial facts, conventions, and constraints the agent must remember.
        Use bullet points.
    </key_knowledge>

    <recent_actions>
        A summary of the last few significant agent actions and their outcomes. Focus on facts.
    </recent_actions>
</state_snapshot>

First think in a private <scratchpad>. Then generate the <state_snapshot>.
"""


class SmartCompressor(Skill):
    """Сжатие контекста разговора по мотивам ReMe (Alibaba).

    Три режима (WorkingMemoryMode):
      COMPACT  — офлоад больших tool outputs в файлы (lossless)
      COMPRESS — LLM-сжатие старых сообщений в state_snapshot (оригинал сохраняется)
      AUTO     — сначала COMPACT, если compact_ratio > threshold → ещё COMPRESS

    Параметры:
      max_total_tokens        — порог токенов, при котором включается сжатие
      max_tool_message_tokens — COMPACT: tool output больше этого → офлоадится
      preview_char_length     — COMPACT: сколько символов оставить как preview (0 = нет)
      keep_recent_count       — последние N сообщений не трогать никогда
      compact_ratio_threshold — AUTO: если после COMPACT ratio > этого → применить COMPRESS
      group_token_threshold   — COMPRESS: бить старые сообщения на группы по N токенов
      cleanup_max_age_days    — удалять файлы старше N дней при запуске (0 = не чистить)
    """

    def __init__(
        self,
        model_name: str,
        api_key: str,
        base_url: str,
        mode: WorkingMemoryMode = WorkingMemoryMode.AUTO,
        max_total_tokens: int = 20_000,
        max_tool_message_tokens: int = 2_000,
        preview_char_length: int = 200,
        keep_recent_count: int = 10,
        compact_ratio_threshold: float = 0.75,
        group_token_threshold: int | None = None,
        cleanup_max_age_days: int = 7,
    ):
        super().__init__()
        self._client = Agent.OpenAI(api_key, base_url)
        self.model_name = model_name
        self._cleanup_max_age_days = cleanup_max_age_days
        self.store_dir = None
        self.mode = mode
        self.max_total_tokens = max_total_tokens
        self.max_tool_message_tokens = max_tool_message_tokens
        self.preview_char_length = preview_char_length
        self.keep_recent_count = keep_recent_count
        self.compact_ratio_threshold = compact_ratio_threshold
        self.group_token_threshold = group_token_threshold

    async def start(self):
        self.store_dir = os.path.join(self.agent.memory.memory_dir, "compressed")
        if self._cleanup_max_age_days > 0:
            self._cleanup_old_files(self._cleanup_max_age_days)

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------

    async def compress(self, turns: list) -> list:
        """Точка входа для Memory(compress_fn=compressor.compress)."""
        if self.mode == WorkingMemoryMode.COMPACT:
            result, _ = self._compact(turns)
            return result

        if self.mode == WorkingMemoryMode.COMPRESS:
            return await self._compress(turns)

        return await self._auto(turns)

    # ------------------------------------------------------------------
    # Приватные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _fix_split(to_process: list, recent: list) -> tuple[list, list]:
        """Сдвигает orphan tool-туры из начала recent в конец to_process.
        Гарантирует, что пара assistant(tool_calls)+tool никогда не разрывается на границе разреза."""
        while recent and isinstance(recent[0], dict) and recent[0].get("role") == "tool":
            to_process.append(recent.pop(0))
        return to_process, recent

    def _compact(self, turns: list) -> tuple[list, dict]:
        """Офлоад больших tool outputs в файлы (lossless)."""
        if Memory.count_tokens(turns) <= self.max_total_tokens:
            return turns, {}

        recent = turns[-self.keep_recent_count:] if self.keep_recent_count > 0 else []
        to_process = turns[:-self.keep_recent_count] if self.keep_recent_count > 0 else turns[:]
        to_process, recent = self._fix_split(to_process, recent)

        write_file_dict = {}
        result = []

        # Pre-pass: build tool_call_id → name map from assistant turns
        id_to_name: dict[str, str] = {}
        for turn in to_process:
            if isinstance(turn, dict) and turn.get("role") == "assistant":
                for tc in turn.get("tool_calls") or []:
                    tc_id = tc.get("id", "")
                    tc_name = tc.get("function", {}).get("name", tc_id)
                    id_to_name[tc_id] = tc_name

        for turn in to_process:
            if not isinstance(turn, dict) or turn.get("role") != "tool":
                result.append(turn)
                continue

            tool_call_id = turn.get("tool_call_id", "")
            tool_name = id_to_name.get(tool_call_id, tool_call_id)
            resp_text = turn.get("content", "")
            if not isinstance(resp_text, str):
                resp_text = json.dumps(resp_text, ensure_ascii=False)
            part_tokens = len(resp_text) // 4

            if part_tokens <= self.max_tool_message_tokens:
                result.append(turn)
                continue

            path = self._save_to_file(resp_text, suffix=".txt")
            write_file_dict[path] = resp_text

            compact_text = f"tool_result for {tool_name!r} stored in file: {path}"
            if self.preview_char_length > 0:
                compact_text += f"\npreview: {resp_text[:self.preview_char_length]}…"

            log.info("[compact] offloaded %s (%d tokens) → %s", tool_name, part_tokens, path)
            result.append({**turn, "content": compact_text})

        return result + recent, write_file_dict

    async def _compress(self, turns: list) -> list:
        """LLM-сжатие старых тёрнов в state_snapshot."""
        if Memory.count_tokens(turns) <= self.max_total_tokens:
            return turns

        recent = turns[-self.keep_recent_count:] if self.keep_recent_count > 0 else []
        old = turns[:-self.keep_recent_count] if self.keep_recent_count > 0 else turns[:]
        old, recent = self._fix_split(old, recent)

        if not old:
            return turns

        groups = (
            self._split_by_token_threshold(old, self.group_token_threshold)
            if self.group_token_threshold
            else [old]
        )

        summary_parts = []
        for idx, group in enumerate(groups):
            original_path = self._save_to_file(group)
            prompt = COMPRESS_PROMPT.format(messages_content=self._format_turns_for_llm(group))

            log.info("[compress] group %d/%d: %d turns → LLM", idx + 1, len(groups), len(group))
            try:
                response = await self._client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = response.choices[0].message.content or ""
            except Exception as e:
                log.warning("[compress] LLM failed for group %d: %s — keeping original", idx, e, exc_info=True)
                return turns

            snapshot = self._extract_state_snapshot(raw) or raw.strip()
            summary_parts.append(
                f"[Compressed conversation history — part {idx + 1}/{len(groups)}]\n"
                f"{snapshot}\n"
                f"(Original {len(group)} turns saved to: {original_path})"
            )
            log.info("[compress] group %d done: %d chars → %s", idx + 1, len(snapshot), original_path)

        return [
            {"role": "user", "content": "\n\n".join(summary_parts), "_compressed": True}
        ] + recent

    async def _auto(self, turns: list) -> list:
        """COMPACT → если ratio > threshold → COMPRESS."""
        origin_tokens = Memory.count_tokens(turns)
        compacted, _ = self._compact(turns)
        compact_tokens = Memory.count_tokens(compacted)

        if origin_tokens > 0:
            ratio = compact_tokens / origin_tokens
            log.info("[auto] compact ratio=%.2f (threshold=%.2f)", ratio, self.compact_ratio_threshold)
            if ratio > self.compact_ratio_threshold:
                return await self._compress(compacted)

        return compacted

    # ------------------------------------------------------------------
    # Статические вспомогательные методы
    # ------------------------------------------------------------------


    @staticmethod
    def _format_turns_for_llm(turns: list) -> str:
        id_to_name: dict[str, str] = {}
        for turn in turns:
            if isinstance(turn, dict) and turn.get("role") == "assistant":
                for tc in turn.get("tool_calls") or []:
                    tc_id = tc.get("id", "")
                    tc_name = tc.get("function", {}).get("name", tc_id)
                    id_to_name[tc_id] = tc_name

        lines = []
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role", "?")

            if role in ("user", "assistant"):
                content = turn.get("content")
                if isinstance(content, str) and content:
                    lines.append(f"[{role}]: {content}")
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                            lines.append(f"[{role}]: {block['text']}")
                for tc in turn.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    try:
                        args = json.dumps(json.loads(fn.get("arguments", "{}")), ensure_ascii=False)
                    except Exception:
                        args = fn.get("arguments", "{}")
                    lines.append(f"[{role}/tool_call]: {fn.get('name')} {args}")

            elif role == "tool":
                tool_call_id = turn.get("tool_call_id", "")
                tool_name = id_to_name.get(tool_call_id, tool_call_id)
                resp = turn.get("content", "")
                if not isinstance(resp, str):
                    resp = json.dumps(resp, ensure_ascii=False)
                if len(resp) > 500:
                    resp = resp[:500] + "…"
                lines.append(f"[tool_result/{tool_name}]: {resp}")

        return "\n".join(lines)

    @staticmethod
    def _extract_state_snapshot(text: str) -> str | None:
        m = re.search(r"<state_snapshot>.*?</state_snapshot>", text, re.DOTALL)
        return m.group(0).strip() if m else None

    @staticmethod
    def _split_by_token_threshold(turns: list, threshold: int) -> list[list]:
        groups, current, current_tokens = [], [], 0
        for turn in turns:
            t = Memory.count_tokens([turn])
            if t > threshold:
                if current:
                    groups.append(current)
                    current, current_tokens = [], 0
                groups.append([turn])
            elif current_tokens + t > threshold and current:
                groups.append(current)
                current, current_tokens = [turn], t
            else:
                current.append(turn)
                current_tokens += t
        if current:
            groups.append(current)
        return groups

    def _save_to_file(self, data, suffix: str = ".json") -> str:
        os.makedirs(self.store_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        uid = uuid4().hex[:8]
        path = os.path.join(self.store_dir, f"{ts}_{uid}{suffix}")
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.store_dir, delete=False, suffix=".tmp"
            ) as f:
                if suffix == ".json":
                    json.dump(data, f, ensure_ascii=False, default=str)
                else:
                    f.write(data)
                tmp = f.name
            os.replace(tmp, path)
        except Exception as e:
            log.warning("[compress] save failed: %s", e, exc_info=True)
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        return path

    def _cleanup_old_files(self, max_age_days: int) -> None:
        if not os.path.exists(self.store_dir):
            return
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        for fname in os.listdir(self.store_dir):
            fpath = os.path.join(self.store_dir, fname)
            if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                try:
                    os.unlink(fpath)
                    removed += 1
                except Exception as e:
                    log.warning("[compress] cleanup failed for %s: %s", fpath, e, exc_info=True)
        if removed:
            log.info("[compress] cleaned up %d files older than %d days", removed, max_age_days)
