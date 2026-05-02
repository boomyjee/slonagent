"""EchoBackend — фейковый бэкенд без LLM. Эхо-ответ с эмуляцией стриминга
через transport.send_message. Для отладки UI/multi-thread без реальных вызовов."""

import asyncio
import logging

from src.agent.backends.base import BaseBackend

log = logging.getLogger(__name__)


class EchoBackend(BaseBackend):
    def __init__(self, agent, delay: float = 0.1, prefix: str = "эхо: "):
        super().__init__(agent)
        self._delay = delay
        self._prefix = prefix
        self._stream_counter: int = 0

    async def _stream_lines(self, text: str, send):
        self._stream_counter += 1
        stream_id = self._stream_counter
        acc = ""
        for line in text.splitlines(keepends=True):
            acc += line
            await send(acc, stream_id=stream_id, final=False)
            if self._delay: await asyncio.sleep(self._delay)
        await send(text, stream_id=stream_id, final=True)

    async def llm(self, **_kwargs):
        user_query = self.agent.memory.last_user_query() or "(пусто)"
        text = f"{self._prefix}{user_query}"
        await self._stream_lines(text, self.agent.transport.send_thinking)
        await self._stream_lines(text, self.agent.transport.send_message)
        log.info("[echo] %s", text)
        return {"role": "assistant", "content": text}
