"""MultiTransport for sandbox scripts — mirrors host MultiTransport.

Broadcasts all outgoing events to every child transport, and fans
incoming messages from one child to the others via inject_message so a
message typed in one UI (e.g. web) surfaces in the others (e.g. telegram).
Child transports may be local sandbox transports or Proxies to host
transports — both are treated uniformly; setting on_message on a Proxy
is a local no-op, which is fine because those transports receive their
messages on the host side and are managed by the host's MultiTransport.
"""
from rpc import Proxy
from src.transport.base import BaseTransport


class MultiTransport(BaseTransport):
    def __init__(self, transports):
        super().__init__()
        self.transports = transports

    def set_agent(self, agent):
        super().set_agent(agent)
        for t in self.transports:
            # Proxy transports live on the host — their agent and on_message
            # are managed by the host's MultiTransport. Skip set_agent here;
            # a wire call would hit the host whitelist. on_message assignment
            # on a Proxy is a local no-op, so it's harmless to leave.
            if not isinstance(t, Proxy):
                t.set_agent(agent)
            t.on_message = lambda parts, uid=None, trigger_answer=True, src=t: self._child_message(src, parts, uid, trigger_answer)

    async def _child_message(self, source, content_parts, user_message_id=None, trigger_answer=True):
        text = "\n".join(p.get("text", "") for p in content_parts if p.get("type") == "text")
        if text:
            for t in self.transports:
                if t is not source:
                    await t.inject_message(text)
        await self.agent.process_message(content_parts, user_message_id, trigger_answer=trigger_answer)

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

    def get_skills(self):
        # Proxy transports live on the host with a different skill set; we
        # only gather skills from local sandbox transports.
        return [s for t in self.transports if not isinstance(t, Proxy) for s in t.get_skills()]

    async def inject_message(self, text):
        for t in self.transports:
            await t.inject_message(text)

    async def send_app_url(self, url, text, button=""):
        for t in self.transports:
            await t.send_app_url(url, text, button)
