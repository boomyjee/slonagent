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
from src.memory.providers.base import BaseProvider
from src.skills.cron import CronSkill


class PassthroughCompressor(BaseProvider):
    def __init__(self): super().__init__(consolidate_tokens=0)
    async def compress(self, turns): return turns


def make_agent(tmp_path, thread_id=""):
    from agent import Agent
    return Agent(
        id="test",
        model_name="test",
        api_key="test",
        base_url="http://test",
        agent_dir=str(tmp_path),
        thread_id=thread_id,
        memory_compressor=PassthroughCompressor(),
    )


def make_cron(tmp_path, thread_id=""):
    agent = make_agent(tmp_path, thread_id=thread_id)
    cron = CronSkill()
    cron.register(agent)
    return cron


@pytest.fixture(autouse=True)
def reset_cron_class_state():
    """Class-level cron loop state живёт на CronSkill — между тестами сбрасываем."""
    CronSkill._root_dir = None
    CronSkill._make_agent = None
    CronSkill._loop_task = None
    yield
    CronSkill._root_dir = None
    CronSkill._make_agent = None
    CronSkill._loop_task = None


# ═══════════════════════════════════════════════════════════════════════════════
# _next_run
# ═══════════════════════════════════════════════════════════════════════════════

class TestNextRun:

    def test_once_returns_none(self):
        assert CronSkill._next_run("2026-01-01T10:00:00", "once") is None

    def test_hourly(self):
        assert "11:00" in CronSkill._next_run("2026-01-01T10:00:00", "hourly")

    def test_daily(self):
        assert "2026-01-02" in CronSkill._next_run("2026-01-01T10:00:00", "daily")

    def test_weekly(self):
        assert "2026-01-08" in CronSkill._next_run("2026-01-01T10:00:00", "weekly")


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

def _wire_global_loop(tmp_path, agent):
    """Подключить class-level CronSkill loop к temp dir + фабрика возвращает
    переданного агента. Для unit-тестов которые гоняют CronSkill._tick()."""
    CronSkill._root_dir = str(tmp_path)
    async def factory(agent_id, thread_id):
        return agent
    CronSkill._make_agent = staticmethod(factory)


class TestCronTick:

    @pytest.mark.asyncio
    async def test_due_task_fires(self, tmp_path):
        cron = make_cron(tmp_path)
        injected = []
        cron.agent.transport = MagicMock()
        cron.agent.transport.inject_message = AsyncMock(
            side_effect=lambda msg: injected.append(msg)
        )
        cron.agent.transport.process_message = AsyncMock()
        _wire_global_loop(tmp_path, cron.agent)

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await cron.schedule_task("fire me", past, "once")
        await CronSkill._tick()

        assert len(injected) == 1
        assert "fire me" in injected[0]

    @pytest.mark.asyncio
    async def test_future_task_does_not_fire(self, tmp_path):
        cron = make_cron(tmp_path)
        cron.agent.transport = MagicMock()
        cron.agent.transport.inject_message = AsyncMock()
        _wire_global_loop(tmp_path, cron.agent)

        future = (datetime.now() + timedelta(hours=1)).isoformat()
        await cron.schedule_task("don't fire", future, "once")
        await CronSkill._tick()

        cron.agent.transport.inject_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_once_task_removed_after_fire(self, tmp_path):
        cron = make_cron(tmp_path)
        cron.agent.transport = MagicMock()
        cron.agent.transport.inject_message = AsyncMock()
        cron.agent.transport.process_message = AsyncMock()
        _wire_global_loop(tmp_path, cron.agent)

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await cron.schedule_task("once task", past, "once")
        await CronSkill._tick()

        remaining = await cron.list_tasks()
        assert remaining["tasks"] == []

    @pytest.mark.asyncio
    async def test_daily_task_rescheduled_after_fire(self, tmp_path):
        cron = make_cron(tmp_path)
        cron.agent.transport = MagicMock()
        cron.agent.transport.inject_message = AsyncMock()
        cron.agent.transport.process_message = AsyncMock()
        _wire_global_loop(tmp_path, cron.agent)

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await cron.schedule_task("daily task", past, "daily")
        await CronSkill._tick()

        remaining = await cron.list_tasks()
        assert len(remaining["tasks"]) == 1
        # Следующий запуск — в будущем
        next_dt = datetime.fromisoformat(remaining["tasks"][0]["scheduled_at"]).replace(tzinfo=None)
        assert next_dt > datetime.now()

    @pytest.mark.asyncio
    async def test_tick_drops_task_if_make_agent_unavailable(self, tmp_path):
        cron = make_cron(tmp_path)
        # _root_dir выставлен, но _make_agent=None → агент не резолвится
        CronSkill._root_dir = str(tmp_path)
        CronSkill._make_agent = None

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await cron.schedule_task("ghost", past, "once")
        await CronSkill._tick()

        # task должен быть удалён, ничего не падает
        remaining = await cron.list_tasks()
        assert remaining["tasks"] == []


