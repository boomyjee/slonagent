"""ToolProvider — память об использовании инструментов.

Собирает статистику по каждому инструменту (total_calls, success rate, avg_tokens, avg_time)
и генерирует обогащённое описание через LLM на основе реальных примеров использования из диалога.
Описание подмешивается в объявление инструмента перед каждым вызовом LLM через get_tool_prompt.
Данные хранятся в memory/tool/tool_memory.json.
"""
import asyncio, json, logging, os
from datetime import datetime

import httpx
from openai import AsyncOpenAI

from agent import Agent
from src.memory.providers.base import BaseProvider
from src.memory.memory import Memory

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
    def __init__(self, model_name: str, api_key: str, base_url: str, consolidate_tokens: int = 3_000):
        super().__init__(consolidate_tokens=consolidate_tokens)
        self.model_name = model_name
        self._tool_stats_file: str = ""
        self._tool_stats: dict = {}

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.AsyncClient(proxy=proxy_url) if proxy_url else None
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http_client)

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
        tool_names: set[str] = set()
        pending_calls: dict[str, tuple[dict, str]] = {}  # tool_call_id -> (call_info, call_time)
        contents = []
        for turn in pending:
            if not isinstance(turn, dict): continue
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

        contents = Agent.strip_contents_private(contents)
        if tool_names:
            await asyncio.gather(*[self._summarize_tool_use(name, contents) for name in tool_names])
            log.info("[ToolProvider] consolidated %d tools: %s", len(tool_names), list(tool_names))
        self._save()

    async def _summarize_tool_use(self, tool_name: str, contents: list):
        entry = self._tool_stats.setdefault(tool_name, {"content": "", "total_calls": 0, "total_success": 0})

        instruction = SUMMARIZE_PROMPT.format(
            tool_name=tool_name,
            previous_content=entry.get("content") or "(none)",
        )
        max_retries, delay = 5, 1.0
        for attempt in range(max_retries):
            try:
                response = await self._client.chat.completions.create(
                    model=self.model_name,
                    messages=[*contents, {"role": "user", "content": instruction}],
                )
                entry["content"] = (response.choices[0].message.content or "").strip()
                log.info("[ToolProvider] summarized %s", tool_name)
                return
            except Exception as e:
                if attempt + 1 == max_retries:
                    log.warning("[ToolProvider] summarize failed for %s after %d attempts: %s", tool_name, max_retries, e, exc_info=True)
                else:
                    wait = delay * 2 ** attempt
                    log.warning("[ToolProvider] summarize attempt %d/%d for %s in %.0fs: %s", attempt + 1, max_retries, tool_name, wait, e)
                    await asyncio.sleep(wait)

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
