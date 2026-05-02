"""
Тесты LogCompressor.

Запуск:
    venv\\Scripts\\python -m pytest tests/test_log_compressor.py -v
"""
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from src.memory.compressors.log import (
    _add_relative_time,
    _format_gap,
    _format_relative_time,
    _optimize_for_context,
    _parse_observations,
    _parse_xml_tag,
    LogCompressor,
)
from agent import Skill
from src.memory.providers.base import BaseProvider


class PassthroughCompressor(BaseProvider):
    def __init__(self): super().__init__(consolidate_tokens=0)
    async def compress(self, turns): return turns


def make_agent(tmp_path):
    from agent import Agent
    return Agent(
        id="test",
        model_name="test",
        api_key="test",
        base_url="http://test",
        agent_dir=str(tmp_path),
        memory_compressor=PassthroughCompressor(),
    )


def make_turns(n: int, chars: int = 1000) -> list:
    turns = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        turns.append({"role": role, "content": [{"type": "text", "text": "x" * chars}] if role == "user" else "x" * chars})
    return turns


# ═══════════════════════════════════════════════════════════════════════════════
# Чистые функции
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseXmlTag:

    def test_extracts_content(self):
        text = "<observations>hello world</observations>"
        assert _parse_xml_tag(text, "observations") == "hello world"

    def test_multiline(self):
        text = "<observations>\nline1\nline2\n</observations>"
        assert _parse_xml_tag(text, "observations") == "line1\nline2"

    def test_missing_tag_returns_empty(self):
        assert _parse_xml_tag("no tags here", "observations") == ""

    def test_case_insensitive(self):
        assert _parse_xml_tag("<OBSERVATIONS>data</OBSERVATIONS>", "observations") == "data"


class TestParseObservations:

    def test_extracts_xml_observations(self):
        text = "<observations>\n* item1\n* item2\n</observations>"
        result = _parse_observations(text)
        assert "item1" in result

    def test_fallback_to_bullet_list(self):
        text = "some text\n- item1\n- item2\n"
        result = _parse_observations(text)
        assert "item1" in result

    def test_fallback_to_raw_when_no_bullets(self):
        text = "plain text no bullets"
        result = _parse_observations(text)
        assert result == "plain text no bullets"


class TestFormatRelativeTime:

    def test_today(self):
        now = datetime(2025, 6, 15)
        assert _format_relative_time(datetime(2025, 6, 15), now) == "today"

    def test_yesterday(self):
        now = datetime(2025, 6, 15)
        assert _format_relative_time(datetime(2025, 6, 14), now) == "yesterday"

    def test_days_ago(self):
        now = datetime(2025, 6, 15)
        assert "days ago" in _format_relative_time(datetime(2025, 6, 10), now)

    def test_weeks_ago(self):
        now = datetime(2025, 6, 15)
        assert "week" in _format_relative_time(datetime(2025, 6, 1), now)

    def test_months_ago(self):
        now = datetime(2025, 6, 15)
        assert "month" in _format_relative_time(datetime(2025, 4, 1), now)

    def test_future(self):
        now = datetime(2025, 6, 15)
        result = _format_relative_time(datetime(2025, 6, 17), now)
        assert "in" in result


class TestFormatGap:

    def test_same_day_no_gap(self):
        assert _format_gap(datetime(2025, 6, 15), datetime(2025, 6, 15)) is None

    def test_next_day_no_gap(self):
        assert _format_gap(datetime(2025, 6, 15), datetime(2025, 6, 16)) is None

    def test_few_days_gap(self):
        result = _format_gap(datetime(2025, 6, 10), datetime(2025, 6, 15))
        assert result is not None and "days" in result

    def test_week_gap(self):
        result = _format_gap(datetime(2025, 6, 1), datetime(2025, 6, 9))
        assert result is not None and "week" in result


