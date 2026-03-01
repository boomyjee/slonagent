"""ToolProvider — память об использовании инструментов.

Собирает статистику по каждому инструменту (total_calls, success rate, avg_tokens, avg_time)
и генерирует обогащённое описание через LLM на основе реальных примеров использования из диалога.
Описание подмешивается в объявление инструмента перед каждым вызовом LLM через get_tool_prompt.
Данные хранятся в memory/tool/tool_memory.json.
"""
import asyncio, json, logging, os
from datetime import datetime

import httpx
from google import genai

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
    def __init__(self, model_name: str, api_key: str, consolidate_tokens: int = 3_000):
        super().__init__(consolidate_tokens=consolidate_tokens)
        self.model_name = model_name

        data_dir = os.path.join(Memory.memory_dir, "tool")
        os.makedirs(data_dir, exist_ok=True)
        self._tool_stats_file = os.path.join(data_dir, "tool_memory.json")
        self._tool_stats = self._load()

        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
        http_options = {"httpx_client": http_client, "api_version": "v1alpha"} if http_client else {"api_version": "v1alpha"}
        self._client = genai.Client(api_key=api_key, http_options=http_options)

    def _load(self) -> dict:
        try:
            with open(self._tool_stats_file, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            log.warning("[ToolProvider] load failed: %s", e)
            return {}

    def _save(self):
        try:
            tmp = self._tool_stats_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._tool_stats, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._tool_stats_file)
        except Exception as e:
            log.warning("[ToolProvider] save failed: %s", e)

    async def _consolidate(self, pending: list):
        tool_names: set[str] = set()
        call = None
        call_time = None
        for turn in pending:
            if not isinstance(turn, dict): continue
            for part in turn.get("parts", []):
                if isinstance(part, dict):
                    fc = part.get("functionCall")
                    fr = part.get("functionResponse")
                else:
                    raw_fc = getattr(part, "function_call", None)
                    raw_fr = getattr(part, "function_response", None)
                    fc = {"name": raw_fc.name, "args": dict(raw_fc.args or {})} if raw_fc else None
                    fr = {"name": raw_fr.name, "response": dict(raw_fr.response or {})} if raw_fr else None

                if fc:
                    call = fc
                    call_time = turn.get("_timestamp")
                elif res := fr:
                    if call and call["name"] == res["name"]:
                        success = "error" not in res.get("response", {})
                        entry = self._tool_stats.setdefault(call["name"], {"content": "", "total_calls": 0, "total_success": 0, "avg_tokens": 0.0, "avg_time": 0.0})
                        entry["total_calls"] += 1
                        entry["total_success"] += int(success)
                        n = entry["total_calls"]

                        token_cost = (len(json.dumps(call.get("args", {}))) + len(json.dumps(res.get("response", {})))) // 4
                        entry["avg_tokens"] += (token_cost - entry["avg_tokens"]) / n

                        try:
                            delta_time = (datetime.fromisoformat(turn["_timestamp"]) - datetime.fromisoformat(call_time)).total_seconds()
                            entry["avg_time"] += (delta_time - entry["avg_time"]) / n
                        except Exception:
                            pass

                        tool_names.add(call["name"])
                    call = None

        contents = Agent.strip_contents_private(pending)
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
        try:
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self.model_name,
                contents=[*contents, {"role": "user", "parts": [{"text": instruction}]}],
            )
            entry["content"] = (response.text or "").strip()
            log.info("[ToolProvider] summarized %s", tool_name)
        except Exception as e:
            log.warning("[ToolProvider] summarize failed for %s: %s", tool_name, e)

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
