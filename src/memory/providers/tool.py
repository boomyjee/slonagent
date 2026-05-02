"""ToolProvider — память об использовании инструментов.

Собирает статистику по каждому инструменту (total_calls, success rate, avg_tokens, avg_time)
и генерирует обогащённое описание через LLM на основе реальных примеров использования из диалога.
Описание подмешивается в объявление инструмента перед каждым вызовом LLM через get_tool_prompt.
Данные хранятся в memory/tool/tool_memory.json.
"""
import asyncio, json, logging, os
from datetime import datetime

from agent import Agent
from src.memory.providers.base import BaseProvider

log = logging.getLogger(__name__)

SUMMARIZE_PROMPT = """\
You are analyzing how an AI agent uses its tools, based on the conversation above.

## Tool to analyze: {tool_name}

## Previous usage guideline for this tool (if any):
{previous_content}

## Task:
Based on the conversation above, update the usage guideline for tool `{tool_name}`.
Focus on:
1. **When to use it**: triggers and user intents that lead to this tool
2. **What works**: parameter patterns, input formats that succeeded
3. **What doesn't work**: inputs or scenarios that failed or disappointed the user
4. **Best practices**: concrete recommendations from observed usage

Write a concise guideline (max 200 words). Plain text, no code blocks.\
"""


class ToolProvider(BaseProvider):
    def __init__(self, model_name: str, api_key: str = "", base_url: str = "",
                 backend: str = "openai", backend_params: dict | None = None,
                 consolidate_tokens: int = 3_000):
        super().__init__(consolidate_tokens=consolidate_tokens)
        self._model_name = model_name
        self._api_key = api_key
        self._base_url = base_url
        self._backend = backend
        self._backend_params = backend_params
        self._tool_stats_file: str = ""
        self._tool_stats: dict = {}

    async def start(self):
        await super().start()
        os.makedirs(self.provider_dir, exist_ok=True)
        self._tool_stats_file = os.path.join(self.provider_dir, "tool_memory.json")
        self._tool_stats = self._load()

    def _load(self) -> dict:
        try:
            with open(self._tool_stats_file, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            log.warning("[ToolProvider] load failed: %s", e, exc_info=True)
            return {}

    def _save(self):
        try:
            tmp = self._tool_stats_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._tool_stats, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._tool_stats_file)
        except Exception as e:
            log.warning("[ToolProvider] save failed: %s", e, exc_info=True)

    async def _consolidate(self, pending: list):
        by_thread: dict[str, list] = {}
        for turn in pending:
            if isinstance(turn, dict):
                by_thread.setdefault(turn.get("_thread_id", ""), []).append(turn)

        for tid, thread_pending in by_thread.items():
            await self._consolidate_thread(tid, thread_pending)
        self._save()

    async def _consolidate_thread(self, thread_id: str, pending: list):
        tool_names: set[str] = set()
        pending_calls: dict[str, tuple[dict, str]] = {}  # tool_call_id -> (call_info, call_time)
        contents = []
        for turn in pending:
            role = turn.get("role", "")
            text_parts = []

            if role == "assistant":
                content = turn.get("content")
                if isinstance(content, str) and content:
                    text_parts.append(content)
                for tc in turn.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except Exception:
                        args = {}
                    pending_calls[tc["id"]] = ({"name": name, "args": args}, turn.get("_timestamp"))
                    text_parts.append(f"\n[Tool call: {name}({json.dumps(args, ensure_ascii=False)}]\n")

            elif role == "tool":
                tool_call_id = turn.get("tool_call_id", "")
                response_content = turn.get("content", "")
                if tool_call_id in pending_calls:
                    call, call_time = pending_calls.pop(tool_call_id)
                    name = call["name"]
                    args = call["args"]
                    try:
                        response = json.loads(response_content) if isinstance(response_content, str) else {}
                    except Exception:
                        response = {"result": response_content}
                    success = "error" not in response
                    entry = self._tool_stats.setdefault(name, {"content": "", "total_calls": 0, "total_success": 0, "avg_tokens": 0.0, "avg_time": 0.0})
                    entry["total_calls"] += 1
                    entry["total_success"] += int(success)
                    n = entry["total_calls"]
                    token_cost = (len(json.dumps(args)) + len(json.dumps(response))) // 4
                    entry["avg_tokens"] += (token_cost - entry["avg_tokens"]) / n
                    try:
                        if call_time:
                            delta_time = (datetime.fromisoformat(turn["_timestamp"]) - datetime.fromisoformat(call_time)).total_seconds()
                            entry["avg_time"] += (delta_time - entry["avg_time"]) / n
                    except Exception:
                        pass
                    tool_names.add(name)
                    text_parts.append(f"\n[Tool response: {name} → {json.dumps(response, ensure_ascii=False)}]\n")

            elif role == "user":
                content = turn.get("content", "")
                if isinstance(content, str):
                    text_parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block["text"])

            if text_parts:
                oai_role = "assistant" if role == "assistant" else "user"
                contents.append({"role": oai_role, "content": "\n".join(text_parts)})

        contents = self.agent.strip_contents_private(contents, self._model_name)
        if tool_names:
            await asyncio.gather(*[self._summarize_tool_use(name, contents) for name in tool_names])
            log.info("[ToolProvider] thread %r: consolidated %d tools: %s", thread_id, len(tool_names), list(tool_names))

    def _make_sub_agent(self) -> Agent:
        """Эфемерный Agent для одного LLM-вызова. Параллельные вызовы безопасны."""
        return Agent(
            id="", model_name=self._model_name,
            api_key=self._api_key, base_url=self._base_url,
            backend=self._backend, backend_params=self._backend_params,
        )

    async def _summarize_tool_use(self, tool_name: str, contents: list):
        entry = self._tool_stats.setdefault(tool_name, {"content": "", "total_calls": 0, "total_success": 0})

        instruction = SUMMARIZE_PROMPT.format(
            tool_name=tool_name,
            previous_content=entry.get("content") or "(none)",
        )
        # Эфемерный sub-Agent — каждый параллельный вызов получает свою память
        # и backend client, без shared state.
        sub = self._make_sub_agent()
        try:
            # SUMMARIZE_PROMPT ссылается на "conversation above" — кладём его
            # последним user-turn'ом, чтобы диалог реально был выше инструкции.
            await sub.memory.add_turn(*contents, {"role": "user", "content": instruction})
            try:
                turn = await sub.llm()
            except Exception as e:
                log.warning("[ToolProvider] summarize failed for %s: %s", tool_name, e, exc_info=True)
                return
            entry["content"] = Agent.turn_text(turn)
            log.info("[ToolProvider] summarized %s", tool_name)
        finally:
            await sub.close()

    async def get_tool_prompt(self, tool_name: str) -> str:
        entry = self._tool_stats.get(tool_name)
        if not entry or not entry.get("content"):
            return ""
        total = entry.get("total_calls", 0)
        success_rate = entry.get("total_success", 0) / total if total else 0
        avg_tokens = entry.get("avg_tokens", 0)
        avg_time = entry.get("avg_time", 0)
        stats = f"{total} calls | {success_rate:.0%} success | ~{avg_tokens:.0f} tokens | ~{avg_time:.1f}s"
        return f"{entry['content']}\n\n_{stats}_"