class TestOptimizeForContext:

    def test_removes_yellow_emoji(self):
        assert "🟡" not in _optimize_for_context("🟡 item")

    def test_removes_green_emoji(self):
        assert "🟢" not in _optimize_for_context("🟢 item")

    def test_keeps_red_emoji(self):
        assert "🔴" in _optimize_for_context("🔴 item")

    def test_replaces_arrow(self):
        result = _optimize_for_context("a -> b")
        assert "->" not in result

    def test_collapses_extra_newlines(self):
        result = _optimize_for_context("a\n\n\n\nb")
        assert "\n\n\n" not in result


class TestAddRelativeTime:

    def test_adds_relative_to_date_header(self):
        obs = "Date: Jun 1, 2025\n* item"
        now = datetime(2025, 6, 15)
        result = _add_relative_time(obs, now)
        assert "(" in result  # относительное время добавлено

    def test_no_dates_passthrough(self):
        obs = "* just items no dates"
        now = datetime(2025, 6, 15)
        assert _add_relative_time(obs, now) == obs

    def test_gap_inserted_between_dates(self):
        obs = "Date: Jun 1, 2025\n* item1\n\nDate: Jun 10, 2025\n* item2"
        now = datetime(2025, 6, 15)
        result = _add_relative_time(obs, now)
        assert "later" in result or "days" in result


# ═══════════════════════════════════════════════════════════════════════════════
# LogCompressor.compress — keep recent + unobserved
# ═══════════════════════════════════════════════════════════════════════════════

class TestLogCompressorCompress:

    def _make_compressor(self, tmp_path, **kwargs):
        agent = make_agent(tmp_path)
        c = LogCompressor(
            model_name="test", api_key="test", base_url="http://test",
            recent_tokens=kwargs.get("recent_tokens", 100),
            min_recent_turns=kwargs.get("min_recent_turns", 1),
            max_recent_turns_tokens=kwargs.get("max_recent_turns_tokens", 50_000),
            compress_after_tokens=kwargs.get("compress_after_tokens", 30_000),
            reflect_after_tokens=kwargs.get("reflect_after_tokens", 999_999),
        )
        c.register(agent)
        return c

    @pytest.mark.asyncio
    async def test_keeps_all_turns_when_all_unobserved(self, tmp_path):
        c = self._make_compressor(tmp_path)
        turns = make_turns(6, chars=500)  # все unobserved
        result = await c.compress(turns)
        assert result == turns

    @pytest.mark.asyncio
    async def test_trims_observed_to_recent_budget(self, tmp_path):
        c = self._make_compressor(tmp_path, recent_tokens=50, min_recent_turns=1)
        turns = make_turns(10, chars=500)
        for t in turns:
            t["_observed"] = True
        result = await c.compress(turns)
        assert 1 <= len(result) < len(turns)
        assert result == turns[-len(result):]

    @pytest.mark.asyncio
    async def test_min_recent_turns_floor(self, tmp_path):
        c = self._make_compressor(tmp_path, recent_tokens=1, min_recent_turns=3)
        turns = make_turns(6, chars=200)
        for t in turns:
            t["_observed"] = True
        result = await c.compress(turns)
        assert len(result) >= 3

    @pytest.mark.asyncio
    async def test_unobserved_kept_beyond_recent_budget(self, tmp_path):
        c = self._make_compressor(tmp_path, recent_tokens=50, min_recent_turns=0)
        turns = make_turns(10, chars=500)
        # старые observed, новые unobserved
        for t in turns[:6]:
            t["_observed"] = True
        result = await c.compress(turns)
        # 4 unobserved обязательно в keep, observed — по бюджету
        assert all(t in result for t in turns[6:])

    @pytest.mark.asyncio
    async def test_observation_message_filtered(self, tmp_path):
        c = self._make_compressor(tmp_path)
        om = {"role": "user", "content": "obs", "_observation_message": True}
        turns = [om] + make_turns(4, chars=100)
        result = await c.compress(turns)
        assert om not in result


# ═══════════════════════════════════════════════════════════════════════════════
# _optimize_for_context — items collapsed preservation
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptimizeForContextCollapsed:

    def test_preserves_items_collapsed_marker(self):
        text = "* [72 items collapsed - ID: b1fa] some summary"
        result = _optimize_for_context(text)
        assert "72 items collapsed" in result

    def test_removes_regular_semantic_tags(self):
        result = _optimize_for_context("* [label, context] some observation")
        assert "[label, context]" not in result

    def test_keeps_checkmark_emoji(self):
        result = _optimize_for_context("* ✅ Task done")
        assert "✅" in result


