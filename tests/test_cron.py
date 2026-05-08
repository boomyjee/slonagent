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


def make_agent(tmp_path, thread_id="", agent_id="main"):
    from agent import Agent
    # id="main" по умолчанию: совпадает с тем, что _discover_files infer'ит
    # для tmp_path/memory/CRON*.json. Иначе тесты с CronSkill._tick не
    # резолвят реестр-fallback правильно.
    return Agent(
        id=agent_id,
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
    """Class-level state крон-демона + Agent._instances реестра: между тестами
    сбрасываем чтобы не утекало."""
    from agent import Agent
    CronSkill._root_dir = None
    CronSkill._loop_task = None
    Agent._instances.clear()
    yield
    CronSkill._root_dir = None
    CronSkill._loop_task = None
    Agent._instances.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# _next_run
# ═══════════════════════════════════════════════════════════════════════════════

class TestNextRun:

    def test_once_returns_none(self):
        assert CronSkill._next_run("2026-01-01T10:00:00", "once") is None

    def test_hourly(self):
        base = (datetime.now() + timedelta(days=365)).astimezone()
        result = CronSkill._next_run(base.isoformat(), "hourly")
        assert result == (base + timedelta(hours=1)).isoformat()

    def test_daily(self):
        base = (datetime.now() + timedelta(days=365)).astimezone()
        result = CronSkill._next_run(base.isoformat(), "daily")
        assert result == (base + timedelta(days=1)).isoformat()

    def test_weekly(self):
        base = (datetime.now() + timedelta(days=365)).astimezone()
        result = CronSkill._next_run(base.isoformat(), "weekly")
        assert result == (base + timedelta(weeks=1)).isoformat()


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

def _wire_global_loop(monkeypatch, tmp_path, agent):
    """Подключить class-level CronSkill loop к temp dir + замокать Agent.get
    чтобы возвращал переданного агента. Для unit-тестов CronSkill._tick()."""
    from agent import Agent
    CronSkill._root_dir = str(tmp_path)
    async def fake_get(agent_id, thread_id="", **kw):
        return agent
    monkeypatch.setattr(Agent, "get", fake_get)


class TestCronTick:

    @pytest.mark.asyncio
    async def test_due_task_fires(self, tmp_path, monkeypatch):
        cron = make_cron(tmp_path)
        injected = []
        cron.agent.transport = MagicMock()
        cron.agent.transport.inject_message = AsyncMock(
            side_effect=lambda msg: injected.append(msg)
        )
        cron.agent.transport.process_message = AsyncMock()
        _wire_global_loop(monkeypatch, tmp_path, cron.agent)

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await cron.schedule_task("fire me", past, "once")
        await CronSkill._tick()

        assert len(injected) == 1
        assert "fire me" in injected[0]

    @pytest.mark.asyncio
    async def test_future_task_does_not_fire(self, tmp_path, monkeypatch):
        cron = make_cron(tmp_path)
        cron.agent.transport = MagicMock()
        cron.agent.transport.inject_message = AsyncMock()
        _wire_global_loop(monkeypatch, tmp_path, cron.agent)

        future = (datetime.now() + timedelta(hours=1)).isoformat()
        await cron.schedule_task("don't fire", future, "once")
        await CronSkill._tick()

        cron.agent.transport.inject_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_once_task_removed_after_fire(self, tmp_path, monkeypatch):
        cron = make_cron(tmp_path)
        cron.agent.transport = MagicMock()
        cron.agent.transport.inject_message = AsyncMock()
        cron.agent.transport.process_message = AsyncMock()
        _wire_global_loop(monkeypatch, tmp_path, cron.agent)

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await cron.schedule_task("once task", past, "once")
        await CronSkill._tick()

        remaining = await cron.list_tasks()
        assert remaining["tasks"] == []

    @pytest.mark.asyncio
    async def test_daily_task_rescheduled_after_fire(self, tmp_path, monkeypatch):
        cron = make_cron(tmp_path)
        cron.agent.transport = MagicMock()
        cron.agent.transport.inject_message = AsyncMock()
        cron.agent.transport.process_message = AsyncMock()
        _wire_global_loop(monkeypatch, tmp_path, cron.agent)

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await cron.schedule_task("daily task", past, "daily")
        await CronSkill._tick()

        remaining = await cron.list_tasks()
        assert len(remaining["tasks"]) == 1
        # Следующий запуск — в будущем
        next_dt = datetime.fromisoformat(remaining["tasks"][0]["scheduled_at"]).replace(tzinfo=None)
        assert next_dt > datetime.now()

    @pytest.mark.asyncio
    async def test_unresolved_agent_keeps_task_for_retry(self, tmp_path, monkeypatch):
        # Agent.get вернёт None — task должен остаться и попробовать снова
        # на следующем тике (а не пропасть).
        from agent import Agent
        cron = make_cron(tmp_path)
        CronSkill._root_dir = str(tmp_path)
        async def fake_get(*a, **kw): return None
        monkeypatch.setattr(Agent, "get", fake_get)

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await cron.schedule_task("ghost", past, "once")
        await CronSkill._tick()

        remaining = await cron.list_tasks()
        assert len(remaining["tasks"]) == 1
        assert remaining["tasks"][0]["message"] == "ghost"

    @pytest.mark.asyncio
    async def test_tick_uses_registered_agent_from_instances(self, tmp_path):
        # Агент уже создан и закеширован в Agent._instances (через make_agent
        # из main.py при старте). Cron вызывает Agent.get → cache hit → агент.
        from agent import Agent
        cron = make_cron(tmp_path)
        cron.agent.transport = MagicMock()
        cron.agent.transport.inject_message = AsyncMock()
        cron.agent.transport.process_message = AsyncMock()
        # Эмулируем что agent уже зарегистрирован через Agent.get
        Agent._instances[(cron.agent.id, cron.agent.thread_id)] = cron.agent
        CronSkill._root_dir = str(tmp_path)

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await cron.schedule_task("from registry", past, "once")
        await CronSkill._tick()

        cron.agent.transport.inject_message.assert_called_once()


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
    async def test_discovers_main_and_thread_files(self, tmp_path, monkeypatch):
        # main и тред-агент → файлы CRON.json и CRON_t1.json в одной memory_dir
        from agent import Agent
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

        async def fake_get(agent_id, thread_id="", **kw):
            agents_resolved.append((agent_id, thread_id))
            return agent_main if thread_id == "" else agent_thread
        CronSkill._root_dir = str(tmp_path)
        monkeypatch.setattr(Agent, "get", fake_get)

        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        await main.schedule_task("main task", past, "once")
        await thread.schedule_task("thread task", past, "once")
        await CronSkill._tick()

        # обе задачи зарезолвились через Agent.get
        assert ("main", "") in agents_resolved
        assert ("main", "t1") in agents_resolved
        # каждая до своего агента
        agent_main.transport.inject_message.assert_called_once()
        agent_thread.transport.inject_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_discovers_fork_files(self, tmp_path, monkeypatch):
        # эмулируем структуру: cwd/forks/myfork/memory/CRON.json
        from agent import Agent
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
        async def fake_get(agent_id, thread_id="", **kw):
            resolved.append((agent_id, thread_id))
            return agent_stub
        CronSkill._root_dir = str(tmp_path)
        monkeypatch.setattr(Agent, "get", fake_get)

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
