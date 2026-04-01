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
# LogCompressor._split_recent
# ═══════════════════════════════════════════════════════════════════════════════

class TestSplitRecent:

    def _make_compressor(self, recent_tokens=500, min_recent_turns=2):
        return LogCompressor(
            model_name="test", api_key="test", base_url="http://test",
            recent_tokens=recent_tokens,
            min_recent_turns=min_recent_turns,
            compress_after_tokens=1,
            reflect_after_tokens=999999,
        )

    def test_all_recent_when_small(self):
        c = self._make_compressor(recent_tokens=999999)
        turns = make_turns(6)
        to_obs, recent = c._split_recent(turns)
        assert to_obs == []
        assert recent == turns

    def test_old_turns_go_to_observe(self):
        c = self._make_compressor(recent_tokens=100, min_recent_turns=0)
        turns = make_turns(10, chars=200)
        to_obs, recent = c._split_recent(turns)
        assert len(to_obs) > 0
        assert len(recent) > 0
        assert to_obs + recent == turns

    def test_min_recent_turns_respected(self):
        # Даже если budget исчерпан раньше — гарантируем min_recent_turns
        c = self._make_compressor(recent_tokens=1, min_recent_turns=3)
        turns = make_turns(6, chars=200)
        _, recent = c._split_recent(turns)
        assert len(recent) >= 3


# ═══════════════════════════════════════════════════════════════════════════════
# LogCompressor.compress — с мок LLM
# ═══════════════════════════════════════════════════════════════════════════════

class TestLogCompressorCompress:

    def _make_compressor(self, tmp_path):
        agent = make_agent(tmp_path)
        c = LogCompressor(
            model_name="test", api_key="test", base_url="http://test",
            recent_tokens=100,
            min_recent_turns=1,
            compress_after_tokens=1,
            reflect_after_tokens=999999,
        )
        c.register(agent)
        return c

    def _mock_llm(self, c, response_text: str):
        c._client.chat.completions.create = AsyncMock(return_value=_make_response(response_text))

    @pytest.mark.asyncio
    async def test_returns_all_turns_below_threshold(self, tmp_path):
        c = LogCompressor(
            model_name="test", api_key="test", base_url="http://test",
            compress_after_tokens=999999,
        )
        c.register(make_agent(tmp_path))
        turns = make_turns(4)
        result = await c.compress(turns)
        assert result == turns

    @pytest.mark.asyncio
    async def test_compress_produces_om_turn_plus_recent(self, tmp_path):
        c = self._make_compressor(tmp_path)
        obs_text = "<observations>\n* 🔴 (10:00) User said hello.\n</observations>"
        self._mock_llm(c, obs_text)

        turns = make_turns(10, chars=500)
        result = await c.compress(turns)

        om_turns = [t for t in result if isinstance(t, dict) and t.get("_observation_message")]
        assert len(om_turns) == 1
        assert "_raw_observations" in om_turns[0]

    @pytest.mark.asyncio
    async def test_compress_om_turn_is_first(self, tmp_path):
        c = self._make_compressor(tmp_path)
        obs_text = "<observations>\n* 🔴 item\n</observations>"
        self._mock_llm(c, obs_text)

        result = await c.compress(make_turns(10, chars=500))
        assert result[0].get("_observation_message") is True

    @pytest.mark.asyncio
    async def test_existing_om_turn_updated(self, tmp_path):
        c = self._make_compressor(tmp_path)
        self._mock_llm(c, "<observations>\n* 🔴 new obs\n</observations>")

        existing_om = {
            "role": "user",
            "content": "old observations block",
            "_observation_message": True,
            "_raw_observations": "Date: Jan 1, 2025\n* 🔴 old obs",
        }
        new_turns = make_turns(6, chars=500)
        result = await c.compress([existing_om] + new_turns)

        om = next(t for t in result if isinstance(t, dict) and t.get("_observation_message"))
        assert "old obs" in om["_raw_observations"]
        assert "new obs" in om["_raw_observations"]


def _make_response(text: str):
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


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
# _split_recent — tool-pair boundary
# ═══════════════════════════════════════════════════════════════════════════════