# ═══════════════════════════════════════════════════════════════════════════════
# LogCompressor._consolidate — observer + reflector через mock LLM
# ═══════════════════════════════════════════════════════════════════════════════

class TestLogCompressorConsolidate:

    def _make_compressor(self, tmp_path, reflect_after=999999):
        agent = make_agent(tmp_path)
        c = LogCompressor(
            model_name="test", api_key="test", base_url="http://test",
            recent_tokens=100,
            min_recent_turns=1,
            compress_after_tokens=1,
            reflect_after_tokens=reflect_after,
        )
        c.register(agent)
        return c

    def _mock_llm(self, c, response_text: str):
        fake = MagicMock()
        fake.memory.clear = MagicMock()
        fake.memory.add_turn = AsyncMock()
        fake.llm = AsyncMock(return_value={"role": "assistant", "content": response_text})
        c._llm_agent = fake
        return fake

    @pytest.mark.asyncio
    async def test_observer_empty_does_nothing(self, tmp_path):
        c = self._make_compressor(tmp_path)
        self._mock_llm(c, "")
        turns = make_turns(4, chars=200)
        await c._consolidate(turns)
        # LOG.md не появился
        assert not os.path.exists(c._log_path())
        # _observed не проставлен (observer вернул пусто)
        assert not any(t.get("_observed") for t in turns)

    @pytest.mark.asyncio
    async def test_observer_writes_log_and_marks_observed(self, tmp_path):
        c = self._make_compressor(tmp_path)
        self._mock_llm(c, "<observations>\n* 🔴 hello\n</observations>")
        turns = make_turns(4, chars=200)
        await c._consolidate(turns)
        assert os.path.exists(c._log_path())
        assert all(t.get("_observed") for t in turns)
        log_content = c._read_log()
        assert "hello" in log_content

    @pytest.mark.asyncio
    async def test_thread_grouping_wraps_each(self, tmp_path):
        c = self._make_compressor(tmp_path)
        self._mock_llm(c, "<observations>\n* 🔴 obs\n</observations>")
        turns = [
            {"role": "user", "content": "a", "_thread_id": "t1"},
            {"role": "assistant", "content": "b", "_thread_id": "t1"},
            {"role": "user", "content": "c", "_thread_id": "t2"},
            {"role": "assistant", "content": "d", "_thread_id": "t2"},
        ]
        await c._consolidate(turns)
        log_content = c._read_log()
        assert '<thread id="t1">' in log_content
        assert '<thread id="t2">' in log_content

    @pytest.mark.asyncio
    async def test_reflector_triggered_when_obs_exceed_threshold(self, tmp_path):
        c = self._make_compressor(tmp_path, reflect_after=50)
        big_obs = "<observations>\n* 🔴 (10:00) " + "x" * 400 + "\n</observations>"
        small_reflected = "<observations>\n* 🔴 (10:00) condensed\n</observations>"

        call_count = 0
        async def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"role": "assistant", "content": big_obs if call_count == 1 else small_reflected}

        fake = self._mock_llm(c, "")
        fake.llm = AsyncMock(side_effect=side_effect)
        await c._consolidate(make_turns(4, chars=200))

        assert call_count == 2
        assert "condensed" in c._read_log()

    @pytest.mark.asyncio
    async def test_reflector_escalates_compression_level(self, tmp_path):
        c = self._make_compressor(tmp_path, reflect_after=1)
        big_obs = "* 🔴 (10:00) " + "x" * 400
        obs_response = f"<observations>\n{big_obs}\n</observations>"

        call_count = 0
        async def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"role": "assistant", "content": obs_response}

        fake = self._mock_llm(c, "")
        fake.llm = AsyncMock(side_effect=side_effect)
        await c._consolidate(make_turns(4, chars=200))

        # 1 observer + 5 reflector (уровни 0→1→2→3→4)
        assert call_count == 6


