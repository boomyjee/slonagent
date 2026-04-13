"""MultiTransport stub for sandbox scripts.

Wraps multiple transports. set_agent calls set_agent only on local
transports (skips Proxies — their host-side originals are already managed).
send_message/etc delegate to all children.
"""
from src.transport.base import BaseTransport


class MultiTransport(BaseTransport):
    def __init__(self, transports):
        super().__init__()
        self.transports = transports

    async def set_agent(self, agent):
        self.agent = agent
        for t in self.transports:
            if isinstance(t, BaseTransport):
                await t.set_agent(agent)

    async def send_message(self, text, stream_id=None, final=True):
        for t in self.transports:
            await t.send_message(text, stream_id=stream_id, final=final)

    async def send_thinking(self, text, stream_id=None, final=False):
        for t in self.transports:
            await t.send_thinking(text, stream_id=stream_id, final=final)

    async def send_system_prompt(self, text):
        for t in self.transports:
            await t.send_system_prompt(text)

    async def on_tool_call(self, name, args):
        for t in self.transports:
            await t.on_tool_call(name, args)

    async def on_tool_result(self, name, result):
        for t in self.transports:
            await t.on_tool_result(name, result)

    async def send_processing(self, active):
        for t in self.transports:
            await t.send_processing(active)

    async def inject_message(self, text):
        for t in self.transports:
            await t.inject_message(text)

    async def send_app_url(self, url, text, button=""):
        for t in self.transports:
            await t.send_app_url(url, text, button)
