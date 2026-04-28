"""WebTransport stub for sandbox scripts — thin facade over host bridge.

Construction eagerly creates a `WebTransportBridge` on the host via RPC
and stores a Proxy; all transport methods forward to it. Locally kept:
`on_message` + `process_message` (so outgoing messages flow through the
sub-agent's MultiTransport lambda chain) and the user-overridable hooks
`register_routes` / `ws_handle_message`.
"""

import inspect, logging
from pathlib import PurePosixPath

from rpc import Proxy, active_channel

log = logging.getLogger(__name__)


class WebTransport:
    def __init__(self, prefix="", verbose=True):
        self.agent = None
        self.on_message = None
        ui_dir = str(PurePosixPath(inspect.getfile(type(self))).parent / "ui")
        self._proxy = Proxy(active_channel(), "WebTransportBridge")(
            prefix=prefix, verbose=verbose, ui_dir=ui_dir, proxy=self,
        )

    def set_agent(self, agent):
        self.agent = agent
        self._proxy.set_agent(agent)

    def set_on_message(self, callback):
        self.on_message = callback
        self._proxy.set_on_message(callback)

    async def process_message(self, content_parts, user_message_id=None, trigger_answer=True):
        if self.on_message:
            await self.on_message(content_parts, user_message_id, trigger_answer=trigger_answer)
        else:
            log.warning("process_message called but on_message not set")

    def get_skills(self):
        return []

    def register_route(self, method, path, handler):
        raise NotImplementedError(
            "register_route isn't callable from sandbox — sandbox handlers "
            "can't satisfy FastAPI signature introspection. "
            "Use register_json_route instead."
        )

    def register_json_route(self, method, path, handler):
        self._proxy.register_json_route(method, path, handler)

    def register_routes(self):
        self._proxy.super_register_routes()

    async def ws_handle_message(self, msg):
        pass

    def cleanup(self):
        if self._proxy:
            self._proxy.cleanup()

    async def send_message(self, text, stream_id=None, final=True):
        return await self._proxy.send_message(text, stream_id=stream_id, final=final)

    async def send_thinking(self, text, stream_id=None, final=False):
        return await self._proxy.send_thinking(text, stream_id=stream_id, final=final)

    async def send_memory_info(self, text, stream_id=None, final=False):
        return await self._proxy.send_memory_info(text, stream_id=stream_id, final=final)

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