# ═══════════════════════════════════════════════════════════════════════════════
# Изоляция по thread_id — main и треды одного форка не видят друг-друга
# ═══════════════════════════════════════════════════════════════════════════════

class TestCronThreadIsolation:

    def test_tasks_path_differs_per_thread(self, tmp_path):
        main = make_cron(tmp_path, thread_id="")
        thread_a = make_cron(tmp_path, thread_id="abc")
        thread_b = make_cron(tmp_path, thread_id="xyz")
        assert main._tasks_path.endswith("CRON.json")
        assert thread_a._tasks_path.endswith("CRON_abc.json")
        assert thread_b._tasks_path.endswith("CRON_xyz.json")
        # Все три — разные файлы
        paths = {main._tasks_path, thread_a._tasks_path, thread_b._tasks_path}
        assert len(paths) == 3

    @pytest.mark.asyncio
    async def test_thread_tasks_invisible_to_main(self, tmp_path):
        main = make_cron(tmp_path, thread_id="")
        thread = make_cron(tmp_path, thread_id="abc")
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        await thread.schedule_task("task in thread", future, "once")
        # main не должен видеть таску треда
        main_list = await main.list_tasks()
        assert main_list["tasks"] == []
        # тред видит свою
        thread_list = await thread.list_tasks()
        assert len(thread_list["tasks"]) == 1
        assert thread_list["tasks"][0]["message"] == "task in thread"

    @pytest.mark.asyncio
    async def test_main_tasks_invisible_to_thread(self, tmp_path):
        main = make_cron(tmp_path, thread_id="")
        thread = make_cron(tmp_path, thread_id="abc")
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        await main.schedule_task("task in main", future, "once")
        thread_list = await thread.list_tasks()
        assert thread_list["tasks"] == []
        main_list = await main.list_tasks()
        assert len(main_list["tasks"]) == 1

    @pytest.mark.asyncio
    async def test_threads_isolated_from_each_other(self, tmp_path):
        a = make_cron(tmp_path, thread_id="abc")
        b = make_cron(tmp_path, thread_id="xyz")
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        await a.schedule_task("only in A", future, "once")
        await b.schedule_task("only in B", future, "once")
        a_msgs = [t["message"] for t in (await a.list_tasks())["tasks"]]
        b_msgs = [t["message"] for t in (await b.list_tasks())["tasks"]]
        assert a_msgs == ["only in A"]
        assert b_msgs == ["only in B"]

# ═══════════════════════════════════════════════════════════════════════════════
# Глобальный CronSkill._tick — discovery main + forks
# ═══════════════════════════════════════════════════════════════════════════════

class TestGlobalTick:

    @pytest.mark.asyncio
    async def test_discovers_main_and_thread_files(self, tmp_path):
        # main и тред-агент → файлы CRON.json и CRON_t1.json в одной memory_dir
        main = make_cron(tmp_path, thread_id="")
        thread = make_cron(tmp_path, thread_id="t1")
        agents_resolved = []
        agent_main = main.agent
        agent_main.transport = MagicMock()
        agent_main.transport.inject_message = AsyncMock()
        agent_main.transport.process_message = AsyncMock()
        agent_thread = thread.agent
        agent_thread.transport = MagicMock()
        agent_thread.transport.inject_message = AsyncMock()
        agent_thread.transport.process_message = AsyncMock()

        async def factory(agent_id, thread_id):
            agents_resolved.append((agent_id, thread_id))
            return agent_main if thread_id == "" else agent_thread
        CronSkill._root_dir = str(tmp_path)
        CronSkill._make_agent = staticmethod(factory)

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await main.schedule_task("main task", past, "once")
        await thread.schedule_task("thread task", past, "once")
        await CronSkill._tick()

        # обе задачи зарезолвились через factory
        assert ("main", "") in agents_resolved
        assert ("main", "t1") in agents_resolved
        # каждая до своего агента
        agent_main.transport.inject_message.assert_called_once()
        agent_thread.transport.inject_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_discovers_fork_files(self, tmp_path):
        # эмулируем структуру: cwd/forks/myfork/memory/CRON.json
        fork_mem = tmp_path / "forks" / "myfork" / "memory"
        fork_mem.mkdir(parents=True)
        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        task = {"id": "abc1234", "message": "in fork", "scheduled_at": past, "repeat": "once"}
        (fork_mem / "CRON.json").write_text(
            __import__("json").dumps(task) + "\n", encoding="utf-8",
        )

        agent_stub = MagicMock()
        agent_stub.transport = MagicMock()
        agent_stub.transport.inject_message = AsyncMock()
        agent_stub.transport.process_message = AsyncMock()

        resolved = []
        async def factory(agent_id, thread_id):
            resolved.append((agent_id, thread_id))
            return agent_stub
        CronSkill._root_dir = str(tmp_path)
        CronSkill._make_agent = staticmethod(factory)

        await CronSkill._tick()

        assert resolved == [("myfork", "")]
        agent_stub.transport.inject_message.assert_called_once()


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
