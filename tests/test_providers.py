"""
Тесты ToolProvider с мок-LLM.

Запуск:
    venv\\Scripts\\python -m pytest tests/test_providers.py -v
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Skill
from src.memory.providers.base import BaseProvider


class PassthroughCompressor(BaseProvider):
    def __init__(self): super().__init__(consolidate_tokens=0)
    async def compress(self, turns): return turns


def make_agent(tmp_path):
    from agent import Agent
    return Agent(
        id="test",
        model_name="test-model",
        api_key="test-key",
        base_url="http://test",
        agent_dir=str(tmp_path),
        memory_compressor=PassthroughCompressor(),
    )


def make_text_turn(text: str) -> dict:
    return {"role": "assistant", "content": text}


def mock_sub_agent(provider, response_text: str = "ok"):
    """Подменяет factory эфемерных Agent'ов фейком с замоканным llm()/memory."""
    fake = MagicMock()
    fake.memory.add_turn = AsyncMock()
    fake.llm = AsyncMock(return_value=make_text_turn(response_text))
    fake.close = AsyncMock()
    provider._make_sub_agent = lambda: fake
    return fake


PENDING = [
    {"role": "user", "content": "Меня зовут Иван"},
    {"role": "assistant", "content": "Привет, Иван!"},
]



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
        mock_sub_agent(p, "Хороший инструмент.")
        await p._consolidate(TOOL_PENDING)

        assert "calculator" in p._tool_stats
        stats = p._tool_stats["calculator"]
        assert stats["total_calls"] == 1
        assert stats["total_success"] == 1

    @pytest.mark.asyncio
    async def test_consolidate_saves_to_disk(self, tmp_path):
        p = await self._make_provider(tmp_path)
        mock_sub_agent(p, "Описание инструмента.")
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
        mock_sub_agent(p, "Используй для вычислений.")
        await p._consolidate(TOOL_PENDING)
        prompt = await p.get_tool_prompt("calculator")
        assert "Используй" in prompt

    @pytest.mark.asyncio
    async def test_consolidate_error_not_in_response_is_success(self, tmp_path):
        p = await self._make_provider(tmp_path)
        mock_sub_agent(p, "ok")
        await p._consolidate(TOOL_PENDING)
        assert p._tool_stats["calculator"]["total_success"] == 1

    @pytest.mark.asyncio
    async def test_consolidate_error_in_response_is_failure(self, tmp_path):
        p = await self._make_provider(tmp_path)
        mock_sub_agent(p, "ok")
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

    @pytest.mark.asyncio
    async def test_consolidate_groups_by_thread(self, tmp_path):
        """Pending мешает 2 треда — саммари запускается по разу на каждый тред где использовался тул."""
        p = await self._make_provider(tmp_path)
        fake = mock_sub_agent(p, "ok")
        # тред A использует tool_a, тред B использует tool_b
        mixed_pending = [
            {"role": "user", "content": "a", "_thread_id": "thr_a"},
            {"role": "assistant", "content": None, "_thread_id": "thr_a",
             "tool_calls": [{"id": "ca", "type": "function", "function": {"name": "tool_a", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "ca", "content": '{"ok": 1}', "_thread_id": "thr_a"},
            {"role": "assistant", "content": "done", "_thread_id": "thr_a"},
            {"role": "user", "content": "b", "_thread_id": "thr_b"},
            {"role": "assistant", "content": None, "_thread_id": "thr_b",
             "tool_calls": [{"id": "cb", "type": "function", "function": {"name": "tool_b", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "cb", "content": '{"ok": 1}', "_thread_id": "thr_b"},
            {"role": "assistant", "content": "done", "_thread_id": "thr_b"},
        ]
        await p._consolidate(mixed_pending)
        # Стата для обоих тулов
        assert p._tool_stats["tool_a"]["total_calls"] == 1
        assert p._tool_stats["tool_b"]["total_calls"] == 1
        # Саммари — по одному на тред
        assert fake.llm.call_count == 2

    @pytest.mark.asyncio
    async def test_consolidate_summary_contents_per_thread(self, tmp_path):
        """Контент саммари для каждого треда содержит реплики только этого треда."""
        p = await self._make_provider(tmp_path)
        captured = []
        async def capture_turns(*turns):
            captured.append(list(turns))
        fake = MagicMock()
        fake.memory.add_turn = AsyncMock(side_effect=capture_turns)
        fake.llm = AsyncMock(return_value=make_text_turn("ok"))
        fake.close = AsyncMock()
        p._make_sub_agent = lambda: fake
        mixed = [
            {"role": "user", "content": "марсиане", "_thread_id": "thr_a"},
            {"role": "assistant", "content": None, "_thread_id": "thr_a",
             "tool_calls": [{"id": "x", "type": "function", "function": {"name": "shared", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "x", "content": '{"ok": 1}', "_thread_id": "thr_a"},
            {"role": "assistant", "content": "done", "_thread_id": "thr_a"},
            {"role": "user", "content": "лошадки", "_thread_id": "thr_b"},
            {"role": "assistant", "content": None, "_thread_id": "thr_b",
             "tool_calls": [{"id": "y", "type": "function", "function": {"name": "shared", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "y", "content": '{"ok": 1}', "_thread_id": "thr_b"},
            {"role": "assistant", "content": "done", "_thread_id": "thr_b"},
        ]
        await p._consolidate(mixed)
        # Каждый add_turn содержит реплики ровно одного треда
        assert len(captured) == 2
        joined = ["\n".join(t.get("content", "") for t in turns if isinstance(t.get("content"), str)) for turns in captured]
        assert any("марсиане" in j and "лошадки" not in j for j in joined)
        assert any("лошадки" in j and "марсиане" not in j for j in joined)


# ═══════════════════════════════════════════════════════════════════════════════
# FactProvider — per-thread retain items
# ═══════════════════════════════════════════════════════════════════════════════

class TestFactProviderThreadGrouping:

    def _make_provider(self):
        from src.memory.providers.fact import FactProvider
        return FactProvider(model_name="test", api_key="test", base_url="http://test")

    def test_build_retain_items_single_thread(self):
        p = self._make_provider()
        thread_pending = [
            {"role": "user", "content": "марсиане атакуют",
             "_thread_id": "thr_a", "_timestamp": "2025-01-01T10:00:00"},
            {"role": "assistant", "content": "приготовь зонтик",
             "_thread_id": "thr_a", "_timestamp": "2025-01-01T10:00:01"},
        ]
        items = p._build_retain_items(thread_pending)
        # Один conversation-item на тред
        conv = [i for i in items if i.context == "conversation"]
        assert len(conv) == 1
        assert "марсиане" in conv[0].content
        assert "приготовь" in conv[0].content

    def test_consolidate_groups_by_thread(self, tmp_path, monkeypatch):
        """_consolidate с микшированным pending передаёт в retain N conversation-items (по одному на тред)."""
        captured = []
        import src.memory.providers.fact as fact_module
        monkeypatch.setattr(fact_module, "retain",
                            lambda items, *a, **kw: captured.extend(items))

        p = self._make_provider()
        # storage обходится — retain замокан, OpenAI клиент не дёргается
        mixed = [
            {"role": "user", "content": "марсиане",
             "_thread_id": "thr_a", "_timestamp": "2025-01-01T10:00:00"},
            {"role": "assistant", "content": "ответ A",
             "_thread_id": "thr_a", "_timestamp": "2025-01-01T10:00:01"},
            {"role": "user", "content": "лошадки",
             "_thread_id": "thr_b", "_timestamp": "2025-01-01T11:00:00"},
            {"role": "assistant", "content": "ответ B",
             "_thread_id": "thr_b", "_timestamp": "2025-01-01T11:00:01"},
        ]
        import asyncio
        asyncio.run(p._consolidate(mixed))

        conv_items = [i for i in captured if i.context == "conversation"]
        assert len(conv_items) == 2
        joined = [i.content for i in conv_items]
        assert any("марсиане" in c and "лошадки" not in c for c in joined)
        assert any("лошадки" in c and "марсиане" not in c for c in joined)