# ═══════════════════════════════════════════════════════════════════════════════
# Integration — реальный LLM (OpenAI-совместимый и Claude)
# ═══════════════════════════════════════════════════════════════════════════════

# Реалистичный диалог с конкретными фактами — observer должен их извлечь.
_REAL_DIALOG = [
    {"role": "user", "content": "Привет! Меня зовут Иван, я работаю backend-разработчиком в Авито."},
    {"role": "assistant", "content": "Привет, Иван! Чем могу помочь по backend-разработке?"},
    {"role": "user", "content": "Я пишу на Go уже 5 лет, сейчас разбираюсь с Kafka."},
    {"role": "assistant", "content": "Отличный стек. С Kafka бывает много нюансов — что именно изучаешь?"},
    {"role": "user", "content": "Хочу понять как настроить exactly-once семантику для критичных платежей."},
    {"role": "assistant", "content": "Для exactly-once в Kafka нужен идемпотентный producer + transactional API. Ключевые настройки: enable.idempotence=true, transactional.id, isolation.level=read_committed на consumer."},
    {"role": "user", "content": "Понял, спасибо. А ещё у меня есть pet-project на Rust — пишу свой движок для key-value хранилища."},
    {"role": "assistant", "content": "Круто! Rust для системного программирования — отличный выбор. Какие алгоритмы используешь для индексации?"},
    {"role": "user", "content": "Пока думаю между LSM-tree и B+tree. Тяну в сторону LSM из-за write-heavy нагрузки."},
    {"role": "assistant", "content": "LSM хорошо подходит для write-heavy. Посмотри на RocksDB — там много идей. Но bloom filters обязательно добавь, иначе чтение будет страдать."},
]


@pytest.mark.integration
class TestLogCompressorIntegrationOpenAI:
    """Прогоняем реальную консолидацию через OpenAI-совместимый бекенд (Gemini Flash)."""

    @pytest.mark.asyncio
    async def test_consolidate_real_dialog(self, tmp_path):
        api_key = os.environ.get("LLM_KEY")
        if not api_key:
            pytest.skip("LLM_KEY не задан")
        base_url = os.environ.get("LLM_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
        model = os.environ.get("LLM_MODEL", "gemini-3-flash-preview")

        c = LogCompressor(
            model_name=model,
            api_key=api_key,
            base_url=base_url,
            backend="openai",
            recent_tokens=50,
            min_recent_turns=2,
            compress_after_tokens=1,
            reflect_after_tokens=999_999,
        )
        c.register(make_agent(tmp_path))

        await c._consolidate(list(_REAL_DIALOG))

        log = c._read_log()
        assert log, "LOG.md пустой"

        keywords = ["иван", "ivan", "go", "kafka", "rust", "lsm", "авито", "avito"]
        assert any(k in log.lower() for k in keywords), \
            f"LOG.md не содержит ни одного ключевого слова из {keywords}: {log[:300]}"


@pytest.mark.integration
class TestLogCompressorIntegrationClaude:
    """Прогоняем реальную консолидацию через Claude-бекенд (sonnet) в bare-режиме."""

    @pytest.mark.asyncio
    async def test_consolidate_real_dialog_via_claude(self, tmp_path):
        model = os.environ.get("CLAUDE_MODEL", "sonnet")

        c = LogCompressor(
            model_name=model,
            backend="claude",
            recent_tokens=50,
            min_recent_turns=2,
            compress_after_tokens=1,
            reflect_after_tokens=999_999,
        )
        c.register(make_agent(tmp_path))

        try:
            await c._consolidate(list(_REAL_DIALOG))
        finally:
            if hasattr(c, "_llm_agent"):
                await c._llm_agent.close()

        log = c._read_log()
        assert log, "LOG.md пустой"

        keywords = ["иван", "ivan", "go", "kafka", "rust", "lsm", "авито", "avito"]
        assert any(k in log.lower() for k in keywords), \
            f"LOG.md не содержит ни одного ключевого слова из {keywords}: {log[:300]}"
