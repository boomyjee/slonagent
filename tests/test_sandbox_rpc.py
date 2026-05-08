"""
Тесты RPC-канала между хостом и sandbox-контейнером.

Все проверки идут in-process: вместо pipes между процессами используем две
очереди как транспорт между двумя Channel'ами. Это покрывает всю логику
сериализации, прокси, pinning async handlers, проброс ошибок и watchdogs
без необходимости поднимать podman.

Запуск:
    .venv\\Scripts\\python -m pytest tests/test_sandbox_rpc.py -v
"""
import asyncio
import json
import os
import queue
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# runner.py делает `from rpc import Channel` — внутри контейнера rpc.py лежит
# в корне PYTHONPATH. Эмулируем это добавлением container_lib в sys.path.
sys.path.insert(0, os.path.join(ROOT, "src", "skills", "sandbox", "container_lib"))
os.chdir(ROOT)

from src.skills.sandbox.container_lib.rpc import Channel, Proxy, RpcFuture


# ── helpers ───────────────────────────────────────────────────────────────────

class _Pair:
    """Двунаправленная связка двух Channel'ов через очереди.

    host-side Channel соответствует реальному хосту (ref_prefix='h'),
    peer-side Channel — sandbox-контейнеру (ref_prefix='s'). Обе стороны
    живут в одном процессе, но в разных потоках-ридерах.
    """

    # По умолчанию разрешаем всё (object ловит любой тип) — чтобы тесты
    # фокусировались на поведении RPC. Тесты про whitelist задают allowed явно.
    _DEFAULT_ALLOW = {object: None}

    def __init__(self, allowed_host=None, allowed_peer=None,
                 host_loop=None, max_workers=32):
        if allowed_host is None: allowed_host = self._DEFAULT_ALLOW
        if allowed_peer is None: allowed_peer = self._DEFAULT_ALLOW
        self._q_h2p = queue.Queue()  # host writes → peer reads
        self._q_p2h = queue.Queue()  # peer writes → host reads

        def _rw(qin, qout):
            def readline():
                line = qin.get()
                return "" if line is None else line
            def writeline(msg):
                qout.put(json.dumps(msg, ensure_ascii=False) + "\n")
            return readline, writeline

        r_h, w_h = _rw(self._q_p2h, self._q_h2p)
        r_p, w_p = _rw(self._q_h2p, self._q_p2h)

        self.host = Channel(r_h, w_h, allowed=allowed_host, ref_prefix="h",
                            async_loop=host_loop, max_workers=max_workers)
        self.peer = Channel(r_p, w_p, allowed=allowed_peer, ref_prefix="s",
                            max_workers=max_workers)

    def start(self):
        self.host.start()
        self.peer.start()

    def close(self):
        # Unblock reader threads.
        self._q_h2p.put(None)
        self._q_p2h.put(None)
        self.host.close()
        self.peer.close()


@pytest.fixture
async def pair_async():
    """Стандартная связка: async handlers на хосте пинятся к текущему loop."""
    loop = asyncio.get_running_loop()
    p = _Pair(host_loop=loop)
    p.start()
    yield p
    p.close()


# ── базовый RPC ───────────────────────────────────────────────────────────────

class Calc:
    def add(self, a, b): return a + b
    def greet(self, name): return f"hi {name}"
    async def aadd(self, a, b):
        await asyncio.sleep(0)
        return a + b
    def crash(self, msg):
        raise RuntimeError(msg)


async def test_simple_sync_call(pair_async):
    pair_async.host.register("calc", Calc())
    result = await pair_async.peer.call("calc", "add", 2, 3)
    assert result == 5


async def test_call_with_kwargs(pair_async):
    pair_async.host.register("calc", Calc())
    result = await pair_async.peer.call("calc", "add", a=10, b=20)
    assert result == 30


