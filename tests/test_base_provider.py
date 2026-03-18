"""
Тесты BaseProvider — накопление pending и порог консолидации.

Запуск:
    venv\\Scripts\\python -m pytest tests/test_base_provider.py -v
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Skill
from src.memory.memory import Memory
from src.memory.providers.base import BaseProvider


class PassthroughCompressor(Skill):
    async def compress(self, turns): return turns


def make_agent(tmp_path):
    from agent import Agent
    return Agent(
        model_name="test",
        api_key="test",
        base_url="http://test",
        agent_dir=str(tmp_path),
        memory_compressor=PassthroughCompressor(),
    )


class TrackingProvider(BaseProvider):
    """BaseProvider, запоминающий, сколько раз вызвалась консолидация."""

    def __init__(self, consolidate_tokens: int = 100):
        super().__init__(consolidate_tokens=consolidate_tokens)
        self.consolidated_batches: list[list] = []

    async def _consolidate(self, pending):
        self.consolidated_batches.append(list(pending))


class TestBaseProvider:

    async def _setup(self, tmp_path, consolidate_tokens=100):
        agent = make_agent(tmp_path)
        provider = TrackingProvider(consolidate_tokens=consolidate_tokens)
        provider.register(agent)
        await provider.start()
        return provider

    @pytest.mark.asyncio
    async def test_accumulates_turns(self, tmp_path):
        p = await self._setup(tmp_path, consolidate_tokens=100_000)
        await p.add_turn({"role": "user", "content": "hi"})
        await p.add_turn({"role": "assistant", "content": "hello"})
        assert len(p._pending) == 2

    @pytest.mark.asyncio
    async def test_consolidate_triggers_on_assistant_above_threshold(self, tmp_path):
        # consolidate_tokens=1 — сразу срабатывает при первом assistant-тёрне
        p = await self._setup(tmp_path, consolidate_tokens=1)
        await p.add_turn({"role": "user", "content": "hi"})
        await p.add_turn({"role": "assistant", "content": "hello"})
        assert len(p.consolidated_batches) == 1
        assert p._pending == []

    @pytest.mark.asyncio
    async def test_consolidate_not_triggered_on_user_turn(self, tmp_path):
        p = await self._setup(tmp_path, consolidate_tokens=1)
        await p.add_turn({"role": "user", "content": "hi"})
        # Только user — консолидации нет
        assert len(p.consolidated_batches) == 0

    @pytest.mark.asyncio
    async def test_consolidate_not_triggered_below_threshold(self, tmp_path):
        # Высокий порог — assistant не запускает консолидацию
        p = await self._setup(tmp_path, consolidate_tokens=100_000)
        await p.add_turn({"role": "user", "content": "hi"})
        await p.add_turn({"role": "assistant", "content": "hello"})
        assert len(p.consolidated_batches) == 0

    @pytest.mark.asyncio
    async def test_pending_cleared_after_consolidation(self, tmp_path):
        p = await self._setup(tmp_path, consolidate_tokens=1)
        await p.add_turn({"role": "user", "content": "x" * 100})
        await p.add_turn({"role": "assistant", "content": "x" * 100})
        assert p._pending == []

    @pytest.mark.asyncio
    async def test_pending_persisted_to_file(self, tmp_path):
        p = await self._setup(tmp_path, consolidate_tokens=100_000)
        await p.add_turn({"role": "user", "content": "stored"})
        await p.add_turn({"role": "assistant", "content": "also stored"})
        assert os.path.exists(p._pending_file)

    @pytest.mark.asyncio
    async def test_consolidate_tokens_zero_disables_provider(self, tmp_path):
        p = await self._setup(tmp_path, consolidate_tokens=0)
        await p.add_turn({"role": "user", "content": "hi"})
        await p.add_turn({"role": "assistant", "content": "hello"})
        assert len(p.consolidated_batches) == 0
        assert len(p._pending) == 0  # consolidate_tokens=0 → add_turn возвращается сразу

    @pytest.mark.asyncio
    async def test_consolidate_batch_contains_all_turns(self, tmp_path):
        p = await self._setup(tmp_path, consolidate_tokens=1)
        turns = [
            {"role": "user", "content": "x" * 100},
            {"role": "assistant", "content": "x" * 100},
        ]
        for t in turns:
            await p.add_turn(t)
        assert p.consolidated_batches[0] == turns