class TestSplitRecentToolPair:

    def _make_compressor(self, recent_tokens=99999, min_recent_turns=0):
        return LogCompressor(
            model_name="test", api_key="test", base_url="http://test",
            recent_tokens=recent_tokens,
            min_recent_turns=min_recent_turns,
            compress_after_tokens=1,
            reflect_after_tokens=999999,
        )

    def test_tool_turn_not_first_in_recent(self):
        """recent не должен начинаться с tool-тура без парного assistant перед ним."""
        turns = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "result"},
            {"role": "assistant", "content": "done"},
        ]
        c = self._make_compressor(recent_tokens=1, min_recent_turns=0)
        to_obs, recent = c._split_recent(turns)
        assert not (recent and isinstance(recent[0], dict) and recent[0].get("role") == "tool")

    def test_tool_turns_moved_to_observe(self):
        """Если граница режет между assistant(tool_calls) и tool — tool уходит в to_observe."""
        tool_turn = {"role": "tool", "tool_call_id": "1", "content": "x" * 200}
        turns = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": []},
            tool_turn,
            {"role": "user", "content": "x" * 200},
            {"role": "assistant", "content": "x" * 200},
        ]
        c = self._make_compressor(recent_tokens=120, min_recent_turns=0)
        to_obs, recent = c._split_recent(turns)
        if recent and recent[0].get("role") == "tool":
            pytest.fail("recent starts with tool turn")
        assert to_obs + recent == turns


# ═══════════════════════════════════════════════════════════════════════════════
# compress — edge cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestLogCompressorEdgeCases:

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
        c._client.chat.completions.create = AsyncMock(return_value=_make_response(response_text))

    @pytest.mark.asyncio
    async def test_observer_empty_returns_original_turns(self, tmp_path):
        """Если observer вернул пустую строку — turns не меняются."""
        c = self._make_compressor(tmp_path)
        self._mock_llm(c, "")
        turns = make_turns(10, chars=500)
        result = await c.compress(turns)
        assert result == turns

    @pytest.mark.asyncio
    async def test_reflector_triggered_when_obs_exceed_threshold(self, tmp_path):
        """Рефлектор вызывается когда observations превышают reflect_after_tokens."""
        # reflect_after_tokens=1 — любые observations превысят порог
        c = self._make_compressor(tmp_path, reflect_after=1)

        # observer возвращает большой блок (~100 токенов)
        big_obs = "<observations>\n* 🔴 (10:00) " + "x" * 400 + "\n</observations>"
        # reflector возвращает маленький — гарантированно меньше по токенам
        small_reflected = "<observations>\n* 🔴 (10:00) condensed\n</observations>"

        call_count = 0

        async def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            return _make_response(big_obs if call_count == 1 else small_reflected)

        c._client.chat.completions.create = side_effect
        result = await c.compress(make_turns(10, chars=500))

        assert call_count == 2
        om = next(t for t in result if isinstance(t, dict) and t.get("_observation_message"))
        assert "condensed" in om["_raw_observations"]

    @pytest.mark.asyncio
    async def test_reflector_escalates_compression_level(self, tmp_path):
        """Если reflector не сжимает — уровень растёт до тех пор пока не достигнет 4."""
        c = self._make_compressor(tmp_path, reflect_after=1)

        # observer даёт 400 символов → ~100 токенов
        big_obs = "* 🔴 (10:00) " + "x" * 400
        obs_response = f"<observations>\n{big_obs}\n</observations>"

        # reflector всегда возвращает такой же размер → не сжимает → уровень растёт
        call_count = 0

        async def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            return _make_response(obs_response)

        c._client.chat.completions.create = side_effect
        await c.compress(make_turns(10, chars=500))

        # 1 вызов observer + 5 вызовов reflector (уровни 0→1→2→3→4, на 4 останавливается)
        assert call_count == 6

    @pytest.mark.asyncio
    async def test_compress_no_to_observe_returns_original(self, tmp_path):
        """Если все turns попали в recent — turns не меняются."""
        c = LogCompressor(
            model_name="test", api_key="test", base_url="http://test",
            recent_tokens=999999,
            min_recent_turns=0,
            compress_after_tokens=1,
            reflect_after_tokens=999999,
        )
        c.register(make_agent(tmp_path))
        turns = make_turns(4, chars=10)
        result = await c.compress(turns)
        assert result == turns