async def test_async_handler_on_host_loop(pair_async):
    """async handler на хосте должен отработать, даже если пришёл из чужого
    потока — именно эта схема нужна, чтобы callbacks в транспорт (aiogram,
    uvicorn), чьи сессии привязаны к конкретному loop, работали."""
    pair_async.host.register("calc", Calc())
    result = await pair_async.peer.call("calc", "aadd", 40, 2)
    assert result == 42


async def test_proxy_syntax(pair_async):
    """Sync remote → Proxy returns the value directly (no await)."""
    pair_async.host.register("calc", Calc())
    proxy = Proxy(pair_async.peer, "calc")
    assert proxy.add(7, 8) == 15


async def test_proxy_attribute_path(pair_async):
    """proxy.foo.bar(...) должен пройти по пути foo.bar на объекте."""
    class Nested:
        def __init__(self): self.sub = Calc()
    pair_async.host.register("nested", Nested())
    proxy = Proxy(pair_async.peer, "nested")
    assert proxy.sub.add(1, 1) == 2


async def test_proxy_async_remote_on_loop(pair_async):
    """Async remote → Proxy returns awaitable that resolves to value."""
    pair_async.host.register("calc", Calc())
    proxy = Proxy(pair_async.peer, "calc")
    result = await proxy.aadd(40, 2)
    assert result == 42


# ── проброс ошибок с трейсом ──────────────────────────────────────────────────

async def test_remote_exception_surfaces(pair_async):
    pair_async.host.register("calc", Calc())
    with pytest.raises(RuntimeError) as exc_info:
        await pair_async.peer.call("calc", "crash", "boom!")
    # Проверяем, что наше сообщение дошло...
    assert "boom!" in str(exc_info.value)


async def test_remote_exception_includes_full_traceback(pair_async):
    """Ключевое: LLM пишет свои тулзы и должна видеть файл+строку, где упало.
    str(e) недостаточно — без трейса невозможно отладиться."""
    pair_async.host.register("calc", Calc())
    with pytest.raises(RuntimeError) as exc_info:
        await pair_async.peer.call("calc", "crash", "kaboom")
    msg = str(exc_info.value)
    assert "Traceback (most recent call last)" in msg
    assert 'raise RuntimeError(msg)' in msg
    assert "RuntimeError: kaboom" in msg
    # Имя файла с пользовательским кодом тоже должно быть.
    assert "test_sandbox_rpc.py" in msg


async def test_unknown_method_returns_error(pair_async):
    pair_async.host.register("calc", Calc())
    with pytest.raises(RuntimeError) as exc_info:
        await pair_async.peer.call("calc", "nonexistent")
    # Либо AttributeError в трейсе, либо пермишен-ошибка — главное, RPC
    # не зависает и не падает молча.
    assert str(exc_info.value)


# ── whitelist доступа ─────────────────────────────────────────────────────────

async def test_permission_whitelist_enforced():
    """Если allowed ограничивает методы класса — неразрешённое зовом должно
    вернуть ошибку, а не выполниться."""
    class Restricted:
        def allowed_method(self): return "ok"
        def secret(self): return "nope"

    loop = asyncio.get_running_loop()
    p = _Pair(
        allowed_host={Restricted: {"allowed_method"}},
        host_loop=loop,
    )
    p.start()
    try:
        p.host.register("obj", Restricted())
        # Разрешённый метод работает.
        assert await p.peer.call("obj", "allowed_method") == "ok"
        # Запрещённый — падает.
        with pytest.raises(RuntimeError) as exc_info:
            await p.peer.call("obj", "secret")
        assert "secret" in str(exc_info.value) or "Permission" in str(exc_info.value)
    finally:
        p.close()


async def test_permission_allow_all_via_none():
    """allowed={cls: None} означает «все атрибуты разрешены» для этого класса."""
    class Open:
        def anything(self): return 42
        def other(self): return 99

    loop = asyncio.get_running_loop()
    p = _Pair(allowed_host={Open: None}, host_loop=loop)
    p.start()
    try:
        p.host.register("obj", Open())
        assert await p.peer.call("obj", "anything") == 42
        assert await p.peer.call("obj", "other") == 99
    finally:
        p.close()


