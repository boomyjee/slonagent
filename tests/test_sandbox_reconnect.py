"""Тесты автореконнекта канала container_lib/agent.py.

WS-канал к хосту глобально кэшируется (один на процесс). Если хост-агент
перезапустился или WS оборвался — старый код просто использовал мёртвый
канал, RPC-вызовы зависали. Здесь проверяем:

1. _ensure_channel при здоровом ws возвращает закешированный канал
2. _ensure_channel при закрытом ws открывает новый
3. readline на EOF (recv→"") возвращает "" → reader thread выходит → pending fail
4. writeline при send-ошибке закрывает ws и проbрасывает исключение
5. Полный цикл: канал умер, .wait() на pending бросает (не висит вечно)
"""
import importlib.util
import os
import queue
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
_CL = os.path.join(ROOT, "src", "skills", "sandbox", "container_lib")


def _load_container(modname, fname):
    """Грузит модуль container_lib по файлу под заданным именем — без правки
    sys.path. Контейнерный agent кладём под уникальным именем (_container_agent),
    чтобы не перетереть корневой sys.modules['agent']: иначе при полном прогоне
    либо соседние тесты видят контейнерный agent, либо этот тест ловит
    AttributeError на закешированном корневом. rpc — под бара-именем (корневого
    rpc нет), нужен для ленивого `from rpc import ...` внутри контейнерного agent."""
    spec = importlib.util.spec_from_file_location(modname, os.path.join(_CL, fname))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


_load_container("rpc", "rpc.py")
agent_mod = _load_container("_container_agent", "agent.py")
from rpc import Proxy


# ── FakeWS — мок websockets.sync.client.connect ──────────────────────────────

class _State:
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class FakeWS:
    """Минимальный мок ClientConnection: thread-safe recv через очередь,
    send в список, close меняет state и разблокирует recv."""

    def __init__(self):
        self._inq: queue.Queue[str] = queue.Queue()
        self.sent: list[str] = []
        self.state = _State.OPEN
        self.send_will_raise = False

    def recv(self):
        if self.state != _State.OPEN:
            return ""
        item = self._inq.get()
        return item if item is not None else ""

    def send(self, msg):
        if self.send_will_raise or self.state != _State.OPEN:
            raise ConnectionError("ws closed")
        self.sent.append(msg)

    def close(self):
        if self.state == _State.OPEN:
            self.state = _State.CLOSED
            self._inq.put(None)  # unblock blocked recv


@pytest.fixture
def fake_ws_factory(monkeypatch):
    """Подменяет websockets.sync.client.connect — каждый _open_channel
    получает свежий FakeWS. Возвращает список созданных ws для проверок."""
    created: list[FakeWS] = []

    def fake_connect(url):
        ws = FakeWS()
        created.append(ws)
        return ws

    # Подменяем State enum внутри agent._ws_open (он импортирует из websockets.protocol).
    class _FakeStateModule:
        class State:
            OPEN = _State.OPEN
    monkeypatch.setitem(sys.modules, "websockets.sync.client",
                        type(sys)("websockets.sync.client"))
    sys.modules["websockets.sync.client"].connect = fake_connect
    monkeypatch.setitem(sys.modules, "websockets.protocol", _FakeStateModule)

    # Канал глобальный — обнуляем перед каждым тестом.
    monkeypatch.setattr(agent_mod, "_channel", None)
    monkeypatch.setattr(agent_mod, "_read_rpc_url", lambda: "ws://fake/")

    yield created

    # Cleanup: закрываем все ws и канал
    if agent_mod._channel is not None:
        try: agent_mod._channel.close()
        except Exception: pass
    for ws in created:
        try: ws.close()
        except Exception: pass


# ── _ensure_channel: первое открытие + кеширование ───────────────────────────

def test_first_call_opens_channel(fake_ws_factory):
    ch = agent_mod._ensure_channel()
    assert ch is not None
    assert len(fake_ws_factory) == 1
    assert fake_ws_factory[0].state == _State.OPEN


def test_second_call_reuses_channel(fake_ws_factory):
    ch1 = agent_mod._ensure_channel()
    ch2 = agent_mod._ensure_channel()
    assert ch1 is ch2
    assert len(fake_ws_factory) == 1


# ── reconnect: ws закрыт между вызовами ──────────────────────────────────────

def test_dead_ws_triggers_reconnect(fake_ws_factory):
    ch1 = agent_mod._ensure_channel()
    fake_ws_factory[0].close()
    time.sleep(0.1)  # reader-thread'у пора заметить EOF

    ch2 = agent_mod._ensure_channel()
    assert ch2 is not ch1, "ожидался свежий Channel"
    assert len(fake_ws_factory) == 2
    assert fake_ws_factory[1].state == _State.OPEN


# ── readline: пустая recv должна возвращать "" (а не "\n") ───────────────────

def test_readline_returns_empty_on_clean_close(fake_ws_factory):
    """Когда recv() = "" (нормальное закрытие), readline возвращает "" —
    это сигнал reader-thread'у выйти из loop."""
    agent_mod._ensure_channel()
    ws = fake_ws_factory[0]
    ws.state = _State.CLOSED  # recv будет возвращать "" сразу
    ws._inq.put(None)

    ch = agent_mod._channel
    line = ch._readline()
    assert line == ""


def test_readline_returns_empty_on_recv_exception(fake_ws_factory):
    agent_mod._ensure_channel()
    ws = fake_ws_factory[0]

    def boom():
        raise ConnectionError("read err")
    ws.recv = boom

    ch = agent_mod._channel
    assert ch._readline() == ""


# ── writeline: ошибка ws.send должна закрыть ws и пробросить ─────────────────

def test_writeline_closes_ws_on_send_error(fake_ws_factory):
    agent_mod._ensure_channel()
    ws = fake_ws_factory[0]
    ws.send_will_raise = True

    ch = agent_mod._channel
    with pytest.raises(Exception):
        ch._writeline({"hello": "world"})
    assert ws.state == _State.CLOSED, "writeline должен был закрыть ws"


# ── full cycle: после close ws .wait() на in-flight call raises ──────────────

def test_pending_wait_raises_after_close(fake_ws_factory):
    """Если канал закрылся пока caller ждёт ответ — wait() должен бросить
    исключение, не висеть forever."""
    agent_mod._ensure_channel()
    ws = fake_ws_factory[0]
    ch = agent_mod._channel

    # Шлём fake call (никто не ответит)
    rfut = ch.call("host", "ping")
    time.sleep(0.05)
    assert len(ws.sent) == 1, "выходной фрейм должен уйти"

    # Имитируем смерть ws
    ws.close()

    # .wait() должен поднять exception
    with pytest.raises(Exception):
        rfut.wait(timeout=2.0)
