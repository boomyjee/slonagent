"""
Тесты SummaryProvider и ToolProvider с мок-LLM.

Запуск:
    venv\\Scripts\\python -m pytest tests/test_providers.py -v
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Skill


class PassthroughCompressor(Skill):
    async def compress(self, turns): return turns


def make_agent(tmp_path):
    from agent import Agent
    return Agent(
        model_name="test-model",
        api_key="test-key",
        base_url="http://test",
        agent_dir=str(tmp_path),
        memory_compressor=PassthroughCompressor(),
    )


def make_llm_response(tool_name: str, args: dict):
    """Создаёт mock OpenAI response с одним tool_call."""
    tc = MagicMock()
    tc.function = MagicMock()
    tc.function.name = tool_name
    tc.function.arguments = json.dumps(args, ensure_ascii=False)

    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = None

    choice = MagicMock()
    choice.message = msg

    resp = MagicMock()
    resp.choices = [choice]
    return resp


def make_text_response(text: str):
    msg = MagicMock()
    msg.tool_calls = []
    msg.content = text

    choice = MagicMock()
    choice.message = msg

    resp = MagicMock()
    resp.choices = [choice]
    return resp


PENDING = [
    {"role": "user", "content": "Меня зовут Иван"},
    {"role": "assistant", "content": "Привет, Иван!"},
]


# ═══════════════════════════════════════════════════════════════════════════════
# SummaryProvider
# ═══════════════════════════════════════════════════════════════════════════════

class TestSummaryProvider:

    async def _make_provider(self, tmp_path):
        from src.memory.providers.summary import SummaryProvider
        agent = make_agent(tmp_path)
        p = SummaryProvider(model_name="test", api_key="test", base_url="http://test")
        p.register(agent)
        await p.start()
        return p

    @pytest.mark.asyncio
    async def test_consolidate_writes_memory_and_history(self, tmp_path):
        p = await self._make_provider(tmp_path)

        llm_args = {
            "history_entry": "[2024-01-01 12:00] Иван представился.",
            "memory_update": "Пользователь: Иван",
        }
        p._client.chat.completions.create = AsyncMock(
            return_value=make_llm_response("save_memory", llm_args)
        )

        await p._consolidate(PENDING)

        assert os.path.exists(p.memory_file)
        assert os.path.exists(p.history_file)
        with open(p.memory_file, encoding="utf-8") as f:
            assert "Иван" in f.read()
        with open(p.history_file, encoding="utf-8") as f:
            assert "Иван представился" in f.read()

    @pytest.mark.asyncio
    async def test_consolidate_no_tool_call_does_not_crash(self, tmp_path):
        p = await self._make_provider(tmp_path)
        p._client.chat.completions.create = AsyncMock(
            return_value=make_text_response("OK")
        )
        await p._consolidate(PENDING)
        # Файлы не созданы, но и исключения нет
        assert not os.path.exists(p.memory_file)

    @pytest.mark.asyncio
    async def test_get_context_prompt_empty(self, tmp_path):
        p = await self._make_provider(tmp_path)
        prompt = await p.get_context_prompt()
        assert "пусто" in prompt.lower()

    @pytest.mark.asyncio
    async def test_get_context_prompt_with_memory(self, tmp_path):
        p = await self._make_provider(tmp_path)
        with open(p.memory_file, "w", encoding="utf-8") as f:
            f.write("Пользователь: Иван")
        prompt = await p.get_context_prompt()
        assert "Иван" in prompt

    @pytest.mark.asyncio
    async def test_read_history_tool_empty(self, tmp_path):
        p = await self._make_provider(tmp_path)
        result = p.read_history("anything")
        assert "пуст" in result["result"].lower()

    @pytest.mark.asyncio
    async def test_read_history_tool_finds_match(self, tmp_path):
        p = await self._make_provider(tmp_path)
        with open(p.history_file, "w", encoding="utf-8") as f:
            f.write("2024-01-01 Иван представился\n")
            f.write("2024-01-02 Обсуждали Python\n")
        result = p.read_history("Иван")
        assert "Иван" in result["result"]

    @pytest.mark.asyncio
    async def test_read_history_tool_no_match(self, tmp_path):
        p = await self._make_provider(tmp_path)
        with open(p.history_file, "w", encoding="utf-8") as f:
            f.write("2024-01-01 Иван представился\n")
        result = p.read_history("Python")
        assert "не найдено" in result["result"].lower()


# ═══════════════════════════════════════════════════════════════════════════════
# ToolProvider
# ═══════════════════════════════════════════════════════════════════════════════

TOOL_PENDING = [
    {"role": "user", "content": "посчитай", "_timestamp": "2024-01-01T12:00:00"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "calculator", "arguments": '{"expr":"2+2"}'}}
        ],
        "_timestamp": "2024-01-01T12:00:01",
    },
    {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"result": 4}',
        "_timestamp": "2024-01-01T12:00:02",
    },
    {"role": "assistant", "content": "Ответ: 4", "_timestamp": "2024-01-01T12:00:03"},
]


class TestToolProvider:

    async def _make_provider(self, tmp_path):
        from src.memory.providers.tool import ToolProvider
        agent = make_agent(tmp_path)
        p = ToolProvider(model_name="test", api_key="test", base_url="http://test")
        p.register(agent)
        await p.start()
        return p

    @pytest.mark.asyncio
    async def test_consolidate_tracks_stats(self, tmp_path):
        p = await self._make_provider(tmp_path)
        p._client.chat.completions.create = AsyncMock(
            return_value=make_text_response("Хороший инструмент.")
        )
        await p._consolidate(TOOL_PENDING)

        assert "calculator" in p._tool_stats
        stats = p._tool_stats["calculator"]
        assert stats["total_calls"] == 1
        assert stats["total_success"] == 1

    @pytest.mark.asyncio
    async def test_consolidate_saves_to_disk(self, tmp_path):
        p = await self._make_provider(tmp_path)
        p._client.chat.completions.create = AsyncMock(
            return_value=make_text_response("Описание инструмента.")
        )
        await p._consolidate(TOOL_PENDING)
        assert os.path.exists(p._tool_stats_file)

    @pytest.mark.asyncio
    async def test_get_tool_prompt_empty_for_unknown(self, tmp_path):
        p = await self._make_provider(tmp_path)
        prompt = await p.get_tool_prompt("unknown_tool")
        assert prompt == ""

    @pytest.mark.asyncio
    async def test_get_tool_prompt_after_consolidation(self, tmp_path):
        p = await self._make_provider(tmp_path)
        p._client.chat.completions.create = AsyncMock(
            return_value=make_text_response("Используй для вычислений.")
        )
        await p._consolidate(TOOL_PENDING)
        prompt = await p.get_tool_prompt("calculator")
        assert "Используй" in prompt

    @pytest.mark.asyncio
    async def test_consolidate_error_not_in_response_is_success(self, tmp_path):
        p = await self._make_provider(tmp_path)
        p._client.chat.completions.create = AsyncMock(
            return_value=make_text_response("ok")
        )
        await p._consolidate(TOOL_PENDING)
        assert p._tool_stats["calculator"]["total_success"] == 1

    @pytest.mark.asyncio
    async def test_consolidate_error_in_response_is_failure(self, tmp_path):
        p = await self._make_provider(tmp_path)
        p._client.chat.completions.create = AsyncMock(
            return_value=make_text_response("ok")
        )
        error_pending = [
            {"role": "user", "content": "сломай"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "breaker", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c2", "content": '{"error": "не работает"}'},
            {"role": "assistant", "content": "Ой"},
        ]
        await p._consolidate(error_pending)
        assert p._tool_stats["breaker"]["total_success"] == 0
        assert p._tool_stats["breaker"]["total_calls"] == 1