# ── возврат объектов по ссылке (Proxy) ────────────────────────────────────────

async def test_allowed_class_returned_as_proxy():
    """Когда handler возвращает объект allowed-класса, клиент получает Proxy,
    а не сериализованную копию. Это основной паттерн spawn_subagent/factory."""
    class Bag:
        def __init__(self, n): self.n = n
        def value(self): return self.n

    class Factory:
        def make(self, n): return Bag(n)

    loop = asyncio.get_running_loop()
    p = _Pair(allowed_host={Factory: None, Bag: None}, host_loop=loop)
    p.start()
    try:
        p.host.register("factory", Factory())
        bag_proxy = await p.peer.call("factory", "make", 7)
        assert isinstance(bag_proxy, Proxy)
        # Вызов на Proxy реально дёргает удалённый объект.
        assert bag_proxy.value() == 7
    finally:
        p.close()


# ── двунаправленный callback ──────────────────────────────────────────────────

async def test_bidirectional_callback():
    """Самый важный сценарий: host передаёт sandbox'у свой транспорт через
    Proxy, sandbox зовёт transport.send_message(...) — вызов летит обратно
    на хост в нужный loop. Имитируем это в одном процессе.
    """
    received = []

    class Transport:
        async def send_message(self, text):
            received.append(text)
            return "ack"

    class Sandbox:
        async def use_transport(self, transport):
            # transport здесь — Proxy на хост-объект
            r1 = await transport.send_message("chunk 1")
            r2 = await transport.send_message("chunk 2")
            return [r1, r2]

    loop = asyncio.get_running_loop()
    p = _Pair(
        allowed_host={Transport: None},
        allowed_peer={Sandbox: None},
        host_loop=loop,
    )
    p.start()
    try:
        transport = Transport()
        p.host.register("transport", transport)
        p.peer.register("sandbox", Sandbox())

        # Хост зовёт sandbox, передавая ему свой транспорт как аргумент.
        result = await p.host.call("sandbox", "use_transport", transport)
        assert result == ["ack", "ack"]
        assert received == ["chunk 1", "chunk 2"]
    finally:
        p.close()


async def test_streaming_callbacks_order(pair_async):
    """Много последовательных callbacks (имитация стрим-чанков) должны
    прийти строго в порядке отправки."""
    received = []

    class Sink:
        def push(self, i, text):
            received.append((i, text))
            return i

    pair_async.host.register("sink", Sink())
    proxy = Proxy(pair_async.peer, "sink")
    for i in range(20):
        proxy.push(i, f"chunk-{i}")
    assert received == [(i, f"chunk-{i}") for i in range(20)]


# ── служебное поведение ───────────────────────────────────────────────────────

async def test_pool_exhaustion_rejects_further_calls():
    """Когда все воркеры заняты pinned handlers (например, long-poll),
    следующий вызов должен быть отвергнут с явной ошибкой, а не молча
    стоять в очереди. Молчаливая очередь — классический дедлок."""
    block = threading.Event()

    class Blocker:
        def hang(self):
            block.wait(5)
            return "done"

    loop = asyncio.get_running_loop()
    # max_workers=1: один воркер занят навсегда, следующие — отвергаются.
    p = _Pair(host_loop=loop, max_workers=1)
    p.start()
    try:
        p.host.register("b", Blocker())
        # Первый зависает в воркере
        hang_fut = p.peer.call("b", "hang")
        # Дадим reader'у диспатчнуть hang в воркер
        await asyncio.sleep(0.05)
        # Второй — должен мгновенно вернуть ошибку
        with pytest.raises(RuntimeError) as exc_info:
            await asyncio.wait_for(p.peer.call("b", "hang"), timeout=1.0)
        assert "pool exhausted" in str(exc_info.value)
        block.set()
        assert await asyncio.wait_for(hang_fut, timeout=1.0) == "done"
    finally:
        block.set()
        p.close()


