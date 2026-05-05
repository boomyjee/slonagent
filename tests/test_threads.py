"""
Тесты thread-функциональности: Agent.thread_name/rename, auto-регистрация в start,
MultiTransport fan-out, WebTransport/WebFork шаринг и роутинг.
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
from src.transport.web import WebTransport, WebFork, WebTransportServer
from src.memory.providers.base import BaseProvider


# WebFork без HTTP-роутов — для unit-тестов без поднятия FastAPI/uvicorn.
class _NoMountFork(WebFork):
    mount = False


class _NoMountTransport(WebTransport):
    def create_fork(self, agent):
        return _NoMountFork(ref_agent=agent)


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
        # Agent.__init__ авто-регистрирует свой thread_id ("" по дефолту) через
        # thread_ensure → в файле помимо нашего "u1" будет ещё "".
        assert data["u1"] == {"name": "Hello"}

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
# WebTransport ↔ WebFork: разделение форка по agent.id, refcount, start()
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebTransportForkSharing:

    def setup_method(self):
        WebTransport._forks.clear()

    def test_two_transports_share_fork(self):
        t1 = _NoMountTransport(); t1.set_agent(MagicMock(id="forkX"))
        t2 = _NoMountTransport(); t2.set_agent(MagicMock(id="forkX"))
        assert t1.fork is t2.fork
        # State (clients, counter) тоже один общий — это и был смысл шаринга.
        t1.fork.clients.add("ws1")
        assert "ws1" in t2.fork.clients
        t1.fork.message_id_counter = 5
        assert t2.fork.message_id_counter == 5

    def test_different_forks_isolated(self):
        t1 = _NoMountTransport(); t1.set_agent(MagicMock(id="a"))
        t2 = _NoMountTransport(); t2.set_agent(MagicMock(id="b"))
        assert t1.fork is not t2.fork
        t1.fork.clients.add("ws1")
        assert "ws1" not in t2.fork.clients

    def test_refcount_lifecycle(self):
        a = MagicMock(id="forkX")
        t1 = _NoMountTransport(); t1.set_agent(a)
        t2 = _NoMountTransport(); t2.set_agent(MagicMock(id="forkX"))
        assert t1.fork.refcount == 2
        t1.cleanup()
        assert t2.fork.refcount == 1
        assert "forkX" in WebTransport._forks
        t2.cleanup()
        assert "forkX" not in WebTransport._forks


class TestWebTransportStart:

    def test_start_stashes_make_agent_on_transport(self, monkeypatch):
        # WebTransportServer.start не должен зависеть от фабрики — её WebTransport
        # держит у себя как class-attr и читает в WebFork.ws_handle_message.
        monkeypatch.setattr(WebTransportServer, "start",
                            classmethod(lambda cls, config: None))
        WebTransport.make_agent = None

        async def factory(*a, **kw): return None
        WebTransport.start({"port": 1234}, factory)
        assert WebTransport.make_agent is factory

    def test_start_without_factory_keeps_existing(self, monkeypatch):
        monkeypatch.setattr(WebTransportServer, "start",
                            classmethod(lambda cls, config: None))
        async def existing(*a, **kw): return None
        WebTransport.make_agent = staticmethod(existing)
        WebTransport.start({"port": 1234})  # без make_agent
        assert WebTransport.make_agent is existing


class TestWebForkInbound:

    def setup_method(self):
        WebTransport.make_agent = None

    @pytest.mark.asyncio
    async def test_thread_rename_inbound_calls_agent(self):
        agent = MagicMock(id="forkX", thread_id="")
        agent.thread_rename = AsyncMock()
        fork = _NoMountFork(ref_agent=agent)
        await fork.ws_handle_message({
            "type": "transport", "method": "thread_rename",
            "uuid": "u1", "name": "N",
        })
        agent.thread_rename.assert_awaited_once_with("u1", "N")

    @pytest.mark.asyncio
    async def test_process_message_routes_to_thread_transport(self):
        main = MagicMock(id="forkX", thread_id="")
        fork = _NoMountFork(ref_agent=main)
        main_t = MagicMock(); main_t.process_message = AsyncMock()
        thread_t = MagicMock(); thread_t.process_message = AsyncMock()
        fork.transports[""] = main_t
        fork.transports["abc"] = thread_t

        await fork.ws_handle_message({
            "type": "transport", "method": "process_message",
            "thread_id": "abc",
            "content_parts": [{"type": "text", "text": "hi"}],
        })
        thread_t.process_message.assert_awaited_once()
        main_t.process_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_process_message_main_thread_routes_via_main_transport(self):
        main = MagicMock(id="forkX", thread_id="")
        fork = _NoMountFork(ref_agent=main)
        main_t = MagicMock(); main_t.process_message = AsyncMock()
        fork.transports[""] = main_t

        await fork.ws_handle_message({
            "type": "transport", "method": "process_message",
            "thread_id": "",
            "content_parts": [{"type": "text", "text": "hi"}],
        })
        main_t.process_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_thread_invokes_make_agent_then_routes(self, monkeypatch):
        # Если транспорта для треда нет — фабрика поднимает агента, его
        # set_agent прописывает себя в fork.transports, дальше уже есть кому
        # отправить process_message.
        main = MagicMock(id="forkX", thread_id="")
        fork = _NoMountFork(ref_agent=main)
        new_t = MagicMock(); new_t.process_message = AsyncMock()

        async def factory(agent_id, thread_id):
            assert (agent_id, thread_id) == ("forkX", "newtab")
            fork.transports[thread_id] = new_t  # имитация set_agent побочки
            return MagicMock()
        monkeypatch.setattr(WebTransport, "make_agent", staticmethod(factory))

        await fork.ws_handle_message({
            "type": "transport", "method": "process_message",
            "thread_id": "newtab",
            "content_parts": [{"type": "text", "text": "hi"}],
        })
        new_t.process_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_thread_no_factory_drops_message(self):
        main = MagicMock(id="forkX", thread_id="")
        fork = _NoMountFork(ref_agent=main)
        # Никакой регистрации, никакой фабрики — сообщение должно безопасно
        # дропнуться с warning'ом, а не упасть.
        WebTransport.make_agent = None

        await fork.ws_handle_message({
            "type": "transport", "method": "process_message",
            "thread_id": "ghost",
            "content_parts": [{"type": "text", "text": "hi"}],
        })
        # Доехали без исключения — этого достаточно.

    @pytest.mark.asyncio
    async def test_replay_sends_single_batch_envelope(self):
        # Replay шлётся одним сообщением {type:'replay', events:[...]} — клиент
        # развернёт его синхронно, чтоб все setState'ы схлопнулись в один render.
        main = MagicMock(id="forkX", thread_id="")
        fork = _NoMountFork(ref_agent=main)
        for i in range(3):
            await fork.send({"type": "log", "category": "agent", "text": f"l{i}"}, replay=True)

        ws = MagicMock(); ws.send_text = AsyncMock()
        await fork.ws_handle_message({"type": "replay", "last_seen_id": -1}, ws=ws)

        ws.send_text.assert_awaited_once()
        payload = json.loads(ws.send_text.await_args.args[0])
        assert payload["type"] == "replay"
        assert len(payload["events"]) == 3
        assert [e["text"] for e in payload["events"]] == ["l0", "l1", "l2"]

    @pytest.mark.asyncio
    async def test_replay_filters_by_last_seen_id(self):
        main = MagicMock(id="forkX", thread_id="")
        fork = _NoMountFork(ref_agent=main)
        for i in range(3):
            await fork.send({"type": "log", "category": "agent", "text": f"l{i}"}, replay=True)
        cutoff = fork.replay_other[1]["id"]   # пропускаем l0, l1; ждём только l2

        ws = MagicMock(); ws.send_text = AsyncMock()
        await fork.ws_handle_message({"type": "replay", "last_seen_id": cutoff}, ws=ws)

        ws.send_text.assert_awaited_once()
        payload = json.loads(ws.send_text.await_args.args[0])
        assert [e["text"] for e in payload["events"]] == ["l2"]

    @pytest.mark.asyncio
    async def test_replay_with_empty_buffer_sends_nothing(self):
        main = MagicMock(id="forkX", thread_id="")
        fork = _NoMountFork(ref_agent=main)

        ws = MagicMock(); ws.send_text = AsyncMock()
        await fork.ws_handle_message({"type": "replay", "last_seen_id": -1}, ws=ws)
        ws.send_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replay_merges_transport_and_other_in_id_order(self):
        # Replay-буферы держат разные типы событий; в пакете они выходят
        # отсортированы по id (heapq.merge).
        main = MagicMock(id="forkX", thread_id="")
        fork = _NoMountFork(ref_agent=main)
        await fork.send({"type": "transport", "method": "send_message", "thread_id": "", "text": "t1"}, replay=True)
        await fork.send({"type": "log", "category": "agent", "text": "log1"}, replay=True)
        await fork.send({"type": "transport", "method": "send_message", "thread_id": "", "text": "t2"}, replay=True)

        ws = MagicMock(); ws.send_text = AsyncMock()
        await fork.ws_handle_message({"type": "replay", "last_seen_id": -1}, ws=ws)

        payload = json.loads(ws.send_text.await_args.args[0])
        ids = [e["id"] for e in payload["events"]]
        assert ids == sorted(ids)
        assert len(payload["events"]) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# WebFork: persistence transport-истории в memory_dir/WEB_<thread>.json
# ═══════════════════════════════════════════════════════════════════════════════

def _real_agent(tmp_path, thread_id=""):
    """Настоящий Agent (не MagicMock) — нужен для memory.memory_dir."""
    return Agent(
        id="forkX", model_name="m", api_key="k", base_url="http://t",
        agent_dir=str(tmp_path), thread_id=thread_id,
        memory_compressor=PassthroughCompressor(),
    )


class TestWebForkPersistence:

    @pytest.mark.asyncio
    async def test_transport_event_appended_to_file(self, tmp_path):
        agent = _real_agent(tmp_path)
        fork = _NoMountFork(ref_agent=agent)
        await fork.send({"type": "transport", "method": "send_message", "thread_id": "", "text": "hi"}, replay=True)
        with open(agent.memory.memory_dir + "/WEB.json") as f:
            lines = f.readlines()
        assert len(lines) == 1
        ev = json.loads(lines[0])
        assert ev["text"] == "hi"
        assert ev["method"] == "send_message"

    @pytest.mark.asyncio
    async def test_send_processing_not_persisted(self, tmp_path):
        agent = _real_agent(tmp_path)
        fork = _NoMountFork(ref_agent=agent)
        await fork.send({"type": "transport", "method": "send_processing", "thread_id": "", "active": True}, replay=True)
        assert not os.path.exists(agent.memory.memory_dir + "/WEB.json")

    @pytest.mark.asyncio
    async def test_non_transport_event_goes_to_replay_other(self, tmp_path):
        agent = _real_agent(tmp_path)
        fork = _NoMountFork(ref_agent=agent)
        await fork.send({"type": "log", "category": "agent", "text": "..."}, replay=True)
        assert not os.path.exists(agent.memory.memory_dir + "/WEB.json")
        assert len(fork.replay_other) == 1

    @pytest.mark.asyncio
    async def test_per_thread_files(self, tmp_path):
        agent = _real_agent(tmp_path)
        fork = _NoMountFork(ref_agent=agent)
        await fork.send({"type": "transport", "method": "send_message", "thread_id": "", "text": "main"}, replay=True)
        await fork.send({"type": "transport", "method": "send_message", "thread_id": "abc", "text": "from-abc"}, replay=True)
        main_path = agent.memory.memory_dir + "/WEB.json"
        thread_path = agent.memory.memory_dir + "/WEB_abc.json"
        with open(main_path) as f: assert "main" in f.read()
        with open(thread_path) as f: assert "from-abc" in f.read()

    @pytest.mark.asyncio
    async def test_history_survives_fork_recreation(self, tmp_path):
        # Симулирует рестарт slonagent: пишем события, форк уничтожаем, новый
        # форк должен отдать ту же историю через /api/history (загрузка с диска).
        agent = _real_agent(tmp_path)
        fork1 = _NoMountFork(ref_agent=agent)
        await fork1.send({"type": "transport", "method": "send_message", "thread_id": "", "text": "before-restart"}, replay=True)

        agent2 = _real_agent(tmp_path)
        fork2 = _NoMountFork(ref_agent=agent2)
        events = fork2._load_history_tail("")
        texts = [e.get("text") for e in events]
        assert "before-restart" in texts

    @pytest.mark.asyncio
    async def test_history_per_thread(self, tmp_path):
        agent = _real_agent(tmp_path)
        fork = _NoMountFork(ref_agent=agent)
        await fork.send({"type": "transport", "method": "send_message", "thread_id": "", "text": "main-msg"}, replay=True)
        await fork.send({"type": "transport", "method": "send_message", "thread_id": "abc", "text": "thread-msg"}, replay=True)

        main_events = fork._load_history_tail("")
        thread_events = fork._load_history_tail("abc")
        assert [e["text"] for e in main_events] == ["main-msg"]
        assert [e["text"] for e in thread_events] == ["thread-msg"]

    def test_message_id_counter_is_epoch_ms(self, tmp_path):
        # Счётчик стартует от текущего unix-времени в мс, чтоб переживать
        # рестарты без чтения файлов.
        import time
        agent = _real_agent(tmp_path)
        before = int(time.time() * 1000)
        fork = _NoMountFork(ref_agent=agent)
        after = int(time.time() * 1000)
        assert before <= fork.message_id_counter <= after


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
