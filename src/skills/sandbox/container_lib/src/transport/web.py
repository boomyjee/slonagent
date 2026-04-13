"""WebTransport stub for sandbox scripts.

Mirrors the host import path: from src.transport.web import WebTransport

Subclass this, override register_routes() / ws_handle_message().
Transport is activated via set_agent() (called by spawn_subagent internally).
After that, send_message/send/get_url delegate to the host proxy.
Route handlers and ws_handle_message are called back from host via RPC.
"""

import asyncio, inspect
from pathlib import PurePosixPath

from src.transport.base import BaseTransport


class WebTransport(BaseTransport):
    def __init__(self, prefix="", verbose=True):
        self.agent = None
        self._prefix = prefix
        self._verbose = verbose
        self._proxy = None
        self._route_defs = []
        self._handlers = {}

    async def set_agent(self, agent):
        self.agent = agent
        self.register_routes()
        ui_dir = str(PurePosixPath(inspect.getfile(type(self))).parent / "ui")
        ch = agent._ch
        self._proxy = await ch.call("web_transport_factory", "create",
            agent=agent,
            prefix=self._prefix,
            verbose=self._verbose,
            routes=self._route_defs,
            has_ws_handler=type(self).ws_handle_message is not WebTransport.ws_handle_message,
            callback=self,
            ui_dir=ui_dir,
        )

    def register_route(self, method, path, handler):
        self._route_defs.append((method, path, handler.__name__))
        self._handlers[handler.__name__] = handler

    def register_routes(self):
        pass

    async def ws_handle_message(self, msg):
        pass

    async def handle_route(self, name, query=None, body=None):
        handler = self._handlers[name]
        result = handler(query or {}, body)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def cleanup(self):
        if self._proxy:
            await self._proxy.cleanup()

    async def send_message(self, text, stream_id=None, final=True):
        return await self._proxy.send_message(text, stream_id=stream_id, final=final)

    async def send_thinking(self, text, stream_id=None, final=False):
        return await self._proxy.send_thinking(text, stream_id=stream_id, final=final)

    async def send_system_prompt(self, text):
        return await self._proxy.send_system_prompt(text)

    async def send_processing(self, active):
        return await self._proxy.send_processing(active)

    async def on_tool_call(self, name, args):
        return await self._proxy.on_tool_call(name, args)

    async def on_tool_result(self, name, result):
        return await self._proxy.on_tool_result(name, result)

    async def inject_message(self, text):
        return await self._proxy.inject_message(text)

    async def send_app_url(self, url, text, button=""):
        return await self._proxy.send_app_url(url, text, button)

    async def send(self, event, replay=False):
        return await self._proxy.send(event, replay=replay)

    async def get_url(self, sub_path=""):
        return await self._proxy.get_url(sub_path)

    async def get_auth_url(self, sub_path=""):
        return await self._proxy.get_auth_url(sub_path)

    async def process_message(self, **kw):
        return await self._proxy.process_message(**kw)
