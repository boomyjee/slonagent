"""Тонкий BaseTransport-адаптер: per-agent привязка + thread_id stamping.
Вся фактическая работа (HTTP/WS, replay, uploads, dispatch) — в WebFork."""

import logging

from src.transport.base import BaseTransport
from src.transport.web.server import WebTransportServer
from src.transport.web.skill import WebTransportSkill
from src.transport.web.fork import WebFork

__all__ = ["WebTransport", "WebFork", "WebTransportServer", "WebTransportSkill"]

log = logging.getLogger(__name__)


class WebTransport(BaseTransport):
    _forks: dict[str, WebFork] = {}
    make_agent = None

    @classmethod
    def start(cls, config: dict, make_agent=None):
        WebTransportServer.start(config)
        if make_agent is not None:
            WebTransport.make_agent = staticmethod(make_agent)

    def __init__(self, verbose: bool = True):
        super().__init__()
        self._verbose = verbose
        self.fork: WebFork | None = None
        self._web_skill = WebTransportSkill(self)

    def create_fork(self, agent):
        return WebFork(ref_agent=agent)

    def set_agent(self, agent):
        super().set_agent(agent)
        if not self.fork:
            if not agent.id in self._forks:
                self._forks[agent.id] = self.create_fork(agent)
            self.fork = self._forks[agent.id]
            self.fork.refcount += 1
            self.fork.transports[self.agent.thread_id] = self

    def cleanup(self):
        if self.fork is None: return
        self.fork.transports.pop(self.agent.thread_id,None)
        self.fork.refcount -= 1
        if self.fork.refcount <= 0:
            self.fork.cleanup()
            self._forks.pop(self.fork.ref_agent.id, None)
        self.fork = None

    def get_skills(self):
        return [self._web_skill]

    async def _transport_event(self, method: str, replay: bool = True, **kwargs):
        await self.fork.send(
            {"type": "transport", "method": method, "thread_id": self.agent.thread_id, **kwargs},
            replay=replay,
        )

    async def send_message(self, text: str, stream_id=None, final: bool = True):
        await self._transport_event("send_message", replay=final, text=text, stream_id=stream_id, final=final)

    async def send_thinking(self, text: str, stream_id=None, final: bool = False):
        await self._transport_event("send_thinking", replay=final, text=text, stream_id=stream_id, final=final)

    async def send_memory_info(self, text: str):
        await self._transport_event("send_memory_info", replay=True, text=text)

    async def send_system_prompt(self, text: str):
        if not self._verbose:
            return
        await self._transport_event("send_system_prompt", text=text)

    async def on_tool_call(self, name: str, args: dict):
        await self._transport_event("on_tool_call", name=name, args={k: str(v) for k, v in args.items()})

    async def on_tool_result(self, name: str, result):
        await self._transport_event("on_tool_result", name=name, result=result)

    async def send_processing(self, active: bool):
        await self._transport_event("send_processing", active=active)

    async def inject_message(self, text: str):
        await self._transport_event("inject_message", text=text)

    async def thread_rename(self, uuid: str, name: str):
        await self._transport_event("thread_rename", uuid=uuid, name=name)

    async def send_images(self, paths: list[str]):
        items = [self.fork.publish_file(p, compress_image=True) for p in paths]
        await self._transport_event("send_images", items=items)

    async def send_files(self, paths: list[str]):
        items = [self.fork.publish_file(p) for p in paths]
        await self._transport_event("send_files", items=items)

    async def send_voice(self, audio_path: str):
        item = self.fork.publish_file(audio_path)
        await self._transport_event("send_voice", item=item)

    async def send_suggestions(self, text: str, options: list[str]):
        await self._transport_event("send_suggestions", text=text, options=options)