async def test_rpcfuture_sync_wait():
    """RpcFuture.wait() должен работать из sync-кода (блокирует до результата)."""
    loop = asyncio.get_running_loop()
    p = _Pair(host_loop=loop)
    p.start()
    try:
        p.host.register("calc", Calc())
        # Вызов из async-контекста, но .wait() в отдельном потоке.
        fut = p.peer.call("calc", "add", 100, 200)
        result = await asyncio.to_thread(fut.wait, 2.0)
        assert result == 300
    finally:
        p.close()


async def test_nested_dict_and_list_roundtrip(pair_async):
    """Вложенные dict/list должны проходить через pack/unpack без потерь."""
    class Echo:
        def back(self, data): return data

    pair_async.host.register("echo", Echo())
    payload = {
        "chunks": [{"text": "one", "final": False}, {"text": "two", "final": True}],
        "meta": {"stream_id": 42, "nested": {"x": [1, 2, 3]}},
    }
    result = await pair_async.peer.call("echo", "back", payload)
    assert result == payload


# ── интеграция с Runner (как в реальном sandbox) ──────────────────────────────

async def test_runner_dispatches_skill_tool(tmp_path):
    """_find_tool находит нужный Skill-подкласс в модуле, инстанс зовёт tool."""
    from src.skills.sandbox.container_lib.runner import _load, _find_tool

    script = tmp_path / "mini.py"
    script.write_text(
        "from agent import Skill, tool\n"
        "from typing import Annotated\n"
        "class MiniSkill(Skill):\n"
        "    @tool('doubles')\n"
        "    async def dbl(self, x: Annotated[int, 'n']) -> int:\n"
        "        return x * 2\n"
        "    @tool('crashes')\n"
        "    async def boom(self, m: Annotated[str, 'msg']='x') -> int:\n"
        "        raise ValueError(m)\n",
        encoding="utf-8",
    )
    mod = _load(str(script))

    cls, fn = _find_tool(mod, "mini_dbl")
    assert await fn(cls(), x=21) == 42

    cls, fn = _find_tool(mod, "mini_boom")
    with pytest.raises(ValueError) as exc_info:
        await fn(cls(), m="nope")
    assert "nope" in str(exc_info.value)


async def test_runner_error_propagates_with_traceback_over_rpc(tmp_path):
    """End-to-end: тонкая обёртка вокруг _find_tool регистрируется в Channel,
    хост зовёт её, тулза кидает — трейс доезжает до хоста с указанием
    пользовательского файла."""
    from src.skills.sandbox.container_lib.runner import _load, _find_tool

    script = tmp_path / "boom.py"
    script.write_text(
        "from agent import Skill, tool\n"
        "from typing import Annotated\n"
        "class BoomSkill(Skill):\n"
        "    @tool('crashes')\n"
        "    async def crash(self, m: Annotated[str, 'msg']='x'):\n"
        "        raise RuntimeError(m)\n",
        encoding="utf-8",
    )
    mod = _load(str(script))

    class _Runner:
        async def run_tool(self, name, args, agent=None):
            cls, fn = _find_tool(mod, name)
            return await fn(cls(), **args)

    loop = asyncio.get_running_loop()
    p = _Pair(
        allowed_peer={_Runner: {"run_tool"}},
        host_loop=loop,
    )
    p.start()
    try:
        p.peer.register("runner", _Runner())
        with pytest.raises(RuntimeError) as exc_info:
            await p.host.call("runner", "run_tool",
                              name="boom_crash", args={"m": "ой"}, agent=None)
        msg = str(exc_info.value)
        assert "Traceback (most recent call last)" in msg
        assert "RuntimeError: ой" in msg
        assert "boom.py" in msg
    finally:
        p.close()
