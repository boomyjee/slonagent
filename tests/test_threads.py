"""
Тесты thread-функциональности: Agent.thread_name/rename, auto-регистрация в start,
MultiTransport fan-out, WebTransport mount-state шаринг и роутинг.
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import Agent
from src.transport.base import BaseTransport
from src.transport.multi import MultiTransport
from src.transport.web import WebTransport, MountState
from src.memory.providers.base import BaseProvider


class PassthroughCompressor(BaseProvider):
    def __init__(self): super().__init__(consolidate_tokens=0)
    async def compress(self, turns): return turns


class StubTransport(BaseTransport):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, str]] = []
    async def thread_rename(self, uuid, name):
        self.calls.append((uuid, name))


def _make_agent(tmp_path, thread_id="", transport=None):
    return Agent(
        id="test", model_name="m", api_key="k", base_url="http://t",
        agent_dir=str(tmp_path), thread_id=thread_id,
        memory_compressor=PassthroughCompressor(),
        transport=transport,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Agent.thread_name / thread_rename
# ═══════════════════════════════════════════════════════════════════════════════

class TestThreadRename:

    @pytest.mark.asyncio
    async def test_writes_to_threads_json(self, tmp_path):
        a = _make_agent(tmp_path, transport=StubTransport())
        await a.thread_rename("u1", "Hello")
        with open(tmp_path / "memory" / "THREADS.json") as f:
            data = json.load(f)
        assert data == {"u1": {"name": "Hello"}}

    @pytest.mark.asyncio
    async def test_thread_name_reads(self, tmp_path):
        a = _make_agent(tmp_path, transport=StubTransport())
        assert a.thread_name("u1") is None
        await a.thread_rename("u1", "Hello")
        assert a.thread_name("u1") == "Hello"

    @pytest.mark.asyncio
    async def test_idempotent_skip_same_name(self, tmp_path):
        t = StubTransport()
        a = _make_agent(tmp_path, transport=t)
        await a.thread_rename("u1", "Hello")
        await a.thread_rename("u1", "Hello")  # тот же name — должен пропуститься
        assert t.calls == [("u1", "Hello")]

    @pytest.mark.asyncio
    async def test_overwrites_different_name(self, tmp_path):
        t = StubTransport()
        a = _make_agent(tmp_path, transport=t)
        await a.thread_rename("u1", "A")
        await a.thread_rename("u1", "B")
        assert a.thread_name("u1") == "B"
        assert t.calls == [("u1", "A"), ("u1", "B")]

    @pytest.mark.asyncio
    async def test_fans_out_to_transport(self, tmp_path):
        t = StubTransport()
        a = _make_agent(tmp_path, transport=t)
        await a.thread_rename("u1", "Hi")
        assert t.calls == [("u1", "Hi")]


# ═══════════════════════════════════════════════════════════════════════════════
# Agent.start auto-регистрирует свой thread_id
# ═══════════════════════════════════════════════════════════════════════════════

class TestStartAutoRegister:

    @pytest.mark.asyncio
    async def test_main_thread_registered(self, tmp_path):
        t = StubTransport()
        a = _make_agent(tmp_path, thread_id="", transport=t)
        await a.start(run_loop=False)
        with open(tmp_path / "memory" / "THREADS.json") as f:
            data = json.load(f)
        assert "" in data
        assert data[""]["name"] == ""

    @pytest.mark.asyncio
    async def test_thread_with_existing_name_preserved(self, tmp_path):
        t = StubTransport()
        a = _make_agent(tmp_path, thread_id="abc", transport=t)
        await a.thread_rename("abc", "MyThread")
        await a.start(run_loop=False)
        # start не должен обнулить уже существующее имя
        assert a.thread_name("abc") == "MyThread"


# ═══════════════════════════════════════════════════════════════════════════════
# MultiTransport.thread_rename — fan-out
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiTransportFanOut:

    @pytest.mark.asyncio
    async def test_fans_to_all_children(self):
        a = StubTransport()
        b = StubTransport()
        m = MultiTransport([a, b])
        await m.thread_rename("u1", "X")
        assert a.calls == [("u1", "X")]
        assert b.calls == [("u1", "X")]


# ═══════════════════════════════════════════════════════════════════════════════
# WebTransport: MountState shared per-mount, ws inbound, агент-фабрика, start()
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebTransportMountState:

    def setup_method(self):
        WebTransport._mount_states.clear()
        WebTransport._agent_factory = None

    def test_mount_state_raises_before_set_agent(self):
        from src.transport.dashboard import DashboardTransport
        t = DashboardTransport()
        with pytest.raises(KeyError):
            t._mount_state

    def test_two_instances_share_mount_state(self):
        WebTransport._mount_states["forkX"] = MountState()
        from src.transport.dashboard import DashboardTransport
        t1 = DashboardTransport(); t1._mount_id = "forkX"
        t2 = DashboardTransport(); t2._mount_id = "forkX"
        t1._mount_state.clients.add("ws1")
        assert "ws1" in t2._mount_state.clients
        t1._mount_state.message_id_counter = 5
        assert t2._mount_state.message_id_counter == 5

    def test_different_mounts_isolated(self):
        WebTransport._mount_states["a"] = MountState()
        WebTransport._mount_states["b"] = MountState()
        from src.transport.dashboard import DashboardTransport
        t1 = DashboardTransport(); t1._mount_id = "a"
        t2 = DashboardTransport(); t2._mount_id = "b"
        t1._mount_state.clients.add("ws1")
        assert "ws1" not in t2._mount_state.clients


class TestWebTransportStart:

    @pytest.mark.asyncio
    async def test_start_without_factory(self):
        WebTransport._agent_factory = None
        WebTransport.start({"port": 1234})
        assert WebTransport._port == 1234
        assert WebTransport._agent_factory is None

    @pytest.mark.asyncio
    async def test_start_with_factory(self):
        async def fake_factory(*a, **kw): return None
        WebTransport.start({}, fake_factory)
        assert WebTransport._agent_factory is not None


class TestWebTransportInbound:

    def setup_method(self):
        WebTransport._mount_states.clear()
        WebTransport._agent_factory = None

    @pytest.mark.asyncio
    async def test_thread_rename_inbound_calls_agent(self):
        from src.transport.dashboard import DashboardTransport
        t = DashboardTransport()
        t._mount_id = "forkX"
        WebTransport._mount_states["forkX"] = MountState()
        agent = MagicMock()
        agent.id = "forkX"
        agent.thread_id = ""
        agent.thread_rename = AsyncMock()
        t.agent = agent
        await t.ws_handle_message({"type": "transport", "method": "thread_rename", "uuid": "u1", "name": "N"})
        agent.thread_rename.assert_awaited_once_with("u1", "N")

    @pytest.mark.asyncio
    async def test_process_message_routes_to_thread_agent(self):
        from src.transport.dashboard import DashboardTransport
        t = DashboardTransport()
        t._mount_id = "forkX"
        t._thread_id = ""
        WebTransport._mount_states["forkX"] = MountState()

        main_agent = MagicMock()
        main_agent.id = "forkX"; main_agent.thread_id = ""
        main_agent.process_message = AsyncMock()
        t.agent = main_agent

        thread_agent = MagicMock()
        thread_agent.process_message = AsyncMock()

        async def factory(agent_id, thread_id):
            assert agent_id == "forkX"
            assert thread_id == "abc"
            return thread_agent

        WebTransport._agent_factory = staticmethod(factory)

        await t.ws_handle_message({
            "type": "transport", "method": "process_message",
            "thread_id": "abc",
            "content_parts": [{"type": "text", "text": "hi"}],
        })
        thread_agent.process_message.assert_awaited_once()
        main_agent.process_message.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════════
# EchoBackend
# ═══════════════════════════════════════════════════════════════════════════════

class TestEchoBackend:

    @pytest.mark.asyncio
    async def test_echo_returns_user_query(self, tmp_path):
        a = Agent(id="test", model_name="m", agent_dir=str(tmp_path),
                  memory_compressor=PassthroughCompressor(), backend="echo",
                  backend_params={"delay": 0})
        await a.memory.add_turn({"role": "user", "content": "привет"})
        turn = await a.llm()
        assert turn == {"role": "assistant", "content": "эхо: привет"}

    @pytest.mark.asyncio
    async def test_echo_streams_thinking_then_message(self, tmp_path):
        thinking, message = [], []
        t = BaseTransport()
        async def _t(text, stream_id=None, final=False): thinking.append((text, stream_id, final))
        async def _m(text, stream_id=None, final=False): message.append((text, stream_id, final))
        t.send_thinking = _t
        t.send_message = _m
        a = Agent(id="test", model_name="m", agent_dir=str(tmp_path),
                  memory_compressor=PassthroughCompressor(), backend="echo",
                  backend_params={"delay": 0}, transport=t)
        await a.memory.add_turn({"role": "user", "content": "x"})
        await a.llm()
        # Стриминг теки: хотя бы одно partial + финальное.
        assert thinking, "должны быть thinking-события"
        assert thinking[-1][2] is True  # последний final
        assert message, "должны быть message-события"
        assert message[-1][2] is True
        # Stream IDs для thinking и message — разные
        assert thinking[0][1] != message[0][1]
