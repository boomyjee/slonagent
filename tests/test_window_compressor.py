"""
Тесты WindowCompressor — скользящее окно без LLM.

Запуск:
    venv\\Scripts\\python -m pytest tests/test_window_compressor.py -v
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from src.memory.compressors.window import WindowCompressor


def make_turns(n: int, role: str = "user", chars_each: int = 100) -> list:
    """Создаёт n тёрнов заданной длины."""
    return [
        {"role": role, "content": "x" * chars_each, "_user_message_id": i}
        for i in range(n)
    ]


class TestWindowCompressor:

    @pytest.mark.asyncio
    async def test_empty_returns_empty(self):
        c = WindowCompressor()
        assert await c.compress([]) == []

    @pytest.mark.asyncio
    async def test_within_limits_returns_all(self):
        # 5 тёрнов по 100 символов = ~25 токенов — значительно меньше лимита
        c = WindowCompressor(hard_limit_tokens=10_000, soft_limit_tokens=5_000)
        turns = make_turns(5)
        result = await c.compress(turns)
        assert result == turns

    @pytest.mark.asyncio
    async def test_hard_limit_trims(self):
        # Каждый тёрн ~250 токенов (1000 символов / 4), hard=400 → влезает 1
        c = WindowCompressor(hard_limit_tokens=400, soft_limit_tokens=99999, min_user_turns=0)
        turns = make_turns(5, chars_each=1000)
        result = await c.compress(turns)
        assert len(result) < len(turns)
        # Самые новые сохраняются
        assert result[-1] == turns[-1]

    @pytest.mark.asyncio
    async def test_soft_limit_with_enough_user_turns_trims(self):
        # soft=100 токенов, min_user_turns=2, 10 тёрнов по ~25 токенов
        # После набора 5 user_ids (>= min_user_turns=2) и превышения soft — обрезаем
        c = WindowCompressor(hard_limit_tokens=99999, soft_limit_tokens=50, min_user_turns=2)
        turns = make_turns(10, chars_each=100)
        result = await c.compress(turns)
        assert len(result) < len(turns)

    @pytest.mark.asyncio
    async def test_soft_limit_without_enough_user_turns_keeps_all(self):
        # soft превышен, но min_user_turns не набрано — всё сохраняем
        c = WindowCompressor(hard_limit_tokens=99999, soft_limit_tokens=1, min_user_turns=100)
        turns = make_turns(3)
        result = await c.compress(turns)
        assert result == turns

    @pytest.mark.asyncio
    async def test_preserves_order(self):
        c = WindowCompressor(hard_limit_tokens=99999, soft_limit_tokens=99999)
        turns = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ]
        result = await c.compress(turns)
        assert [t["content"] for t in result] == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_newest_kept_when_trimming(self):
        # При обрезке сохраняются последние тёрны
        c = WindowCompressor(hard_limit_tokens=200, soft_limit_tokens=99999, min_user_turns=0)
        turns = make_turns(10, chars_each=200)  # каждый ~50 токенов
        result = await c.compress(turns)
        if len(result) < len(turns):
            assert result[-1] == turns[-1]
            assert result[0] == turns[len(turns) - len(result)]
