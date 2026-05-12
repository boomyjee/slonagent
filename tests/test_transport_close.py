"""Транспортный close()-lifecycle и анти-orphan регрессия.

Покрывает баг: при удалении треда асинхронный broadcast ошибки из
TelegramTransport._ensure_chat_and_topic срабатывал ПОСЛЕ web.thread_delete
и пересоздавал WEB_<id>.json orphan'ом. Зашитили двумя фиксами:

1. Близ-lifecycle: BaseTransport.close → MultiTransport.close (рекурсивно) →
   TelegramTransport.close (cancel'ит свои background-таски). Agent.close
   зовёт self.transport.close.
2. asyncio.create_task в _ensure_chat_and_topic заменили на await — broadcast
   теперь завершается синхронно, никаких отложенных корутин.
"""
import asyncio
import pytest

from src.transport.base import BaseTransport
from src.transport.multi import MultiTransport


async def test_base_transport_close_is_noop():
    t = BaseTransport()
    await t.close()  # не падает, ничего не делает


async def test_multi_close_propagates_to_children():
    closed = []

    class FakeT(BaseTransport):
        async def close(self):
            closed.append(self)

    t1, t2 = FakeT(), FakeT()
    m = MultiTransport([t1, t2])
    await m.close()

    assert closed == [t1, t2]


async def test_telegram_close_cancels_its_tasks():
    from src.transport.telegram import TelegramTransport

    async def forever():
        await asyncio.sleep(3600)

    t = TelegramTransport(verbose=False)
    t._update_commands_task = asyncio.create_task(forever())
    t._flush_task = asyncio.create_task(forever())
    t._typing_task = asyncio.create_task(forever())
    t._queue_task = asyncio.create_task(forever())

    await t.close()
    await asyncio.sleep(0)   # дать cancellation провести себя

    assert t._update_commands_task.cancelled()
    assert t._flush_task.cancelled()
    assert t._typing_task.cancelled()
    assert t._queue_task.cancelled()


async def test_agent_close_calls_transport_close():
    from src.agent.agent import Agent

    closed = []

    class FakeT(BaseTransport):
        async def close(self):
            closed.append(True)

    agent = Agent(id="test_close", model_name="echo", backend="echo",
                  agent_dir=None, transport=FakeT())
    await agent.close()
    assert closed == [True]


async def test_ensure_chat_and_topic_broadcasts_synchronously():
    """Регрессия orphan-бага: после фейла _ensure_chat_and_topic broadcast
    ошибки должен завершиться СИНХРОННО, в стеке самого _ensure. Если бы
    использовался asyncio.create_task, send_message был бы вызван ПОСЛЕ
    возврата _ensure — что и приводило к race с thread_delete."""
    from src.transport.telegram import TelegramTransport

    called: list = []

    class FakeAgent:
        def __init__(self):
            self.id = "test"
            self.thread_id = "abc12345"
            self.transport = self

        def thread_name(self, uuid):
            return None

        async def send_message(self, text, stream_id=None, final=True):
            called.append(text)

    class FakeBot:
        async def create_forum_topic(self, chat_id, name):
            raise RuntimeError("boom")

    t = TelegramTransport(verbose=False)
    t.bot = FakeBot()
    t.chat_id = -1
    t.thread_id = None
    t.agent = FakeAgent()
    TelegramTransport._topic_warned.clear()   # снимаем глобальный once-per-key gate

    result = await t._ensure_chat_and_topic()

    assert result is False
    # КЛЮЧЕВОЕ: к моменту возврата _ensure broadcast уже состоялся.
    assert len(called) == 1
    assert "не удалось создать топик" in called[0]
