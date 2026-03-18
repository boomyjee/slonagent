"""
Тесты CronSkill.

Запуск:
    venv\\Scripts\\python -m pytest tests/test_cron.py -v
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Skill
from src.skills.cron import CronSkill


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


def make_cron(tmp_path):
    agent = make_agent(tmp_path)
    cron = CronSkill()
    cron.register(agent)
    return cron


# ═══════════════════════════════════════════════════════════════════════════════
# _next_run
# ═══════════════════════════════════════════════════════════════════════════════

class TestNextRun:

    def setup_method(self):
        self.cron = CronSkill()

    def test_once_returns_none(self):
        assert self.cron._next_run("2026-01-01T10:00:00", "once") is None

    def test_hourly(self):
        result = self.cron._next_run("2026-01-01T10:00:00", "hourly")
        assert "11:00" in result

    def test_daily(self):
        result = self.cron._next_run("2026-01-01T10:00:00", "daily")
        assert "2026-01-02" in result

    def test_weekly(self):
        result = self.cron._next_run("2026-01-01T10:00:00", "weekly")
        assert "2026-01-08" in result


# ═══════════════════════════════════════════════════════════════════════════════
# schedule_task / cancel_task / list_tasks
# ═══════════════════════════════════════════════════════════════════════════════

class TestCronTools:

    @pytest.mark.asyncio
    async def test_schedule_task_creates_file(self, tmp_path):
        cron = make_cron(tmp_path)
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        result = await cron.schedule_task("do something", future, "once")
        assert result["status"] == "scheduled"
        assert os.path.exists(cron._tasks_path)

    @pytest.mark.asyncio
    async def test_schedule_task_invalid_date(self, tmp_path):
        cron = make_cron(tmp_path)
        result = await cron.schedule_task("do something", "not-a-date", "once")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_schedule_and_list(self, tmp_path):
        cron = make_cron(tmp_path)
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        await cron.schedule_task("task A", future, "once")
        await cron.schedule_task("task B", future, "daily")
        result = await cron.list_tasks()
        assert len(result["tasks"]) == 2
        messages = [t["message"] for t in result["tasks"]]
        assert "task A" in messages
        assert "task B" in messages

    @pytest.mark.asyncio
    async def test_cancel_task(self, tmp_path):
        cron = make_cron(tmp_path)
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        scheduled = await cron.schedule_task("to cancel", future)
        task_id = scheduled["task_id"]

        result = await cron.cancel_task(task_id)
        assert result["status"] == "cancelled"

        remaining = await cron.list_tasks()
        assert all(t["id"] != task_id for t in remaining["tasks"])

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_error(self, tmp_path):
        cron = make_cron(tmp_path)
        result = await cron.cancel_task("nonexistent-id")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_list_empty(self, tmp_path):
        cron = make_cron(tmp_path)
        result = await cron.list_tasks()
        assert result["tasks"] == []


# ═══════════════════════════════════════════════════════════════════════════════
# _tick — срабатывание задач
# ═══════════════════════════════════════════════════════════════════════════════

class TestCronTick:

    @pytest.mark.asyncio
    async def test_due_task_fires(self, tmp_path):
        cron = make_cron(tmp_path)
        injected = []
        cron.agent.transport = MagicMock()
        cron.agent.transport.inject_message = AsyncMock(
            side_effect=lambda msg: injected.append(msg)
        )

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await cron.schedule_task("fire me", past, "once")
        await cron._tick()

        assert len(injected) == 1
        assert "fire me" in injected[0]

    @pytest.mark.asyncio
    async def test_future_task_does_not_fire(self, tmp_path):
        cron = make_cron(tmp_path)
        cron.agent.transport = MagicMock()
        cron.agent.transport.inject_message = AsyncMock()

        future = (datetime.now() + timedelta(hours=1)).isoformat()
        await cron.schedule_task("don't fire", future, "once")
        await cron._tick()

        cron.agent.transport.inject_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_once_task_removed_after_fire(self, tmp_path):
        cron = make_cron(tmp_path)
        cron.agent.transport = MagicMock()
        cron.agent.transport.inject_message = AsyncMock()

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await cron.schedule_task("once task", past, "once")
        await cron._tick()

        remaining = await cron.list_tasks()
        assert remaining["tasks"] == []

    @pytest.mark.asyncio
    async def test_daily_task_rescheduled_after_fire(self, tmp_path):
        cron = make_cron(tmp_path)
        cron.agent.transport = MagicMock()
        cron.agent.transport.inject_message = AsyncMock()

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await cron.schedule_task("daily task", past, "daily")
        await cron._tick()

        remaining = await cron.list_tasks()
        assert len(remaining["tasks"]) == 1
        # Следующий запуск — в будущем
        next_dt = datetime.fromisoformat(remaining["tasks"][0]["scheduled_at"]).replace(tzinfo=None)
        assert next_dt > datetime.now()


# ═══════════════════════════════════════════════════════════════════════════════
# /cron bypass
# ═══════════════════════════════════════════════════════════════════════════════

class TestCronBypass:

    @pytest.mark.asyncio
    async def test_cron_bypass_empty(self, tmp_path):
        cron = make_cron(tmp_path)
        result = await cron.dispatch_bypass("/cron")
        assert "нет" in result.lower()

    @pytest.mark.asyncio
    async def test_cron_bypass_shows_tasks(self, tmp_path):
        cron = make_cron(tmp_path)
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        await cron.schedule_task("important thing", future)
        result = await cron.dispatch_bypass("/cron")
        assert "important thing" in result
